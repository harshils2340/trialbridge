#!/usr/bin/env python3
"""BridgeMD - a doctor-facing wrapper on ClinicalTrials.gov.

Paste a de-identified patient note, get ranked recruiting trials with a plain
explanation of the fit, then refer a patient in one click and track that
referral through the pipeline (referred -> contacted -> screened -> enrolled).
BridgeMD never pays clinicians for referrals or enrollments - status is
tracked for follow-up only (anti-kickback / fee-splitting compliance).

Run:
    cd matcher
    export LLM_API_KEY="sk-..."          # OpenAI-compatible (optional; see below)
    .venv/bin/python web/app.py          # http://127.0.0.1:5000

Without an LLM key the app still fetches + gates trials (deterministic age/sex
screening) but skips the per-trial eligibility reasoning.
"""
import functools
import hashlib
import hmac
import io
import json
import math
import os
import pathlib
import re
import secrets
import sys
import time
import datetime as dt
import csv
import mimetypes
import urllib.parse
import urllib.request
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import (Flask, abort, flash, g, jsonify, make_response, redirect,
                   render_template, request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# Reuse the matching engine + referral helpers from the parent package.
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import match_trials as mt  # noqa: E402
import refer as rf  # noqa: E402

import alerts as alerts_mod  # noqa: E402
import analytics  # noqa: E402
import calendar_invites  # noqa: E402
import campaigns as campaigns_mod  # noqa: E402
import codes  # noqa: E402
import db  # noqa: E402
import fhir  # noqa: E402
import ingest  # noqa: E402
import intake as intake_mod  # noqa: E402
import logistics  # noqa: E402
import mailer  # noqa: E402
import notifications as notifications_mod  # noqa: E402
import payer  # noqa: E402
import records as records_mod  # noqa: E402
import redcap  # noqa: E402
import reminders as reminders_mod  # noqa: E402
import summarize  # noqa: E402
import trends  # noqa: E402
import ctis  # noqa: E402

app = Flask(__name__)
trends.configure(app)
# Short, plain-English card teaser (deterministic) available in every template.
app.jinja_env.globals["card_blurb"] = summarize.card_blurb
app.jinja_env.globals["patient_card_title"] = summarize.patient_card_title


def _patient_why(text):
    """Trim the internal 'but lacks info / leading to unknowns' hedging from an
    eligibility rationale so the PATIENT only sees the relevant, positive part.
    (The full rationale is still shown to the study team.)"""
    s = (text or "").strip()
    if not s:
        return s
    low = s.lower()
    # Cut at the first connective that introduces missing-data / unknown caveats.
    markers = (" but lacks", " but there", " but no ", " but is missing",
               " but insufficient", " but without", ", but ", " however",
               " lacks information", " lacks specific", " leading to",
               " which leaves", " with several unknowns", " with many unknowns",
               " - leading to")
    cut = len(s)
    for mk in markers:
        p = low.find(mk)
        if 0 <= p < cut:
            cut = p
    trimmed = s[:cut].strip().rstrip(",;:- ")
    if not trimmed:
        return s  # never blank it out
    if trimmed[-1] not in ".!?":
        trimmed += "."
    return trimmed


app.jinja_env.filters["patient_why"] = _patient_why

# Stable secret so sessions survive restarts. Prefer an env var (set this on any
# host so logins survive redeploys); otherwise generate + store one locally.
_env_secret = os.environ.get("SECRET_KEY", "").strip()
if _env_secret:
    app.secret_key = _env_secret
else:
    _secret_file = HERE / ".secret"
    if not _secret_file.exists():
        _secret_file.write_text(secrets.token_hex(32))
    app.secret_key = _secret_file.read_text().strip()

# Behind a hosting proxy (Render/Railway/Fly), trust X-Forwarded-* so redirects and
# secure cookies work over HTTPS.
if os.environ.get("BEHIND_PROXY", "0") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# "Production" indicator: running behind a hosting proxy or ENV=production. Used
# to fail-closed on demo mode and to require HTTPS-only cookies.
IS_PROD = (os.environ.get("BEHIND_PROXY", "0") == "1"
           or os.environ.get("ENV", "").strip().lower() in ("prod", "production"))

# TEMP no-login / demo preview. MUST be OFF in production: it opens the clinician
# ATS to anonymous visitors and seeds fake patients into the DB. Refuse to boot
# if it's enabled while a production indicator is set.
NO_LOGIN = os.environ.get("NO_LOGIN", "0") == "1"
if NO_LOGIN and IS_PROD:
    raise RuntimeError(
        "NO_LOGIN/demo mode is enabled while a production indicator is set "
        "(BEHIND_PROXY=1 or ENV=production). Refusing to start: demo mode "
        "exposes the study-team ATS and patient PHI without login. Unset "
        "NO_LOGIN for production deployments.")

# Health-records / EHR sync (SMART Health IT) is hidden for now — the connector
# is still a sandbox and not patient-ready. Flip RECORDS_UI=1 to re-enable the
# "Connect records" / sync banners across the patient UI.
RECORDS_UI = os.environ.get("RECORDS_UI", "0") == "1"

# Session cookie hardening (the session carries the auth user id). Secure is on
# in production so the cookie is never sent over plain HTTP.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PROD,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14,  # 14 days
)

MAX_MATCH = int(os.environ.get("WEB_MAX_MATCH", "15"))  # LLM calls per search
WEB_LLM_PARALLELISM = max(
    1, min(8, int(os.environ.get("WEB_LLM_PARALLELISM", "6"))))
MATCH_QUALITY_MIN = max(
    0, min(100, int(os.environ.get("MATCH_QUALITY_MIN", "85"))))
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024    # 16 MB upload cap
app.teardown_appcontext(db.close_db)

# Uploaded documents (candidate-thread files + team room). Stored on disk (not
# in the DB) so production can point UPLOAD_DIR at a persistent disk. The name
# on disk is randomized; the human filename lives only in the DB row.
UPLOAD_DIR = pathlib.Path(os.environ.get("UPLOAD_DIR") or (HERE / "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
_ALLOWED_UPLOAD_EXT = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".doc", ".docx",
    ".txt", ".csv", ".xls", ".xlsx", ".rtf", ".heic",
}


def _save_upload(file_storage):
    """Persist an uploaded file under a random name. Returns
    (orig_name, stored_name, mime, size) or None if missing/disallowed."""
    if not file_storage or not file_storage.filename:
        return None
    orig = secure_filename(file_storage.filename) or "file"
    ext = pathlib.Path(orig).suffix.lower()
    if ext and ext not in _ALLOWED_UPLOAD_EXT:
        return None
    stored = secrets.token_hex(16) + ext
    dest = UPLOAD_DIR / stored
    file_storage.save(str(dest))
    try:
        size = dest.stat().st_size
    except OSError:
        size = 0
    return orig, stored, (file_storage.mimetype or ""), size


def _copy_stored_file(stored_name, orig_name):
    """Duplicate an already-stored upload under a fresh random name so each
    broadcast recipient owns an independent copy. Returns the same tuple as
    _save_upload, or None on failure."""
    safe = os.path.basename(stored_name or "")
    src = UPLOAD_DIR / safe
    if not safe or not src.exists():
        return None
    ext = pathlib.Path(safe).suffix.lower()
    new_stored = secrets.token_hex(16) + ext
    dest = UPLOAD_DIR / new_stored
    try:
        dest.write_bytes(src.read_bytes())
        size = dest.stat().st_size
    except OSError:
        return None
    mime = mimetypes.guess_type(orig_name or safe)[0] or ""
    return orig_name or safe, new_stored, mime, size


def _send_stored_file(stored_name, orig_name):
    """Serve a stored upload as an attachment, guarding against path escape."""
    safe = os.path.basename(stored_name or "")
    path = UPLOAD_DIR / safe
    if not safe or not path.exists():
        abort(404)
    return send_file(str(path), as_attachment=True,
                     download_name=orig_name or safe)

# Create tables on import so the app is safe under any launcher (flask run, wsgi).
db.init_db()

# In no-login demo mode, seed a few realistic (clearly fake) candidates so the
# study-team review board shows an end-to-end picture. No-op once real leads exist.
# Gated on NO_LOGIN so a production DB never starts with fake "patients".
if NO_LOGIN:
    try:
        db.seed_demo_leads()
    except Exception:
        app.logger.exception("demo lead seeding failed")


# --------------------------------------------------------------------------- #
# Error handlers - never show a raw stack trace to a doctor.
# --------------------------------------------------------------------------- #
@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404,
                           msg="That page doesn't exist."), 404


@app.errorhandler(413)
def too_large(_e):
    return render_template("error.html", code=413,
                           msg="That file is too large (16 MB max)."), 413


@app.errorhandler(405)
def bad_method(_e):
    return render_template("error.html", code=405,
                           msg="That action isn't allowed here."), 405


@app.errorhandler(410)
def gone(_e):
    return render_template("error.html", code=410,
                           msg="This secure link has expired or was revoked."), 410


@app.errorhandler(500)
def server_error(_e):
    app.logger.exception("Unhandled error")
    return render_template("error.html", code=500,
                           msg="Something went wrong on our end. Please try again."), 500


@app.route("/healthz")
def healthz():
    """Lightweight process-level health probe for hosting checks."""
    return jsonify({"ok": True}), 200


def _ops_key_ok():
    want = (os.environ.get("OPS_READINESS_KEY", "").strip()
            or os.environ.get("ALERTS_CRON_KEY", "").strip())
    if not want:
        return False
    got = (request.args.get("key", "").strip()
           or request.headers.get("X-Ops-Key", "").strip())
    return bool(got and hmac.compare_digest(got, want))


@app.route("/ops/readiness")
def ops_readiness():
    """Operational go-live posture (safe; no secrets returned)."""
    if not _ops_key_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    db_path = os.environ.get("DB_PATH", "").strip()
    readiness = {
        "ok": True,
        "integrations": {
            "records_live": False,
            "payer_live": bool(os.environ.get("PAYER_API_URL", "").strip()
                               and os.environ.get("PAYER_API_KEY", "").strip()),
            "logistics_live": bool(os.environ.get("LOGISTICS_API_URL", "").strip()
                                   and os.environ.get("LOGISTICS_API_KEY", "").strip()),
            "notifications_email_live": bool(os.environ.get("NOTIFY_LIVE", "0") == "1"
                                             and os.environ.get("SMTP_HOST", "").strip()
                                             and os.environ.get("SMTP_FROM", "").strip()),
            "notifications_sms_live": bool(os.environ.get("NOTIFY_SMS", "0") == "1"
                                           and notifications_mod.sms_configured()),
        },
        "infra": {
            "db_path": db_path or str(db.DB_PATH),
            "persistent_db_hint": bool((db_path or "").startswith("/var/data/")
                                       or (db_path or "").startswith("/data/")),
            "cron_key_set": bool(os.environ.get("ALERTS_CRON_KEY", "").strip()),
            "background_loops_disabled": (
                os.environ.get("ALERTS_BACKGROUND", "0") == "0"
                and os.environ.get("REMINDERS_BACKGROUND", "0") == "0"
            ),
        },
        "security": {
            "no_login_off": not NO_LOGIN,
            "secret_key_set": bool(os.environ.get("SECRET_KEY", "").strip()),
        },
    }
    gaps = []
    if not readiness["integrations"]["records_live"]:
        gaps.append("records_live_not_configured")
    if not readiness["integrations"]["payer_live"]:
        gaps.append("payer_live_not_configured")
    if not readiness["integrations"]["logistics_live"]:
        gaps.append("logistics_live_not_configured")
    if not readiness["integrations"]["notifications_email_live"]:
        gaps.append("notifications_email_not_live")
    if not readiness["infra"]["persistent_db_hint"]:
        gaps.append("persistent_db_not_configured")
    if not readiness["infra"]["cron_key_set"]:
        gaps.append("alerts_cron_key_missing")
    readiness["gaps"] = gaps
    readiness["ok"] = not gaps
    return jsonify(readiness), 200


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
# NO_LOGIN (defined near the top with the production guard) enables the no-login
# demo shell. It is forced off in production so the ATS is never anonymous.
_DEMO_EMAIL = "demo@bridgemd.local"
_DEMO_PATIENT_EMAIL = "demo.patient@bridgemd.local"
_DEMO_PATIENT_NAME = os.environ.get("DEMO_PATIENT_NAME", "Harshil Test User").strip() or "Harshil Test User"
DEMO_SESSION_KEY = "demo_mode"
USER_SESSION_KEY = "user_id"
USER_PENDING_KEY = "user_pending_id"
USER_PENDING_PURPOSE_KEY = "user_pending_purpose"
USER_NEXT_KEY = "user_next"
USER_GOOGLE_STATE_KEY = "user_google_state"
PATIENT_SESSION_KEY = "patient_user_id"
PATIENT_PENDING_KEY = "patient_pending_id"
PATIENT_PENDING_PURPOSE_KEY = "patient_pending_purpose"
PATIENT_NEXT_KEY = "patient_next"
PATIENT_GOOGLE_STATE_KEY = "patient_google_state"
CSRF_SESSION_KEY = "_csrf_token"
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_OAUTH_SCOPE = "openid email profile"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
RATE_LIMIT_WINDOW_SECONDS = max(
    1, int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "300")))
RATE_LIMIT_DEFAULT_MSG = (
    "Too many attempts from this network. Please wait a few minutes and try again.")
RATE_LIMIT_ROUTES = {
    "user_login": max(1, int(os.environ.get("RATE_LIMIT_USER_LOGIN_MAX", "10"))),
    "user_verify": max(1, int(os.environ.get("RATE_LIMIT_USER_VERIFY_MAX", "10"))),
    "user_verify_resend": max(
        1, int(os.environ.get("RATE_LIMIT_USER_VERIFY_RESEND_MAX", "5"))),
    "account_signup": max(1, int(os.environ.get("RATE_LIMIT_SIGNUP_MAX", "8"))),
    "account_login": max(1, int(os.environ.get("RATE_LIMIT_LOGIN_MAX", "10"))),
    "account_verify": max(1, int(os.environ.get("RATE_LIMIT_VERIFY_MAX", "10"))),
    "account_verify_resend": max(
        1, int(os.environ.get("RATE_LIMIT_VERIFY_RESEND_MAX", "5"))),
    "interest": max(1, int(os.environ.get("RATE_LIMIT_INTEREST_MAX", "12"))),
    # Inline "verify your email to apply" flow. Sending a code hits SMTP, so keep
    # it tighter than the verify check (which just compares a code).
    "apply_send_code": max(1, int(os.environ.get("RATE_LIMIT_APPLY_SEND_MAX", "6"))),
    "apply_verify_code": max(
        1, int(os.environ.get("RATE_LIMIT_APPLY_VERIFY_MAX", "12"))),
    # Public search is the one expensive public endpoint (LLM calls per query),
    # so cap it per IP to blunt bursts/bots. Generous for real users.
    "find": max(1, int(os.environ.get("RATE_LIMIT_FIND_MAX", "20"))),
    # Public "book a demo" form on the For-clinics page (emails the team).
    "demo_request": max(1, int(os.environ.get("RATE_LIMIT_DEMO_MAX", "6"))),
}

# Global daily ceiling on LLM-backed searches so a traffic spike or abuse can't
# run up the model bill / exhaust the API key. When exceeded, search still works
# - it just returns matches without per-trial eligibility reasoning. 0 disables.
LLM_DAILY_SEARCH_CAP = max(0, int(os.environ.get("LLM_DAILY_SEARCH_CAP", "600")))

# Contact + last-updated shown on the Privacy / Terms pages and footer.
LEGAL_CONTACT = os.environ.get("LEGAL_CONTACT", "hello@bridgemd.health").strip()
LEGAL_UPDATED = os.environ.get("LEGAL_UPDATED", "July 2026").strip()


def _ensure_demo_user():
    u = db.get_user_by_email(_DEMO_EMAIL)
    if not u:
        pw = generate_password_hash("demo-no-login", method="pbkdf2:sha256")
        db.create_user(_DEMO_EMAIL, pw, "Demo Clinician", "", "")
        u = db.get_user_by_email(_DEMO_EMAIL)
    return u


def _ensure_demo_patient():
    p = db.get_patient_by_email(_DEMO_PATIENT_EMAIL)
    if not p:
        pw = generate_password_hash("demo-patient", method="pbkdf2:sha256")
        pid = db.create_patient_user(
            _DEMO_PATIENT_EMAIL, pw, _DEMO_PATIENT_NAME, verified=True)
        db.set_patient_onboarding(
            pid, primary_interest="Obesity", notify_email=_DEMO_PATIENT_EMAIL,
            email_alerts=True)
        p = db.get_patient_user(pid)
    elif not p["verified"]:
        db.mark_patient_verified(p["id"])
        p = db.get_patient_user(p["id"])
    # Keep demo identity human-readable in previews instead of generic "Demo".
    try:
        cur_name = (p["full_name"] or "").strip() if p else ""
        if p and (not cur_name or cur_name.lower().startswith("demo")):
            conn = db.get_db()
            conn.execute("UPDATE patient_users SET full_name = ? WHERE id = ?",
                         (_DEMO_PATIENT_NAME, p["id"]))
            conn.commit()
            p = db.get_patient_user(p["id"])
    except Exception:
        app.logger.exception("demo patient rename failed")
    db.seed_demo_patient_apps(p["applicant_token"] if p else "")
    return p


def _seed_demo_surfaces(user_id=None):
    """Populate demo data across patient/clinician/study-team surfaces.

    When demo mode is enabled we should show a fully working product view
    immediately (claimed studies, queue, messages, schedule links), without
    requiring manual setup steps.
    """
    try:
        db.seed_demo_leads()
    except Exception:
        app.logger.exception("demo lead seeding failed")
    try:
        db.seed_demo_engagement(user_id)
    except Exception:
        app.logger.exception("demo engagement seeding failed")
    try:
        db.seed_demo_referrals(user_id)
    except Exception:
        app.logger.exception("demo referral seeding failed")
    try:
        _ensure_demo_patient()
    except Exception:
        app.logger.exception("demo patient seeding failed")
    try:
        db.seed_demo_collaboration(user_id)
    except Exception:
        app.logger.exception("demo collaboration seeding failed")
    try:
        _seed_demo_documents()
    except Exception:
        app.logger.exception("demo document seeding failed")


def _seed_demo_documents():
    """Drop one real sample file on disk + attachment rows so the demo's
    'Shared documents' panel has a working download. Idempotent."""
    sample = UPLOAD_DIR / "sample-consent-form.txt"
    if not sample.exists():
        sample.write_text(
            "SAMPLE - Informed Consent Summary\n\n"
            "This is a demo document shared through BridgeMD's secure candidate "
            "thread. In production this would be your IRB/REB-approved consent "
            "form or intake packet.\n")

    def _share_doc(lead_id):
        if not db.list_attachments(lead_id):
            db.add_attachment(lead_id, "site", "sample-consent-form.txt",
                              sample.name, "text/plain",
                              sample.stat().st_size, "Please review and sign.")

    # Study-team side: docs on the first couple of accepted candidates.
    revealed = [ld for ld in db.list_leads() if ld["revealed"]]
    for ld in revealed[:2]:
        _share_doc(ld["id"])

    # Patient side: seed a doc + checklist on the demo patient's own active
    # application so the "My applications" view demos the full loop too.
    patient = db.get_patient_by_email(_DEMO_PATIENT_EMAIL)
    if patient:
        p_leads = [ld for ld in db.list_leads_by_applicant(patient["applicant_token"])
                   if ld["status"] not in db.LEAD_CLOSED]
        if p_leads:
            ld = p_leads[0]
            _share_doc(ld["id"])
            if not db.list_tasks(ld["id"]):
                db.add_task(ld["id"], "Review and sign the consent form",
                            assigned_to="patient", created_by="site")
                db.add_task(ld["id"], "Upload a photo of your insurance card",
                            assigned_to="patient", created_by="site")


def _demo_mode_enabled():
    """True only for explicitly-enabled local/demo builds - never in production.

    Requires NO_LOGIN (forced off in prod). The session toggle can only refine
    behavior within a demo build; it can never turn demo mode on in production,
    so a tampered/forged session cookie can't expose the ATS or patient PHI."""
    if not NO_LOGIN:
        return False
    if DEMO_SESSION_KEY in session:
        return bool(session.get(DEMO_SESSION_KEY))
    return True


# Seed the retention/engagement surfaces (messages, visits, a physician referral)
# on top of the demo leads so a fresh no-login demo shows the whole loop alive.
if NO_LOGIN:
    try:
        with app.test_request_context():
            _demo = _ensure_demo_user()
            _seed_demo_surfaces(_demo["id"] if _demo else None)
    except Exception:
        app.logger.exception("demo engagement seeding failed")


def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return view(*a, **k)
    return wrapped


# Private owner-only analytics. Only this account sees the visitor dashboard;
# every other signed-in staff user gets a 404 (so its existence isn't leaked).
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "harshils2340@gmail.com").strip().lower()

# Analytics: don't count your own traffic. Set ANALYTICS_IGNORE_IPS to a
# comma-separated list of IPs to drop from the visitor funnel (e.g. your home /
# office IP). Events from these IPs — and from anyone signed in as OWNER_EMAIL —
# are never logged. Find your current IP on the /app/analytics page.
ANALYTICS_IGNORE_IPS = frozenset(
    ip.strip() for ip in os.environ.get("ANALYTICS_IGNORE_IPS", "").split(",")
    if ip.strip())


def _client_ip():
    """Best-effort client IP. Behind a proxy, ProxyFix rewrites remote_addr to
    the real client (X-Forwarded-For); otherwise remote_addr is the peer."""
    return (request.remote_addr or "").strip()


def _mask_ip(ip):
    """Partly redact an IP for on-screen display so the owner can recognize it
    without exposing the full address (e.g. in screenshots / screen shares)."""
    ip = (ip or "").strip()
    if not ip:
        return ""
    if ":" in ip:  # IPv6 - keep first group only
        return ip.split(":", 1)[0] + ":\u2022\u2022\u2022\u2022"
    parts = ip.split(".")
    if len(parts) == 4:  # IPv4 - keep first two octets
        return parts[0] + "." + parts[1] + ".\u2022\u2022\u2022.\u2022\u2022\u2022"
    return "\u2022\u2022\u2022"


# Substrings that mark a request as automated (crawlers, scrapers, monitors,
# link previewers, headless browsers, CLI HTTP clients). Matched case-insensitively
# against the User-Agent. This is the standard, low-maintenance way to keep bots
# out of visitor analytics so the funnel numbers stay legit.
_BOT_UA_MARKERS = (
    "bot", "crawl", "spider", "slurp", "search", "scrape", "fetch",
    "curl", "wget", "python-requests", "python-httpx", "aiohttp", "httpclient",
    "okhttp", "go-http", "java/", "libwww", "phantomjs", "headless",
    "puppeteer", "playwright", "selenium", "webdriver", "lighthouse",
    "monitor", "uptime", "pingdom", "statuscake", "datadog", "newrelic",
    "facebookexternalhit", "facebot", "embedly", "quora link", "outbrain",
    "whatsapp", "telegrambot", "discordbot", "slackbot", "twitterbot",
    "linkedinbot", "bingpreview", "yandex", "baidu", "duckduckbot",
    "semrush", "ahrefs", "mj12bot", "dotbot", "petalbot", "gptbot",
    "ccbot", "claudebot", "google-inspectiontool", "chrome-lighthouse",
)


def _is_bot(ua=None):
    """True when the User-Agent looks automated. Empty UAs are treated as bots:
    real browsers always send one, most scripted clients don't."""
    if ua is None:
        try:
            ua = request.headers.get("User-Agent", "")
        except Exception:
            ua = ""
    ua = (ua or "").strip().lower()
    if not ua:
        return True
    return any(marker in ua for marker in _BOT_UA_MARKERS)


def _analytics_ignored():
    """True when the current request should be excluded from visitor analytics:
    automated traffic (bots), the owner's own IP(s), or the signed-in owner."""
    if _is_bot():
        return True
    if _client_ip() in ANALYTICS_IGNORE_IPS:
        return True
    return _is_owner()


def _is_owner():
    try:
        return bool(g.user and (g.user["email"] or "").strip().lower() == OWNER_EMAIL)
    except (KeyError, TypeError):
        return False


def owner_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        if not _is_owner():
            abort(404)
        return view(*a, **k)
    return wrapped


def patient_login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        if not g.patient_user:
            flash("Sign in to continue.", "error")
            return redirect(url_for("patient_login", next=request.path))
        return view(*a, **k)
    return wrapped


@app.before_request
def load_user():
    uid = session.get(USER_SESSION_KEY)
    g.user = db.get_user(uid) if uid else None
    if g.user is None and _demo_mode_enabled():
        g.user = _ensure_demo_user()
    if _demo_mode_enabled() and g.user:
        _seed_demo_surfaces(g.user["id"])
    pid = session.get(PATIENT_SESSION_KEY)
    g.patient_user = db.get_patient_user(pid) if pid else None
    # In preview mode, show patient-side flows as already signed in so demos can
    # focus on product behavior (apply/message tracking) instead of auth prompts.
    if g.patient_user is None and _demo_mode_enabled():
        g.patient_user = _ensure_demo_patient()


APPLICANT_COOKIE = "tb_app"
INVITE_COOKIE = "tb_invite"
# Recruitment-campaign attribution: set when a visitor arrives via a tracked
# campaign/placement link (/go/<token>), read on apply to credit the source.
CAMPAIGN_COOKIE = "tb_camp"


def get_applicant_token():
    """Stable per-visitor id used to group a person's trial applications.
    No login: we keep it in a long-lived cookie. Returns '' if not set yet."""
    if getattr(g, "patient_user", None):
        return g.patient_user["applicant_token"] or ""
    return request.cookies.get(APPLICANT_COOKIE, "")


def _set_applicant_cookie(resp, token):
    resp.set_cookie(APPLICANT_COOKIE, token, max_age=60 * 60 * 24 * 365,
                    samesite="Lax", httponly=True,
                    secure=bool(os.environ.get("BEHIND_PROXY")))
    return resp


# --------------------------------------------------------------------------- #
# Privacy-safe web analytics: an anonymous visitor id + first-touch attribution
# (where the click came from), used to log the visit->search->view->apply funnel.
# No PII: the visitor id is a random cookie, attribution is UTM/referrer only.
# --------------------------------------------------------------------------- #
VISITOR_COOKIE = "tb_vid"
ATTR_COOKIE = "tb_attr"


def _external_referrer_host():
    ref = request.referrer or ""
    if not ref:
        return ""
    try:
        host = urllib.parse.urlparse(ref).netloc.lower()
        own = urllib.parse.urlparse(request.host_url).netloc.lower()
    except Exception:
        return ""
    if not host or host == own:
        return ""
    return host[4:] if host.startswith("www.") else host


def _compute_attribution():
    a = request.args
    source = (a.get("utm_source") or "").strip().lower()
    medium = (a.get("utm_medium") or "").strip().lower()
    campaign = (a.get("utm_campaign") or "").strip().lower()
    ref_host = _external_referrer_host()
    if not source and ref_host:
        source = ref_host
        medium = medium or "referral"
    return {"s": source[:80], "m": medium[:40], "c": campaign[:80],
            "r": ref_host[:200]}


@app.before_request
def _web_analytics_ctx():
    g._new_vid = None
    g._new_attr = None
    g.visitor_id = ""
    g.attr = {}
    if request.endpoint == "static":
        return
    vid = request.cookies.get(VISITOR_COOKIE, "")
    if not vid:
        vid = secrets.token_urlsafe(12)
        g._new_vid = vid
    g.visitor_id = vid
    attr = None
    raw = request.cookies.get(ATTR_COOKIE, "")
    if raw:
        try:
            attr = json.loads(raw)
        except (ValueError, TypeError):
            attr = None
    if attr is None:                       # first touch: lock the source
        attr = _compute_attribution()
        g._new_attr = attr
    g.attr = attr or {}


@app.after_request
def _web_analytics_cookies(resp):
    try:
        secure = bool(os.environ.get("BEHIND_PROXY"))
        if getattr(g, "_new_vid", None):
            resp.set_cookie(VISITOR_COOKIE, g._new_vid,
                            max_age=60 * 60 * 24 * 365, samesite="Lax",
                            httponly=True, secure=secure)
        if getattr(g, "_new_attr", None) is not None:
            resp.set_cookie(ATTR_COOKIE,
                            json.dumps(g._new_attr, separators=(",", ":")),
                            max_age=60 * 60 * 24 * 90, samesite="Lax",
                            httponly=True, secure=secure)
    except Exception:
        pass
    # Never let a CDN/browser serve a stale HTML page - otherwise it keeps
    # pointing at old (cached) CSS/JS even after we ship fixes. Static assets
    # are exempt (they're long-cached and busted via ?v= in static_url()).
    try:
        if resp.headers.get("Content-Type", "").startswith("text/html"):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate, max-age=0"
    except Exception:
        pass
    return resp


def _log_event(name, detail=None):
    """Best-effort funnel event with the current visitor + attribution.
    Owner traffic (ignored IPs or the signed-in owner) is never logged."""
    try:
        if _analytics_ignored():
            return
        attr = getattr(g, "attr", {}) or {}
        db.log_web_event(
            name, visitor=getattr(g, "visitor_id", ""), path=request.path,
            source=attr.get("s", ""), medium=attr.get("m", ""),
            campaign=attr.get("c", ""), referrer=attr.get("r", ""),
            detail=detail, ua=request.headers.get("User-Agent", ""))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Delivery / notifications for the consumer loop. Fully wired but OFF by default
# so nothing is emailed during testing. Go-live is ~2 min: set these env vars.
#   NOTIFY_LIVE=1
#   SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_FROM
#   NOTIFY_SMS=1 + TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER
#   SITE_NOTIFY_EMAIL=coordinator@site          (where new candidates are sent)
#   PUBLIC_BASE_URL=https://yourdomain.com      (optional, for correct email links)
# While OFF, the loop still works end to end: the operator copies the secure
# /c/<token> link from the dashboard and hands it to the site by hand.
# --------------------------------------------------------------------------- #
NOTIFY_LIVE = os.environ.get("NOTIFY_LIVE", "0") == "1"
SITE_NOTIFY_EMAIL = os.environ.get("SITE_NOTIFY_EMAIL", "").strip()
# Internal inbox(es) that get a heads-up on every new application, so the operator
# can confirm the funnel is producing real, legit applications. De-identified.
# Comma-separated; defaults to the brand inbox + your personal owner email so you
# get it directly. Override with OWNER_NOTIFY_EMAIL.
OWNER_NOTIFY_EMAIL = os.environ.get(
    "OWNER_NOTIFY_EMAIL", f"hello@bridgemd.health, {OWNER_EMAIL}").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
_NOTIFIER = notifications_mod.Notifier(
    live=NOTIFY_LIVE,
    send_email_fn=mailer.send_email if mailer.smtp_configured() else None,
)


def _abs_url(endpoint, **kw):
    """Absolute URL for links placed inside emails."""
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL + url_for(endpoint, **kw)
    return url_for(endpoint, _external=True, **kw)


def notifications_ready():
    return _NOTIFIER.email_ready()


def _notify(to_addr, subject, body):
    """Single switch for every consumer-loop email. No-op (never errors) unless
    go-live is on AND SMTP is configured AND there's a recipient."""
    return _NOTIFIER.send(to_email=to_addr, subject=subject, email_body=body,
                          allow_sms=False)


def _notify_patient(to_email, to_phone, subject, email_body, sms_body):
    """Patient-facing notify: email by default, optional SMS when env-enabled."""
    return _NOTIFIER.send(
        to_email=to_email,
        to_phone=to_phone,
        subject=subject,
        email_body=email_body,
        sms_body=sms_body,
    )


def _notify_site_new_candidate(token):
    """Tell the study site a new de-identified candidate is waiting (with the
    secure review link). Recipient: the lead's site email, else SITE_NOTIFY_EMAIL."""
    lead = db.get_lead_by_token(token)
    if not lead:
        return False
    to_addr = db.site_contact_for_nct(lead["nct"]) or SITE_NOTIFY_EMAIL
    link = _abs_url("candidate_page", token=lead["site_token"])
    subject, body = mailer.build_candidate_message(lead, link)
    return _notify(to_addr, subject, body)


def _dedup_email_list(raw, exclude=None):
    """Split a comma-separated address string into a de-duplicated, order-
    preserving list, dropping any addresses in `exclude` (case-insensitive).
    Prevents the same inbox from getting the same notification twice."""
    ex = set()
    for e in (exclude or []):
        for part in str(e or "").split(","):
            part = part.strip().lower()
            if part:
                ex.add(part)
    out, seen = [], set()
    for part in str(raw or "").split(","):
        part = part.strip()
        key = part.lower()
        if not part or key in seen or key in ex:
            continue
        seen.add(key)
        out.append(part)
    return out


def _notify_owner_new_application(token, exclude_emails=None):
    """Heads-up to the operator's inbox (OWNER_NOTIFY_EMAIL) that a new
    application came in, so they can confirm the funnel is producing real,
    legit applications. De-identified: links to the dashboard for details.
    No-op while notifications are off (NOTIFY_LIVE) or no recipient set.

    `exclude_emails` are addresses that already received a notification for this
    application (e.g. the site-candidate email), so a poster who is also the
    operator doesn't get two near-duplicate emails for one apply."""
    recipients = _dedup_email_list(OWNER_NOTIFY_EMAIL, exclude=exclude_emails)
    if not recipients:
        return False
    lead = db.get_lead_by_token(token)
    if not lead:
        return False
    link = _abs_url("leads")
    subject, body = mailer.build_owner_new_application(lead, link)
    return _notify(", ".join(recipients), subject, body)


def _notify_applicant(token, kind):
    """Tell the applicant their status changed. kind in {accepted, declined,
    screening, enrolled}."""
    lead = db.get_lead_by_token(token)
    if not lead or (not lead["email"] and not lead["phone"]):
        return False
    link = _abs_url("applications")
    subject, body = mailer.build_applicant_message(lead, kind, link)
    return _notify_patient(lead["email"], lead["phone"], subject, body, "")


def _notify_applicant_by_id(lead_id, kind):
    lead = db.get_lead(lead_id)
    return _notify_applicant(lead["token"], kind) if lead else False


def _notify_applicant_apply_confirmation(token):
    """Send the applicant a friendly 'we got your application' confirmation right
    after they apply. No-op if there's no email/phone or notifications are off."""
    lead = db.get_lead_by_token(token)
    if not lead or (not lead["email"] and not lead["phone"]):
        return False
    subject, body = mailer.build_apply_confirmation(lead, _abs_url("applications"))
    return _notify_patient(lead["email"], lead["phone"], subject, body, "")


def _notify_applicant_schedule(lead):
    """Email the applicant their booking link so they can self-schedule."""
    if (not lead or not lead["schedule_url"] or
            (not lead["email"] and not lead["phone"])):
        return False
    subject, body = mailer.build_schedule_message(
        lead, lead["schedule_url"], _abs_url("applications"))
    sms = mailer.build_schedule_sms(lead, lead["schedule_url"])
    return _notify_patient(lead["email"], lead["phone"], subject, body, sms)


def _notify_applicant_message(lead, body):
    if not lead or (not lead["email"] and not lead["phone"]):
        return False
    link = _abs_url("applications")
    subject, msg = mailer.build_dm_message(
        lead, body, _abs_url("applications"), to="patient")
    sms = mailer.build_dm_sms(lead, link, to="patient")
    return _notify_patient(lead["email"], lead["phone"], subject, msg, sms)


def _notify_site_message(lead, body):
    if not lead:
        return False
    to_addr = db.site_contact_for_nct(lead["nct"]) or SITE_NOTIFY_EMAIL
    link = _abs_url("candidate_page", token=lead["site_token"])
    subject, msg = mailer.build_dm_message(lead, body, link, to="site")
    return _notify(to_addr, subject, msg)


def _notify_applicant_visit(lead, when, location, invite_url=""):
    if not lead or (not lead["email"] and not lead["phone"]):
        return False
    subject, body = mailer.build_visit_message(
        lead, when, location, _abs_url("applications"), invite_url=invite_url)
    sms = mailer.build_reminder_sms(lead, when, location, _abs_url("applications"))
    return _notify_patient(lead["email"], lead["phone"], subject, body, sms)


def _notify_alert(alert, new_matches):
    """Push new matching trials to the patient who saved this alert."""
    if not alert["email"]:
        return False
    link = _abs_url("alerts")
    subject, body = mailer.build_alert_message(alert, new_matches, link)
    return _notify(alert["email"], subject, body)


# Watch ClinicalTrials.gov in the background and push new matches to patients.
alerts_mod.configure(app, notifier=_notify_alert)


def _remind_visit(visit):
    """Email a patient a reminder about an upcoming visit (row from a JOIN)."""
    if not visit["email"] and not visit["phone"]:
        return False
    subject, body = mailer.build_reminder_message(
        visit, visit["visit_at"], visit["location"], _abs_url("applications"))
    sms = mailer.build_reminder_sms(
        visit, visit["visit_at"], visit["location"], _abs_url("applications"))
    return _notify_patient(visit["email"], visit["phone"], subject, body, sms)


def _nudge_applicant(lead):
    if not lead["email"] and not lead["phone"]:
        return False
    subject, body = mailer.build_nudge_message(lead, _abs_url("applications"))
    sms = mailer.build_nudge_sms(lead, _abs_url("applications"))
    return _notify_patient(lead["email"], lead["phone"], subject, body, sms)


# Proactively remind about visits and re-engage quiet applicants (retention).
reminders_mod.configure(app, on_visit=_remind_visit, on_nudge=_nudge_applicant)


@app.context_processor
def inject_globals():
    token = get_applicant_token()
    try:
        apps_n = db.count_applications(token)
    except Exception:
        apps_n = 0
    # Whether this visitor has connected their health records once already, so
    # the apply form can auto-fill instead of asking again.
    try:
        rec = db.get_records_profile(token) if (token and RECORDS_UI) else None
    except Exception:
        rec = None
    try:
        alerts_new = db.new_matches_count(token) if token else 0
    except Exception:
        alerts_new = 0
    try:
        msgs_unread = db.unread_for_patient(token) if token else 0
    except Exception:
        msgs_unread = 0
    try:
        site_unread = db.unread_for_site(g.user["id"]) if g.user else 0
    except Exception:
        site_unread = 0
    # In no-login testing mode we show a small switcher so you can preview all
    # three POVs (patient / clinician / study team) without signing in.
    path = request.path or "/"
    if (path.startswith("/app/leads") or path.startswith("/app/dashboard")
            or path.startswith("/app/site") or path.startswith("/app/messages")
            or path.startswith("/app/analytics")
            or path.startswith("/app/campaign") or path.startswith("/app/intake")):
        pov = "study"
    elif path.startswith("/app") or path.startswith("/referral"):
        pov = "clinician"
    else:
        pov = "patient"
    return {"user": g.user, "llm_on": bool(mt.LLM_API_KEY),
            "applications_count": apps_n, "pov_demo": _demo_mode_enabled(),
            "demo_available": NO_LOGIN, "pov": pov,
            "records_profile": rec, "records_provider": records_mod.provider_label(),
            "records_ui": RECORDS_UI,
            "alerts_new_count": alerts_new, "messages_unread": msgs_unread,
            "site_unread": site_unread, "patient_user": g.patient_user,
            "is_owner": _is_owner()}


@app.route("/demo-mode", methods=["POST"])
def set_demo_mode():
    """Allow quick POV testing without creating accounts in local demos.
    Disabled entirely unless this is a demo build (NO_LOGIN) so it can never be
    used to flip a production instance into anonymous demo mode."""
    if not NO_LOGIN:
        abort(404)
    vals = request.form.getlist("enabled")
    enabled = "1" in vals
    session[DEMO_SESSION_KEY] = bool(enabled)
    if enabled:
        try:
            actor = g.user if g.user else _ensure_demo_user()
            _seed_demo_surfaces(actor["id"] if actor else None)
        except Exception:
            app.logger.exception("demo engagement seeding failed")
    nxt = request.form.get("next", "").strip()
    if not nxt.startswith("/"):
        nxt = url_for("home")
    return redirect(nxt)


@app.route("/demo-lane")
def demo_lane_redirect():
    """Safe lane switch target for demo picker."""
    lane = request.args.get("lane", "").strip()
    if lane.startswith("/") and not lane.startswith("//"):
        return redirect(lane)
    return redirect(url_for("home"))


def _site_claims():
    """Claimed NCT ids for the current logged-in study-team account."""
    if not g.user:
        return set()
    try:
        return db.user_claimed_ncts(g.user["id"])
    except Exception:
        return set()


def _ensure_site_access_for_lead(lead_id):
    """403 unless this lead's NCT belongs to the current account's claims."""
    if not g.user or not db.lead_belongs_to_user(lead_id, g.user["id"]):
        abort(403)


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("dashboard"))
    nxt = _safe_next(request.args.get("next", ""))
    if nxt:
        session[USER_NEXT_KEY] = nxt
    if request.method == "POST":
        f = request.form
        blocked = _guard_ip_rate_limit("user_login", template_name="register.html",
                                       google_enabled=_google_ready())
        if blocked:
            return blocked
        email = f.get("email", "").strip().lower()
        pw = f.get("password", "")
        name = f.get("name", "").strip()
        err = None
        if not (email and pw and name):
            err = "Name, email, and password are required."
        elif len(pw) < 8:
            err = "Use a password of at least 8 characters."
        elif db.get_user_by_email(email):
            err = "An account with that email already exists."
        if err:
            flash(err, "error")
        else:
            pw_hash = generate_password_hash(pw, method="pbkdf2:sha256")
            uid = db.create_user(email, pw_hash, name,
                                 f.get("specialty", ""), f.get("institution", ""))
            user = db.get_user(uid)
            ok, msg = _issue_user_code(user, "signup")
            if not ok:
                flash(msg, "error")
                return render_template("register.html",
                                       google_enabled=_google_ready())
            session[USER_PENDING_KEY] = uid
            session[USER_PENDING_PURPOSE_KEY] = "signup"
            flash("Check your email for a 6-digit verification code.", "success")
            return redirect(url_for("user_verify"))
    return render_template("register.html", google_enabled=_google_ready())


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    nxt = _safe_next(request.args.get("next", ""))
    if nxt:
        session[USER_NEXT_KEY] = nxt
    if request.method == "POST":
        blocked = _guard_ip_rate_limit("user_login", template_name="login.html",
                                       google_enabled=_google_ready())
        if blocked:
            return blocked
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        user = db.get_user_by_email(email)
        if user and check_password_hash(user["password_hash"], pw):
            purpose = "login" if user["verified"] else "signup"
            ok, msg = _issue_user_code(user, purpose)
            if not ok:
                flash(msg, "error")
                return render_template("login.html",
                                       google_enabled=_google_ready())
            session[USER_PENDING_KEY] = user["id"]
            session[USER_PENDING_PURPOSE_KEY] = purpose
            if purpose == "signup":
                flash("Verify your email to finish account setup. We sent a fresh code.",
                      "success")
            else:
                flash("Enter the 6-digit code we sent to your email.", "success")
            return redirect(url_for("user_verify"))
        flash("Wrong email or password.", "error")
    return render_template("login.html", google_enabled=_google_ready())


@app.route("/logout")
def logout():
    session.pop(USER_SESSION_KEY, None)
    session.pop(USER_PENDING_KEY, None)
    session.pop(USER_PENDING_PURPOSE_KEY, None)
    session.pop(USER_NEXT_KEY, None)
    session.pop(USER_GOOGLE_STATE_KEY, None)
    return redirect(url_for("login"))


@app.route("/login/verify", methods=["GET", "POST"])
def user_verify():
    uid = session.get(USER_PENDING_KEY)
    purpose = session.get(USER_PENDING_PURPOSE_KEY, "signup")
    user = db.get_user(uid) if uid else None
    if not user:
        flash("Start by creating an account or signing in.", "error")
        return redirect(url_for("login"))
    if request.method == "POST":
        blocked = _guard_ip_rate_limit("user_verify",
                                       template_name="login_verify.html",
                                       email=user["email"], purpose=purpose)
        if blocked:
            return blocked
        code = request.form.get("code", "").strip()
        if db.verify_user_code(user["id"], purpose, code, int(time.time())):
            if purpose == "signup":
                db.mark_user_verified(user["id"])
            session[USER_SESSION_KEY] = user["id"]
            session.pop(USER_PENDING_KEY, None)
            session.pop(USER_PENDING_PURPOSE_KEY, None)
            flash("You're verified and signed in.", "success")
            return _post_user_login_redirect()
        flash("Invalid or expired code. Request a new one.", "error")
    return render_template("login_verify.html", email=user["email"], purpose=purpose)


@app.route("/login/verify/resend", methods=["POST"])
def user_verify_resend():
    uid = session.get(USER_PENDING_KEY)
    purpose = session.get(USER_PENDING_PURPOSE_KEY, "signup")
    user = db.get_user(uid) if uid else None
    if not user:
        flash("Start by signing in first.", "error")
        return redirect(url_for("login"))
    blocked = _guard_ip_rate_limit("user_verify_resend",
                                   template_name="login_verify.html",
                                   email=user["email"], purpose=purpose)
    if blocked:
        return blocked
    ok, msg = _issue_user_code(user, purpose)
    flash("Code re-sent to your email." if ok else msg, "success" if ok else "error")
    return redirect(url_for("user_verify"))


@app.route("/login/google")
def user_google_start():
    if g.user:
        return _post_user_login_redirect()
    if not _google_ready():
        flash("Google sign-in is not configured yet.", "error")
        return redirect(url_for("login"))
    nxt = _safe_next(request.args.get("next", ""))
    if nxt:
        session[USER_NEXT_KEY] = nxt
    state = secrets.token_urlsafe(24)
    session[USER_GOOGLE_STATE_KEY] = state
    qs = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": _abs_url("user_google_callback"),
        "response_type": "code",
        "scope": GOOGLE_OAUTH_SCOPE,
        "state": state,
        "prompt": "select_account",
    })
    return redirect(f"{GOOGLE_AUTH_URL}?{qs}")


@app.route("/login/google/callback")
def user_google_callback():
    if not _google_ready():
        flash("Google sign-in is not configured yet.", "error")
        return redirect(url_for("login"))
    if request.args.get("error"):
        flash("Google sign-in was cancelled. Please try again.", "error")
        return redirect(url_for("login"))
    state = request.args.get("state", "")
    good_state = session.pop(USER_GOOGLE_STATE_KEY, "")
    if not state or not good_state or not hmac.compare_digest(state, good_state):
        flash("Google sign-in failed state validation. Please try again.", "error")
        return redirect(url_for("login"))
    code = request.args.get("code", "").strip()
    if not code:
        flash("Google sign-in did not return a code.", "error")
        return redirect(url_for("login"))
    try:
        tok = _google_exchange_code(code, "user_google_callback")
        access_token = (tok or {}).get("access_token", "")
        profile = _google_userinfo(access_token)
    except Exception:
        app.logger.exception("google oauth callback failed (user)")
        flash("Google sign-in failed. Please try again.", "error")
        return redirect(url_for("login"))

    email = (profile.get("email", "") or "").strip().lower()
    sub = (profile.get("sub", "") or "").strip()
    name = (profile.get("name", "") or "").strip() or "Clinician"
    picture = (profile.get("picture", "") or "").strip()
    if not email or not sub:
        flash("Google did not return required profile fields.", "error")
        return redirect(url_for("login"))

    user = db.get_user_by_oauth("google", sub)
    if not user:
        user = db.get_user_by_email(email)
        if user:
            db.link_user_oauth(user["id"], "google", sub, name, picture)
            user = db.get_user(user["id"])
        else:
            uid = db.create_user(
                email=email,
                password_hash=generate_password_hash(
                    secrets.token_urlsafe(32), method="pbkdf2:sha256"),
                name=name,
                specialty="",
                institution="",
                verified=True,
                oauth_provider="google",
                oauth_sub=sub,
                oauth_picture=picture,
            )
            user = db.get_user(uid)

    session[USER_SESSION_KEY] = user["id"]
    session.pop(USER_PENDING_KEY, None)
    session.pop(USER_PENDING_PURPOSE_KEY, None)
    flash("Signed in with Google.", "success")
    return _post_user_login_redirect()


def _gen_code():
    return f"{secrets.randbelow(1000000):06d}"


def _safe_next(raw):
    """Accept only in-app relative paths for post-auth redirects."""
    v = (raw or "").strip()
    if v.startswith("/"):
        return v
    try:
        p = urllib.parse.urlparse(v)
    except Exception:
        return ""
    return p.path if p.path.startswith("/") else ""


def _csrf_token():
    tok = session.get(CSRF_SESSION_KEY, "")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = tok
    return tok


app.jinja_env.globals["csrf_token"] = _csrf_token


_ASSET_VER_CACHE = {}


def static_url(filename):
    """url_for('static', ...) with a ?v=<mtime> cache-buster so browsers/CDNs
    fetch a fresh copy whenever a static asset changes (otherwise long-lived
    caches keep serving stale CSS/JS/logo)."""
    ver = _ASSET_VER_CACHE.get(filename)
    if ver is None or app.debug:
        try:
            ver = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        except OSError:
            ver = 0
        _ASSET_VER_CACHE[filename] = ver
    return url_for("static", filename=filename) + (f"?v={ver}" if ver else "")


app.jinja_env.globals["static_url"] = static_url


@app.before_request
def _csrf_guard():
    _csrf_token()
    if request.method != "POST":
        return None
    if request.endpoint in {"alerts_run", "reminders_run", "redcap_webhook",
                            "ops_verify_claim"}:
        return None
    sent = (request.form.get("_csrf_token", "")
            or request.headers.get("X-CSRF-Token", ""))
    good = session.get(CSRF_SESSION_KEY, "")
    if sent and good and hmac.compare_digest(sent, good):
        return None
    if _is_json_request():
        return jsonify({"ok": False, "error": "csrf_failed"}), 403
    flash("Your session expired. Please retry.", "error")
    return redirect(request.referrer or request.path or url_for("home"))


def _is_json_request():
    accepts = request.accept_mimetypes
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    return (accepts.best == "application/json"
            and accepts["application/json"] > accepts["text/html"])


def _rate_limited_response(msg, retry_after, template_name="", **template_ctx):
    retry_after = max(1, int(retry_after or RATE_LIMIT_WINDOW_SECONDS))
    if _is_json_request():
        resp = jsonify({"ok": False, "error": msg, "retry_after": retry_after})
        resp.status_code = 429
    elif template_name:
        resp = make_response(render_template(template_name, **template_ctx), 429)
    else:
        resp = make_response(
            render_template("error.html", code=429, msg=msg), 429)
    resp.headers["Retry-After"] = str(retry_after)
    return resp


def _llm_search_budget_ok():
    """Consume one unit of the global daily LLM-search budget. Returns False once
    the day's ceiling is hit, so search falls back to non-LLM matching instead of
    burning through the model budget. Fails open on counter errors."""
    if LLM_DAILY_SEARCH_CAP <= 0:
        return True
    try:
        ok, _ = db.check_and_bump_ip_limit(
            "llm_search_daily", "global", LLM_DAILY_SEARCH_CAP, 86400)
        return ok
    except Exception:
        app.logger.exception("llm daily budget check failed")
        return True


def _guard_ip_rate_limit(route_key, template_name="", **template_ctx):
    """Rate-limit sensitive POST flows by client IP."""
    limit = RATE_LIMIT_ROUTES.get(route_key, 0)
    ip = (request.remote_addr or "").strip()
    if not ip or limit <= 0 or RATE_LIMIT_WINDOW_SECONDS <= 0:
        flash("Temporarily unavailable. Please try again shortly.", "error")
        return _rate_limited_response(RATE_LIMIT_DEFAULT_MSG,
                                      RATE_LIMIT_WINDOW_SECONDS,
                                      template_name, **template_ctx)
    try:
        ok, retry_after = db.check_and_bump_ip_limit(
            route_key, ip, limit, RATE_LIMIT_WINDOW_SECONDS)
    except Exception:
        app.logger.exception("rate-limit check failed for %s", route_key)
        ok, retry_after = False, RATE_LIMIT_WINDOW_SECONDS
    if ok:
        return None
    mins = max(1, int(math.ceil(float(retry_after) / 60.0)))
    flash(f"{RATE_LIMIT_DEFAULT_MSG} Try again in about {mins} minute(s).", "error")
    return _rate_limited_response(RATE_LIMIT_DEFAULT_MSG, retry_after,
                                  template_name, **template_ctx)


def _issue_patient_code(patient, purpose):
    """Create and send a short-lived email verification/login code."""
    if not mailer.smtp_configured():
        return False, "Email verification is unavailable right now."
    db.invalidate_patient_codes(patient["id"], purpose)
    code = _gen_code()
    exp = int(time.time()) + (10 * 60)  # 10 minutes
    db.create_patient_code(patient["id"], purpose, code, exp)
    action = {
        "signup": "sign-up verification",
        "apply": "email verification",
    }.get(purpose, "login verification")
    subject = f"Your BridgeMD {action} code"
    body = "\n".join([
        f"Hi {patient['full_name'] or 'there'},",
        "",
        f"Your BridgeMD {action} code is:",
        "",
        f"  {code}",
        "",
        "It expires in 10 minutes.",
        "",
        "If you didn't request this, ignore this email.",
    ])
    ok, msg = mailer.send_email(patient["email"], subject, body)
    return ok, msg


def _issue_user_code(user, purpose):
    """Create and send a short-lived clinician verification/login code."""
    if not mailer.smtp_configured():
        return False, "Email verification is unavailable right now."
    db.invalidate_user_codes(user["id"], purpose)
    code = _gen_code()
    exp = int(time.time()) + (10 * 60)  # 10 minutes
    db.create_user_code(user["id"], purpose, code, exp)
    action = "sign-up verification" if purpose == "signup" else "login verification"
    subject = f"Your BridgeMD {action} code"
    body = "\n".join([
        f"Hi {user['name'] or 'there'},",
        "",
        f"Your BridgeMD {action} code is:",
        "",
        f"  {code}",
        "",
        "It expires in 10 minutes.",
        "",
        "If you didn't request this, ignore this email.",
    ])
    ok, msg = mailer.send_email(user["email"], subject, body)
    return ok, msg


def _google_ready():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _google_exchange_code(code, redirect_endpoint="patient_google_callback"):
    payload = urllib.parse.urlencode({
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": _abs_url(redirect_endpoint),
        "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _google_userinfo(access_token):
    req = urllib.request.Request(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _post_patient_login_redirect():
    fresh = db.get_patient_user(session.get(PATIENT_SESSION_KEY))
    if fresh and not fresh["onboarding_done"]:
        return redirect(url_for("patient_onboarding"))
    nxt = session.pop(PATIENT_NEXT_KEY, "")
    return redirect(nxt if nxt.startswith("/") else url_for("home"))


def _post_user_login_redirect():
    nxt = session.pop(USER_NEXT_KEY, "")
    return redirect(nxt if nxt.startswith("/") else url_for("dashboard"))


@app.route("/account/signup", methods=["GET", "POST"])
def patient_signup():
    if g.patient_user:
        return redirect(url_for("patient_onboarding")
                        if not g.patient_user["onboarding_done"] else url_for("home"))
    nxt = _safe_next(request.args.get("next", ""))
    if nxt:
        session[PATIENT_NEXT_KEY] = nxt
    if request.method == "POST":
        blocked = _guard_ip_rate_limit("account_signup",
                                       template_name="patient_signup.html")
        if blocked:
            return blocked
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        if not full_name or not email or not pw:
            flash("Name, email, and password are required.", "error")
            return render_template("patient_signup.html",
                                   google_enabled=_google_ready())
        if not request.form.get("agree"):
            flash("Please agree to the Terms and Privacy notice to create an account.",
                  "error")
            return render_template("patient_signup.html",
                                   google_enabled=_google_ready())
        if len(pw) < 8:
            flash("Use at least 8 characters for your password.", "error")
            return render_template("patient_signup.html",
                                   google_enabled=_google_ready())
        pw_hash = generate_password_hash(pw, method="pbkdf2:sha256")
        existing = db.get_patient_by_email(email)
        if existing:
            # A real, password-protected account already owns this email.
            if existing["password_hash"]:
                flash("An account with that email already exists. Try signing in.",
                      "error")
                return redirect(url_for("patient_login"))
            # Apply-first (passwordless) account: claim it by setting a password on
            # the SAME account, so every application they already submitted with
            # this email stays attached to their new login.
            db.set_patient_password(existing["id"], pw_hash)
            if existing["verified"]:
                session[PATIENT_SESSION_KEY] = existing["id"]
                flash("Account created - your earlier application is saved here.",
                      "success")
                return _post_patient_login_redirect()
            ok, msg = _issue_patient_code(existing, "signup")
            if not ok:
                flash(msg, "error")
                return render_template("patient_signup.html",
                                       google_enabled=_google_ready())
            session[PATIENT_PENDING_KEY] = existing["id"]
            session[PATIENT_PENDING_PURPOSE_KEY] = "signup"
            flash("Check your email for a 6-digit verification code.", "success")
            return redirect(url_for("patient_verify"))
        pid = db.create_patient_user(email, pw_hash, full_name)
        patient = db.get_patient_user(pid)
        ok, msg = _issue_patient_code(patient, "signup")
        if not ok:
            flash(msg, "error")
            return render_template("patient_signup.html",
                                   google_enabled=_google_ready())
        session[PATIENT_PENDING_KEY] = pid
        session[PATIENT_PENDING_PURPOSE_KEY] = "signup"
        flash("Check your email for a 6-digit verification code.", "success")
        return redirect(url_for("patient_verify"))
    return render_template("patient_signup.html", google_enabled=_google_ready())


@app.route("/account/verify", methods=["GET", "POST"])
def patient_verify():
    pid = session.get(PATIENT_PENDING_KEY)
    purpose = session.get(PATIENT_PENDING_PURPOSE_KEY, "signup")
    patient = db.get_patient_user(pid) if pid else None
    if not patient:
        flash("Start by signing up or signing in.", "error")
        return redirect(url_for("patient_signup"))
    if request.method == "POST":
        blocked = _guard_ip_rate_limit(
            "account_verify",
            template_name="patient_verify.html",
            email=patient["email"], purpose=purpose)
        if blocked:
            return blocked
        code = request.form.get("code", "").strip()
        if db.verify_patient_code(patient["id"], purpose, code, int(time.time())):
            if purpose == "signup":
                db.mark_patient_verified(patient["id"])
            session[PATIENT_SESSION_KEY] = patient["id"]
            session.pop(PATIENT_PENDING_KEY, None)
            session.pop(PATIENT_PENDING_PURPOSE_KEY, None)
            flash("You're verified and signed in.", "success")
            return _post_patient_login_redirect()
        flash("Invalid or expired code. Request a new one.", "error")
    return render_template("patient_verify.html", email=patient["email"],
                           purpose=purpose)


@app.route("/account/verify/resend", methods=["POST"])
def patient_verify_resend():
    pid = session.get(PATIENT_PENDING_KEY)
    purpose = session.get(PATIENT_PENDING_PURPOSE_KEY, "signup")
    patient = db.get_patient_user(pid) if pid else None
    if not patient:
        flash("Start by signing up or signing in first.", "error")
        return redirect(url_for("patient_signup"))
    blocked = _guard_ip_rate_limit(
        "account_verify_resend",
        template_name="patient_verify.html",
        email=patient["email"], purpose=purpose)
    if blocked:
        return blocked
    ok, msg = _issue_patient_code(patient, purpose)
    flash("Code re-sent to your email." if ok else msg, "success" if ok else "error")
    return redirect(url_for("patient_verify"))


def _rate_limited_json(route_key):
    """Lightweight per-IP rate limit for JSON endpoints (no template rendering)."""
    limit = RATE_LIMIT_ROUTES.get(route_key, 0)
    ip = (request.remote_addr or "").strip()
    if not ip or limit <= 0 or RATE_LIMIT_WINDOW_SECONDS <= 0:
        return jsonify({"ok": False, "error": "unavailable",
                        "message": "Temporarily unavailable. Please try again shortly."}), 503
    try:
        ok, retry_after = db.check_and_bump_ip_limit(
            route_key, ip, limit, RATE_LIMIT_WINDOW_SECONDS)
    except Exception:
        app.logger.exception("rate-limit check failed for %s", route_key)
        ok, retry_after = False, RATE_LIMIT_WINDOW_SECONDS
    if ok:
        return None
    mins = max(1, int(math.ceil(float(retry_after) / 60.0)))
    return jsonify({"ok": False, "error": "rate_limited",
                    "message": f"Too many attempts. Try again in about {mins} minute(s)."}), 429


def _find_or_create_apply_patient(email, name=""):
    """Return an existing patient account for `email`, or create a passwordless one.

    Apply-first accounts store an empty password_hash to mark them "claimable":
    the visitor never set a password, so a later sign-up with the same email just
    sets one on THIS same account (keeping their applications), and sign-in works
    by email code. The empty hash never matches a password, so it's safe."""
    existing = db.get_patient_by_email(email)
    if existing:
        return existing
    pid = db.create_patient_user(email, "", (name or "").strip())
    return db.get_patient_user(pid)


@app.route("/apply/send-code", methods=["POST"])
def apply_send_code():
    """Inline anti-bot step on the apply form: email a 6-digit code to the address
    the visitor typed. No account/password wall - just proves a real inbox. Creates
    (or reuses) a passwordless patient account behind the scenes for follow-up."""
    limited = _rate_limited_json("apply_send_code")
    if limited:
        return limited
    email = (request.form.get("email", "") or "").strip().lower()
    name = (request.form.get("name", "") or "").strip()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"ok": False, "error": "bad_email",
                        "message": "Enter a valid email address."}), 400
    # Already signed in with this email? No need to verify again.
    if g.patient_user and (g.patient_user["email"] or "").lower() == email \
            and g.patient_user["verified"]:
        return jsonify({"ok": True, "already_verified": True})
    patient = _find_or_create_apply_patient(email, name)
    ok, msg = _issue_patient_code(patient, "apply")
    if not ok:
        return jsonify({"ok": False, "error": "send_failed",
                        "message": msg or "Couldn't send the code. Try again."}), 502
    _log_event("apply_code_sent")
    return jsonify({"ok": True})


@app.route("/apply/verify-code", methods=["POST"])
def apply_verify_code():
    """Check the code the visitor entered; on success mark the email verified and
    sign them in so the normal apply POST goes through with no login wall."""
    limited = _rate_limited_json("apply_verify_code")
    if limited:
        return limited
    email = (request.form.get("email", "") or "").strip().lower()
    code = (request.form.get("code", "") or "").strip()
    if not email or not code:
        return jsonify({"ok": False, "error": "missing",
                        "message": "Enter the code we emailed you."}), 400
    patient = db.get_patient_by_email(email)
    if not patient or not db.verify_patient_code(
            patient["id"], "apply", code, int(time.time())):
        return jsonify({"ok": False, "error": "bad_code",
                        "message": "Invalid or expired code. Send a new one."}), 400
    if not patient["verified"]:
        db.mark_patient_verified(patient["id"])
    session[PATIENT_SESSION_KEY] = patient["id"]
    _log_event("apply_verified")
    return jsonify({"ok": True})


@app.route("/account/login", methods=["GET", "POST"])
def patient_login():
    if g.patient_user:
        return redirect(url_for("patient_onboarding")
                        if not g.patient_user["onboarding_done"] else url_for("home"))
    nxt = _safe_next(request.args.get("next", ""))
    if nxt:
        session[PATIENT_NEXT_KEY] = nxt
    if request.method == "POST":
        blocked = _guard_ip_rate_limit("account_login",
                                       template_name="patient_login.html")
        if blocked:
            return blocked
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        patient = db.get_patient_by_email(email)
        if not patient:
            flash("Wrong email or password.", "error")
            return render_template("patient_login.html",
                                   google_enabled=_google_ready())
        # Apply-first account that never set a password: don't reject - send an
        # email sign-in code so they can reach the applications tied to this email.
        if not patient["password_hash"]:
            purpose = "login" if patient["verified"] else "signup"
            ok, msg = _issue_patient_code(patient, purpose)
            if not ok:
                flash(msg, "error")
                return render_template("patient_login.html",
                                       google_enabled=_google_ready())
            session[PATIENT_PENDING_KEY] = patient["id"]
            session[PATIENT_PENDING_PURPOSE_KEY] = purpose
            flash("You applied without a password. We emailed you a 6-digit "
                  "sign-in code.", "success")
            return redirect(url_for("patient_verify"))
        if not check_password_hash(patient["password_hash"], pw):
            flash("Wrong email or password.", "error")
            return render_template("patient_login.html",
                                   google_enabled=_google_ready())
        if not patient["verified"]:
            ok, msg = _issue_patient_code(patient, "signup")
            if not ok:
                flash(msg, "error")
                return render_template("patient_login.html",
                                       google_enabled=_google_ready())
            session[PATIENT_PENDING_KEY] = patient["id"]
            session[PATIENT_PENDING_PURPOSE_KEY] = "signup"
            flash("Verify your email to finish account setup. We sent a fresh code.", "success")
            return redirect(url_for("patient_verify"))
        ok, msg = _issue_patient_code(patient, "login")
        if not ok:
            flash(msg, "error")
            return render_template("patient_login.html",
                                   google_enabled=_google_ready())
        session[PATIENT_PENDING_KEY] = patient["id"]
        session[PATIENT_PENDING_PURPOSE_KEY] = "login"
        flash("Enter the 6-digit code we sent to your email.", "success")
        return redirect(url_for("patient_verify"))
    return render_template("patient_login.html", google_enabled=_google_ready())


@app.route("/account/code", methods=["GET", "POST"])
def patient_code_login():
    """Passwordless email-code sign-in. Meant for people who applied first (their
    account has no password) and for anyone who'd rather not use a password. We
    email a 6-digit code and hand off to the shared verify step, which signs them
    in and shows their applications."""
    if g.patient_user:
        return _post_patient_login_redirect()
    nxt = _safe_next(request.args.get("next", ""))
    if nxt:
        session[PATIENT_NEXT_KEY] = nxt
    if request.method == "POST":
        blocked = _guard_ip_rate_limit("account_login",
                                       template_name="patient_code_login.html")
        if blocked:
            return blocked
        email = request.form.get("email", "").strip().lower()
        patient = db.get_patient_by_email(email)
        if patient:
            purpose = "login" if patient["verified"] else "signup"
            ok, msg = _issue_patient_code(patient, purpose)
            if ok:
                session[PATIENT_PENDING_KEY] = patient["id"]
                session[PATIENT_PENDING_PURPOSE_KEY] = purpose
                return redirect(url_for("patient_verify"))
            flash(msg, "error")
            return render_template("patient_code_login.html")
        # Neutral message so we don't reveal which emails have accounts.
        flash("If that email has applications with us, we just sent a 6-digit "
              "sign-in code. Check your inbox.", "success")
        return render_template("patient_code_login.html")
    return render_template("patient_code_login.html")


@app.route("/account/google")
def patient_google_start():
    if g.patient_user:
        return _post_patient_login_redirect()
    if not _google_ready():
        flash("Google sign-in is not configured yet.", "error")
        return redirect(url_for("patient_login"))
    nxt = _safe_next(request.args.get("next", ""))
    if nxt:
        session[PATIENT_NEXT_KEY] = nxt
    state = secrets.token_urlsafe(24)
    session[PATIENT_GOOGLE_STATE_KEY] = state
    qs = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": _abs_url("patient_google_callback"),
        "response_type": "code",
        "scope": GOOGLE_OAUTH_SCOPE,
        "state": state,
        "prompt": "select_account",
    })
    return redirect(f"{GOOGLE_AUTH_URL}?{qs}")


@app.route("/account/google/callback")
def patient_google_callback():
    if not _google_ready():
        flash("Google sign-in is not configured yet.", "error")
        return redirect(url_for("patient_login"))
    if request.args.get("error"):
        flash("Google sign-in was cancelled. Please try again.", "error")
        return redirect(url_for("patient_login"))
    state = request.args.get("state", "")
    good_state = session.pop(PATIENT_GOOGLE_STATE_KEY, "")
    if not state or not good_state or not hmac.compare_digest(state, good_state):
        flash("Google sign-in failed state validation. Please try again.", "error")
        return redirect(url_for("patient_login"))
    code = request.args.get("code", "").strip()
    if not code:
        flash("Google sign-in did not return a code.", "error")
        return redirect(url_for("patient_login"))
    try:
        tok = _google_exchange_code(code)
        access_token = (tok or {}).get("access_token", "")
        profile = _google_userinfo(access_token)
    except Exception:
        app.logger.exception("google oauth callback failed")
        flash("Google sign-in failed. Please try again.", "error")
        return redirect(url_for("patient_login"))

    email = (profile.get("email", "") or "").strip().lower()
    sub = (profile.get("sub", "") or "").strip()
    name = (profile.get("name", "") or "").strip()
    picture = (profile.get("picture", "") or "").strip()
    if not email or not sub:
        flash("Google did not return required profile fields.", "error")
        return redirect(url_for("patient_login"))

    patient = db.get_patient_by_oauth("google", sub)
    if not patient:
        patient = db.get_patient_by_email(email)
        if patient:
            db.link_patient_oauth(patient["id"], "google", sub, name, picture)
            patient = db.get_patient_user(patient["id"])
        else:
            pid = db.create_patient_user(
                email=email,
                password_hash=generate_password_hash(
                    secrets.token_urlsafe(32), method="pbkdf2:sha256"),
                full_name=name,
                oauth_provider="google",
                oauth_sub=sub,
                oauth_picture=picture,
                verified=True,
            )
            patient = db.get_patient_user(pid)
    session[PATIENT_SESSION_KEY] = patient["id"]
    session.pop(PATIENT_PENDING_KEY, None)
    session.pop(PATIENT_PENDING_PURPOSE_KEY, None)
    flash("Signed in with Google.", "success")
    return _post_patient_login_redirect()


@app.route("/account/logout", methods=["POST"])
def patient_logout():
    session.pop(PATIENT_SESSION_KEY, None)
    flash("Signed out.", "success")
    return redirect(url_for("home"))


@app.route("/account/onboarding", methods=["GET", "POST"])
@patient_login_required
def patient_onboarding():
    if request.method == "POST":
        f = request.form
        interest = f.get("primary_interest", "").strip()
        # Always the verified account email - never a user-supplied address, so
        # our emails can't be redirected to someone else's inbox.
        notify_email = g.patient_user["email"]
        wants_alerts = bool(f.get("email_alerts"))
        db.set_patient_onboarding(
            g.patient_user["id"], interest, notify_email, wants_alerts)
        if wants_alerts and interest:
            alert_id = db.create_alert({
                "applicant_token": g.patient_user["applicant_token"],
                "label": interest,
                "condition": interest,
                "intervention": "",
                "location": "",
                "lat": None,
                "lon": None,
                "cc": "",
                "radius": 50,
                "unit": "km",
                "email": notify_email,
            })
            try:
                alerts_mod.seed_baseline(alert_id)
            except Exception:
                app.logger.exception("onboarding alert baseline failed")
        flash("Onboarding complete.", "success")
        nxt = session.pop(PATIENT_NEXT_KEY, "")
        return redirect(nxt if nxt.startswith("/") else url_for("home"))
    return render_template("patient_onboarding.html")


# --------------------------------------------------------------------------- #
# Dashboard (clinician app lives under /app; "/" is the public consumer site)
# --------------------------------------------------------------------------- #
@app.route("/app")
@login_required
def dashboard():
    referrals = db.list_referrals(g.user["id"])
    counts = db.status_counts(g.user["id"])
    enrolled = db.enrolled_count(g.user["id"])
    invites = db.list_invites(g.user["id"])
    proactive = [x for x in invites if (x.get("applied") or 0) > 0]
    proactive.sort(
        key=lambda x: (
            -int(x.get("enrolled") or 0),
            -int(x.get("active") or 0),
            -int(x.get("applied") or 0),
        )
    )
    if _demo_mode_enabled() and not proactive:
        proactive = [
            {"invite": {"nct": "NCT05869903", "title": "Semaglutide in Adults With Obesity",
                        "condition": "Obesity"},
             "applied": 3, "active": 2, "enrolled": 1},
            {"invite": {"nct": "NCT06034262", "title": "Tirzepatide vs Placebo for Type 2 Diabetes and Weight",
                        "condition": "Type 2 diabetes"},
             "applied": 2, "active": 1, "enrolled": 0},
        ]
    proactive_summary = {
        "trials": len(invites),
        "applied": sum(int(x.get("applied") or 0) for x in invites),
        "active": sum(int(x.get("active") or 0) for x in invites),
        "enrolled": sum(int(x.get("enrolled") or 0) for x in invites),
    }
    # Lane 4 of the demo: "EHR background matching". Same route so the POV
    # switcher's deep link (wf=proactive) keeps working, but a dedicated view
    # that clearly shows the clinic-wide EHR feeding new patient matches.
    wf = (request.args.get("wf") or "").strip().lower()
    if wf == "proactive":
        return render_template(
            "ehr_matching.html",
            ehr=(_ehr_matching_demo() if _demo_mode_enabled() else None))
    return render_template(
        "dashboard.html", referrals=referrals[:8], counts=counts,
        total=len(referrals), enrolled=enrolled, statuses=db.STATUSES,
        proactive=proactive[:6], proactive_summary=proactive_summary)


def _ehr_matching_demo():
    """Demo payload for lane 4 (EHR background matching). Tells one clear story:
    the clinic's EHR is connected clinic-wide, the platform continuously screens
    the clinic's OWN patients against the trials that clinic is running, and new
    matches surface here for a physician to review and reach out.

    Compliance note: this is clinical decision support for the treating clinic
    (their physicians, their patients). Patients are shown de-identified; a
    clinician reviews before any contact; no referral is bought or sold. See
    matcher/COMPLIANCE.md.
    """
    trials = [
        {
            "nct": "NCT05869903",
            "title": "Once-Weekly Semaglutide in Adults With Obesity",
            "condition": "Obesity",
            "lead": "Dr. Alvarez (Endocrinology)",
            "patients": [
                {"ref": "PT-4821", "initials": "J.M.", "age": 54, "sex": "F",
                 "seen": "Seen 3 days ago", "flag": "Strong match", "score": 94,
                 "reason": "BMI 38, HbA1c 6.1%, no prior GLP-1 - meets key criteria",
                 "last_visit": "Endocrinology follow-up · Jul 10, 2026",
                 "vitals": [{"k": "BMI", "v": "38.2"}, {"k": "BP", "v": "128/82"},
                            {"k": "Weight", "v": "104 kg"}],
                 "labs": [{"k": "HbA1c", "v": "6.1%", "tone": "met"},
                          {"k": "eGFR", "v": "88", "tone": "met"},
                          {"k": "ALT", "v": "24 U/L", "tone": "met"}],
                 "meds": ["Lisinopril 10 mg daily", "Atorvastatin 20 mg daily"],
                 "criteria": {
                     "met": ["Age 18-75", "BMI \u2265 30", "No prior GLP-1 receptor agonist",
                             "No type 1 diabetes"],
                     "review": [], "exclude": ["No bariatric surgery in past 12 mo"]},
                 "contact": "Patient portal active · phone on file"},
                {"ref": "PT-3910", "initials": "R.K.", "age": 47, "sex": "M",
                 "seen": "Seen 2 weeks ago", "flag": "Likely", "score": 81,
                 "reason": "BMI 34, prediabetes, stable meds - likely eligible",
                 "last_visit": "Primary care annual physical · Jun 28, 2026",
                 "vitals": [{"k": "BMI", "v": "34.1"}, {"k": "BP", "v": "134/86"},
                            {"k": "Weight", "v": "112 kg"}],
                 "labs": [{"k": "HbA1c", "v": "5.9%", "tone": "met"},
                          {"k": "eGFR", "v": "79", "tone": "met"},
                          {"k": "Fasting glucose", "v": "108 mg/dL", "tone": "review"}],
                 "meds": ["Amlodipine 5 mg daily"],
                 "criteria": {
                     "met": ["Age 18-75", "BMI \u2265 30", "No prior GLP-1 receptor agonist"],
                     "review": ["Weight stable past 3 months (confirm at visit)"],
                     "exclude": ["No type 1 diabetes"]},
                 "contact": "Phone on file · no portal login yet"},
                {"ref": "PT-5567", "initials": "D.O.", "age": 61, "sex": "F",
                 "seen": "Seen 1 month ago", "flag": "Review", "score": 68,
                 "reason": "BMI 41, hypertension controlled - confirm exclusion labs",
                 "last_visit": "Cardiology consult · Jun 12, 2026",
                 "vitals": [{"k": "BMI", "v": "41.0"}, {"k": "BP", "v": "142/88"},
                            {"k": "Weight", "v": "119 kg"}],
                 "labs": [{"k": "HbA1c", "v": "6.4%", "tone": "met"},
                          {"k": "eGFR", "v": "62", "tone": "review"},
                          {"k": "TSH", "v": "pending", "tone": "review"}],
                 "meds": ["Losartan 50 mg daily", "Hydrochlorothiazide 25 mg daily",
                          "Metformin 500 mg BID"],
                 "criteria": {
                     "met": ["Age 18-75", "BMI \u2265 30"],
                     "review": ["eGFR near cutoff - reconfirm renal panel",
                                "TSH result pending"],
                     "exclude": ["No type 1 diabetes"]},
                 "contact": "Patient portal active · phone on file"},
            ],
        },
        {
            "nct": "NCT06034262",
            "title": "Tirzepatide vs Placebo for Type 2 Diabetes and Weight",
            "condition": "Type 2 diabetes",
            "lead": "Dr. Alvarez (Endocrinology)",
            "patients": [
                {"ref": "PT-2244", "initials": "S.P.", "age": 58, "sex": "M",
                 "seen": "Seen 5 days ago", "flag": "Strong match", "score": 91,
                 "reason": "HbA1c 8.2% on metformin, BMI 31 - meets key criteria",
                 "last_visit": "Diabetes management visit · Jul 8, 2026",
                 "vitals": [{"k": "BMI", "v": "31.4"}, {"k": "BP", "v": "126/80"},
                            {"k": "Weight", "v": "96 kg"}],
                 "labs": [{"k": "HbA1c", "v": "8.2%", "tone": "met"},
                          {"k": "eGFR", "v": "91", "tone": "met"},
                          {"k": "C-peptide", "v": "2.1 ng/mL", "tone": "met"}],
                 "meds": ["Metformin 1000 mg BID"],
                 "criteria": {
                     "met": ["Age 18-75", "T2D on metformin", "HbA1c 7.0-10.5%",
                             "BMI \u2265 25"],
                     "review": [], "exclude": ["No insulin in past 90 days"]},
                 "contact": "Patient portal active · phone on file"},
                {"ref": "PT-6620", "initials": "A.L.", "age": 63, "sex": "F",
                 "seen": "Seen 3 weeks ago", "flag": "Likely", "score": 84,
                 "reason": "T2D 6 yrs, HbA1c 7.9%, eGFR 74 - likely eligible",
                 "last_visit": "Primary care follow-up · Jun 22, 2026",
                 "vitals": [{"k": "BMI", "v": "29.6"}, {"k": "BP", "v": "138/84"},
                            {"k": "Weight", "v": "78 kg"}],
                 "labs": [{"k": "HbA1c", "v": "7.9%", "tone": "met"},
                          {"k": "eGFR", "v": "74", "tone": "met"},
                          {"k": "ALT", "v": "31 U/L", "tone": "met"}],
                 "meds": ["Metformin 1000 mg BID", "Empagliflozin 10 mg daily"],
                 "criteria": {
                     "met": ["Age 18-75", "T2D on metformin", "HbA1c 7.0-10.5%"],
                     "review": ["BMI just below 30 - confirm inclusion band"],
                     "exclude": ["No insulin in past 90 days"]},
                 "contact": "Phone on file · portal invite sent"},
            ],
        },
        {
            "nct": "NCT05813233",
            "title": "Resmetirom for Nonalcoholic Steatohepatitis (NASH)",
            "condition": "Fatty liver disease (NASH)",
            "lead": "Dr. Chen (Hepatology)",
            "patients": [
                {"ref": "PT-7788", "initials": "M.T.", "age": 52, "sex": "M",
                 "seen": "Seen 8 days ago", "flag": "Review", "score": 72,
                 "reason": "Elevated ALT, FibroScan F2-F3, T2D - confirm biopsy window",
                 "last_visit": "Hepatology consult · Jul 5, 2026",
                 "vitals": [{"k": "BMI", "v": "33.8"}, {"k": "BP", "v": "130/85"},
                            {"k": "Weight", "v": "101 kg"}],
                 "labs": [{"k": "ALT", "v": "78 U/L", "tone": "met"},
                          {"k": "AST", "v": "64 U/L", "tone": "met"},
                          {"k": "FibroScan", "v": "F2-F3", "tone": "review"}],
                 "meds": ["Metformin 1000 mg BID", "Atorvastatin 40 mg daily"],
                 "criteria": {
                     "met": ["Age 18-80", "T2D", "Elevated transaminases"],
                     "review": ["Biopsy window - confirm within screening period",
                                "FibroScan stage needs central read"],
                     "exclude": ["No cirrhosis", "No significant alcohol use"]},
                 "contact": "Patient portal active · phone on file"},
            ],
        },
    ]
    new_matches = sum(len(t["patients"]) for t in trials)
    return {
        "connected": True,
        "system": "Epic",
        "clinic": "Riverside Family Medicine",
        "records": "2,431",
        "last_sync": "12 min ago",
        "active_trials": len(trials),
        "new_matches": new_matches,
        "trials": trials,
    }


# --------------------------------------------------------------------------- #
# Public consumer site ("/") - the patient front door + programmatic SEO.
# The clinician tool lives under /app; sponsors/sites are the paying side.
# --------------------------------------------------------------------------- #
import re as _re2


def slugify(s):
    return _re2.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


# Beachhead vertical: metabolic / cardiometabolic - huge self-motivated consumer
# search demand and heavy sponsor spend. These seed the programmatic SEO pages.
VERTICAL = "Metabolic & weight"
SEED_CONDITIONS = [
    "Obesity", "Type 2 diabetes", "Weight loss", "Prediabetes",
    "Fatty liver disease (NAFLD/NASH)", "High cholesterol", "Metabolic syndrome",
    "High blood pressure", "PCOS", "Chronic kidney disease",
]
SEARCH_CONDITION_OPTIONS = [
    "Obesity", "Type 2 diabetes", "Prediabetes", "Weight loss",
    "Fatty liver disease (NAFLD/NASH)", "Metabolic syndrome", "High cholesterol",
    "High blood pressure", "PCOS", "Chronic kidney disease",
    "Heart failure", "Coronary artery disease", "Atrial fibrillation",
    "Stroke recovery", "Sleep apnea", "Migraine", "Depression", "Anxiety",
    "Alzheimer's disease", "Parkinson's disease", "Multiple sclerosis",
    "Rheumatoid arthritis", "Osteoporosis", "Psoriasis", "Crohn's disease", "Ulcerative colitis",
    "COPD", "Asthma", "Long COVID", "Endometriosis", "Lupus",
    "Breast cancer", "Prostate cancer", "Lung cancer", "Colon cancer",
]
SEED_CITIES = [
    "Toronto, ON", "Vancouver, BC", "Montreal, QC", "Calgary, AB",
    "New York, NY", "Los Angeles, CA", "Chicago, IL", "Houston, TX",
    "Miami, FL", "Boston, MA",
]

# --------------------------------------------------------------------------- #
# Programmatic SEO surface. These drive the condition + condition/city landing
# pages, their internal links, and sitemap.xml. Curated (not dumped) so every
# generated page targets a real, high-search condition that CT.gov actually has
# recruiting trials for -> no thin/empty pages that Google would penalize.
# People search "<condition> clinical trials near me"; each entry becomes a page.
# --------------------------------------------------------------------------- #
SEO_CONDITIONS = [
    # Metabolic / endocrine
    "Obesity", "Type 2 diabetes", "Type 1 diabetes", "Prediabetes", "Weight loss",
    "Metabolic syndrome", "High cholesterol", "High triglycerides",
    "High blood pressure", "PCOS", "Hypothyroidism", "Thyroid disease", "Gout",
    "Fatty liver disease (NAFLD/NASH)",
    # Cardiovascular
    "Heart failure", "Coronary artery disease", "Atrial fibrillation", "Stroke",
    "Stroke recovery", "Peripheral artery disease", "Pulmonary hypertension",
    "Deep vein thrombosis",
    # Renal
    "Chronic kidney disease", "Diabetic kidney disease", "IgA nephropathy",
    "Polycystic kidney disease",
    # Neurology
    "Migraine", "Alzheimer's disease", "Parkinson's disease", "Multiple sclerosis",
    "Epilepsy", "ALS", "Huntington's disease", "Peripheral neuropathy",
    "Restless legs syndrome", "Essential tremor", "Dementia",
    # Mental health
    "Depression", "Anxiety", "Bipolar disorder", "Schizophrenia", "PTSD", "OCD",
    "ADHD", "Insomnia", "Postpartum depression", "Alcohol use disorder",
    "Substance use disorder",
    # Respiratory
    "COPD", "Asthma", "Long COVID", "Cystic fibrosis", "Pulmonary fibrosis",
    "Sleep apnea", "Chronic cough",
    # Gastrointestinal
    "Crohn's disease", "Ulcerative colitis", "Irritable bowel syndrome",
    "Celiac disease", "GERD", "Eosinophilic esophagitis", "Gastroparesis",
    # Rheumatology / immune
    "Rheumatoid arthritis", "Lupus", "Psoriatic arthritis", "Ankylosing spondylitis",
    "Osteoarthritis", "Knee osteoarthritis", "Osteoporosis", "Sjogren's syndrome",
    "Scleroderma", "Fibromyalgia",
    # Dermatology
    "Psoriasis", "Eczema (atopic dermatitis)", "Acne", "Vitiligo",
    "Hidradenitis suppurativa", "Rosacea", "Alopecia areata",
    # Women's / men's health
    "Endometriosis", "Uterine fibroids", "Menopause", "Infertility",
    "Erectile dysfunction", "Enlarged prostate (BPH)",
    # Oncology
    "Breast cancer", "Prostate cancer", "Lung cancer", "Non-small cell lung cancer",
    "Colorectal cancer", "Pancreatic cancer", "Ovarian cancer", "Melanoma",
    "Leukemia", "Lymphoma", "Multiple myeloma", "Bladder cancer", "Kidney cancer",
    "Liver cancer", "Glioblastoma", "Head and neck cancer", "Cervical cancer",
    "Endometrial cancer", "Gastric cancer",
    # Eye
    "Age-related macular degeneration", "Diabetic retinopathy", "Glaucoma",
    "Dry eye disease",
    # Infectious disease
    "HIV", "Hepatitis B", "Hepatitis C", "COVID-19", "Influenza", "RSV",
    # Blood
    "Sickle cell disease", "Anemia", "Hemophilia", "Thalassemia",
    # Pain / other
    "Chronic pain", "Low back pain", "Smoking cessation", "Tinnitus",
    "Hearing loss", "Chronic fatigue syndrome",
]

SEO_CITIES = [
    # Canada
    "Toronto, ON", "Vancouver, BC", "Montreal, QC", "Calgary, AB", "Edmonton, AB",
    "Ottawa, ON", "Winnipeg, MB", "Quebec City, QC", "Hamilton, ON",
    "Kitchener, ON", "London, ON", "Halifax, NS",
    # United States
    "New York, NY", "Los Angeles, CA", "Chicago, IL", "Houston, TX",
    "Phoenix, AZ", "Philadelphia, PA", "San Antonio, TX", "San Diego, CA",
    "Dallas, TX", "Miami, FL", "Boston, MA", "Atlanta, GA", "Seattle, WA",
    "Denver, CO", "Washington, DC", "Minneapolis, MN", "Detroit, MI",
    "Portland, OR", "San Francisco, CA", "Nashville, TN", "Cleveland, OH",
    "Pittsburgh, PA",
]

# Fixed coordinates for the SEO cities so each condition/city page can show
# genuinely LOCAL recruiting trials (CT.gov distance filter) -> unique content per
# city, which avoids Google's "doorway page" penalty for near-duplicate pages.
SEO_CITY_COORDS = {
    "Toronto, ON": (43.6532, -79.3832), "Vancouver, BC": (49.2827, -123.1207),
    "Montreal, QC": (45.5019, -73.5674), "Calgary, AB": (51.0447, -114.0719),
    "Edmonton, AB": (53.5461, -113.4938), "Ottawa, ON": (45.4215, -75.6972),
    "Winnipeg, MB": (49.8951, -97.1384), "Quebec City, QC": (46.8139, -71.2080),
    "Hamilton, ON": (43.2557, -79.8711), "Kitchener, ON": (43.4516, -80.4925),
    "London, ON": (42.9849, -81.2453), "Halifax, NS": (44.6488, -63.5752),
    "New York, NY": (40.7128, -74.0060), "Los Angeles, CA": (34.0522, -118.2437),
    "Chicago, IL": (41.8781, -87.6298), "Houston, TX": (29.7604, -95.3698),
    "Phoenix, AZ": (33.4484, -112.0740), "Philadelphia, PA": (39.9526, -75.1652),
    "San Antonio, TX": (29.4241, -98.4936), "San Diego, CA": (32.7157, -117.1611),
    "Dallas, TX": (32.7767, -96.7970), "Miami, FL": (25.7617, -80.1918),
    "Boston, MA": (42.3601, -71.0589), "Atlanta, GA": (33.7490, -84.3880),
    "Seattle, WA": (47.6062, -122.3321), "Denver, CO": (39.7392, -104.9903),
    "Washington, DC": (38.9072, -77.0369), "Minneapolis, MN": (44.9778, -93.2650),
    "Detroit, MI": (42.3314, -83.0458), "Portland, OR": (45.5152, -122.6784),
    "San Francisco, CA": (37.7749, -122.4194), "Nashville, TN": (36.1627, -86.7816),
    "Cleveland, OH": (41.4993, -81.6944), "Pittsburgh, PA": (40.4406, -79.9959),
}

# The GLP-1 / peptide trend: people search by drug name, not condition. These
# power intervention-based landing pages and are queried via CT.gov query.intr.
# Ordered most-viral first; these are the metabolic/weight peptides driving the
# current wave of consumer interest (Ozempic/Mounjaro/Zepbound + the next-gen
# candidates people read about on social/news). Every entry has recruiting
# trials on CT.gov, so chips built from this list never dead-end.
SEED_DRUGS = [
    "Semaglutide", "Tirzepatide", "Retatrutide", "Orforglipron",
    "CagriSema", "Survodutide", "Mazdutide", "Pemvidutide",
    "Cagrilintide", "Ecnoglutide", "Liraglutide", "Dulaglutide",
]
# Common generic + brand names (and short forms) people actually type, mapped to
# the canonical drug we search CT.gov by. Lets "reta" -> Retatrutide instead of
# the freeform LLM collapsing it to a broad indication like "obesity".
_DRUG_ALIASES = {
    # generics
    "semaglutide": "Semaglutide", "tirzepatide": "Tirzepatide",
    "retatrutide": "Retatrutide", "survodutide": "Survodutide",
    "orforglipron": "Orforglipron", "cagrisema": "CagriSema",
    "cagrilintide": "Cagrilintide", "liraglutide": "Liraglutide",
    "dulaglutide": "Dulaglutide", "mazdutide": "Mazdutide",
    "pemvidutide": "Pemvidutide", "ecnoglutide": "Ecnoglutide",
    # brand names
    "ozempic": "Semaglutide", "wegovy": "Semaglutide", "rybelsus": "Semaglutide",
    "mounjaro": "Tirzepatide", "zepbound": "Tirzepatide",
    "saxenda": "Liraglutide", "victoza": "Liraglutide", "trulicity": "Dulaglutide",
}


def _detect_drug_query(text):
    """Return the canonical drug name when `text` is (or clearly begins) a known
    peptide/GLP-1 drug the user typed (e.g. "reta" -> "Retatrutide"). Returns ""
    for anything that isn't an obvious drug-name query, so real conditions and
    free-text descriptions fall through to the normal condition path."""
    q = (text or "").strip().lower()
    if not q:
        return ""
    if q in _DRUG_ALIASES:
        return _DRUG_ALIASES[q]
    # Only treat a short, single term as a drug-name query - never hijack a
    # sentence like "I take ozempic for diabetes" (that stays a description).
    if len(q.split()) > 1 or len(q) < 4:
        return ""
    for alias, canon in _DRUG_ALIASES.items():
        if alias.startswith(q) or canon.lower().startswith(q):
            return canon
    return ""
# Hand the curated candidate pools to the trends engine so it can rank them by
# live CT.gov recruiting volume (see trends.py). Configured after the app object
# in trends.configure(app); seeds are only available here once defined.
trends.set_seeds(drug_seeds=SEED_DRUGS, condition_seeds=SEED_CONDITIONS)

# Build slug -> canonical maps from the FULL SEO surface (seeds + SEO list),
# so every generated condition/city page resolves and links cleanly.
_COND_BY_SLUG = {slugify(c): c for c in (SEO_CONDITIONS + SEED_CONDITIONS)}
_CITY_BY_SLUG = {slugify(c): c for c in (SEO_CITIES + SEED_CITIES)}


def _titleize(slug):
    """Turn a slug back into a readable term for pages built from live traffic."""
    return " ".join(w.capitalize() for w in (slug or "").split("-") if w)


def _merge_terms(live, seed, limit):
    """Live (most-trafficked) terms first, then seeds to fill, deduped by
    lowercase. Guarantees the chips are never empty even with no traffic yet."""
    out, seen = [], set()
    for t in list(live) + list(seed):
        k = " ".join((t or "").lower().split())
        if k and k not in seen:
            seen.add(k)
            out.append(t)
        if len(out) >= limit:
            break
    return out


def trending_conditions(limit=8):
    """Live trending conditions from an external source (GPT + CT.gov
    validation, see trends.py), backfilled with seeds so it's never empty."""
    try:
        return _merge_terms(trends.get_trending("condition"), SEED_CONDITIONS, limit)
    except Exception:
        return SEED_CONDITIONS[:limit]


def trending_drugs(limit=6):
    try:
        return _merge_terms(trends.get_trending("drug"), SEED_DRUGS, limit)
    except Exception:
        return SEED_DRUGS[:limit]


# Condition typeahead: proxy ClinicalTrials.gov's own condition dictionary so the
# search box suggests the SAME full universe of conditions CT.gov does (every
# ADHD/lupus/etc. variant), not a short hardcoded list. Cached + falls back to
# our local lists if CT.gov is slow/unreachable, so the box always suggests
# something.
_COND_SUGGEST_CACHE = OrderedDict()
_COND_SUGGEST_TTL = 6 * 3600
_COND_SUGGEST_MAX = 1000
_CT_SUGGEST_URL = "https://clinicaltrials.gov/api/int/suggest"


def _clean_suggest(s):
    """CT.gov returns regex-escaped strings (e.g. 'Lupus \\(LN\\)') for its own
    highlighting. Strip the escapes so suggestions read cleanly."""
    for a, b in (("\\(", "("), ("\\)", ")"), ("\\[", "["), ("\\]", "]"),
                 ("\\-", "-"), ("\\.", ".")):
        s = s.replace(a, b)
    return " ".join(s.split())


def _local_condition_matches(q, limit=12):
    ql = q.lower()
    out, seen = [], set()
    for c in (SEARCH_CONDITION_OPTIONS + SEO_CONDITIONS):
        lc = c.lower()
        if ql in lc and lc not in seen:
            seen.add(lc)
            out.append(c)
        if len(out) >= limit:
            break
    return out


# NLM RxTerms drug autocomplete: drug-only (no procedures/behavioral arms like
# CT.gov's intervention dictionary returns), so the drug typeahead stays clean.
_RXTERMS_URL = "https://clinicaltables.nlm.nih.gov/api/rxterms/v3/search"
_DRUG_SKIP = ("pack", "starter", "package")


def _drug_suggest_matches(q, limit=6):
    """Suggest drug/peptide names for the search typeahead. Curated GLP-1 seeds
    and brand aliases come first (so "ozempic" -> Semaglutide, "reta" ->
    Retatrutide - these cover investigational peptides RxTerms lacks), then NLM
    RxTerms so any approved drug the user types also surfaces. Returns dicts
    tagged kind="drug"; `value` is the name we search CT.gov by, `note` is the
    recognizable brand the user typed (when we mapped it to a generic)."""
    ql = q.lower().strip()
    if not ql:
        return []
    out, seen = [], set()

    def add(value, note=""):
        k = (value or "").lower()
        if value and k not in seen:
            seen.add(k)
            out.append({"label": value, "value": value,
                        "note": note, "kind": "drug"})

    # Curated brand/generic aliases -> canonical (keep the typed brand as a note).
    for alias, canon in _DRUG_ALIASES.items():
        if alias.startswith(ql) or canon.lower().startswith(ql):
            note = alias.title() if alias != canon.lower() else ""
            add(canon, note)
    for d in SEED_DRUGS:
        if ql in d.lower():
            add(d)
    # Backfill from NLM RxTerms (clean, drug-only, broad coverage of approved
    # drugs). Strip the "(Injectable)/(Oral Pill)" form suffix and skip packs.
    if len(out) < limit and len(ql) >= 2:
        try:
            url = (_RXTERMS_URL + "?maxList=8&terms=" + urllib.parse.quote(q))
            req = urllib.request.Request(
                url, headers={"User-Agent": "BridgeMD/1.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.load(r)
            names = data[1] if isinstance(data, list) and len(data) > 1 else []
            for raw in names:
                base = str(raw).split(" (")[0].strip()
                low = base.lower()
                if not base or any(w in low for w in _DRUG_SKIP):
                    continue
                canon = _DRUG_ALIASES.get(low)     # brand -> curated generic
                add(canon or base.title(),
                    base.title() if canon else "")
                if len(out) >= limit:
                    break
        except Exception:
            pass
    return out[:limit]


@app.route("/api/condition-suggest")
def condition_suggest():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"items": []})
    key = q.lower()
    now_ts = time.time()
    hit = _COND_SUGGEST_CACHE.get(key)
    if hit and (now_ts - hit[0]) < _COND_SUGGEST_TTL:
        _COND_SUGGEST_CACHE.move_to_end(key)
        return jsonify({"items": hit[1]})
    conds = []
    try:
        url = (_CT_SUGGEST_URL + "?dictionary=Condition&input="
               + urllib.parse.quote(q))
        req = urllib.request.Request(url, headers={"User-Agent": "BridgeMD/1.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.load(r)
        if isinstance(data, list):
            conds = [_clean_suggest(str(x)) for x in data if str(x).strip()]
            conds = [x for x in conds if x][:12]
    except Exception:
        conds = []
    if not conds:                                    # CT.gov down/slow -> local
        conds = _local_condition_matches(q)
    # People search by drug name too (GLP-1 wave: ozempic, retatrutide). Surface
    # matching drugs first, then conditions, in one dropdown.
    drugs = _drug_suggest_matches(q)
    items = drugs + [{"label": c, "value": c, "kind": "condition"}
                     for c in conds]
    _COND_SUGGEST_CACHE[key] = (now_ts, items)
    _COND_SUGGEST_CACHE.move_to_end(key)
    while len(_COND_SUGGEST_CACHE) > _COND_SUGGEST_MAX:
        _COND_SUGGEST_CACHE.popitem(last=False)
    return jsonify({"items": items})


def build_patient_note(condition, age="", sex="", about="", pregnant="",
                       other_trial=""):
    """Turn a patient's self-reported details into a note the matcher can read.
    AGE/SEX feed the deterministic hard gate; pregnancy and concurrent-trial
    status are added as plain lines so the per-trial LLM eligibility read can
    apply them (they are not structured CT.gov fields)."""
    lines = []
    if str(age).strip():
        lines.append(f"AGE: {str(age).strip()}")
    if sex in ("male", "female"):
        lines.append(f"SEX: {sex}")
    if condition:
        lines.append(f"Condition: {condition}.")
    if str(pregnant).strip().lower() == "yes":
        lines.append("Currently pregnant or breastfeeding.")
    if str(other_trial).strip().lower() == "yes":
        lines.append("Currently enrolled in another clinical trial.")
    if about.strip():
        lines.append(about.strip())
    return "\n".join(lines)


@app.route("/")
def home():
    # Home should always be the public search landing.
    # Users can switch surfaces from the POV switcher.
    return _render_landing()


@app.route("/e/visit", methods=["POST"])
def track_visit():
    """Client-side visit beacon. The landing page fires this on load, so only
    real browsers that execute JavaScript are counted — most crawlers never run
    JS and so never reach here. Bot user-agents and owner traffic are dropped
    inside _log_event. Always returns 204 (best-effort, no body)."""
    _log_event("visit")
    return ("", 204)


# Homepage credibility stats. Baked from a ClinicalTrials.gov scrape (run
# tools/refresh_home_stats.py to recompute) instead of hitting the API on every
# page load. Floors are rounded DOWN so the "+" stays truthful.
#   Refreshed 2026-07-20 against https://clinicaltrials.gov/api/v2/studies:
#     overallStatus=RECRUITING ............................. 65,213  -> 65,000+
#     RECRUITING + (compensation wording OR healthy volunteers)  2,680  ->  2,500+
#       (paid-study proxy; CT.gov rarely indexes pay wording, so this is a
#        conservative floor of trials that may compensate participants.)
HOME_STATS = {
    "recruiting": "65,000+",
    "paid": "2,500+",
}


def _patient_search_ctx():
    """Prefill + shortcuts for a signed-in patient's search page, so returning
    users don't start from scratch: reuse their known location and age/sex, and
    offer one-tap re-search of conditions they've looked at. Uses only the
    patient's own data; nothing is exposed to other users."""
    p = g.patient_user
    if not p:
        return None
    token = p["applicant_token"]
    leads = db.list_leads_by_applicant(token) if token else []
    alerts = db.list_alerts(token) if token else []
    # Location: prefer a saved alert (it carries lat/lon/cc), else last applied lead.
    loc = {"location": "", "lat": "", "lon": "", "cc": ""}
    for a in alerts:
        if a["location"]:
            loc = {"location": a["location"], "lat": a["lat"] or "",
                   "lon": a["lon"] or "", "cc": a["cc"] or ""}
            break
    if not loc["location"]:
        for ld in leads:
            if ld["location"]:
                loc["location"] = ld["location"]
                break
    # Age/sex from the most recent application that has them -> lets us skip
    # re-asking the intake questions on every search.
    age, sex = "", ""
    for ld in leads:
        if not age and (ld["age"] or "").strip():
            age = ld["age"].strip()
        if not sex and ld["sex"] in ("male", "female"):
            sex = ld["sex"]
        if age and sex:
            break
    # Recent conditions for one-tap re-search (deduped, order preserved).
    recent, seen = [], set()
    for ld in leads:
        c = (ld["condition"] or "").strip()
        k = c.lower()
        if c and k not in seen:
            seen.add(k)
            recent.append(c)
        if len(recent) >= 4:
            break
    full = (p["full_name"] or "").strip()
    return {
        "location": loc["location"], "lat": loc["lat"], "lon": loc["lon"],
        "cc": loc["cc"], "age": age, "sex": sex,
        "interest": (p["primary_interest"] or "").strip(),
        "recent": recent,
        "apps_count": db.count_applications(token),
        "alerts_count": len(alerts),
        "first_name": full.split(" ")[0] if full else "",
    }


def _render_landing():
    condition_options = _merge_terms(
        trending_conditions(12), SEARCH_CONDITION_OPTIONS, 30)
    condition_prefill = request.args.get("condition", "").strip()
    location_prefill = request.args.get("location", "").strip()
    patient_ctx = _patient_search_ctx()
    # For a signed-in patient with no query-string prefill, fall back to their
    # own saved location so the field is filled instead of blank.
    if patient_ctx and not location_prefill:
        location_prefill = patient_ctx["location"]
    return render_template("landing.html", vertical=VERTICAL,
                           conditions=trending_conditions(8),
                           drugs=trending_drugs(6), slugify=slugify,
                           condition_options=condition_options,
                           drug_options=SEED_DRUGS,
                           condition_value=condition_prefill,
                           location_value=location_prefill,
                           patient_ctx=patient_ctx,
                           home_stats=HOME_STATS,
                           landing_page=True)


# --------------------------------------------------------------------------- #
# Search result cache. A search is expensive (CT.gov fetch + per-trial LLM), so
# we keep the ranked results keyed by a short id. That lets each trial open on
# its own detail page (like an Airbnb listing) without re-running the search or
# losing the eligibility read computed at search time.
#
# The cache is persisted in SQLite (db.save_search / db.get_search) so it is
# shared across gunicorn workers - otherwise a detail request routed to a
# different worker than the one that ran the search wouldn't find the results
# and would wrongly report "that trial result expired". A small in-process
# OrderedDict is kept as an L1 fast path in front of the DB.
# --------------------------------------------------------------------------- #
_SEARCH_CACHE = OrderedDict()
_SEARCH_CACHE_MAX = 80
_SEARCH_QUERY_CACHE = OrderedDict()   # query-key -> {"sid": str, "ts": float}
_SEARCH_QUERY_CACHE_MAX = 250
_SEARCH_QUERY_TTL_SECONDS = int(os.environ.get("SEARCH_QUERY_CACHE_TTL", "900"))

# Trial-specific pre-screen questions are generated once per NCT (LLM) and cached
# so repeat views of the same trial don't re-pay for generation.
_PRESCREEN_CACHE = OrderedDict()      # nct -> {"questions": list, "ts": float}
_PRESCREEN_CACHE_MAX = 500
_PRESCREEN_TTL_SECONDS = int(os.environ.get("PRESCREEN_CACHE_TTL", "86400"))


def _prescreen_for_trial(trial):
    """Return cached/generated patient-answerable pre-screen questions for a
    trial. Never raises: on any failure returns [] so the apply form falls back
    to the generic screener."""
    nct = (trial or {}).get("nctId") or ""
    if not nct or not mt.LLM_API_KEY:
        return []
    hit = _PRESCREEN_CACHE.get(nct)
    if hit and (time.time() - float(hit.get("ts") or 0)) <= _PRESCREEN_TTL_SECONDS:
        _PRESCREEN_CACHE.move_to_end(nct)
        return hit["questions"]
    try:
        questions = mt.prescreen_questions(trial)
    except Exception:
        app.logger.exception("prescreen question generation failed")
        questions = []
    _PRESCREEN_CACHE[nct] = {"questions": questions, "ts": time.time()}
    _PRESCREEN_CACHE.move_to_end(nct)
    while len(_PRESCREEN_CACHE) > _PRESCREEN_CACHE_MAX:
        _PRESCREEN_CACHE.popitem(last=False)
    return questions


def _prescreen_cached(trial):
    """Non-blocking peek at the pre-screen cache. Returns the cached questions
    when they've already been generated for this trial, else None. Used to keep
    the trial detail page fast: we never trigger the (slow) LLM call on the
    render path - that happens asynchronously via `trial_prescreen`."""
    nct = (trial or {}).get("nctId") or ""
    if not nct:
        return None
    hit = _PRESCREEN_CACHE.get(nct)
    if hit and (time.time() - float(hit.get("ts") or 0)) <= _PRESCREEN_TTL_SECONDS:
        _PRESCREEN_CACHE.move_to_end(nct)
        return hit["questions"]
    return None


def _cache_search(results, ctx):
    sid = secrets.token_urlsafe(9)
    entry = {"results": results, "ctx": ctx, "ts": time.time()}
    _SEARCH_CACHE[sid] = entry
    while len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX:
        _SEARCH_CACHE.popitem(last=False)
    try:
        db.save_search(sid, json.dumps({"results": results, "ctx": ctx}))
    except Exception:
        app.logger.exception("search cache persist failed")
    return sid


def _cache_query_key(ctx):
    """Stable key for a patient search input (for fast repeat-query hits)."""
    payload = json.dumps({
        "condition": (ctx.get("q_condition") or "").strip().lower(),
        "intervention": (ctx.get("q_intervention") or "").strip().lower(),
        "location": (ctx.get("location") or "").strip().lower(),
        "age": (ctx.get("q_age") or "").strip(),
        "sex": (ctx.get("q_sex") or "").strip().lower(),
        "about": (ctx.get("q_about") or "").strip().lower(),
        "radius": int(ctx.get("q_radius") or 0),
        "lat": (ctx.get("q_lat") or "").strip(),
        "lon": (ctx.get("q_lon") or "").strip(),
        "cc": (ctx.get("q_cc") or "").strip().upper(),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _remember_query_sid(key, sid):
    _SEARCH_QUERY_CACHE[key] = {"sid": sid, "ts": time.time()}
    while len(_SEARCH_QUERY_CACHE) > _SEARCH_QUERY_CACHE_MAX:
        _SEARCH_QUERY_CACHE.popitem(last=False)


def _lookup_query_sid(key):
    hit = _SEARCH_QUERY_CACHE.get(key)
    if not hit:
        return ""
    if (time.time() - float(hit.get("ts") or 0)) > _SEARCH_QUERY_TTL_SECONDS:
        _SEARCH_QUERY_CACHE.pop(key, None)
        return ""
    sid = hit.get("sid") or ""
    if not sid or not _load_search(sid):
        _SEARCH_QUERY_CACHE.pop(key, None)
        return ""
    return sid


def _load_search(sid):
    """Return the cached search entry ({'results', 'ctx'}) for an id, checking
    the in-process cache first and falling back to the shared SQLite store."""
    entry = _SEARCH_CACHE.get(sid)
    if entry:
        return entry
    try:
        payload = db.get_search(sid)
    except Exception:
        app.logger.exception("search cache read failed")
        payload = None
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    entry = {"results": data.get("results") or [],
             "ctx": data.get("ctx") or {}, "ts": time.time()}
    _SEARCH_CACHE[sid] = entry   # warm the L1 cache for subsequent hits
    while len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX:
        _SEARCH_CACHE.popitem(last=False)
    return entry


def _get_cached_trial(sid, nct):
    """Return (result_dict, ctx) for one trial in a cached search, or (None, ctx)."""
    entry = _load_search(sid)
    if not entry:
        return None, None
    for r in entry["results"]:
        if (r.get("trial") or {}).get("nctId") == nct:
            return r, entry["ctx"]
    return None, entry["ctx"]


def _render_cached_results(search_id):
    """Render one cached patient result set by id (GET-safe, no resubmission)."""
    entry = _load_search(search_id)
    if not entry:
        flash("That search expired - please run it again.", "error")
        return redirect(url_for("home"))
    ctx = {
        "condition": "",
        "location": "",
        "unit": "km",
        "q_condition": "",
        "q_intervention": "",
        "q_age": "",
        "q_sex": "",
        "q_about": "",
        "q_radius": 50,
        "q_lat": "",
        "q_lon": "",
        "q_cc": "",
    }
    ctx.update(entry.get("ctx") or {})
    results = entry.get("results") or []
    applied = db.applied_ncts(get_applicant_token())
    return render_template("patient_results.html", results=results,
                           search_id=search_id, applied=applied, **ctx)


def _finish_find_redirect(search_id, nct=""):
    """Send the searcher to their results - or straight into a specific trial's
    detail page when a deep-open NCT was requested (an SEO trial card) and it's in
    the result set. Falls back to the results list if that trial isn't matched."""
    if nct:
        r_open, _ = _get_cached_trial(search_id, nct)
        if r_open:
            return redirect(url_for("trial_detail", search_id=search_id, nct=nct))
    return redirect(url_for("find_results", search_id=search_id))


@app.route("/find", methods=["GET", "POST"])
def find():
    """Public, no-login patient search. The search form lives on the homepage;
    this endpoint handles the POST and renders patient-friendly results. A GET
    just bounces back to the homepage (carrying any prefill)."""
    if request.method == "GET":
        if g.user and not g.patient_user:
            return redirect(url_for("dashboard"))
        return _render_landing()

    blocked = _guard_ip_rate_limit("find")
    if blocked is not None:
        return redirect(url_for("home"))

    condition = request.form.get("condition", "").strip()
    intervention = request.form.get("intervention", "").strip()
    # A trending drug chip fills the visible condition box (for feedback) AND the
    # hidden intervention field with the same drug. Treat that as a drug-only
    # search so we don't also filter by condition=<drug>, which over-narrows to
    # near-zero results.
    if intervention and condition.lower() == intervention.lower():
        condition = ""
    location = request.form.get("location", "").strip()
    age = request.form.get("age", "").strip()
    sex = request.form.get("sex", "").strip()
    about = request.form.get("about", "").strip()
    pregnant = request.form.get("pregnant", "").strip()
    other_trial = request.form.get("other_trial", "").strip()
    freeform = request.form.get("freeform", "") == "1"
    # Optional deep-open target: SEO trial cards POST the study's NCT so we can
    # drop the searcher directly on that trial's detail page after the search.
    nct = request.form.get("nct", "").strip()
    # Drug/peptide search: if the typed query is a known drug (e.g. "reta" ->
    # Retatrutide, "ozempic" -> Semaglutide), search CT.gov by intervention so we
    # return that drug's actual trials, instead of the freeform LLM collapsing it
    # to a broad indication like "obesity" (many of which aren't that drug).
    if not intervention:
        drug = _detect_drug_query(condition) or _detect_drug_query(about)
        if drug:
            intervention = drug
            condition = ""      # searched by drug, not a guessed condition
            about = ""          # the typed term was the drug, not a description
            freeform = False
    # Free-text ("describe it in your own words") mode: the box holds a sentence,
    # not a condition term. Route it to the note and let the matcher extract the
    # condition (LLM) instead of querying CT.gov with a whole sentence.
    if freeform and condition:
        about = (condition + ("\n" + about if about else "")).strip()
        condition = ""

    condition_terms = [x.strip() for x in condition.split(",") if x.strip()]
    condition_query = condition_terms[0] if condition_terms else condition
    condition_label = ", ".join(condition_terms) if condition_terms else condition
    try:
        radius = int(request.form.get("radius", "50") or 0)
    except ValueError:
        radius = 50

    label = condition_label or intervention
    if not label and not about:
        flash("Tell us the condition, or describe what you're looking for.",
              "error")
        return redirect(url_for("home", location=location))
    if not location:
        flash("Enter your city or postal code so we only show trials near you.",
              "error")
        return redirect(url_for("home", condition=condition_label))

    coords, unit = None, "km"
    lat_in = request.form.get("lat", "").strip()
    lon_in = request.form.get("lon", "").strip()
    cc_in = request.form.get("cc", "").strip()
    if lat_in and lon_in:
        try:
            coords = (float(lat_in), float(lon_in))
            unit = units_for(request.form.get("cc", ""))
        except ValueError:
            coords = None
    if coords is None:
        try:
            geo = geocode(location)
        except Exception:
            geo = None
        if geo:
            coords = (geo[0], geo[1])
            unit = units_for(geo[2])

    note = build_patient_note(label, age, sex, about, pregnant, other_trial)
    # Fast path: identical recent search -> reuse cached result set instantly.
    candidate_ctx = {
        "condition": label, "location": location, "unit": unit,
        "q_condition": condition_label, "q_intervention": intervention,
        "q_age": age, "q_sex": sex, "q_about": about, "q_radius": radius,
        "q_pregnant": pregnant, "q_other_trial": other_trial,
        "q_lat": lat_in, "q_lon": lon_in, "q_cc": cc_in,
    }
    qkey = _cache_query_key(candidate_ctx)
    cached_sid = _lookup_query_sid(qkey)
    if cached_sid:
        _log_event("search", {"q": label, "cached": 1})
        return _finish_find_redirect(cached_sid, nct)
    try:
        detected, results = run_search(note, condition_query, "", False, coords,
                                       radius, unit, interventional_only=True,
                                       intervention=intervention)
    except RuntimeError as e:
        flash(str(e), "error")
        return redirect(url_for("home", condition=condition_label, location=location))
    except Exception:
        app.logger.exception("public find failed")
        flash("Search failed unexpectedly. Please try again.", "error")
        return redirect(url_for("home", condition=condition_label, location=location))

    # Free-text search: adopt the condition the matcher extracted so results,
    # caching, and analytics have a real label instead of a raw sentence.
    if not label:
        label = (detected or "").strip() or "your search"

    # Record the search so "trending" reflects real site traffic. Drug-name
    # queries go through the intervention field; everything else is a condition.
    try:
        if intervention:
            db.log_search_term(intervention, "drug")
        for t in condition_terms[:6]:
            db.log_search_term(t, "condition")
        if condition and not condition_terms:
            db.log_search_term(condition, "condition")
        if freeform and detected:
            db.log_search_term(detected, "condition")
    except Exception:
        app.logger.exception("search stat logging failed")

    _log_event("search", {"q": label, "results": len(results),
                          "intervention": 1 if intervention else 0})

    ctx = {"condition": label, "location": location, "unit": unit,
           "q_condition": condition_label or (label if freeform else ""),
           "q_intervention": intervention,
           "q_age": age, "q_sex": sex, "q_about": about, "q_radius": radius,
           "q_lat": lat_in, "q_lon": lon_in, "q_cc": cc_in}
    search_id = _cache_search(results, ctx)
    _remember_query_sid(qkey, search_id)
    return _finish_find_redirect(search_id, nct)


@app.route("/find/<search_id>")
def find_results(search_id):
    """GET endpoint for one cached result set (PRG target from POST /find)."""
    return _render_cached_results(search_id)


@app.route("/trial/<search_id>/<nct>")
def trial_detail(search_id, nct):
    """Airbnb-style listing page for one trial: full info on the left, an apply
    panel on the right. Reads from the cached search so the eligibility breakdown
    matches what the patient saw in the results list."""
    r, ctx = _get_cached_trial(search_id, nct)
    if not r:
        flash("That trial result expired - please run your search again.", "error")
        return redirect(url_for("find"))
    _log_event("trial_view", {"nct": nct})
    applied = db.applied_ncts(get_applicant_token())
    summary = summarize.plain(r["trial"])
    # Never block the page render on the (slow) LLM pre-screen call. If the
    # questions are already cached we render them inline; otherwise the page
    # ships instantly with the generic screener and JS upgrades it via the
    # `trial_prescreen` endpoint below.
    prescreen = _prescreen_cached(r["trial"]) or []
    prescreen_ai = bool(mt.LLM_API_KEY) and bool((r["trial"] or {}).get("criteria"))
    plain_terms = summarize.plain_terms(r["trial"])
    return render_template("trial_detail.html", r=r, search_id=search_id,
                           applied=applied, summary=summary, plain_terms=plain_terms,
                           prescreen=prescreen, prescreen_ai=prescreen_ai, **ctx)


@app.route("/trial/<search_id>/<nct>/prescreen.json")
def trial_prescreen(search_id, nct):
    """Async source for the AI-tailored pre-screen questions. Kept off the
    detail-page render path so first views stay fast; the LLM call (and its
    caching) happens here instead."""
    r, _ = _get_cached_trial(search_id, nct)
    if not r:
        return jsonify({"questions": []}), 404
    try:
        questions = _prescreen_for_trial(r["trial"])
    except Exception:
        app.logger.exception("async prescreen generation failed")
        questions = []
    return jsonify({"questions": questions})


@app.route("/interest", methods=["POST"])
def interest():
    """Patient asks to be contacted about a specific trial -> consented lead."""
    blocked = _guard_ip_rate_limit("interest")
    if blocked:
        return blocked
    # Apply-first flow: the form verifies the visitor's email inline (which signs
    # them into a passwordless account), so g.patient_user is normally set here.
    # This stays as a fallback for a stale tab or a bot posting without verifying.
    if not g.patient_user:
        flash("Verify your email on the form to apply.", "error")
        return redirect(url_for("patient_login", next=_safe_next(request.referrer) or request.path))
    f = request.form
    if not f.get("consent"):
        flash("Please check the consent box so a coordinator can contact you.",
              "error")
        return redirect(request.referrer or url_for("find"))
    name = (f.get("name", "").strip() or g.patient_user["full_name"] or "").strip()
    email = (f.get("email", "").strip() or g.patient_user["email"] or "").strip()
    contact = (email or f.get("phone", "").strip())
    if not name or not contact:
        flash("Add your name and an email or phone so the site can reach you.",
              "error")
        return redirect(request.referrer or url_for("find"))
    applicant = g.patient_user["applicant_token"]

    # Short screener: quick gate questions the patient can answer about
    # themselves. Universal logistics/consent gates use fixed keys (labelled via
    # SCREENER_LABELS); trial-specific questions (generated from the study's
    # eligibility criteria) arrive as sq_i (question text) / sa_i (answer) /
    # sf_i (the answer that is a concern) and are stored keyed by their text.
    screener = {}
    for q in ("travel", "other_trial", "pregnancy", "consent_capable"):
        v = f.get(q, "").strip()
        if v:
            screener[q] = v
    flagged = []
    dyn_idx = sorted({
        int(k[3:]) for k in f.keys()
        if k.startswith("sq_") and k[3:].isdigit()
    })
    for i in dyn_idx:
        qtext = f.get(f"sq_{i}", "").strip()
        ans = f.get(f"sa_{i}", "").strip()
        if not qtext or not ans:
            continue
        screener[qtext] = ans
        flag_if = f.get(f"sf_{i}", "").strip().lower()
        if flag_if and ans.lower() == flag_if:
            flagged.append(qtext)
    if flagged:
        screener["_flags"] = flagged

    # Carry the eligibility profile computed at search time (met/unknown/not_met)
    # so the study team gets a criteria breakdown with no extra LLM cost.
    elig_raw = f.get("eligibility", "").strip()
    try:
        elig = json.loads(elig_raw) if elig_raw else None
    except (ValueError, TypeError):
        elig = None

    age = f.get("age", "").strip()
    sex = f.get("sex", "").strip()

    # Auto-fill from records the patient already connected once. Age/sex fill any
    # blanks, the de-identified summary rides along, and connected clinical facts
    # promote "unknown" eligibility items to "met" (records.py, conservative).
    prof = db.get_records_profile(applicant)
    record_summary, records_connected = "", 0
    if prof:
        records_connected = 1
        record_summary = records_mod.summary_text(prof)
        if not age and prof.get("age") not in (None, ""):
            age = str(prof.get("age"))
        if not sex and prof.get("sex") in ("male", "female"):
            sex = prof.get("sex")
        if isinstance(elig, dict):
            elig, _ = records_mod.autofill_eligibility(prof, elig)

    # Physician-referral attribution: if the patient arrived via a doctor's invite
    # link, credit that clinician so they can see the outcome (closes their loop).
    referred_by, invite_token, source = "", "", "web"
    inv_tok = request.cookies.get(INVITE_COOKIE, "")
    if inv_tok:
        inv = db.get_invite(inv_tok)
        if inv:
            referred_by = inv["clinician_name"] or "your physician"
            invite_token = inv_tok
            source = "referral"

    # One-time pre-screen readiness estimate from the patient's own answers +
    # eligibility read. Stored so both the patient and study team see the same
    # number without repeat LLM cost. Never blocks the application if it fails.
    readiness = ""
    try:
        readiness = json.dumps(_evaluate_prescreen_readiness(
            f.get("title", "").strip(), f.get("condition", "").strip(),
            screener, elig))
    except Exception:
        app.logger.exception("readiness compute failed")

    token = db.create_lead({
        "applicant_token": applicant,
        "nct": f.get("nct", "").strip(), "title": f.get("title", "").strip(),
        "condition": f.get("condition", "").strip(),
        "location": f.get("location", "").strip(),
        "site": f.get("site", "").strip(), "name": name,
        "email": email, "phone": f.get("phone", "").strip(),
        "age": age, "sex": sex,
        "notes": f.get("about", "").strip(), "consent": 1, "source": source,
        "screener": json.dumps(screener) if screener else "",
        "eligibility": json.dumps(elig) if elig else "",
        "prescreen_readiness": readiness,
        "records_connected": records_connected, "record_summary": record_summary,
        "referred_by": referred_by, "invite_token": invite_token,
    })
    # Go-live hook: tell the site a new blinded candidate is waiting. No-op while
    # NOTIFY_LIVE is off, so nothing is emailed during testing.
    _notify_site_new_candidate(token)
    # Internal heads-up so the operator can confirm real applications are landing.
    # For BridgeMD (site-posted) studies the poster is the operator, so the site
    # already got the candidate email above - exclude that address to avoid a
    # duplicate heads-up landing in the same inbox.
    _lead_for_notify = db.get_lead_by_token(token)
    _site_to = ((db.site_contact_for_nct(_lead_for_notify["nct"]) or SITE_NOTIFY_EMAIL)
                if _lead_for_notify else "")
    _notify_owner_new_application(token, exclude_emails=[_site_to] if _site_to else None)
    # Confirm receipt to the applicant (job-application style). The in-app system
    # message always shows in their thread; the email sends only when go-live is
    # on, so both surfaces stay in sync.
    new_lead = db.get_lead_by_token(token)
    # Recruitment-campaign attribution: if the applicant arrived via a tracked
    # campaign/placement link, credit it - but ONLY if they applied to that
    # campaign's study, so an unrelated application never inflates a campaign
    # (net-throughput guardrail, see enrollment-velocity rule).
    _camp_tok = request.cookies.get(CAMPAIGN_COOKIE, "")
    if _camp_tok and new_lead:
        _tr = db.resolve_tracking_token(_camp_tok)
        if _tr:
            db.attribute_lead(new_lead["id"], _tr["campaign_id"],
                              _tr["placement_id"], only_if_nct=_tr["nct"])
    if new_lead:
        db.add_message(
            new_lead["id"], "system",
            "Thanks for applying - your application was received and sent to the "
            "study team. Someone will respond shortly, usually within a few "
            "business days. You don't need to do anything right now.")
    _notify_applicant_apply_confirmation(token)
    _log_event("apply", {"nct": f.get("nct", "").strip()})
    # Prefill an OPTIONAL "alert me about similar trials" offer on the thank-you
    # page (see thanks.html). Nothing is created unless the patient opts in.
    alert_prefill = {
        "condition": f.get("condition", "").strip(),
        "location": f.get("location", "").strip(),
        "lat": f.get("lat", "").strip(),
        "lon": f.get("lon", "").strip(),
        "cc": f.get("cc", "").strip(),
        "radius": f.get("radius", "").strip() or "50",
        "unit": f.get("unit", "km").strip() or "km",
        "email": email,
    }
    return render_template("thanks.html", title=f.get("title", ""),
                           nct=f.get("nct", ""), alert_prefill=alert_prefill)


@app.route("/how-it-works")
def how_it_works():
    return render_template("how.html", pipeline=db.LEAD_PIPELINE,
                           labels=db.LEAD_LABELS, blurb=db.LEAD_BLURB)


@app.route("/for-clinicians")
def for_clinicians():
    """B2B marketing page for research sites, clinics and sponsors: what BridgeMD
    does for study teams (pre-screened intake, enrollment funnel, compliant
    in-clinic recruitment) plus a live-demo request form. Patient-free by design
    to keep the page role-pure."""
    _log_event("view_for_clinicians")
    return render_template("for_clinicians.html", legal_contact=LEGAL_CONTACT)


@app.route("/for-clinicians/demo", methods=["POST"])
def demo_request():
    """Handle the 'book a live demo' form. Records the request (so it is never
    lost even if email delivery is off) and emails the team. Compliance: this is
    a SaaS sales lead, not a referral - no money moves to any referral source."""
    blocked = _guard_ip_rate_limit("demo_request")
    if blocked is not None:
        return redirect(url_for("for_clinicians") + "#demo")
    name = request.form.get("name", "").strip()
    org = request.form.get("org", "").strip()
    email = request.form.get("email", "").strip()
    role = request.form.get("role", "").strip()
    message = request.form.get("message", "").strip()
    if not (name and org and email):
        flash("Please add your name, organization, and work email.", "error")
        return redirect(url_for("for_clinicians") + "#demo")
    # Log first so the lead is captured even when SMTP/notifications are off.
    _log_event("demo_request", {"org": org, "role": role})
    subject = f"BridgeMD demo request - {org}"
    body = "\n".join([
        "New live-demo request from the For-clinics page:",
        "",
        f"Name:          {name}",
        f"Organization:  {org}",
        f"Work email:    {email}",
        f"Role / type:   {role or '-'}",
        "",
        "What they're recruiting for / notes:",
        message or "-",
    ])
    _notify(OWNER_NOTIFY_EMAIL, subject, body)
    flash("Thanks - we'll email you shortly to schedule your live demo.", "success")
    return redirect(url_for("for_clinicians") + "#demo")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html", legal_contact=LEGAL_CONTACT,
                           legal_updated=LEGAL_UPDATED)


@app.route("/terms")
def terms():
    return render_template("terms.html", legal_contact=LEGAL_CONTACT,
                           legal_updated=LEGAL_UPDATED)


@app.route("/applications")
def applications():
    """Patient-facing 'My applications'."""
    if not g.patient_user:
        flash("Sign in to view your applications.", "error")
        return redirect(url_for("patient_login", next=url_for("applications")))
    token = get_applicant_token()
    leads = db.list_leads_by_applicant(token)
    apps = []
    for ld in leads:
        # Parse the answers the patient submitted (drop the internal _flags key)
        # so we can show them back read-only, and the stored readiness estimate.
        try:
            scr = json.loads(ld["screener"]) if ld["screener"] else {}
        except (ValueError, TypeError):
            scr = {}
        scr.pop("_flags", None)
        answers = [{"q": SCREENER_LABELS.get(k, k), "a": v}
                   for k, v in scr.items() if str(v).strip()]
        try:
            readiness = (json.loads(ld["prescreen_readiness"])
                         if ld["prescreen_readiness"] else None)
        except (ValueError, TypeError):
            readiness = None
        apps.append({
            "lead": ld,
            "events": db.get_lead_events(ld["id"]),
            "messages": db.get_messages(ld["id"]),
            "visits": db.get_visits(ld["id"]),
            "support": db.get_lead_support(ld["id"]),
            "files": db.list_attachments(ld["id"]),
            "tasks": db.list_tasks(ld["id"]),
            "doc_requests": db.list_doc_requests(ld["id"]),
            "answers": answers,
            "readiness": readiness,
        })
        db.mark_thread_read(ld["id"], "patient")
    return render_template("applications.html", apps=apps,
                           pipeline=db.LEAD_PIPELINE, labels=db.LEAD_LABELS,
                           blurb=db.LEAD_BLURB, closed=db.LEAD_CLOSED,
                           support_coverage_labels=SUPPORT_COVERAGE_LABELS,
                           support_travel_labels=SUPPORT_TRAVEL_LABELS)


@app.route("/settings", methods=["GET", "POST"])
def patient_settings():
    """Basic, patient-editable account settings: display name, where updates go,
    whether to receive email updates, and a primary condition of interest."""
    if not g.patient_user:
        flash("Sign in to manage your settings.", "error")
        return redirect(url_for("patient_login", next=url_for("patient_settings")))
    if request.method == "POST":
        f = request.form
        full_name = f.get("full_name", "").strip()
        primary_interest = f.get("primary_interest", "").strip()
        email_alerts = bool(f.get("email_alerts"))
        # notify_email is intentionally NOT editable here: our emails must only
        # ever go to the verified account email, so a user can't redirect them
        # to someone else's inbox.
        db.update_patient_account(
            g.patient_user["id"], full_name=full_name,
            email_alerts=email_alerts, primary_interest=primary_interest)
        g.patient_user = db.get_patient_user(g.patient_user["id"])
        flash("Settings saved.", "success")
        return redirect(url_for("patient_settings"))
    return render_template("settings.html", patient=g.patient_user)


@app.route("/applications/<token>/prescreen-info", methods=["POST"])
def application_prescreen_info(token):
    """Patient optionally answers the 'what the team will confirm' questions
    upfront (e.g. MMSE score, diagnosis stage). We send it to the study team as
    a message so they can pre-screen faster - a direct hit on the contact->
    screen drop-off. All optional; only answered items are shared."""
    if not g.patient_user:
        flash("Sign in to share pre-screen details.", "error")
        return redirect(url_for("patient_login", next=url_for("applications")))
    lead = db.get_lead_by_token(token)
    if not lead or lead["applicant_token"] != get_applicant_token():
        abort(403)
    f = request.form
    idxs = sorted({int(k[3:]) for k in f.keys()
                   if k.startswith("pq_") and k[3:].isdigit()})
    lines = []
    for i in idxs:
        q = f.get(f"pq_{i}", "").strip()
        a = f.get(f"pa_{i}", "").strip()
        if q and a:
            lines.append(f"- {q} {a}")
    if not lines:
        flash("Add at least one answer to share.", "error")
        return redirect(url_for("applications") + f"#app-{lead['id']}")
    body = "Extra pre-screen details I can share:\n" + "\n".join(lines)
    db.add_message(lead["id"], "patient", body)
    _notify_site_message(lead, body)
    flash("Thanks - shared with your study team to help speed up screening.",
          "success")
    return redirect(url_for("applications") + f"#app-{lead['id']}")


@app.route("/applications/<token>/message", methods=["POST"])
def application_message(token):
    """Patient sends a message to the study team about their own application."""
    if not g.patient_user:
        flash("Sign in to message the study team.", "error")
        return redirect(url_for("patient_login", next=url_for("applications")))
    lead = db.get_lead_by_token(token)
    if not lead or lead["applicant_token"] != get_applicant_token():
        abort(403)
    is_ajax = request.headers.get("X-Requested-With") == "fetch"
    body = request.form.get("body", "").strip()
    if body:
        db.add_message(lead["id"], "patient", body)
        _notify_site_message(lead, body)
        if is_ajax:
            return jsonify({"ok": True, "sender": "patient",
                            "body": body, "created_at": db.now()})
        flash("Message sent to the study team.", "success")
    elif is_ajax:
        return jsonify({"ok": False, "error": "empty"}), 400
    return redirect(url_for("applications") + f"#app-{lead['id']}")


@app.route("/applications/<token>/attach", methods=["POST"])
def application_attach(token):
    """Patient uploads a document (e.g. a signed form) into their own thread."""
    if not g.patient_user:
        flash("Sign in to share a document.", "error")
        return redirect(url_for("patient_login", next=url_for("applications")))
    lead = db.get_lead_by_token(token)
    if not lead or lead["applicant_token"] != get_applicant_token():
        abort(403)
    saved = _save_upload(request.files.get("file"))
    if not saved:
        flash("Attach a supported file (PDF, image, doc, spreadsheet).", "error")
        return redirect(url_for("applications") + f"#app-{lead['id']}")
    orig, stored, mime, size = saved
    db.add_attachment(lead["id"], "patient", orig, stored, mime, size)
    db.add_message(lead["id"], "patient", f"Shared a document: {orig}")
    _notify_site_message(lead, f"The applicant shared a document: {orig}")
    flash("Document shared with the study team.", "success")
    return redirect(url_for("applications") + f"#app-{lead['id']}")


@app.route("/applications/<token>/doc-request/<int:req_id>/upload",
           methods=["POST"])
def application_doc_upload(token, req_id):
    """Patient uploads a record the study team specifically asked for."""
    if not g.patient_user:
        flash("Sign in to share a record.", "error")
        return redirect(url_for("patient_login", next=url_for("applications")))
    lead = db.get_lead_by_token(token)
    if not lead or lead["applicant_token"] != get_applicant_token():
        abort(403)
    req = db.get_doc_request(req_id)
    if not req or req["lead_id"] != lead["id"]:
        abort(404)
    saved = _save_upload(request.files.get("file"))
    if not saved:
        flash("Attach a supported file (PDF, image, doc, spreadsheet).", "error")
        return redirect(url_for("applications") + f"#app-{lead['id']}")
    orig, stored, mime, size = saved
    att_id = db.add_attachment(lead["id"], "patient", orig, stored, mime, size,
                               note=f"Record: {req['title']}")
    db.set_doc_request_upload(req_id, att_id)
    db.add_message(lead["id"], "patient",
                   f"Uploaded the requested record ({req['title']}): {orig}")
    _notify_site_message(lead, f"The applicant uploaded a requested record: "
                               f"{req['title']}")
    flash("Record sent to the study team.", "success")
    return redirect(url_for("applications") + f"#app-{lead['id']}")


@app.route("/applications/<token>/task/<int:task_id>", methods=["POST"])
def application_toggle_task(token, task_id):
    """Patient checks off (or reopens) one of their assigned to-dos."""
    if not g.patient_user:
        flash("Sign in to update your checklist.", "error")
        return redirect(url_for("patient_login", next=url_for("applications")))
    lead = db.get_lead_by_token(token)
    if not lead or lead["applicant_token"] != get_applicant_token():
        abort(403)
    task = db.get_task(task_id)
    if not task or task["lead_id"] != lead["id"]:
        abort(404)
    new_status = "open" if task["status"] == "done" else "done"
    db.set_task_status(task_id, new_status)
    flash("Nice - checked off your list." if new_status == "done"
          else "Reopened - back on your to-do list.", "success")
    return redirect(url_for("applications") + f"#app-{lead['id']}")


@app.route("/applications/<token>/coverage-check", methods=["POST"])
def application_coverage_check(token):
    if not g.patient_user:
        flash("Sign in to update coverage.", "error")
        return redirect(url_for("patient_login", next=url_for("applications")))
    lead = db.get_lead_by_token(token)
    if not lead or lead["applicant_token"] != get_applicant_token():
        abort(403)
    info = {
        "payer_name": request.form.get("payer_name", "").strip(),
        "member_id": request.form.get("member_id", "").strip(),
        "group_id": request.form.get("group_id", "").strip(),
        "zip": request.form.get("zip", "").strip(),
        "dob_year": request.form.get("dob_year", "").strip(),
    }
    res = payer.check(lead, info)
    db.set_lead_coverage_check(
        lead["id"], res.get("status", ""), res.get("note", ""),
        payload=res.get("payload"), provider=res.get("provider", ""),
        ref=res.get("reference", ""))
    flash("Coverage check updated.", "success")
    return redirect(url_for("applications") + f"#app-{lead['id']}")


@app.route("/applications/<token>/travel-support", methods=["POST"])
def application_travel_support(token):
    if not g.patient_user:
        flash("Sign in to update travel support.", "error")
        return redirect(url_for("patient_login", next=url_for("applications")))
    lead = db.get_lead_by_token(token)
    if not lead or lead["applicant_token"] != get_applicant_token():
        abort(403)
    info = {
        "distance": request.form.get("distance", "").strip(),
        "preferred_mode": request.form.get("preferred_mode", "").strip(),
        "needs": request.form.get("needs", "").strip(),
        "city": request.form.get("city", "").strip() or lead.get("location", ""),
    }
    res = logistics.plan(lead, info)
    db.set_lead_travel_check(
        lead["id"], res.get("status", ""), res.get("note", ""),
        payload=res.get("payload"), provider=res.get("provider", ""),
        ref=res.get("reference", ""))
    flash("Travel support plan updated.", "success")
    return redirect(url_for("applications") + f"#app-{lead['id']}")


@app.route("/applications/withdraw/<token>", methods=["POST"])
def withdraw_application(token):
    if not g.patient_user:
        flash("Sign in to manage your applications.", "error")
        return redirect(url_for("patient_login", next=url_for("applications")))
    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("Please add a short reason so we can withdraw this application.", "error")
        return redirect(url_for("applications") + f"#app-{token}")
    if db.withdraw_lead(token, get_applicant_token(), reason):
        flash("Application withdrawn.", "success")
    else:
        flash("Couldn't withdraw that application.", "error")
    return redirect(url_for("applications"))


# --------------------------------------------------------------------------- #
# REDCap screening handoff (patient completes the site's intake/screening form)
# --------------------------------------------------------------------------- #
def _redcap_cfg_for_lead(lead):
    """Resolve the REDCap config of the site that owns this lead's study."""
    prof = db.get_site_profile_for_nct(lead["nct"]) if lead and lead["nct"] else None
    return redcap.config_from_profile(prof)


def _url_host(url):
    """Bare host (e.g. redcap.institution.edu) for display in the embed chrome."""
    try:
        return urllib.parse.urlparse(url or "").netloc or ""
    except Exception:
        return ""


def _mark_screening_complete(lead, actor="patient",
                             note="screening intake form completed"):
    """Move the lead into a screening-complete state and notify both sides."""
    cur = lead["status"]
    target = "screening"
    order = db.LEAD_PIPELINE
    if cur in db.LEAD_CLOSED or (
            cur in order and order.index(cur) > order.index("screening")):
        target = cur  # never move backward or reopen a closed application
    db.update_lead_status(lead["id"], target, note, actor=actor)
    db.set_lead_redcap(lead["id"], survey_status="complete")
    db.add_message(
        lead["id"], "system",
        "Screening intake form completed. The study team will review your "
        "responses and follow up with next steps.")


def _send_intake_to_lead(lead):
    """Coordinator pushes the (pre-filled) screening intake form to a patient.

    Moves the lead into the screening stage so the form becomes available, flags
    it sent, and drops a note in their thread. The patient opens the same
    pre-filled, embedded screening handoff - we don't duplicate any form logic.
    Returns True if sent, False if the application is closed.
    """
    if not lead or lead["status"] in db.LEAD_CLOSED:
        return False
    order = db.LEAD_PIPELINE
    cur = lead["status"]
    target = cur
    if cur in order and order.index(cur) < order.index("screening"):
        target = "screening"  # never move a further-along candidate backward
    db.update_lead_status(lead["id"], target, "screening intake form sent",
                          actor="you")
    db.set_lead_redcap(lead["id"], survey_status="sent")
    db.add_message(
        lead["id"], "site",
        "Sent you the screening intake form - it's pre-filled from what you "
        "already shared. Open \u201cComplete screening form,\u201d review, and submit.")
    return True


@app.route("/app/leads/<int:lead_id>/send-intake", methods=["POST"])
@login_required
def send_lead_intake(lead_id):
    """Send one patient their pre-filled screening intake form."""
    _ensure_site_access_for_lead(lead_id)
    back = _lead_action_return()
    lead = db.get_lead(lead_id)
    if not lead:
        flash("Couldn't find that candidate.", "error")
        return redirect(back)
    if _send_intake_to_lead(lead):
        flash(f"Intake form sent to {lead['name'] or candidate_code(lead)}.",
              "success")
    else:
        flash("That candidate's application is closed.", "error")
    return redirect(back)


@app.route("/app/trial/<nct>/send-intake", methods=["POST"])
@login_required
def send_trial_intake(nct):
    """Send every accepted patient in one trial their own pre-filled intake form."""
    if nct not in db.user_claimed_ncts(g.user["id"]):
        abort(403)
    back = (_safe_next(request.form.get("next", ""))
            or url_for("patient_inbox", nct=nct, team=1))
    leads = [ld for ld in db.list_leads_for_user(g.user["id"])
             if ld["revealed"] and ld["status"] not in db.LEAD_CLOSED
             and ld["nct"] == nct]
    n = sum(1 for ld in leads if _send_intake_to_lead(ld))
    if n:
        flash(f"Intake form sent to {n} patient{'s' if n != 1 else ''}.", "success")
    else:
        flash("No accepted patients to send the intake form to yet.", "error")
    return redirect(back)


@app.route("/applications/<token>/screening")
def application_screening(token):
    """Patient-facing 'Complete your screening form' handoff.

    LIVE path (only when the site connected REDCap AND attested the instrument
    is IRB/REB-approved with patient consent): mint a pre-filled unique survey
    link and hand off / embed it. Otherwise show a demo-safe simulated form so
    the flow is always walkable."""
    if not g.patient_user:
        flash("Sign in to complete your screening form.", "error")
        return redirect(url_for("patient_login", next=url_for("applications")))
    lead = db.get_lead_by_token(token)
    if not lead or lead["applicant_token"] != get_applicant_token():
        abort(403)
    cfg = _redcap_cfg_for_lead(lead)
    # Keep demos fully simulated - never call a real REDCap during a demo -
    # unless REDCAP_LIVE=1 is set, which opts a local/staging run into the real
    # integration path even while the no-login preview shell is on (used to test
    # a live REDCap, incl. the mock_redcap.py server, end to end).
    _redcap_live = os.environ.get("REDCAP_LIVE", "0") == "1"
    if cfg.intake_live and (_redcap_live or not _demo_mode_enabled()):
        ok, url, record_id, msg = redcap.survey_link_for_lead(cfg, lead)
        if ok:
            db.set_lead_redcap(lead["id"], record_id=record_id,
                               survey_status="sent")
            db.update_lead_status(lead["id"], lead["status"],
                                  "screening form link generated", actor="you")
            return render_template(
                "screening_form.html", lead=lead, survey_url=url,
                simulated=False, project_label=cfg.project_label,
                redcap_host=_url_host(url))
        flash(msg, "error")
        return redirect(url_for("applications"))
    # Simulated: show a realistic host in the branded chrome (from the site's
    # configured endpoint if any) without calling a real REDCap.
    sim_host = _url_host(cfg.api_url) or "redcap.your-institution.edu"
    return render_template(
        "screening_form.html", lead=lead, survey_url="", simulated=True,
        project_label=cfg.project_label or "Site intake project",
        redcap_host=sim_host, instruments=redcap.SIMULATED_INSTRUMENTS)


@app.route("/applications/<token>/screening/complete", methods=["POST"])
def application_screening_complete(token):
    """Simulated-form submission (demo / not-yet-live): mark screening done."""
    if not g.patient_user:
        flash("Sign in to complete your screening form.", "error")
        return redirect(url_for("patient_login", next=url_for("applications")))
    lead = db.get_lead_by_token(token)
    if not lead or lead["applicant_token"] != get_applicant_token():
        abort(403)
    _mark_screening_complete(lead, actor="patient")
    flash("Thanks - your screening form is complete. The study team will be in "
          "touch.", "success")
    return redirect(url_for("applications") + f"#app-{lead['id']}")


@app.route("/integrations/redcap/webhook", methods=["POST"])
def redcap_webhook():
    """Receive a REDCap survey/Data-Entry-Trigger callback and mark the matching
    lead screening-complete. Requires REDCAP_WEBHOOK_SECRET (passed as an
    X-Redcap-Token header or ?secret= query param); the endpoint is disabled
    (fails closed) until a secret is configured so it can't be triggered by
    anonymous callers to advance arbitrary patients."""
    if not redcap.WEBHOOK_SECRET:
        abort(403)
    provided = (request.headers.get("X-Redcap-Token", "")
                or request.args.get("secret", ""))
    if not (provided and hmac.compare_digest(provided, redcap.WEBHOOK_SECRET)):
        abort(403)
    record = (request.form.get("record", "").strip()
              or request.args.get("record", "").strip())
    lead = db.find_lead_by_redcap_record(record) if record else None
    if not lead:
        return app.response_class("ignored", mimetype="text/plain")
    instrument = request.form.get("instrument", "").strip()
    complete = True
    if instrument:
        val = request.form.get(f"{instrument}_complete", "").strip()
        if val and val != "2":  # "2" == Complete in REDCap
            complete = False
    if complete:
        _mark_screening_complete(lead, actor="redcap",
                                 note="screening form completed (REDCap)")
    return app.response_class("ok", mimetype="text/plain")


@app.route("/integrations/inbound-email", methods=["POST"])
def inbound_email_webhook():
    """Receive one inbound applicant email from the mail provider (SendGrid
    Inbound Parse / Postmark / Mailgun) and route it into the matching trial's
    ATS queue. Requires INTAKE_WEBHOOK_SECRET (X-Intake-Token header or ?secret=);
    fails closed until a secret is set so anonymous callers can't inject leads.

    Accepts either JSON or form-encoded payloads. This carries PHI: a BAA with the
    provider is required in production (see matcher/COMPLIANCE.md)."""
    secret = os.environ.get("INTAKE_WEBHOOK_SECRET", "")
    if not secret:
        abort(403)
    provided = (request.headers.get("X-Intake-Token", "")
                or request.args.get("secret", ""))
    if not (provided and hmac.compare_digest(provided, secret)):
        abort(403)
    payload = {}
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    if not payload:
        payload = {k: v for k, v in request.form.items()}
    result = intake_mod.handle_inbound_email(payload)
    # Always 200 on a well-formed but unroutable message so the provider doesn't
    # retry forever; 200 with ok=false tells us it was dropped on purpose.
    return jsonify(result), 200


@app.route("/app/leads/<int:lead_id>/redcap/refresh", methods=["POST"])
@login_required
def refresh_lead_redcap(lead_id):
    """Site-side poll: check whether a lead's REDCap screening form is complete
    (an alternative to the webhook for projects that can't call out)."""
    _ensure_site_access_for_lead(lead_id)
    lead = db.get_lead(lead_id)
    if not lead:
        flash("Couldn't find that candidate.", "error")
        return redirect(url_for("leads"))
    cfg = redcap.config_from_profile(db.get_site_profile(g.user["id"]))
    ok, done, msg = redcap.record_complete(cfg, lead["redcap_record_id"])
    if not ok:
        flash(msg, "error")
    elif done:
        _mark_screening_complete(lead, actor="you",
                                 note="screening form completed (REDCap)")
        flash("Screening form is complete for this candidate.", "success")
    else:
        flash("Screening form not completed yet.", "success")
    return redirect(url_for("leads"))


@app.route("/records/connect", methods=["POST"])
def records_connect():
    """Entry point from forms/buttons: send patient to authorization step first."""
    if not RECORDS_UI:
        abort(404)
    if not g.patient_user:
        flash("Sign in to connect health records.", "error")
        return redirect(url_for("patient_login", next=_safe_next(request.referrer) or url_for("applications")))
    nxt = _safe_next(request.form.get("next", "") or request.args.get("next", ""))
    if not nxt:
        nxt = _safe_next(request.referrer) or url_for("applications")
    blocked = records_mod.live_blocked_reason()
    if blocked:
        flash(blocked, "error")
    return redirect(url_for("records_authorize", next=nxt))


def _finalize_records_connect(redirect_to):
    """Run connection/sync and persist profile/state; returns a redirect response."""
    applicant = g.patient_user["applicant_token"]
    try:
        prof = records_mod.connect(g.patient_user, applicant)
    except fhir.FhirError as e:
        flash(str(e), "error")
        return redirect(redirect_to)
    except Exception:
        app.logger.exception("records connect failed")
        flash("Couldn't connect your records right now. Please try again.",
              "error")
        return redirect(redirect_to)

    provider = (prof.get("provider") or records_mod.provider_label()).strip()
    sync_status = (prof.get("sync_status") or "").strip().lower()
    if sync_status == "syncing":
        db.set_records_sync_state(
            applicant_token=applicant,
            provider=provider,
            sync_status="syncing",
            source_status=prof.get("source_status", ""),
            external_patient_id=prof.get("external_patient_id", ""),
            external_query_id=prof.get("external_query_id", ""),
            error_msg="",
            allow_regress=True,
        )
        msg = (f"Connected to {provider}. Your records are syncing now - "
               "this can take a few minutes. We'll auto-fill your applications "
               "as soon as data is ready.")
        flash(msg, "success")
        return redirect(redirect_to)

    db.set_records_profile(applicant, prof)
    n = db.attach_records_to_open_leads(applicant, records_mod.summary_text(prof))
    msg = f"Health records connected via {provider}. "
    msg += (f"Auto-filled {n} pending application(s) - "
            if n else "New applications will auto-fill from your history - ")
    msg += "you won't have to re-enter your medical details."
    flash(msg, "success")
    return redirect(redirect_to)


@app.route("/records/authorize", methods=["GET", "POST"])
def records_authorize():
    """Patient-facing consent screen that mirrors real authorization flow."""
    if not RECORDS_UI:
        abort(404)
    if not g.patient_user:
        flash("Sign in to connect health records.", "error")
        return redirect(url_for("patient_login", next=url_for("applications")))
    nxt = _safe_next(request.form.get("next", "") or request.args.get("next", ""))
    if not nxt:
        nxt = _safe_next(request.referrer) or url_for("applications")

    if request.method == "POST":
        if not request.form.get("consent_records"):
            flash("Please confirm consent to continue.", "error")
            return render_template(
                "records_authorize.html",
                records_provider=records_mod.provider_label(),
                records_live=records_mod.is_live(),
                records_live_blocked=records_mod.live_blocked_reason(),
                next_url=nxt,
            )
        return _finalize_records_connect(nxt)

    return render_template(
        "records_authorize.html",
        records_provider=records_mod.provider_label(),
        records_live=records_mod.is_live(),
        records_live_blocked=records_mod.live_blocked_reason(),
        next_url=nxt,
    )


@app.route("/records/refresh", methods=["POST"])
def records_refresh():
    if not RECORDS_UI:
        abort(404)
    if not g.patient_user:
        flash("Sign in to refresh records.", "error")
        return redirect(url_for("patient_login", next=url_for("applications")))
    applicant = g.patient_user["applicant_token"]
    if not records_mod.is_live():
        flash("Preview records are already up to date in this environment.", "success")
        return redirect(request.referrer or url_for("applications"))
    prof = db.get_records_profile(applicant) or {}
    ext_pid = (prof.get("external_patient_id") or "").strip()
    if not ext_pid:
        flash("Connect records first.", "error")
        return redirect(request.referrer or url_for("applications"))
    try:
        qid = records_mod.refresh(ext_pid)
    except fhir.FhirError as e:
        flash(str(e), "error")
        return redirect(request.referrer or url_for("applications"))
    except Exception:
        app.logger.exception("records refresh failed")
        flash("Couldn't refresh records right now. Please try again.", "error")
        return redirect(request.referrer or url_for("applications"))
    db.set_records_sync_state(
        applicant_token=applicant,
        provider=prof.get("provider", ""),
        sync_status="syncing",
        source_status="network_query_started",
        external_patient_id=ext_pid,
        external_query_id=qid,
        error_msg="",
        allow_regress=True,
    )
    flash("Refreshing your records now. We'll update your applications when ready.",
          "success")
    return redirect(request.referrer or url_for("applications"))


@app.route("/records/disconnect", methods=["POST"])
def records_disconnect():
    if not RECORDS_UI:
        abort(404)
    if not g.patient_user:
        flash("Sign in to manage records.", "error")
        return redirect(url_for("patient_login", next=url_for("applications")))
    applicant = get_applicant_token()
    if applicant:
        db.clear_records_profile(applicant)
    flash("Disconnected your health records.", "success")
    return redirect(request.referrer or url_for("applications"))


# --------------------------------------------------------------------------- #
# Trial alerts (saved searches -> push notifications). See alerts.py.
# --------------------------------------------------------------------------- #
@app.route("/alerts")
def alerts():
    """Patient-facing 'My alerts': saved interests + any new matching trials."""
    if not g.patient_user:
        flash("Sign in to manage alerts.", "error")
        return redirect(url_for("patient_login", next=url_for("alerts")))
    token = get_applicant_token()
    items = []
    for a in db.list_alerts(token):
        # Curate stored matches with the same relevance/quality gate (and
        # Strong-fit-only toggle) the email digest uses, so the on-page list
        # reads like tight search results instead of the raw CT.gov dump.
        raw = db.get_alert_matches(a["id"], limit=60)
        items.append({"alert": a,
                      "matches": alerts_mod.curate_for_display(a, raw)})
    # Viewing clears the "new" badge in the nav.
    if token:
        db.clear_new_flags(token)
    # Same condition suggestion list the hero search uses, so the "new alert"
    # form gets identical typeahead results.
    condition_options = _merge_terms(
        trending_conditions(12), SEARCH_CONDITION_OPTIONS, 30)
    return render_template("alerts.html", items=items,
                           condition_options=condition_options)


@app.route("/alerts/create", methods=["POST"])
def alerts_create():
    if not g.patient_user:
        flash("Sign in to save alerts.", "error")
        return redirect(url_for("patient_login", next=_safe_next(request.referrer) or url_for("alerts")))
    """Save an interest. Works from the results CTA (carries the current search)
    or the alerts page form. Baselines immediately so only future trials alert."""
    f = request.form
    condition = f.get("condition", "").strip()
    intervention = f.get("intervention", "").strip()
    # Signed-in patients: always notify their account email (or the notify_email
    # they chose in Settings). Never ask for it in the form.
    email = (g.patient_user["notify_email"] or g.patient_user["email"] or "").strip()
    if not (condition or intervention):
        flash("Tell us a condition or treatment to watch for.", "error")
        return redirect(request.referrer or url_for("alerts"))

    applicant = g.patient_user["applicant_token"]

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # Strong-fit-only defaults ON (quiet, high-signal). Only off if explicitly
    # unchecked in a form that includes the toggle.
    strong_only = True
    if "strong_only" in f:
        strong_only = bool(f.get("strong_only"))
    alert_id = db.create_alert({
        "applicant_token": applicant,
        "label": f.get("label", "").strip() or condition or intervention,
        "condition": condition, "intervention": intervention,
        "location": f.get("location", "").strip(),
        "lat": _f(f.get("lat")), "lon": _f(f.get("lon")),
        "cc": f.get("cc", "").strip(), "radius": f.get("radius", "50").strip() or 50,
        "unit": f.get("unit", "km").strip() or "km", "email": email,
        "strong_only": strong_only,
    })
    # Baseline current matches so the patient isn't spammed with the backlog.
    try:
        alerts_mod.seed_baseline(alert_id)
    except Exception:
        app.logger.exception("alert baseline failed")
    flash("Alert on. We'll only email you about new, strong-fit trials near you "
          "- tune the radius or turn it off anytime below.", "success")
    return redirect(url_for("alerts"))


@app.route("/alerts/<int:alert_id>/update", methods=["POST"])
def alerts_update(alert_id):
    """Tune an alert's quality controls: distance radius/unit and whether to
    send only strong-fit matches. Keeps alerts high-signal and near the patient."""
    if not g.patient_user:
        flash("Sign in to manage alerts.", "error")
        return redirect(url_for("patient_login", next=url_for("alerts")))
    f = request.form
    try:
        radius = int(f.get("radius", "").strip() or 50)
    except ValueError:
        radius = 50
    unit = f.get("unit", "km").strip() or "km"
    strong_only = bool(f.get("strong_only"))
    ok = db.update_alert_prefs(alert_id, get_applicant_token(),
                               radius=radius, unit=unit, strong_only=strong_only)
    flash("Alert updated." if ok else "Couldn't update that alert.",
          "success" if ok else "error")
    return redirect(url_for("alerts"))


@app.route("/alerts/<int:alert_id>/preview")
def alerts_preview(alert_id):
    """Return the trials this alert WOULD email today (current strong-fit
    matches), so the patient can see the quality before trusting it."""
    if not g.patient_user:
        return jsonify({"ok": False, "error": "auth"}), 403
    alert = db.get_alert(alert_id)
    if not alert or alert["applicant_token"] != get_applicant_token():
        return jsonify({"ok": False, "error": "not_found"}), 404
    try:
        matches = alerts_mod.preview_matches(alert, limit=6)
    except Exception:
        app.logger.exception("alert preview failed")
        matches = []
    return jsonify({"ok": True, "strong_only": bool(alert["strong_only"]),
                    "matches": matches})


@app.route("/alerts/<int:alert_id>/delete", methods=["POST"])
def alerts_delete(alert_id):
    if not g.patient_user:
        flash("Sign in to manage alerts.", "error")
        return redirect(url_for("patient_login", next=url_for("alerts")))
    if db.delete_alert(alert_id, get_applicant_token()):
        flash("Alert removed.", "success")
    else:
        flash("Couldn't remove that alert.", "error")
    return redirect(url_for("alerts"))


@app.route("/alerts/run")
def alerts_run():
    """Trigger a sweep (for cron or manual testing). Protect with ALERTS_CRON_KEY
    when set; otherwise only allowed in no-login/testing mode."""
    key = os.environ.get("ALERTS_CRON_KEY", "").strip()
    if key:
        if request.args.get("key", "") != key:
            abort(403)
    elif not NO_LOGIN:
        abort(403)
    n = alerts_mod.check_all()
    return jsonify({"checked": True, "new_matches": n})


@app.route("/reminders/run")
def reminders_run():
    """Trigger a reminder/nudge sweep (cron or manual testing). Keyed by
    ALERTS_CRON_KEY when set, else no-login/testing only."""
    key = os.environ.get("ALERTS_CRON_KEY", "").strip()
    if key:
        if request.args.get("key", "") != key:
            abort(403)
    elif not NO_LOGIN:
        abort(403)
    try:
        return jsonify(reminders_mod.run_all())
    except Exception:
        app.logger.exception("reminders sweep failed")
        return jsonify({"checked": False, "error": "sweep_failed"}), 200


@app.route("/applications/connect-records/<token>", methods=["POST"])
def connect_records(token):
    # Back-compat: the old per-application button now triggers the connect-once
    # flow (records apply to every application, not just one).
    return records_connect()


# Cached recruiting-trial fetch for SEO landing pages. Search-engine crawlers hit
# thousands of these pages, so we cache each condition's recruiting studies for a
# few hours instead of calling ClinicalTrials.gov on every crawl.
_SEO_TRIAL_CACHE = OrderedDict()
_SEO_TRIAL_TTL = 6 * 3600      # refresh a condition's trials at most every 6h
_SEO_TRIAL_MAX = 600           # cap distinct conditions held in memory


def _fetch_recruiting(condition, geo=None, limit=12):
    raw = mt.fetch_trials(condition, max_n=40, geo=geo)
    raw = [t for t in raw
           if (t.get("overallStatus") or "RECRUITING").upper() == "RECRUITING"
           and (t.get("studyType") or "").upper() != "OBSERVATIONAL"]
    return raw[:limit]


def _seo_trials(condition, city=None, limit=12):
    """Recruiting trials for an SEO page, cached per (condition, city). When a
    known city is given, results are geo-filtered to trials near it so each city
    page has unique local content; if none are local, fall back to the national
    list so the page is never empty."""
    cond_key = (condition or "").strip().lower()
    if not cond_key:
        return []
    key = (cond_key, (city or "").strip().lower())
    now_ts = time.time()
    hit = _SEO_TRIAL_CACHE.get(key)
    if hit and (now_ts - hit[0]) < _SEO_TRIAL_TTL:
        _SEO_TRIAL_CACHE.move_to_end(key)
        return hit[1][:limit]
    trials = []
    try:
        geo = None
        coords = SEO_CITY_COORDS.get(city) if city else None
        if coords:
            geo = f"distance({coords[0]},{coords[1]},100mi)"
        trials = _fetch_recruiting(condition, geo=geo, limit=limit)
        if not trials and geo:                      # no local trials -> national
            trials = _fetch_recruiting(condition, geo=None, limit=limit)
        # Record these trials as part of our indexable SEO surface so each gets a
        # crawlable /study/<nct> page and a sitemap entry (scoped to on-topic,
        # recruiting trials - never the whole of ClinicalTrials.gov).
        for t in trials:
            try:
                db.upsert_seo_study(t.get("nctId"), t.get("title"), condition)
            except Exception:
                pass
    except Exception:
        app.logger.exception("seo trials fetch failed for %s", condition)
    _SEO_TRIAL_CACHE[key] = (now_ts, trials)
    _SEO_TRIAL_CACHE.move_to_end(key)
    while len(_SEO_TRIAL_CACHE) > _SEO_TRIAL_MAX:
        _SEO_TRIAL_CACHE.popitem(last=False)
    return trials[:limit]


_SEO_EU_CACHE = OrderedDict()


def _seo_ctis_trials(condition, limit=6):
    """EU CTIS trials for a condition, cached like `_seo_trials`. Used only for
    the 'also recruiting in Europe' discovery block + to register each EU trial
    as a crawlable /study/<ctNumber> page. Never feeds the geo-ranked matcher, so
    it broadens coverage without touching 'near me' relevance. Best-effort: any
    failure returns [] so the (crawler-hit) condition page never breaks."""
    cond_key = (condition or "").strip().lower()
    if not cond_key:
        return []
    now_ts = time.time()
    hit = _SEO_EU_CACHE.get(cond_key)
    if hit and (now_ts - hit[0]) < _SEO_TRIAL_TTL:
        _SEO_EU_CACHE.move_to_end(cond_key)
        return hit[1][:limit]
    cards = []
    try:
        cards = ctis.search(condition, size=limit)
        for c in cards:
            try:
                db.upsert_seo_study(c.get("ctNumber"), c.get("title"), condition)
            except Exception:
                pass
    except Exception:
        app.logger.exception("ctis seo fetch failed for %s", condition)
    _SEO_EU_CACHE[cond_key] = (now_ts, cards)
    _SEO_EU_CACHE.move_to_end(cond_key)
    while len(_SEO_EU_CACHE) > _SEO_TRIAL_MAX:
        _SEO_EU_CACHE.popitem(last=False)
    return cards[:limit]


def _related_conditions(condition, n=24):
    """A short, topically-relevant set of other conditions for internal links.
    SEO_CONDITIONS is grouped by therapeutic area, so a window around the current
    condition surfaces neighbours in the same area; then we top up to `n`."""
    cur = (condition or "").lower()
    if condition in SEO_CONDITIONS:
        i = SEO_CONDITIONS.index(condition)
        window = SEO_CONDITIONS[max(0, i - 12):i] + SEO_CONDITIONS[i + 1:i + 13]
    else:
        window = []
    out = [c for c in window if c.lower() != cur]
    for c in SEO_CONDITIONS:
        if len(out) >= n:
            break
        if c.lower() != cur and c not in out:
            out.append(c)
    return out[:n]


@app.route("/trials/<slug>")
def condition_page(slug):
    condition = _COND_BY_SLUG.get(slug) or _titleize(slug)
    if not condition:
        abort(404)
    try:
        db.log_search_term(condition, "condition")
    except Exception:
        pass
    return render_template(
        "condition.html", condition=condition, city=None,
        trials=_seo_trials(condition), cities=SEO_CITIES[:16],
        eu_trials=_seo_ctis_trials(condition),
        conditions=_related_conditions(condition), slugify=slugify,
        canonical_url=_abs_url("condition_page", slug=slugify(condition)))


@app.route("/trials/<slug>/<city_slug>")
def condition_city_page(slug, city_slug):
    condition = _COND_BY_SLUG.get(slug) or _titleize(slug)
    city = _CITY_BY_SLUG.get(city_slug)
    # Condition can be inferred from the slug, but the city must be one we know
    # (keeps the crawl surface to real metros, not arbitrary strings).
    if not condition or not city:
        abort(404)
    return render_template(
        "condition.html", condition=condition, city=city,
        trials=_seo_trials(condition, city=city), cities=SEO_CITIES[:16],
        conditions=_related_conditions(condition), slugify=slugify,
        canonical_url=_abs_url("condition_city_page",
                               slug=slugify(condition), city_slug=city_slug))


_STUDY_CACHE = OrderedDict()
_STUDY_TTL = 6 * 3600          # re-fetch a study from CT.gov at most every 6h
_STUDY_MAX = 800               # cap distinct studies held in memory
_NCT_RE = re.compile(r"^NCT\d{8}$")


def _get_study(nct):
    """Fetch one CT.gov study (cached). Returns the extracted trial dict or None."""
    nct = (nct or "").strip().upper()
    if not _NCT_RE.match(nct):
        return None
    now_ts = time.time()
    hit = _STUDY_CACHE.get(nct)
    if hit and (now_ts - hit[0]) < _STUDY_TTL:
        _STUDY_CACHE.move_to_end(nct)
        return hit[1]
    try:
        trial = mt.fetch_study(nct)
    except Exception:
        trial = None
    if trial and trial.get("nctId"):
        _STUDY_CACHE[nct] = (now_ts, trial)
        _STUDY_CACHE.move_to_end(nct)
        while len(_STUDY_CACHE) > _STUDY_MAX:
            _STUDY_CACHE.popitem(last=False)
        return trial
    return None


def _get_ctis_study(ct):
    """Fetch one EU CTIS trial (cached, same store as CT.gov - keys never clash
    since CT numbers aren't NCT ids). Returns a normalised trial dict or None."""
    ct = (ct or "").strip()
    if not ctis.is_ct_number(ct):
        return None
    now_ts = time.time()
    hit = _STUDY_CACHE.get(ct)
    if hit and (now_ts - hit[0]) < _STUDY_TTL:
        _STUDY_CACHE.move_to_end(ct)
        return hit[1]
    trial = ctis.fetch(ct)
    if trial and trial.get("ctNumber"):
        _STUDY_CACHE[ct] = (now_ts, trial)
        _STUDY_CACHE.move_to_end(ct)
        while len(_STUDY_CACHE) > _STUDY_MAX:
            _STUDY_CACHE.popitem(last=False)
        return trial
    return None


@app.route("/study/<nct>")
def study_page(nct):
    """Crawlable, server-rendered page for a single trial: neutral registry facts
    + MedicalTrial/FAQ structured data, with a CTA into the real eligibility
    matcher. Serves both ClinicalTrials.gov (NCT) and EU CTIS (EU CT number)
    trials. Indexable only when the trial is recruiting/active AND in our scoped
    SEO surface; any other valid study still renders but is marked noindex."""
    raw = (nct or "").strip()
    key = raw.upper()
    source = ("ctgov" if _NCT_RE.match(key)
              else ("ctis" if ctis.is_ct_number(raw) else None))
    if source is None:
        abort(404)

    if source == "ctgov":
        trial = _get_study(key)
        if not trial:
            abort(404)
        study_id = key
        recruiting = (trial.get("overallStatus") or "").upper() == "RECRUITING"
        plain_title = summarize.patient_card_title(trial)
        # Structured LLM explainer when configured; otherwise plain_terms below
        # still carries the "what/who/how long" content.
        summary = summarize.plain(trial)
        plain_terms = summarize.plain_terms(trial)
        pay = _pay_likelihood(trial)
        support = _participant_support_signal(trial)
        source_name = "ClinicalTrials.gov"
        source_url = "https://clinicaltrials.gov/study/" + key
        applied = key in db.applied_ncts(get_applicant_token())
    else:  # EU CTIS - discovery/content only (no LLM/pay enrichment, no geo match)
        study_id = raw
        trial = _get_ctis_study(study_id)
        if not trial:
            abort(404)
        recruiting = bool(trial.get("_recruiting"))
        plain_title = trial.get("title") or ""
        summary = None
        incl, excl = trial.get("_incl") or [], trial.get("_excl") or []
        plain_terms = {
            "who": incl[:6], "rule_out": excl[:6], "who_basics": "",
            "has_more": (len(incl) > 6 or len(excl) > 6),
            "inc_all": incl, "exc_all": excl,
            "phase": trial.get("phase") or "", "time": "", "design": "",
        }
        pay, support = {}, {}
        source_name = "EU CTIS"
        source_url = trial.get("_view_url")
        applied = False

    try:
        _log_event("study_view", {"id": study_id, "src": source})
    except Exception:
        pass
    indexable = recruiting and db.is_seo_study(study_id)
    condition = (trial.get("conditions") or [""])[0] or ""
    blurb = summarize.card_blurb(trial)
    summary_text = summarize.tidy(trial.get("briefSummary") or "")
    facts = _study_facts(trial)
    faqs = _study_faqs(trial, facts, condition, recruiting, pay, support,
                       plain_terms.get("time", ""))
    return render_template(
        "study.html", trial=trial, nct=study_id, study_id=study_id,
        source=source, source_name=source_name, source_url=source_url,
        recruiting=recruiting, indexable=indexable, condition=condition,
        blurb=blurb, plain_title=plain_title, summary=summary,
        summary_text=summary_text, plain_terms=plain_terms, facts=facts,
        faqs=faqs, pay=pay, support=support,
        related=_related_conditions(condition) if condition else [],
        slugify=slugify, applied=applied,
        canonical_url=_abs_url("study_page", nct=study_id))


def _study_facts(trial):
    """Human-readable, factual fields for the study page + its structured data."""
    def _age(v):
        return (v or "").replace("Years", "years").strip()
    if trial.get("_ages_text"):          # source-provided human string (CTIS)
        ages = trial["_ages_text"]
    else:
        lo, hi = _age(trial.get("minAge")), _age(trial.get("maxAge"))
        if lo and hi:
            ages = f"{lo} to {hi}"
        elif lo:
            ages = f"{lo} and older"
        elif hi:
            ages = f"up to {hi}"
        else:
            ages = "Not specified"
    sex = (trial.get("sex") or "").upper()
    sex_text = {"ALL": "All sexes", "FEMALE": "Female", "MALE": "Male"}.get(sex, "All sexes")
    hv = str(trial.get("healthyVolunteers") or "").strip().lower()
    if hv in ("yes", "true", "y"):
        healthy = "Yes"
    elif hv in ("no", "false", "n"):
        healthy = "No"
    else:
        healthy = "Not specified"
    # Distinct "City, Country" locations (deduped, order-preserving).
    where, seen = [], set()
    for l in trial.get("locations") or []:
        parts = [p for p in [l.get("city"), l.get("state"), l.get("country")] if p]
        label = ", ".join(parts)
        if label and label.lower() not in seen:
            seen.add(label.lower())
            where.append(label)
    return {"ages": ages, "sex": sex_text, "healthy": healthy, "where": where,
            "phase": trial.get("phase") or "", "sponsor": trial.get("leadSponsor") or "",
            "enrollment": trial.get("enrollment") or ""}


def _study_faqs(trial, facts, condition, recruiting, pay=None, support=None,
                time_text=""):
    """Q&A pairs powering both the on-page FAQ and the FAQPage JSON-LD (AEO).

    These deliberately answer the HIGH-INTENT questions people actually Google
    about a trial ("do you get paid", "is it free", "how long") - not a restate
    of the sections already visible on the page. All money/cost answers stay
    hedged and defer to the study team (compliance-safe, no promises)."""
    pay, support = pay or {}, support or {}
    faqs = []

    # #1 search intent for trials: compensation. Kept hedged + team-confirmed.
    has_pay_signal = bool(pay.get("label")) or bool(support.get("stipend"))
    if has_pay_signal:
        pay_ans = ("This study's listing includes signals that participants may "
                   "be compensated or receive a stipend. Amounts vary and are "
                   "set by the study team - confirm the details with them.")
    else:
        pay_ans = ("This listing doesn't specify compensation. Many trials still "
                   "reimburse travel or offer a stipend, so it's worth asking the "
                   "study team when you connect.")
    faqs.append(("Do participants get paid in this trial?", pay_ans))

    # Cost + insurance - a very common blocker/question, hedged.
    cost_ans = ("Searching and applying through BridgeMD is free. In clinical "
                "trials the study-related treatment and visits are generally "
                "provided at no cost to you")
    if support.get("travel"):
        cost_ans += ", and this study mentions travel support"
    cost_ans += (". You usually don't need insurance to take part - confirm "
                 "specifics with the study team.")
    faqs.append(("Is it free to join, and do I need insurance?", cost_ans))

    # Time commitment - only give a number when the source states one.
    if time_text:
        faqs.append(("How long does this study last?",
                     f"The study runs {time_text} per participant, based on its "
                     "public description. The team confirms the exact schedule "
                     "and number of visits before you enroll."))
    else:
        faqs.append(("How long does this study last?",
                     "The listing doesn't state an exact length. The study team "
                     "walks you through the schedule and number of visits before "
                     "you decide to enroll."))

    # Who can join - retained (top search + FAQ rich-result eligibility).
    who = f"This study is enrolling {facts['sex'].lower()}, {facts['ages'].lower()}."
    if facts["healthy"] == "Yes":
        who += " Healthy volunteers may be eligible."
    who += " The study team makes the final eligibility decision."
    faqs.append(("Who can join this trial?", who))

    # Location - powers "clinical trials near me" queries.
    if facts["where"]:
        top = facts["where"][:6]
        more = (f" and {len(facts['where']) - len(top)} more location(s)"
                if len(facts["where"]) > len(top) else "")
        faqs.append(("Where is this trial taking place?",
                     "Study sites include " + "; ".join(top) + more +
                     ". Enter your location above to see the nearest site and "
                     "check your eligibility."))
    return faqs


SCREENER_LABELS = {
    "travel": "Can travel to the study site",
    "other_trial": "Currently in another trial",
    "pregnancy": "Pregnant / planning pregnancy",
    "consent_capable": "Can give own consent",
}
# Answers that are a yellow flag for the study team to look at.
SCREENER_FLAGS = {"other_trial": "yes", "pregnancy": "yes", "consent_capable": "no"}


def _readiness_band(score):
    if score >= 70:
        return "strong"
    if score >= 45:
        return "good"
    return "review"


_QUESTION_STARTS = ("do ", "does ", "are ", "is ", "can ", "have ", "has ",
                    "did ", "were ", "was ", "what", "when", "how", "which",
                    "who", "why")


def _as_question(phrase):
    """Turn a 'thing the team will confirm' phrase (e.g. 'MMSE score') into a
    patient-answerable question ('What is your most recent MMSE score?'), so we
    can optionally collect it upfront. Leaves phrases that are already questions
    mostly intact."""
    p = (phrase or "").strip().rstrip("?.").strip()
    if not p:
        return ""
    low = p.lower()
    if low.startswith(_QUESTION_STARTS):
        return p[0].upper() + p[1:] + "?"
    # Keep original casing so acronyms (MMSE) and proper nouns (Alzheimer's)
    # aren't mangled.
    return "Can you share your " + p + "?"


app.jinja_env.filters["as_question"] = _as_question


def _readiness_from_signals(elig, screener):
    """Deterministic 'chance of clearing pre-screen' from the LLM eligibility
    verdict + the concern flags in the patient's own answers. Used as a fast,
    free baseline and as the fallback when the LLM is unavailable."""
    verdict = (elig or {}).get("verdict", "")
    base = {"likely_eligible": 82, "possible": 58,
            "unlikely": 32}.get(verdict, 55)
    scr = dict(screener or {})
    dyn = scr.pop("_flags", None) or []
    if not isinstance(dyn, list):
        dyn = [dyn]
    concern = sum(1 for k, bad in SCREENER_FLAGS.items() if scr.get(k) == bad)
    concern += len([x for x in dyn if str(x).strip()])
    base -= 12 * concern
    unknown = len((elig or {}).get("unknown") or [])
    base -= min(10, 2 * unknown)
    return max(5, min(95, base))


def _evaluate_prescreen_readiness(title, condition, screener, elig):
    """Estimate how likely the patient's application is to clear the study
    team's pre-screen, from THEIR answers + the eligibility read. Returns
    {score, band, note, confirm[]}. LLM-written note when available, else a
    templated one. This is an ESTIMATE for the patient's benefit - never a
    guarantee, never a sponsor claim (see compliance rule)."""
    base = _readiness_from_signals(elig, screener)
    band = _readiness_band(base)
    # Deterministic, encouraging fallback copy.
    fb_note = {
        "strong": "Your answers line up well with this study's basics.",
        "good": "You look like a reasonable fit - the team will confirm a few details.",
        "review": "A few things need the study team's review, but it's worth applying.",
    }[band]
    confirm = [str(x) for x in ((elig or {}).get("unknown") or [])[:3]]
    result = {"score": base, "band": band, "note": fb_note, "confirm": confirm}
    if not mt.LLM_API_KEY:
        return result
    # Compact, de-identified view of the answers for the model.
    scr = dict(screener or {})
    scr.pop("_flags", None)
    ans_lines = "\n".join(f"- {SCREENER_LABELS.get(k, k)}: {v}"
                          for k, v in scr.items() if str(v).strip())
    elig_bits = []
    if (elig or {}).get("met"):
        elig_bits.append("Meets: " + "; ".join(elig["met"][:6]))
    if (elig or {}).get("unknown"):
        elig_bits.append("To confirm: " + "; ".join(elig["unknown"][:6]))
    if (elig or {}).get("not_met"):
        elig_bits.append("Possible barriers: " + "; ".join(elig["not_met"][:6]))
    system = (
        "You help a patient understand how ready their application looks for a "
        "clinical study team's pre-screen, based ONLY on the patient's own "
        "answers and an eligibility read. Be encouraging and plain-spoken. "
        "This is an ESTIMATE, not a decision or a promise of enrollment; the "
        "study team makes the final call. Never discourage someone from "
        "applying. Return strict JSON: {\"score\": int 0-100, \"note\": string "
        "<=25 words, \"confirm\": [up to 3 SHORT patient-answerable questions "
        "the patient could optionally answer to speed screening, e.g. 'What is "
        "your most recent MMSE score?']}."
    )
    user = (
        f"STUDY: {title or condition or 'a clinical study'}\n"
        f"CONDITION: {condition or 'n/a'}\n\n"
        f"PATIENT ANSWERS:\n{ans_lines or '- (none)'}\n\n"
        f"ELIGIBILITY READ:\n{chr(10).join(elig_bits) or '- (none)'}\n\n"
        f"Baseline score from signals is {base}. Adjust only if the answers "
        f"clearly justify it, and return the JSON."
    )
    try:
        raw = mt.llm_chat(system, user)
        data = mt._extract_json(raw)
        score = int(data.get("score", base))
        score = max(5, min(95, score))
        note = str(data.get("note", "")).strip() or fb_note
        conf = data.get("confirm") or confirm
        if not isinstance(conf, list):
            conf = [str(conf)]
        conf = [str(x).strip() for x in conf if str(x).strip()][:3]
        return {"score": score, "band": _readiness_band(score),
                "note": note, "confirm": conf}
    except Exception:
        app.logger.exception("prescreen readiness eval failed")
        return result
SUPPORT_COVERAGE_LABELS = {
    "": "Not checked",
    "needs_info": "Need more insurance info",
    "manual_review": "Manual payer check needed",
    "likely_covered": "Likely covered",
    "coverage_limited": "Coverage may be limited",
    "api_error": "Payer API error",
}
SUPPORT_TRAVEL_LABELS = {
    "": "Not planned",
    "assist_required": "Assistance required",
    "long_distance": "Long-distance planning needed",
    "supported": "Transit support likely",
    "basic_plan": "Basic travel plan ready",
    "manual_review": "Manual planning needed",
    "api_error": "Logistics API error",
}


def _source_bucket(src):
    s = (src or "").strip().lower()
    return "physician" if s in {
        "referral", "invite", "physician", "emr", "doctor_referral"
    } else "patient"


def age_band(age):
    try:
        a = int(str(age).strip())
    except (ValueError, TypeError):
        return "Adult"
    lo = (a // 10) * 10
    return f"{lo}-{lo + 9}"


def candidate_code(lead):
    return f"Candidate #{lead['id']:04d}"


def _decode_lead(row, recon=None):
    """Attach parsed screener/eligibility + de-identified helpers to a lead row."""
    try:
        elig = json.loads(row["eligibility"]) if row["eligibility"] else {}
    except (ValueError, TypeError):
        elig = {}
    try:
        scr = json.loads(row["screener"]) if row["screener"] else {}
    except (ValueError, TypeError):
        scr = {}
    # Reserved key holds trial-specific answers the patient flagged as concerns.
    dyn_flags = scr.pop("_flags", None) or []
    if not isinstance(dyn_flags, list):
        dyn_flags = [str(dyn_flags)]
    flags = [SCREENER_LABELS.get(k, k) for k, bad in SCREENER_FLAGS.items()
             if scr.get(k) == bad]
    flags += [str(x) for x in dyn_flags if str(x).strip()]
    return {
        "lead": row,
        "events": db.get_lead_events(row["id"]),
        "elig": elig,
        "screener": scr,
        "support": db.get_lead_support(row["id"]),
        "flags": flags,
        "code": candidate_code(row),
        "age_band": age_band(row["age"]),
        "unread": db.lead_unread_for_site(row["id"]),
        "recon": recon or db.latest_reconciliation(row["id"]),
        "messages": db.get_messages(row["id"]),
        "files": db.list_attachments(row["id"]),
        "tasks": db.list_tasks(row["id"]),
        "doc_requests": db.list_doc_requests(row["id"]),
    }


@app.route("/app/leads")
@login_required
def leads():
    if _demo_mode_enabled():
        try:
            db.ensure_demo_claim_volume(g.user["id"], minimum_rows=18)
        except Exception:
            app.logger.exception("demo claim volume seeding failed")
    rows = db.list_leads_for_user(g.user["id"])
    recon = db.latest_reconciliation_for_leads([r["id"] for r in rows])
    claims = db.list_study_claims(g.user["id"])
    site_cfg = redcap.config_from_profile(db.get_site_profile(g.user["id"]))
    counts = db.lead_counts_for_ncts([c["nct"] for c in claims])
    review, active, done = [], [], []
    for r in rows:
        item = _decode_lead(r, recon.get(r["id"]))
        if r["status"] == "prescreen" and not r["decision"]:
            review.append(item)
        elif r["revealed"] and r["status"] not in db.LEAD_CLOSED:
            active.append(item)
        else:
            done.append(item)
    return render_template("leads.html", review=review, active=active, done=done,
                           counts=counts, pipeline=db.LEAD_PIPELINE,
                           statuses=db.LEAD_STATUSES, labels=db.LEAD_LABELS,
                           screener_labels=SCREENER_LABELS,
                           support_coverage_labels=SUPPORT_COVERAGE_LABELS,
                           support_travel_labels=SUPPORT_TRAVEL_LABELS,
                           recon_labels=db.RECON_LABELS,
                           recon_outcomes=db.RECON_OUTCOMES,
                           redcap_on=site_cfg.connected,
                           redcap_intake_live=site_cfg.intake_live,
                           claims=claims)


@app.route("/app/dashboard")
@login_required
def recruitment_dashboard():
    """The recruitment plan + proof: live funnel, conversion, time-in-stage, and
    where candidates drop off - built from the data the pipeline already logs."""
    claims = _site_claims()
    stats = analytics.funnel_stats(claims)
    spend = db.spend_summary_for_user(g.user["id"], ncts=claims)
    enrolled = int((stats.get("totals") or {}).get("enrolled") or 0)
    screened = 0
    for row in stats.get("source_breakdown", []):
        screened += int(row.get("screening") or 0)
    spend["cost_per_enrolled"] = round(spend["total_usd"] / enrolled, 2) \
        if enrolled else None
    spend["cost_per_screened"] = round(spend["total_usd"] / screened, 2) \
        if screened else None
    return render_template(
        "recruitment.html", stats=stats, spend=spend,
        labels=db.LEAD_LABELS, claims=db.list_study_claims(g.user["id"]))


@app.route("/app/analytics")
@owner_required
def owner_analytics():
    """Private, owner-only visitor dashboard: who's coming, what they search,
    and how they move from landing -> search -> view -> apply. Only OWNER_EMAIL
    can see this; any other signed-in staff user gets a 404."""
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    web = db.web_funnel_stats(days=days)
    recent = db.recent_searches(days=days, limit=40)
    series = db.web_timeseries(days=days)
    return render_template("analytics.html", web=web, recent=recent, days=days,
                           series=series,
                           your_ip_masked=_mask_ip(_client_ip()),
                           ip_ignored=_analytics_ignored())


@app.route("/app/analytics/reset", methods=["POST"])
@owner_required
def owner_analytics_reset():
    """Clear all visitor-analytics history. Handy for wiping bot-inflated data
    accumulated before bot filtering was added, so counts start clean."""
    try:
        n = db.clear_web_events()
        flash(f"Cleared {n} analytics event(s). Counts start fresh now.", "success")
    except Exception:
        app.logger.exception("failed to reset web analytics")
        flash("Couldn't reset analytics. Please try again.", "error")
    return redirect(url_for("owner_analytics"))


@app.route("/app/dashboard/spend", methods=["POST"])
@login_required
def recruitment_spend_add():
    claims = _site_claims()
    nct = request.form.get("nct", "").strip().upper()
    if claims and nct and nct not in claims:
        flash("Select a claimed study for spend tracking.", "error")
        return redirect(url_for("recruitment_dashboard"))
    source = request.form.get("source", "").strip().lower()
    if not source:
        source = "patient"
    source = _source_bucket(source)
    try:
        amt = float(request.form.get("amount_usd", "0").strip())
    except Exception:
        amt = -1
    if amt <= 0:
        flash("Add a valid spend amount in USD.", "error")
        return redirect(url_for("recruitment_dashboard"))
    db.add_recruitment_spend(
        g.user["id"], nct, source,
        request.form.get("campaign", "").strip(),
        amt,
        request.form.get("spend_date", "").strip(),
        request.form.get("note", "").strip())
    flash("Recruitment spend logged.", "success")
    return redirect(url_for("recruitment_dashboard"))


@app.route("/app/dashboard/export.csv")
@login_required
def recruitment_export_csv():
    """Sponsor-facing export for cohort/source funnel reporting."""
    stats = analytics.funnel_stats(_site_claims())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "key", "value"])
    for s in stats.get("stages", []):
        k = (s.get("key") or "").strip()
        w.writerow(["stage_reached", k, s.get("reached", 0)])
        w.writerow(["stage_conv_from_prev_pct", k, s.get("conv_from_prev", 0)])
    t = stats.get("totals", {})
    for k in ("total", "active", "enrolled", "verified_enrolled", "withdrawn", "closed"):
        w.writerow(["totals", k, t.get(k, 0)])
    for row in stats.get("source_breakdown", []):
        src = row.get("source", "")
        w.writerow(["source_total", src, row.get("total", 0)])
        w.writerow(["source_screening", src, row.get("screening", 0)])
        w.writerow(["source_enrolled", src, row.get("enrolled", 0)])
        w.writerow(["source_enroll_conv_pct", src, row.get("enroll_conv", 0)])
        w.writerow(["source_verified_enrolled", src, row.get("verified_enrolled", 0)])
    spend = db.spend_summary_for_user(g.user["id"], ncts=_site_claims())
    w.writerow(["spend_total_usd", "all", spend.get("total_usd", 0)])
    for row in spend.get("by_source", []):
        w.writerow(["spend_source_usd", row.get("source", ""),
                    row.get("amount_usd", 0)])
    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=recruitment_export.csv"
    return resp


@app.route("/app/dashboard/summary.json")
@login_required
def recruitment_summary_json():
    """JSON summary for sponsor/customer reporting integrations."""
    stats = analytics.funnel_stats(_site_claims())
    return jsonify({
        "ok": True,
        "generated_at": db.now(),
        "claims": sorted(_site_claims()),
        "stats": stats,
    }), 200


def _render_site_setup(instruments=None, active_tab=None):
    """Render site setup, including per-site REDCap connection state.

    `instruments` is the (optionally freshly-loaded) list of REDCap instruments
    for the picker; falls back to simulated ones in demo mode so the intake
    picker is always walkable. `active_tab` keeps the right section open after a
    re-render (e.g. after "Load my forms") so the coordinator isn't bounced back.
    """
    profile = db.get_site_profile(g.user["id"])
    cfg = redcap.config_from_profile(profile)
    simulated = _demo_mode_enabled() and not cfg.connected
    if instruments is None:
        instruments = list(redcap.SIMULATED_INSTRUMENTS) if simulated else []
    return render_template(
        "site_setup.html",
        profile=profile,
        claims=db.list_study_claims(g.user["id"]),
        posted=db.list_site_posted_studies(user_id=g.user["id"]),
        redcap_on=cfg.connected,
        redcap_token_set=bool(cfg.api_token),
        redcap_field_map=redcap._row_get(profile, "redcap_field_map"),
        redcap_intake_instrument=cfg.intake_instrument,
        redcap_intake_enabled=cfg.intake_enabled,
        redcap_instruments=instruments,
        redcap_simulated=simulated,
        active_tab=active_tab)


@app.route("/app/site", methods=["GET", "POST"])
@login_required
def site_setup():
    """Study-team onboarding: account profile + claim your active NCTs."""
    if request.method == "POST":
        f = request.form
        # REDCap connection lives in its own tab (site_redcap_save); preserve it
        # here so saving the org profile never wipes the stored URL/label.
        prof = db.get_site_profile(g.user["id"])
        db.upsert_site_profile(
            g.user["id"], f.get("org_name", ""), f.get("contact_name", ""),
            f.get("contact_email", ""), f.get("contact_phone", ""),
            f.get("intake_sla_hours", ""), f.get("escalation_email", ""),
            f.get("ctms_endpoint", ""),
            redcap._row_get(prof, "redcap_endpoint"),
            redcap._row_get(prof, "redcap_project_label"))
        flash("Site profile saved.", "success")
        return redirect(url_for("site_setup"))
    return _render_site_setup()


@app.route("/app/site/redcap", methods=["POST"])
@login_required
def site_redcap_save():
    """Save the site's REDCap intake connection (URL, token, instrument, gate).

    The API token is write-only: submitting it blank keeps the stored secret so
    a coordinator can tweak other settings without re-typing the token. The
    token is never rendered back to the page or logged."""
    f = request.form
    token_raw = f.get("redcap_api_token", "")
    # Blank token field -> keep existing secret (None = leave unchanged).
    token = token_raw.strip() if token_raw.strip() else None
    # Live patient forms require an explicit IRB/consent attestation (compliance).
    intake_enabled = bool(f.get("redcap_intake_enabled"))
    db.update_site_redcap(
        g.user["id"],
        endpoint=f.get("redcap_endpoint", ""),
        api_token=token,
        field_map=f.get("redcap_field_map", ""),
        intake_instrument=f.get("redcap_intake_instrument", ""),
        intake_enabled=intake_enabled,
        project_label=f.get("redcap_project_label", ""))
    flash("REDCap connection saved.", "success")
    return redirect(url_for("site_setup") + "#redcap")


@app.route("/app/site/redcap/instruments", methods=["POST"])
@login_required
def site_redcap_instruments():
    """Load the project's instruments so the site can pick the intake form."""
    cfg = redcap.config_from_profile(db.get_site_profile(g.user["id"]))
    if _demo_mode_enabled() and not cfg.connected:
        flash("Showing simulated instruments (REDCap not connected).", "success")
        return _render_site_setup(instruments=list(redcap.SIMULATED_INSTRUMENTS),
                                  active_tab="redcap")
    ok, instruments, msg = redcap.export_instruments(cfg)
    flash(msg, "success" if ok else "error")
    return _render_site_setup(instruments=instruments if ok else None,
                              active_tab="redcap")


@app.route("/app/site/post-study", methods=["POST"])
@login_required
def post_site_study():
    title = request.form.get("title", "").strip()
    condition = request.form.get("condition", "").strip()
    location = request.form.get("location", "").strip()
    if not title or not condition or not location:
        flash("Add title, condition, and location before posting the study.", "error")
        return redirect(url_for("site_setup"))
    geo_lat, geo_lon = None, None
    try:
        geo = geocode(location)
    except Exception:
        geo = None
    if geo:
        geo_lat, geo_lon = geo[0], geo[1]
    row = db.create_site_posted_study(g.user["id"], {
        "title": title,
        "condition": condition,
        "location": location,
        "brief_summary": request.form.get("brief_summary", "").strip(),
        "eligibility": request.form.get("eligibility", "").strip(),
        "site_name": request.form.get("site_name", "").strip(),
        "contact_email": request.form.get("contact_email", "").strip(),
        "contact_phone": request.form.get("contact_phone", "").strip(),
        "phase": request.form.get("phase", "").strip(),
        "status": request.form.get("status", "recruiting").strip().lower(),
        "lat": geo_lat,
        "lon": geo_lon,
    })
    if row:
        flash("Study posted. It now appears in patient search and routes to your ATS.",
              "success")
    else:
        flash("Couldn't post that study. Please try again.", "error")
    return redirect(url_for("site_setup"))


@app.route("/app/site/post-study/remove", methods=["POST"])
@login_required
def remove_posted_study():
    try:
        study_id = int(request.form.get("study_id", "0") or 0)
    except ValueError:
        study_id = 0
    if study_id and db.remove_site_posted_study(g.user["id"], study_id):
        flash("Site-posted study removed.", "success")
    else:
        flash("Couldn't remove that study.", "error")
    return redirect(url_for("site_setup"))


def _auto_verify_claims():
    """Claims are auto-approved only in trusted single-tenant/demo builds.
    In a multi-tenant production instance, new claims stay pending until an
    admin approves them - otherwise any account could claim a trial's NCT and
    read that trial's applicants (PHI)."""
    return _demo_mode_enabled() or os.environ.get("AUTO_VERIFY_CLAIMS", "0") == "1"


@app.route("/app/site/claim", methods=["POST"])
@login_required
def add_study_claim():
    nct = request.form.get("nct", "").strip().upper()
    title = request.form.get("title", "").strip()
    notify_email = request.form.get("notify_email", "").strip()
    auto = _auto_verify_claims()
    if not nct:
        flash("Add an NCT number to claim a study.", "error")
    elif db.add_study_claim(g.user["id"], nct, title,
                            notify_email=notify_email, verified=auto):
        if auto:
            flash("Study claimed. New applicants for that NCT now route to your "
                  "board.", "success")
        else:
            flash("Claim submitted for verification. To protect patient data, "
                  "applicants for this NCT will appear on your board once your "
                  "affiliation with the study is approved.", "success")
    else:
        flash("Couldn't claim that study. Check the NCT and try again.", "error")
    return redirect(url_for("site_setup"))


@app.route("/ops/claims/pending")
def ops_pending_claims():
    """Admin: list study claims awaiting verification (key-protected)."""
    if not _ops_key_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    rows = db.list_pending_claims()
    return jsonify({"ok": True, "pending": [
        {"user_id": r["user_id"], "user_email": r["user_email"],
         "nct": r["nct"], "title": r["title"], "created_at": r["created_at"]}
        for r in rows]}), 200


@app.route("/ops/claims/verify", methods=["POST"])
def ops_verify_claim():
    """Admin: approve (or revoke) a study claim so it grants/stops access.
    Key-protected; not a browser form (CSRF-exempt)."""
    if not _ops_key_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    try:
        uid = int(request.form.get("user_id", "0") or 0)
    except ValueError:
        uid = 0
    nct = request.form.get("nct", "").strip().upper()
    verified = request.form.get("verified", "1").strip() != "0"
    if not (uid and nct):
        return jsonify({"ok": False, "error": "user_id and nct required"}), 400
    db.set_claim_verified(uid, nct, verified=verified)
    return jsonify({"ok": True, "user_id": uid, "nct": nct,
                    "verified": verified}), 200


@app.route("/app/site/claim/remove", methods=["POST"])
@login_required
def remove_study_claim():
    nct = request.form.get("nct", "").strip().upper()
    if nct:
        if nct.startswith("SITE-"):
            flash("Manage site-posted studies in the section below.", "error")
            return redirect(url_for("site_setup"))
        db.remove_study_claim(g.user["id"], nct)
        flash("Study unclaimed.", "success")
    return redirect(url_for("site_setup"))


@app.route("/app/site/claim/notify-email", methods=["POST"])
@login_required
def update_study_claim_notify_email():
    nct = request.form.get("nct", "").strip().upper()
    notify_email = request.form.get("notify_email", "").strip()
    if not nct:
        flash("Missing study id.", "error")
        return redirect(url_for("site_setup"))
    if nct.startswith("SITE-"):
        flash("Set routing email in the site-posted study section.", "error")
        return redirect(url_for("site_setup"))
    db.update_study_claim_notify_email(g.user["id"], nct, notify_email)
    flash("Trial notification email updated.", "success")
    return redirect(url_for("site_setup"))


@app.route("/app/leads/<int:lead_id>/accept", methods=["POST"])
@login_required
def accept_lead(lead_id):
    _ensure_site_access_for_lead(lead_id)
    note = request.form.get("note", "").strip()
    if db.accept_candidate(lead_id, note):
        _notify_applicant_by_id(lead_id, "accepted")
        flash("Candidate accepted - contact details unlocked so you can invite "
              "them to a screening visit.", "success")
    else:
        flash("Couldn't accept that candidate.", "error")
    return redirect(url_for("leads"))


@app.route("/app/leads/<int:lead_id>/decline", methods=["POST"])
@login_required
def decline_lead(lead_id):
    _ensure_site_access_for_lead(lead_id)
    reason = request.form.get("reason", "").strip()
    if db.decline_candidate(lead_id, reason):
        _notify_applicant_by_id(lead_id, "declined")
        flash("Candidate declined. They stay de-identified - no contact details "
              "were revealed.", "success")
    else:
        flash("Couldn't decline that candidate.", "error")
    return redirect(url_for("leads"))


@app.route("/app/leads/<int:lead_id>/schedule", methods=["POST"])
@login_required
def schedule_lead(lead_id):
    _ensure_site_access_for_lead(lead_id)
    """Attach a booking link (Calendly/Acuity/Cal.com/etc.) to an accepted
    candidate so the patient can self-schedule their screening call."""
    url = request.form.get("schedule_url", "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    lead = db.set_lead_schedule(lead_id, url)
    if not lead:
        flash("Couldn't find that candidate.", "error")
    elif url:
        _notify_applicant_schedule(lead)
        flash("Booking link sent - the applicant can now self-schedule their "
              "screening call.", "success")
    else:
        flash("Booking link removed.", "success")
    return redirect(url_for("leads"))


def _lead_action_return():
    """Send lead actions back to wherever they were triggered (leads board or
    the message center), defaulting to the leads board."""
    return _safe_next(request.form.get("next", "")) or url_for("leads")


@app.route("/app/leads/<int:lead_id>/message", methods=["POST"])
@login_required
def message_lead(lead_id):
    """Study-team inbox action: send a message without leaving the board."""
    _ensure_site_access_for_lead(lead_id)
    back = _lead_action_return()
    lead = db.get_lead(lead_id)
    if not lead:
        flash("Couldn't find that candidate.", "error")
        return redirect(back)
    if not lead["revealed"]:
        flash("Accept the candidate first to message them.", "error")
        return redirect(back)
    body = request.form.get("body", "").strip()
    if body:
        db.add_message(lead_id, "site", body)
        _notify_applicant_message(lead, body)
        flash("Message sent.", "success")
    else:
        flash("Write a message first.", "error")
    return redirect(back)


@app.route("/app/leads/<int:lead_id>/attach", methods=["POST"])
@login_required
def attach_lead_file(lead_id):
    """Study team drops a document into a candidate's private thread."""
    _ensure_site_access_for_lead(lead_id)
    back = _lead_action_return()
    lead = db.get_lead(lead_id)
    if not lead:
        flash("Couldn't find that candidate.", "error")
        return redirect(back)
    if not lead["revealed"]:
        flash("Accept the candidate first to share documents.", "error")
        return redirect(back)
    saved = _save_upload(request.files.get("file"))
    if not saved:
        flash("Attach a supported file (PDF, image, doc, spreadsheet).", "error")
        return redirect(back)
    orig, stored, mime, size = saved
    note = request.form.get("note", "").strip()
    db.add_attachment(lead_id, "site", orig, stored, mime, size, note)
    db.add_message(lead_id, "site", f"Shared a document: {orig}"
                   + (f" - {note}" if note else ""))
    _notify_applicant_message(lead, f"Your study team shared a document: {orig}")
    flash("Document shared with the applicant.", "success")
    return redirect(back)


@app.route("/app/leads/<int:lead_id>/task", methods=["POST"])
@login_required
def add_lead_task(lead_id):
    """Study team adds a to-do item for the candidate to complete."""
    _ensure_site_access_for_lead(lead_id)
    back = _lead_action_return()
    lead = db.get_lead(lead_id)
    if not lead:
        flash("Couldn't find that candidate.", "error")
        return redirect(back)
    if not lead["revealed"]:
        flash("Accept the candidate first to assign tasks.", "error")
        return redirect(back)
    title = request.form.get("title", "").strip()
    if not title:
        flash("Describe the task first.", "error")
        return redirect(back)
    db.add_task(lead_id, title, assigned_to="patient", created_by="site")
    db.add_message(lead_id, "site", f"New to-do for you: {title}")
    _notify_applicant_message(lead, f"New to-do from your study team: {title}")
    flash("Task added to the candidate's checklist.", "success")
    return redirect(back)


@app.route("/app/leads/<int:lead_id>/task/<int:task_id>", methods=["POST"])
@login_required
def toggle_lead_task(lead_id, task_id):
    _ensure_site_access_for_lead(lead_id)
    back = _lead_action_return()
    task = db.get_task(task_id)
    if not task or task["lead_id"] != lead_id:
        abort(404)
    new_status = "open" if task["status"] == "done" else "done"
    db.set_task_status(task_id, new_status)
    flash("To-do marked done." if new_status == "done"
          else "To-do reopened.", "success")
    return redirect(back)


@app.route("/app/leads/<int:lead_id>/doc-request", methods=["POST"])
@login_required
def request_lead_doc(lead_id):
    """Study team asks the candidate for a specific record (e.g. a pathology
    report confirming diagnosis). Tracked with its own status so the coordinator
    can see what's still outstanding before the screening window closes."""
    _ensure_site_access_for_lead(lead_id)
    back = _lead_action_return()
    lead = db.get_lead(lead_id)
    if not lead:
        flash("Couldn't find that candidate.", "error")
        return redirect(back)
    if not lead["revealed"]:
        flash("Accept the candidate first to request records.", "error")
        return redirect(back)
    title = request.form.get("title", "").strip()
    note = request.form.get("note", "").strip()
    if not title:
        flash("Name the record you need first.", "error")
        return redirect(back)
    db.add_doc_request(lead_id, title, note, created_by="site")
    ask = f"Please send: {title}" + (f" - {note}" if note else "")
    db.add_message(lead_id, "site", ask)
    _notify_applicant_message(lead, f"Your study team requested a record: {title}")
    flash("Record request sent to the candidate.", "success")
    return redirect(back)


@app.route("/app/leads/<int:lead_id>/doc-request/<int:req_id>/upload",
           methods=["POST"])
@login_required
def upload_lead_doc(lead_id, req_id):
    """Coordinator uploads a requested record on the candidate's behalf (e.g.
    something they collected via a records request)."""
    _ensure_site_access_for_lead(lead_id)
    back = _lead_action_return()
    req = db.get_doc_request(req_id)
    if not req or req["lead_id"] != lead_id:
        abort(404)
    saved = _save_upload(request.files.get("file"))
    if not saved:
        flash("Attach a supported file (PDF, image, doc, spreadsheet).", "error")
        return redirect(back)
    orig, stored, mime, size = saved
    att_id = db.add_attachment(lead_id, "site", orig, stored, mime, size,
                               note=f"Record: {req['title']}")
    db.set_doc_request_upload(req_id, att_id)
    db.add_message(lead_id, "site",
                   f"Added the requested record ({req['title']}): {orig}")
    flash("Record uploaded and marked received.", "success")
    return redirect(back)


@app.route("/app/leads/<int:lead_id>/doc-request/<int:req_id>/review",
           methods=["POST"])
@login_required
def review_lead_doc(lead_id, req_id):
    """Study team accepts a submitted record or rejects it (patient re-uploads)."""
    _ensure_site_access_for_lead(lead_id)
    back = _lead_action_return()
    lead = db.get_lead(lead_id)
    req = db.get_doc_request(req_id)
    if not lead or not req or req["lead_id"] != lead_id:
        abort(404)
    accepted = request.form.get("decision") == "accept"
    review_note = request.form.get("review_note", "").strip()
    db.set_doc_request_review(req_id, accepted, review_note)
    if accepted:
        db.add_message(lead_id, "site",
                       f"Accepted your record: {req['title']}. Thank you.")
        flash("Record accepted.", "success")
    else:
        msg = (f"We need a new copy of: {req['title']}."
               + (f" {review_note}" if review_note else
                  " Please re-upload when you can."))
        db.add_message(lead_id, "site", msg)
        _notify_applicant_message(lead, msg)
        flash("Record sent back for re-upload.", "success")
    return redirect(back)


@app.route("/app/leads/broadcast", methods=["POST"])
@login_required
def broadcast_leads():
    """Fan a message (and optional task/document) out to accepted candidates -
    all of them, or scoped to one trial (nct) - delivered to each private thread,
    never a shared room (who else is enrolled is PHI). Scoping by trial is what
    lets a coordinator send a form to 'everyone in this study' without hunting."""
    body = request.form.get("body", "").strip()
    task_title = request.form.get("task", "").strip()
    scope_nct = request.form.get("nct", "").strip()
    back = _safe_next(request.form.get("next", "")) or url_for("leads")
    # Broadcasts are strictly single-trial: protocol materials are IRB/REB-approved
    # per study, so cross-trial sends are disallowed (avoids wrong-cohort mistakes).
    if not scope_nct or scope_nct not in db.user_claimed_ncts(g.user["id"]):
        flash("Pick a trial to message.", "error")
        return redirect(back)
    saved = _save_upload(request.files.get("file"))
    if not (body or task_title or saved):
        flash("Add a message, a to-do, or a document to send.", "error")
        return redirect(back)
    leads = [ld for ld in db.list_leads_for_user(g.user["id"])
             if ld["revealed"] and ld["status"] not in db.LEAD_CLOSED
             and ld["nct"] == scope_nct]
    if not leads:
        flash("No accepted candidates in that trial yet.", "error")
        return redirect(back)
    for ld in leads:
        if body:
            db.add_message(ld["id"], "site", body)
            _notify_applicant_message(ld, body)
        if task_title:
            db.add_task(ld["id"], task_title, assigned_to="patient",
                        created_by="site")
            db.add_message(ld["id"], "site", f"New to-do for you: {task_title}")
        if saved:
            orig, stored, mime, size = saved
            # Each recipient gets their own stored copy so deletes never leak.
            copy_saved = _copy_stored_file(stored, orig)
            if copy_saved:
                c_orig, c_stored, c_mime, c_size = copy_saved
                db.add_attachment(ld["id"], "site", c_orig, c_stored, c_mime,
                                  c_size)
                db.add_message(ld["id"], "site", f"Shared a document: {c_orig}")
    if saved:
        # Remove the original temp copy; recipients hold their own copies.
        try:
            (UPLOAD_DIR / os.path.basename(saved[1])).unlink(missing_ok=True)
        except OSError:
            pass
    flash(f"Sent to {len(leads)} accepted candidate"
          f"{'s' if len(leads) != 1 else ''}.", "success")
    return redirect(back)


@app.route("/app/leads/<int:lead_id>/tag-toggle", methods=["POST"])
@login_required
def toggle_conv_tag(lead_id):
    """Toggle a quick conversation tag (pin / needs consent / awaiting docs /
    follow up) so coordinators can flag and later filter threads."""
    _ensure_site_access_for_lead(lead_id)
    back = _lead_action_return()
    tag = request.form.get("tag", "").strip()
    if db.toggle_lead_tag(lead_id, tag) is None:
        flash("Couldn't update that tag.", "error")
    return redirect(back)


@app.route("/files/lead/<int:att_id>")
def download_lead_file(att_id):
    """Serve a thread document to either side, access-controlled per lead."""
    att = db.get_attachment(att_id)
    if not att:
        abort(404)
    lead = db.get_lead(att["lead_id"])
    if not lead:
        abort(404)
    site_ok = bool(g.user and db.lead_belongs_to_user(lead["id"], g.user["id"]))
    patient_ok = bool(g.patient_user
                      and lead["applicant_token"] == get_applicant_token())
    if not (site_ok or patient_ok):
        abort(403)
    return _send_stored_file(att["stored_name"], att["orig_name"])


@app.route("/app/trial/<nct>/message", methods=["POST"])
@login_required
def team_channel_post(nct):
    """Post to a trial's internal (staff-only) team channel. Not patient-visible
    - trial-specific coordination that lives next to the patients it's about."""
    if nct not in db.user_claimed_ncts(g.user["id"]):
        abort(403)
    back = (_safe_next(request.form.get("next", ""))
            or url_for("patient_inbox", nct=nct, team=1))
    body = request.form.get("body", "").strip()
    saved = _save_upload(request.files.get("file"))
    if not (body or saved):
        flash("Write a message or attach a file.", "error")
        return redirect(back)
    label = body or (f"Shared a file: {saved[0]}" if saved else "")
    mid = db.add_team_message(nct, g.user["id"], g.user["name"], label)
    if saved and mid:
        orig, stored, mime, size = saved
        db.add_team_attachment(mid, orig, stored, mime, size)
    flash("Posted to the team channel.", "success")
    return redirect(back)


@app.route("/files/team/<int:att_id>")
@login_required
def download_team_file(att_id):
    """Serve an internal team-channel attachment, scoped to the user's trials."""
    att = db.get_team_attachment(att_id)
    if not att:
        abort(404)
    if att["nct"] not in db.user_claimed_ncts(g.user["id"]):
        abort(403)
    return _send_stored_file(att["stored_name"], att["orig_name"])


def _inbox_time_label(ts):
    """Human, glanceable timestamp for the conversation list: time today, a
    weekday this week, else a short date (so a 3-day-old thread never reads as
    'just now')."""
    if not ts:
        return ""
    try:
        when = dt.datetime.strptime(ts[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return ts
    now = dt.datetime.now()
    if when.date() == now.date():
        return when.strftime("%-I:%M %p")
    if (now.date() - when.date()).days < 7:
        return when.strftime("%a")
    if when.year == now.year:
        return when.strftime("%b %-d")
    return when.strftime("%b %-d, %Y")


@app.route("/app/messages")
@login_required
def patient_inbox():
    """Trial-organized conversations: each trial is a channel with an Internal
    (staff-only) team lane and an External lane of per-patient threads. Patient
    threads are the wedge (talking *with participants* moves contacted ->
    screened -> enrolled -> retained); the internal lane keeps trial-specific
    coordination next to the people it's about."""
    uid = g.user["id"]
    claims = db.list_study_claims(uid)
    title_by_nct = {c["nct"]: (c["title"] or c["nct"]) for c in claims}
    rows = db.list_leads_for_user(uid)

    patients_by_nct = defaultdict(list)
    for r in rows:
        if not r["revealed"] or r["status"] in db.LEAD_CLOSED:
            continue
        msgs = db.get_messages(r["id"])
        last = msgs[-1] if msgs else None
        last_at = (last["created_at"] if last else r["updated_at"]) or ""
        tags = db.lead_tags(r)
        patients_by_nct[r["nct"]].append({
            "lead": r,
            "code": candidate_code(r),
            "last_at": last_at,
            "last_label": _inbox_time_label(last_at),
            "preview": (last["body"] if last else "No messages yet"),
            # "You:" only when the coordinator sent last - not system notices.
            "preview_mine": bool(last and last["sender"] == "site"),
            "unread": db.lead_unread_for_site(r["id"]),
            # The patient spoke last and we haven't replied -> our turn.
            "awaiting_reply": bool(last and last["sender"] == "patient"),
            "open_tasks": db.open_task_count(r["id"], "patient"),
            "tags": tags,
            "pinned": "pinned" in tags,
        })

    # Trial order: claimed studies first, then any NCT that has patients.
    ncts = list(dict.fromkeys([c["nct"] for c in claims]
                              + list(patients_by_nct.keys())))
    trials = []
    for nct in ncts:
        # Triage: pinned first, then unread, then "your turn", then most recent.
        pats = sorted(patients_by_nct.get(nct, []),
                      key=lambda c: (c["pinned"], c["unread"] > 0,
                                     c["awaiting_reply"], c["last_at"]),
                      reverse=True)
        trials.append({
            "nct": nct,
            "title": title_by_nct.get(nct, nct),
            "patients": pats,
            "patient_unread": sum(1 for p in pats if p["unread"]),
        })

    valid_ncts = {t["nct"] for t in trials}
    sel_lead = request.args.get("lead_id", type=int)
    sel_nct = request.args.get("nct")

    # Gmail-style: only open a thread when a specific patient is chosen; the
    # default landing is the inbox LIST for whichever trial "account" is open.
    active = None
    if sel_lead:
        for t in trials:
            match = next((p for p in t["patients"]
                          if p["lead"]["id"] == sel_lead), None)
            if match:
                active = {"kind": "patient", "nct": t["nct"], "convo": match}
                break

    # Which trial inbox is open (the Gmail "account"): the opened patient's
    # trial, else an explicit ?nct=, else the first trial.
    if active:
        open_nct = active["nct"]
    elif sel_nct in valid_ncts:
        open_nct = sel_nct
    else:
        open_nct = trials[0]["nct"] if trials else None

    detail = None
    if active:
        p = active["convo"]
        lid = p["lead"]["id"]
        detail = {
            "kind": "patient", "nct": active["nct"],
            "lead": p["lead"], "code": p["code"],
            "messages": db.get_messages(lid),
            "files": db.list_attachments(lid),
            "tags": p["tags"],
        }
        db.mark_thread_read(lid, "site")
        p["unread"] = 0  # reflect the read in the rail

    return render_template("messages.html", trials=trials, active=detail,
                           open_nct=open_nct, labels=db.LEAD_LABELS)


@app.route("/app/leads/<int:lead_id>/coverage-check", methods=["POST"])
@login_required
def coverage_check_lead(lead_id):
    _ensure_site_access_for_lead(lead_id)
    lead = db.get_lead(lead_id)
    if not lead:
        flash("Couldn't find that candidate.", "error")
        return redirect(url_for("leads"))
    existing = db.get_lead_support(lead_id).get("coverage_payload") or {}
    info = {
        "payer_name": request.form.get("payer_name", "").strip()
        or existing.get("payer_name", ""),
        "member_id": request.form.get("member_id", "").strip()
        or existing.get("member_id", ""),
        "group_id": request.form.get("group_id", "").strip()
        or existing.get("group_id", ""),
        "zip": request.form.get("zip", "").strip() or existing.get("patient_zip", ""),
        "dob_year": request.form.get("dob_year", "").strip()
        or existing.get("dob_year", ""),
    }
    res = payer.check(lead, info)
    db.set_lead_coverage_check(
        lead_id, res.get("status", ""), res.get("note", ""),
        payload=res.get("payload"), provider=res.get("provider", ""),
        ref=res.get("reference", ""))
    flash("Coverage check refreshed.", "success")
    return redirect(url_for("leads"))


@app.route("/app/leads/<int:lead_id>/travel-plan", methods=["POST"])
@login_required
def travel_plan_lead(lead_id):
    _ensure_site_access_for_lead(lead_id)
    lead = db.get_lead(lead_id)
    if not lead:
        flash("Couldn't find that candidate.", "error")
        return redirect(url_for("leads"))
    existing = db.get_lead_support(lead_id).get("travel_payload") or {}
    info = {
        "distance": request.form.get("distance", "").strip()
        or existing.get("distance", 0),
        "preferred_mode": request.form.get("preferred_mode", "").strip()
        or existing.get("preferred_mode", ""),
        "needs": request.form.get("needs", "").strip() or existing.get("needs", ""),
        "city": request.form.get("city", "").strip()
        or existing.get("city", "") or lead.get("location", ""),
    }
    res = logistics.plan(lead, info)
    db.set_lead_travel_check(
        lead_id, res.get("status", ""), res.get("note", ""),
        payload=res.get("payload"), provider=res.get("provider", ""),
        ref=res.get("reference", ""))
    flash("Travel planning refreshed.", "success")
    return redirect(url_for("leads"))


@app.route("/c/<token>/schedule", methods=["POST"])
def candidate_schedule(token):
    url = request.form.get("schedule_url", "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    lead = _site_token_lead_or_none(token)
    if not lead:
        abort(410)
    lead = db.set_lead_schedule(lead["id"], url)
    if url:
        _notify_applicant_schedule(lead)
        flash("Booking link sent to the applicant.", "ok")
    else:
        flash("Booking link removed.", "ok")
    return redirect(url_for("candidate_page", token=lead["site_token"]))


@app.route("/app/leads/<int:lead_id>/redcap", methods=["POST"])
@login_required
def push_lead_redcap(lead_id):
    _ensure_site_access_for_lead(lead_id)
    """Drop the candidate into the site's REDCap project so the coordinator
    doesn't re-type anything. No-op with a helpful message until configured."""
    lead = db.get_lead(lead_id)
    if not lead:
        flash("Couldn't find that candidate.", "error")
        return redirect(url_for("leads"))
    cfg = redcap.config_from_profile(db.get_site_profile(g.user["id"]))
    ok, msg = redcap.push_candidate(lead, cfg)
    if ok:
        db.update_lead_status(lead_id, lead["status"], "pushed to REDCap",
                              actor="you")
    flash(msg, "success" if ok else "error")
    return redirect(url_for("leads"))


@app.route("/app/leads/<int:lead_id>/reconcile", methods=["POST"])
@login_required
def reconcile_lead(lead_id):
    """Write auditable enrollment/retention proof for a candidate."""
    _ensure_site_access_for_lead(lead_id)
    outcome = request.form.get("outcome", "").strip()
    src = request.form.get("source_system", "").strip()
    ref = request.form.get("source_ref", "").strip()
    note = request.form.get("note", "").strip()
    actor = g.user["email"] if g.user and g.user["email"] else "site"
    if db.add_reconciliation(lead_id, outcome, src, ref, note, actor=actor):
        flash(f"Saved: {db.RECON_LABELS.get(outcome, outcome)}.", "success")
    else:
        flash("Couldn't save that verification update.", "error")
    return redirect(url_for("leads"))


@app.route("/app/leads/<int:lead_id>/site-token/revoke", methods=["POST"])
@login_required
def revoke_lead_site_token(lead_id):
    _ensure_site_access_for_lead(lead_id)
    if db.revoke_site_token(lead_id, revoked=True):
        db.update_lead_status(
            lead_id, db.get_lead(lead_id)["status"],
            "secure study-team link revoked", actor="you")
        flash("Secure candidate link revoked.", "success")
    else:
        flash("Couldn't revoke secure link.", "error")
    return redirect(url_for("leads"))


@app.route("/app/leads/<int:lead_id>/site-token/extend", methods=["POST"])
@login_required
def extend_lead_site_token(lead_id):
    _ensure_site_access_for_lead(lead_id)
    days = request.form.get("days", "").strip()
    if db.extend_site_token(lead_id, days):
        db.update_lead_status(
            lead_id, db.get_lead(lead_id)["status"],
            "secure study-team link extended", actor="you")
        flash("Secure candidate link extended.", "success")
    else:
        flash("Couldn't extend secure link.", "error")
    return redirect(url_for("leads"))


@app.route("/app/leads/<int:lead_id>/status", methods=["POST"])
@login_required
def update_lead(lead_id):
    _ensure_site_access_for_lead(lead_id)
    status = request.form.get("status", "").strip()
    note = request.form.get("note", "").strip()
    if db.update_lead_status(lead_id, status, note, actor="you"):
        if status in ("screening", "enrolled"):
            _notify_applicant_by_id(lead_id, status)
        flash(f"Application moved to \"{db.LEAD_LABELS.get(status, status)}\".",
              "success")
    else:
        flash("Couldn't update that application.", "error")
    return redirect(url_for("leads"))


@app.route("/googlea0e509518fe5134f.html")
def google_site_verification():
    """Google Search Console ownership check (HTML-file method). These files
    always contain exactly one line: 'google-site-verification: <filename>'."""
    return app.response_class(
        "google-site-verification: googlea0e509518fe5134f.html\n",
        mimetype="text/html")


@app.route("/robots.txt")
def robots():
    body = ("User-agent: *\nAllow: /\nSitemap: "
            + url_for("sitemap", _external=True) + "\n")
    return app.response_class(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    # Static + how-it-works, then the full programmatic SEO surface: one page per
    # condition, plus one per condition x city (real local trials on each).
    def loc(endpoint, **kw):
        return _abs_url(endpoint, **kw) if PUBLIC_BASE_URL \
            else url_for(endpoint, _external=True, **kw)
    # lastmod gives crawlers a freshness signal (important when "recruiting"
    # status is time-sensitive). Programmatic pages recompute continuously, so
    # they carry today's date; study pages carry when we last saw the trial.
    today = time.strftime("%Y-%m-%d")
    urls = [(loc("home"), today), (loc("find"), today),
            (loc("how_it_works"), today)]
    for c in SEO_CONDITIONS:
        cslug = slugify(c)
        urls.append((loc("condition_page", slug=cslug), today))
        for city in SEO_CITIES:
            urls.append((loc("condition_city_page", slug=cslug,
                             city_slug=slugify(city)), today))
    # Per-study pages, scoped to trials surfaced by our condition/city pages.
    try:
        for s in db.list_seo_studies(limit=5000):
            lm = ((s["updated_at"] or today)[:10]) or today
            urls.append((loc("study_page", nct=s["nct"]), lm))
    except Exception:
        app.logger.exception("sitemap study list failed")
    items = "".join(
        f"<url><loc>{u}</loc><lastmod>{lm}</lastmod></url>" for u, lm in urls)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + items + "</urlset>")
    return app.response_class(xml, mimetype="application/xml")


# --------------------------------------------------------------------------- #
# Location: geocoding + distance so we can rank by "nearby"
# --------------------------------------------------------------------------- #
import re as _re

_CA_POSTAL = _re.compile(r"^([A-Za-z]\d[A-Za-z])\s*\d[A-Za-z]\d$")   # L6P 3N6
_CA_FSA = _re.compile(r"^[A-Za-z]\d[A-Za-z]$")                        # L6P
_US_ZIP = _re.compile(r"^\d{5}$")                                    # 90210


def _zippopotam(country, code):
    """Resolve a postal code via zippopotam.us. Returns (lat, lon, cc) or None."""
    try:
        req = urllib.request.Request(
            f"https://api.zippopotam.us/{country}/{urllib.parse.quote(code)}",
            headers={"User-Agent": "BridgeMD/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        p = (data.get("places") or [None])[0]
        if p:
            return float(p["latitude"]), float(p["longitude"]), country.upper()
    except Exception:
        pass
    return None


def _nominatim(query):
    """Free-text geocode. Returns (lat, lon, country_code) or None."""
    try:
        q = urllib.parse.urlencode({"q": query, "format": "json", "limit": "1",
                                    "addressdetails": "1"})
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/search?{q}",
            headers={"User-Agent": "BridgeMD/1.0 (clinical trial finder)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        if data:
            cc = (data[0].get("address", {}).get("country_code") or "").upper()
            return float(data[0]["lat"]), float(data[0]["lon"]), cc
    except Exception:
        pass
    return None


def _nominatim_suggest(query, limit=6):
    """Free-text location suggestions for typeahead address entry."""
    q = (query or "").strip()
    if len(q) < 3:
        return []
    try:
        params = urllib.parse.urlencode({
            "q": q,
            "format": "json",
            "limit": str(max(1, min(limit, 10))),
            "addressdetails": "1",
        })
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/search?{params}",
            headers={"User-Agent": "BridgeMD/1.0 (clinical trial finder)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        out = []
        for it in data or []:
            a = it.get("address", {})
            city = (a.get("city") or a.get("town") or a.get("village")
                    or a.get("municipality") or a.get("county") or "")
            cc = (a.get("country_code") or "").upper()
            region = a.get("state") or a.get("country") or ""
            parts = [p for p in (city, region) if p]
            label = ", ".join(parts)
            postcode = a.get("postcode") or ""
            if cc == "US" and postcode:
                label = (label + " " + postcode).strip(", ").strip()
            if not label:
                label = (it.get("display_name") or "").split(",")[0:3]
                label = ", ".join([x.strip() for x in label if str(x).strip()])
            out.append({
                "label": label or it.get("display_name") or "",
                "lat": float(it.get("lat")),
                "lon": float(it.get("lon")),
                "cc": cc,
            })
        return [x for x in out if x.get("label")]
    except Exception:
        return []


def geocode(place):
    """City / postal code / ZIP / address -> (lat, lon, country_code) or None.
    Postal codes are handled first (Nominatim is unreliable for them)."""
    p = (place or "").strip()
    if not p:
        return None
    up = p.upper()

    m = _CA_POSTAL.match(up)
    if m:
        r = _zippopotam("CA", m.group(1))
        if r:
            return r
    elif _CA_FSA.match(up):
        r = _zippopotam("CA", up)
        if r:
            return r
    elif _US_ZIP.match(up):
        r = _zippopotam("US", up)
        if r:
            return r

    # Google first (higher quality), then Nominatim as a resilient fallback.
    r = _google_geocode(p)
    if r:
        return r
    r = _nominatim(p)
    if r:
        return r
    if _CA_POSTAL.match(up):
        return _nominatim(f"{up[:3]} {up[3:]}, Canada")
    return None


def _nominatim_reverse(lat, lon):
    """(lat, lon) -> (label, country_code, country_name). Best-effort place."""
    try:
        q = urllib.parse.urlencode({"lat": lat, "lon": lon, "format": "json",
                                    "zoom": "12", "addressdetails": "1"})
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/reverse?{q}",
            headers={"User-Agent": "BridgeMD/1.0 (clinical trial finder)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        a = data.get("address", {})
        city = (a.get("city") or a.get("town") or a.get("village")
                or a.get("municipality") or a.get("county") or "")
        cc = (a.get("country_code") or "").upper()
        country = a.get("country") or ""
        # Prefer a clean "City, Region" (e.g. "Toronto, Ontario"); fall back to
        # the country when there's no state/province. For US, append the ZIP so
        # it reads like "New York, New York 10014".
        region = a.get("state") or country or ""
        parts = [p for p in (city, region) if p]
        label = ", ".join(parts)
        postcode = a.get("postcode") or ""
        if cc == "US" and postcode:
            label = (label + " " + postcode).strip(", ").strip()
        label = label or data.get("display_name", "")
        return label, cc, country
    except Exception:
        return "", "", ""


def units_for(country_code):
    """US uses miles; everyone else (incl. Canada) uses kilometres."""
    return "mi" if (country_code or "").upper() == "US" else "km"


@app.route("/geo/reverse")
def geo_reverse():
    # Public helper: the patient-facing search form (no login) reverse-geocodes
    # the browser's coordinates so the location field shows a real place name
    # instead of a raw "Current location" placeholder. No user data involved.
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"ok": False}), 400
    res = _google_reverse(lat, lon)
    provider = "google"
    if res and res[0]:
        label, cc, country = res
    else:
        provider = "nominatim"
        label, cc, country = _nominatim_reverse(lat, lon)
    out = {"ok": bool(label), "label": label, "cc": cc,
           "country": country, "unit": units_for(cc)}
    if request.args.get("debug") == "1":
        out["provider"] = provider
        out["google_configured"] = bool(GOOGLE_MAPS_API_KEY)
        if provider != "google":
            out["google_error"] = _LAST_GEO_ERROR.get("reverse", "n/a")
    return jsonify(out)


# Optional Google Places (New) typeahead. When the key is present we use Google
# for nicer suggestions; otherwise everything falls back to Nominatim so the
# feature keeps working with no key and no billing.
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
_GOOGLE_PLACES_BASE = "https://places.googleapis.com/v1"
_GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
# Last Google error (for diagnostics via /geo/suggest?debug=1). Not secret:
# holds Google's own error text (e.g. referrer/billing/API-not-enabled).
_LAST_GEO_ERROR = {}


def _capture_geo_error(kind, e):
    """Extract a human-readable reason from a urllib error, including the HTTP
    response body Google returns (which names the actual misconfiguration)."""
    detail = ""
    if hasattr(e, "read"):
        try:
            detail = e.read().decode("utf-8", "replace")[:600]
        except Exception:
            detail = ""
    code = getattr(e, "code", "")
    msg = (f"HTTP {code}: " if code else "") + (detail or str(e))
    _LAST_GEO_ERROR[kind] = msg
    app.logger.error("google places %s failed: %s", kind, msg)


def _google_places_suggest(query, session_token="", limit=6):
    """Google Places Autocomplete (New). Returns [{label, place_id}] (no coords -
    coordinates are resolved lazily on selection to stay in the free tier).
    Returns [] on any error so callers can fall back to Nominatim."""
    q = (query or "").strip()
    if len(q) < 3 or not GOOGLE_MAPS_API_KEY:
        return []
    try:
        body = {"input": q}
        if session_token:
            body["sessionToken"] = session_token
        req = urllib.request.Request(
            f"{_GOOGLE_PLACES_BASE}/places:autocomplete",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        out = []
        for s in data.get("suggestions", []):
            pred = s.get("placePrediction") or {}
            label = ((pred.get("text") or {}).get("text") or "").strip()
            pid = (pred.get("placeId") or "").strip()
            if label and pid:
                out.append({"label": label, "place_id": pid})
            if len(out) >= limit:
                break
        _LAST_GEO_ERROR.pop("suggest", None)
        return out
    except Exception as e:
        _capture_geo_error("suggest", e)
        return []


def _google_place_latlon(place_id, session_token=""):
    """Resolve a Google place_id to {lat, lon, cc} via Place Details (New).
    One call per selected suggestion (session-token model). None on error."""
    pid = (place_id or "").strip()
    if not pid or not GOOGLE_MAPS_API_KEY:
        return None
    try:
        url = f"{_GOOGLE_PLACES_BASE}/places/{urllib.parse.quote(pid)}"
        if session_token:
            url += "?sessionToken=" + urllib.parse.quote(session_token)
        req = urllib.request.Request(
            url,
            headers={"X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                     "X-Goog-FieldMask": "location,addressComponents"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        loc = data.get("location") or {}
        lat, lon = loc.get("latitude"), loc.get("longitude")
        if lat is None or lon is None:
            return None
        cc = ""
        for comp in data.get("addressComponents", []):
            if "country" in (comp.get("types") or []):
                cc = (comp.get("shortText") or "").upper()
                break
        _LAST_GEO_ERROR.pop("place", None)
        return {"lat": float(lat), "lon": float(lon), "cc": cc}
    except Exception as e:
        _capture_geo_error("place", e)
        return None


def _google_geocode(place):
    """Forward geocode free text -> (lat, lon, cc) via Places API (New) Text
    Search. Returns None on any error so the caller falls back to Nominatim."""
    q = (place or "").strip()
    if len(q) < 2 or not GOOGLE_MAPS_API_KEY:
        return None
    try:
        body = {"textQuery": q, "maxResultCount": 1}
        req = urllib.request.Request(
            f"{_GOOGLE_PLACES_BASE}/places:searchText",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                     "X-Goog-FieldMask": "places.location,places.addressComponents"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        places = data.get("places") or []
        if not places:
            return None
        loc = (places[0].get("location") or {})
        lat, lon = loc.get("latitude"), loc.get("longitude")
        if lat is None or lon is None:
            return None
        cc = ""
        for comp in places[0].get("addressComponents", []):
            if "country" in (comp.get("types") or []):
                cc = (comp.get("shortText") or "").upper()
                break
        _LAST_GEO_ERROR.pop("geocode", None)
        return float(lat), float(lon), cc
    except Exception as e:
        _capture_geo_error("geocode", e)
        return None


def _google_reverse(lat, lon):
    """Reverse geocode (lat, lon) -> (label, cc, country) via the Geocoding API.
    Builds a concise 'City, Region' label. None on error (caller falls back)."""
    if not GOOGLE_MAPS_API_KEY:
        return None
    try:
        params = urllib.parse.urlencode({"latlng": f"{lat},{lon}",
                                         "key": GOOGLE_MAPS_API_KEY})
        req = urllib.request.Request(f"{_GOOGLE_GEOCODE_URL}?{params}")
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        status = data.get("status", "")
        if status != "OK":
            _LAST_GEO_ERROR["reverse"] = (
                status + " " + (data.get("error_message", "") or "")).strip()
            return None
        results = data.get("results") or []
        if not results:
            return None
        comps = results[0].get("address_components", [])

        def _comp(type_):
            for c in comps:
                if type_ in (c.get("types") or []):
                    return c
            return None
        city_c = (_comp("locality") or _comp("postal_town")
                  or _comp("sublocality") or _comp("administrative_area_level_2"))
        region_c = _comp("administrative_area_level_1")
        country_c = _comp("country")
        postal_c = _comp("postal_code")
        city = (city_c or {}).get("long_name", "")
        region = (region_c or {}).get("long_name", "")
        country = (country_c or {}).get("long_name", "")
        cc = ((country_c or {}).get("short_name") or "").upper()
        parts = [p for p in (city, region or country) if p]
        label = ", ".join(parts)
        postcode = (postal_c or {}).get("long_name", "")
        if cc == "US" and postcode:
            label = (label + " " + postcode).strip(", ").strip()
        label = label or results[0].get("formatted_address", "")
        _LAST_GEO_ERROR.pop("reverse", None)
        return label, cc, country
    except Exception as e:
        _capture_geo_error("reverse", e)
        return None


@app.route("/geo/suggest")
def geo_suggest():
    # Public helper for patient search: lightweight typeahead while entering a
    # location manually. Uses Google Places when configured (coords resolved on
    # selection via /geo/place), otherwise Nominatim (coords included inline).
    q = request.args.get("q", "")
    session = request.args.get("session", "")
    debug = request.args.get("debug") == "1"
    if GOOGLE_MAPS_API_KEY:
        items = _google_places_suggest(q, session_token=session, limit=6)
        if items:
            return jsonify({"ok": True, "provider": "google", "items": items})
        if debug:
            # Diagnose why Google returned nothing (key config, billing, etc.).
            return jsonify({
                "ok": True, "provider": "nominatim_fallback",
                "google_configured": True,
                "google_key_len": len(GOOGLE_MAPS_API_KEY),
                "google_error": _LAST_GEO_ERROR.get("suggest", "no error (empty result set)"),
                "items": _nominatim_suggest(q, limit=6)})
    items = _nominatim_suggest(q, limit=6)
    if debug:
        return jsonify({"ok": True, "provider": "nominatim",
                        "google_configured": bool(GOOGLE_MAPS_API_KEY),
                        "items": items})
    return jsonify({"ok": True, "provider": "nominatim", "items": items})


@app.route("/geo/place")
def geo_place():
    # Resolve a Google place_id (from /geo/suggest) to coordinates. Kept separate
    # so we only pay for Place Details when a user actually picks a suggestion.
    place_id = request.args.get("place_id", "")
    session = request.args.get("session", "")
    res = _google_place_latlon(place_id, session_token=session)
    if not res:
        return jsonify({"ok": False}), 404
    return jsonify({"ok": True, **res})


def haversine(lat1, lon1, lat2, lon2, unit="km"):
    r = 3958.8 if unit == "mi" else 6371.0   # earth radius in mi / km
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _is_active_site(s):
    """A site the patient could actually enroll at (recruiting or unlabeled)."""
    return (s.get("status") or "").upper() in ("", "RECRUITING")


def nearby_sites(trial, lat, lon, unit, radius):
    """Active sites with geo within radius, each annotated with distance,
    sorted nearest-first. Returns [] if none."""
    out = []
    for s in trial.get("locations", []):
        if s.get("lat") is None or s.get("lon") is None or not _is_active_site(s):
            continue
        d = haversine(lat, lon, s["lat"], s["lon"], unit)
        if radius and d > radius:
            continue
        out.append({**s, "distance": d, "contact": _coordinator(trial, s)})
    out.sort(key=lambda x: x["distance"])
    return out


_PAY_KEYWORDS = (
    "compensation", "compensated", "stipend", "payment", "paid", "reimburse",
    "reimbursement", "travel reimbursement", "gift card", "honorarium",
)
_TRAVEL_SUPPORT_KEYWORDS = (
    "travel reimbursement", "travel support", "travel assistance",
    "transportation provided", "parking voucher", "hotel provided",
    "lodging provided", "meal voucher", "ride share", "rideshare",
)
_COVERAGE_KEYWORDS = (
    "study covers", "at no cost", "no cost to participant", "no-cost",
    "costs covered", "covered by sponsor", "sponsor pays",
    "insurance not required",
)
_STIPEND_KEYWORDS = (
    "stipend", "gift card", "debit card", "honorarium", "paid per visit",
    "payment per visit", "participant payment", "compensated",
)
_HIGH_BURDEN_KEYWORDS = (
    "inpatient", "residential", "overnight", "confinement",
    "admit", "admission",
)
_EARLY_PHASE_KEYWORDS = ("phase 1", "phase i", "early phase 1", "first in human")
_PAY_MONEY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")


def _pay_likelihood(trial):
    """Conservative compensation-likelihood signal from CT.gov free text.

    CT.gov has no reliable structured "payment amount" field, so we infer
    likelihood from explicit wording and burden signals. This is intentionally
    non-decisive (helper only), and default ranking remains unchanged.
    """
    text = " ".join([
        trial.get("title", ""),
        trial.get("briefSummary", ""),
        trial.get("detailedDescription", ""),
        trial.get("criteria", ""),
    ])
    low = text.lower()
    score, notes = 0, []
    if "no compensation" in low or "not compensated" in low:
        return {"score": 0, "tier": "none", "label": "", "notes": []}
    if _PAY_MONEY_RE.search(text):
        score += 3
        notes.append("Amount mentioned in study text")
    if any(k in low for k in _PAY_KEYWORDS):
        score += 2
        notes.append("Compensation/reimbursement mentioned")
    if any(k in low for k in _HIGH_BURDEN_KEYWORDS):
        score += 2
        notes.append("Higher-burden participation terms found")
    phase = (trial.get("phase") or "").lower()
    if any(k in phase for k in _EARLY_PHASE_KEYWORDS):
        score += 2
        notes.append("Early phase study")
    if str(trial.get("healthyVolunteers", "")).upper() == "YES":
        score += 1
        notes.append("Healthy-volunteer study")

    if score >= 7:
        tier, label = "high", "Higher-pay potential"
    elif score >= 4:
        tier, label = "likely", "Compensation likely"
    elif score >= 1:
        tier, label = "mentioned", "Compensation mentioned"
    else:
        tier, label = "none", ""
    return {"score": score, "tier": tier, "label": label, "notes": notes[:3]}


def _participant_support_signal(trial):
    """Detect patient-friendly support signals from trial text."""
    text = " ".join([
        trial.get("title", ""),
        trial.get("briefSummary", ""),
        trial.get("detailedDescription", ""),
        trial.get("criteria", ""),
    ])
    low = text.lower()
    travel = any(k in low for k in _TRAVEL_SUPPORT_KEYWORDS)
    coverage = any(k in low for k in _COVERAGE_KEYWORDS)
    stipend = any(k in low for k in _STIPEND_KEYWORDS) or bool(_PAY_MONEY_RE.search(text))
    notes = []
    if travel:
        notes.append("Travel help or reimbursement mentioned")
    if coverage:
        notes.append("Some study costs appear covered")
    if stipend:
        notes.append("Stipend/payment signal mentioned")
    label_bits = []
    if travel:
        label_bits.append("Travel support")
    if coverage:
        label_bits.append("Cost coverage")
    if stipend:
        label_bits.append("Compensation")
    return {
        "travel": travel,
        "coverage": coverage,
        "stipend": stipend,
        "label": " · ".join(label_bits),
        "notes": notes[:3],
    }


def _match_quality(match):
    """Internal quality score for LLM eligibility outputs.

    This catches contradictions (e.g. "you may qualify" + clear blockers) and
    low-signal model copy before it is shown to end users.
    """
    m = mt.normalize_match(match or {})
    rationale = (m.get("rationale") or "").strip()
    low = rationale.lower()
    score = 100
    flags = []

    pos_terms = ("may qualify", "might qualify", "could qualify",
                 "good fit", "likely eligible", "eligible")
    neg_terms = ("disqualif", "ineligible", "not eligible", "fails", "barrier")
    has_pos = any(t in low for t in pos_terms)
    has_neg = any(t in low for t in neg_terms)
    has_blockers = bool(m.get("not_met"))

    if len(rationale) < 20:
        score -= 35
        flags.append("rationale_too_short")
    if not (m.get("met") or m.get("unknown") or m.get("not_met")):
        score -= 40
        flags.append("no_criteria_breakdown")
    if m.get("verdict") == "likely_eligible" and has_blockers:
        score -= 45
        flags.append("eligible_with_blockers")
    if has_blockers and has_pos:
        score -= 40
        flags.append("positive_rationale_with_blockers")
    if m.get("verdict") == "unlikely" and has_pos:
        score -= 30
        flags.append("unlikely_with_positive_rationale")
    if m.get("verdict") == "likely_eligible" and has_neg:
        score -= 35
        flags.append("likely_with_negative_rationale")
    if m.get("verdict") == "possible" and has_blockers:
        score -= 10
        flags.append("possible_despite_blockers")
    if len(m.get("unknown") or []) > 8:
        score -= 10
        flags.append("too_many_unknowns")

    return {"score": max(0, min(100, score)), "flags": flags}


def _quality_gated_match(match):
    """Return normalized + quality-gated eligibility output for UI safety."""
    m = mt.normalize_match(match or {})
    q = _match_quality(m)
    out = dict(m)
    out["quality_score"] = q["score"]
    out["quality_flags"] = q["flags"]
    out["quality_ok"] = q["score"] >= MATCH_QUALITY_MIN

    if out["quality_ok"]:
        return out

    blockers = [x for x in (m.get("not_met") or []) if str(x).strip()]
    unknown = [x for x in (m.get("unknown") or []) if str(x).strip()]
    if blockers:
        r = ("Potential blocker identified: "
             f"{blockers[0]}. Please confirm eligibility with the study team.")
        out.update({"verdict": "unlikely", "score": min(int(m.get("score") or 0), 35),
                    "rationale": r})
    elif unknown:
        r = ("Eligibility needs confirmation: "
             f"{unknown[0]}. The study team can verify this quickly.")
        out.update({"verdict": "possible", "score": min(int(m.get("score") or 0), 55),
                    "rationale": r})
    else:
        out.update({"verdict": "possible", "score": 45,
                    "rationale": ("Eligibility needs manual confirmation with the "
                                  "study team before applying.")})
    return out


def _query_tokens(*parts):
    txt = " ".join((p or "") for p in parts).strip().lower()
    return {t for t in re.split(r"[^a-z0-9]+", txt) if len(t) >= 3}


def _site_posted_rank(study, condition, intervention):
    pool = _query_tokens(condition, intervention)
    hay = _query_tokens(study.get("title", ""), study.get("condition", ""),
                        study.get("brief_summary", ""),
                        study.get("eligibility", ""))
    if not pool:
        return 0
    overlap = pool & hay
    if not overlap:
        return 0
    score = len(overlap) * 8
    if (study.get("condition") or "").strip():
        cond = (study.get("condition") or "").lower()
        if any(tok in cond for tok in pool):
            score += 12
    title = (study.get("title") or "").lower()
    if any(tok in title for tok in pool):
        score += 10
    return score


def _site_posted_as_trial(study):
    posted_contacts = []
    if (study.get("profile_contact_name") or study.get("contact_email") or
            study.get("contact_phone")):
        posted_contacts.append({
            "name": study.get("profile_contact_name") or "Study contact",
            "email": study.get("contact_email") or "",
            "phone": study.get("contact_phone") or "",
            "role": "STUDY_COORDINATOR",
        })
    return {
        "nctId": study["nct"],
        "title": study["title"],
        "phase": study.get("phase") or "",
        "leadSponsor": study.get("org_name") or study.get("site_name") or "",
        "studyType": "INTERVENTIONAL",
        "overallStatus": "RECRUITING",
        "briefSummary": study.get("brief_summary") or "",
        "eligibilityCriteria": study.get("eligibility") or "",
        "conditions": [study.get("condition")] if study.get("condition") else [],
        "locations": [{
            "facility": study.get("site_name") or study.get("org_name") or "",
            "city": study.get("location") or "",
            "status": "RECRUITING",
            "lat": study.get("lat"),
            "lon": study.get("lon"),
        }],
        "centralContacts": posted_contacts,
        "source": "site_posted",
    }


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def run_search(note, condition, country, require_site, coords=None, radius=50,
               unit="km", interventional_only=True, intervention=""):
    """Fetch -> hard-gate -> LLM-score, ranked by relevance then distance.
    `radius`/`unit` define the geographic limit. By default only interventional
    (treatment) trials are kept - a doctor refers for therapy, not to a registry.
    `intervention` searches by drug name (e.g. "semaglutide") instead of/along
    with a condition. Returns (search_label, results)."""
    profile = mt.patient_profile(note)
    if not condition and not intervention and mt.LLM_API_KEY:
        try:
            condition = mt.llm_chat(
                "You extract the single primary medical condition to search "
                "clinical trials for. Reply with ONLY the condition name, no "
                "punctuation.", note[:4000]).strip().strip(".")
        except Exception:
            condition = ""
    search_label = condition or intervention
    if not search_label:
        return "", []

    geo = None
    if coords and radius:
        geo = f"distance({coords[0]},{coords[1]},{int(radius)}{unit})"
    try:
        trials = mt.fetch_trials(condition, max_n=200, geo=geo,
                                 intervention=intervention)
    except Exception:
        raise RuntimeError(
            "Couldn't reach ClinicalTrials.gov right now. Please try again "
            "in a moment.")

    # Only genuinely active studies: recruiting overall (belt-and-suspenders on
    # top of the API filter), and interventional by default.
    trials = [t for t in trials
              if (t.get("overallStatus") or "RECRUITING").upper() == "RECRUITING"]
    if interventional_only:
        trials = [t for t in trials
                  if (t.get("studyType") or "").upper() != "OBSERVATIONAL"]

    # Country filter only matters when we're not already geo-scoping.
    if not coords and country and require_site:
        trials = [t for t in trials if mt.sites_in_country(t, country)]
    elif not coords and country:
        trials.sort(key=lambda t: not bool(mt.sites_in_country(t, country)))

    gated = [t for t in trials if mt.hard_gate(t, profile)[0]]

    # Concept-level relevance from CT.gov's own MeSH vocabulary. We normalize the
    # patient's condition into the same tokens CT.gov tags studies with, so we
    # spend the (costly) per-trial LLM budget on the most on-topic studies first
    # instead of whatever order the loose text search returned. Best-effort: if
    # the NLM lookup or a study's MeSH is missing, relevance is 0 and ordering
    # falls back to the previous behaviour - nothing is ever dropped.
    try:
        patient_tokens = codes.condition_tokens(search_label)
    except Exception:
        patient_tokens = set()

    def _rel(t):
        return codes.relevance(patient_tokens, mt.concept_tokens(t))

    # Which trials to surface. With a location, keep only trials with a
    # RECRUITING site inside the radius, closest first - so we never surface
    # something across the country or a site that isn't enrolling.
    #
    # We DON'T cap what's shown: every matching, recruiting, in-radius trial is
    # returned, best-match first. There's no magic "top N" number - a broad
    # search like "obesity" legitimately has many equally-good options, so we
    # show them all. The per-trial LLM eligibility read is the only thing that's
    # budgeted (it's the expensive part): the strongest matches get the full
    # reasoning, the long tail shows with a lightweight "recruiting & on-topic"
    # verdict, all ranked together below.
    picks = []  # list of (trial, sites_nearby), already in best-match order
    if coords:
        within = []
        for t in gated:
            near = nearby_sites(t, coords[0], coords[1], unit, radius)
            if near:                              # has a reachable recruiting site
                within.append((t, near))
        # Nearest first, but let strong concept relevance win close ties so a
        # slightly-farther on-topic trial isn't buried by an off-topic closer one.
        within.sort(key=lambda x: (round(x[1][0]["distance"], 0), -_rel(x[0])))
        picks = within
    else:
        gated.sort(key=lambda t: -_rel(t))        # most on-topic first
        picks = [(t, []) for t in gated]

    def _build_result(t, near, m):
        t = dict(t or {})
        t.setdefault("source", "ctgov")
        m = _quality_gated_match(m)
        if not m.get("quality_ok", True):
            app.logger.warning(
                "Eligibility output quality gated for %s (score=%s flags=%s)",
                t.get("nctId", ""), m.get("quality_score"),
                ",".join(m.get("quality_flags") or []))
        if near:
            site, dist = near[0], near[0]["distance"]
        else:
            site, dist = rf.best_site(t, country), None

        # Summarize the study's other recruiting sites (beyond the nearby ones)
        # so the doctor can see where else it runs without a wall of text.
        # Order by proximity to the patient so the closest options show first.
        recruiting = [s for s in t.get("locations", [])
                      if (s.get("status") or "").upper() in ("", "RECRUITING")]
        near_ids = {(s.get("facility"), s.get("city")) for s in near}
        others = [s for s in recruiting
                  if (s.get("facility"), s.get("city")) not in near_ids]
        if coords:
            def _d(s):
                if s.get("lat") is None or s.get("lon") is None:
                    return float("inf")
                return haversine(coords[0], coords[1], s["lat"], s["lon"], unit)
            others.sort(key=_d)
        other_regions, seen = [], set()
        for s in others:
            reg = ", ".join(p for p in (s.get("city"), s.get("state"),
                                        s.get("country")) if p)
            if reg and reg not in seen:
                seen.add(reg)
                other_regions.append(reg)

        return {"trial": t, "match": m, "site": site,
                "site_str": _site_str(site),
                "site_maps_url": _maps_url(site),
                "site_dir_url": _maps_dir_url(site),
                "coordinator": _coordinator(t, site),
                "public_contacts": _public_contacts(t, site),
                "distance": dist, "unit": unit, "relevance": _rel(t),
                "nearby": near[:8], "nearby_total": len(near),
                "other_count": len(others), "other_regions": other_regions[:5],
                "pay": _pay_likelihood(t),
                "support": _participant_support_signal(t)}

    # Budget the (expensive) per-trial LLM eligibility read to the strongest
    # matches; everything else still shows with a neutral, honest verdict.
    def _light_match():
        return {"verdict": "possible", "score": 0,
                "rationale": ("Matches your search and is recruiting"
                              + (" near you." if coords else ".")
                              + " The study team confirms full eligibility"
                              " after you apply."),
                "met": [], "not_met": [], "unknown": [],
                "quality_score": 100, "quality_flags": [], "quality_ok": True}

    llm_picks = picks[:MAX_MATCH]
    light_picks = picks[MAX_MATCH:]

    results = []
    if mt.LLM_API_KEY and _llm_search_budget_ok():
        max_workers = min(WEB_LLM_PARALLELISM, len(llm_picks))
        if max_workers <= 1:
            for t, near in llm_picks:
                try:
                    m = mt.llm_match(note, t)
                except Exception as e:
                    m = {"verdict": "error", "score": 0, "rationale": str(e)[:120],
                         "met": [], "not_met": [], "unknown": []}
                results.append(_build_result(t, near, m))
        else:
            future_map = {}
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                for t, near in llm_picks:
                    future_map[ex.submit(mt.llm_match, note, t)] = (t, near)
                for fut in as_completed(future_map):
                    t, near = future_map[fut]
                    try:
                        m = fut.result()
                    except Exception as e:
                        m = {"verdict": "error", "score": 0, "rationale": str(e)[:120],
                             "met": [], "not_met": [], "unknown": []}
                    results.append(_build_result(t, near, m))
    else:
        for t, near in llm_picks:
            results.append(_build_result(t, near, _light_match()))

    # The rest of the matches - shown too, ranked below via rank_key.
    for t, near in light_picks:
        results.append(_build_result(t, near, _light_match()))

    # Also include studies posted directly by sites (not yet on CT.gov).
    for row in db.list_site_posted_studies(status="recruiting"):
        s = dict(row)
        relevance = _site_posted_rank(s, condition, intervention)
        if relevance <= 0:
            continue
        trial = _site_posted_as_trial(s)
        m = {
            "verdict": "possible",
            "score": min(95, 35 + relevance),
            "rationale": ("Posted directly by the study site. The coordinator can "
                          "confirm full eligibility after your application."),
            "met": [],
            "not_met": [],
            "unknown": [],
            "quality_score": 100,
            "quality_flags": [],
            "quality_ok": True,
        }
        dist = None
        loc = (s.get("lat"), s.get("lon"))
        if coords and loc[0] is not None and loc[1] is not None:
            try:
                dist = haversine(coords[0], coords[1], float(loc[0]), float(loc[1]), unit)
            except Exception:
                dist = None
        if coords and radius and dist is not None and dist > radius:
            continue
        site_name = (s.get("site_name") or s.get("org_name") or "").strip()
        site_loc = (s.get("location") or "").strip()
        site_str = ", ".join(x for x in (site_name, site_loc) if x)
        coord_bits = [s.get("profile_contact_name"), s.get("contact_phone"),
                      s.get("contact_email")]
        coordinator = " · ".join(x for x in coord_bits if (x or "").strip())
        results.append({
            "trial": trial,
            "match": m,
            "site": {"facility": site_name, "city": site_loc},
            "site_str": site_str,
            "site_maps_url": _maps_url({"facility": site_name, "city": site_loc,
                                        "lat": loc[0], "lon": loc[1]}),
            "site_dir_url": _maps_dir_url({"facility": site_name, "city": site_loc,
                                           "lat": loc[0], "lon": loc[1]}),
            "coordinator": coordinator,
            "public_contacts": _public_contacts(trial, {"contacts": []}),
            "distance": dist,
            "unit": unit,
            "relevance": relevance,
            "nearby": [],
            "nearby_total": 0,
            "other_count": 0,
            "other_regions": [],
            "pay": {"score": 0, "tier": "unknown", "label": "Compensation not listed",
                     "notes": ["Ask the site coordinator for details"]},
            "support": _participant_support_signal(trial),
        })

    def rank_key(r):
        t, m = r["trial"], r["match"]
        obs = (t.get("studyType") or "").upper() == "OBSERVATIONAL"
        source = (t.get("source") or "ctgov").lower()
        # 1st relevance (verdict), 2nd distance, then quality tiebreakers.
        dist_sort = r["distance"] if r["distance"] is not None else 1e9
        local = True if source == "site_posted" else (
            bool(mt.sites_in_country(t, country)) if country else True)
        return (mt.VERDICT_RANK.get(m.get("verdict"), 3), round(dist_sort, 1),
                0 if local else 1, 0 if not obs else 1, 0 if source == "ctgov" else 1,
                -int(r.get("relevance") or 0),
                len(m.get("not_met") or []), len(m.get("unknown") or []),
                -int(m.get("score") or 0))

    results.sort(key=rank_key)
    return search_label, results


def _site_str(site):
    s = site or {}
    return ", ".join(p for p in (s.get("facility"), s.get("city"),
                                 s.get("state"), s.get("country")) if p)


def _maps_query(site):
    """Best Google Maps query for a site.

    Prefers the human-readable place (facility, city, state, country) so Maps
    opens the NAMED location - not a raw 'lat,lon' pin that just displays ugly
    coordinates. Falls back to coordinates only when there's no usable label."""
    s = site or {}
    label = _site_str(site)
    if label:
        return label
    lat, lon = s.get("lat"), s.get("lon")
    if lat is not None and lon is not None:
        return f"{lat},{lon}"
    return ""


def _maps_url(site):
    """A Google Maps link that opens the site as a named place (see _maps_query)."""
    q = _maps_query(site)
    if not q:
        return ""
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(q)


def _maps_dir_url(site):
    """Google Maps driving-directions link to the site as a named place."""
    q = _maps_query(site)
    if not q:
        return ""
    return ("https://www.google.com/maps/dir/?api=1&destination="
            + urllib.parse.quote(q))


def _clean_name(name):
    """Drop junk that CT.gov sometimes stuffs into the contact name field
    (study-ID blurbs, URLs, 'No attachments…') so we only show real names."""
    n = (name or "").strip()
    if not n:
        return ""
    low = n.lower()
    bad = ("http", "reference study", "no attachments", "study id", "www.")
    if any(b in low for b in bad) or len(n) > 48:
        return ""
    return n


def _fmt_contact(c):
    name = _clean_name(c.get("name"))
    phone = (c.get("phone") or "").strip()
    email = (c.get("email") or "").strip()
    return " · ".join(b for b in (name, phone, email) if b)


def _is_us_only_phone(phone):
    p = (phone or "").lower()
    return "u.s. only" in p or "us only" in p or "us-only" in p


def _public_contacts(trial, site, limit=4):
    """Public trial contacts to show patients (from CT.gov or site-posted data)."""
    out, seen = [], set()
    site_country = ((site or {}).get("country") or "").strip().lower()
    non_us_patient = bool(site_country) and site_country not in (
        "united states", "usa", "us", "u.s.", "u.s.a.")

    def _push(c):
        name = _clean_name(c.get("name")) or "Study contact"
        email = (c.get("email") or "").strip()
        phone = (c.get("phone") or "").strip()
        # A US-only sponsor hotline is a dead end for a patient at a non-US site,
        # so drop the number (the email works globally). Avoids showing a
        # "(U.S. Only)" line next to a Canadian site.
        if phone and non_us_patient and _is_us_only_phone(phone):
            phone = ""
        role = (c.get("role") or "").strip().replace("_", " ").title()
        if not (email or phone):
            return
        key = (name.lower(), email.lower(), phone)
        if key in seen:
            return
        seen.add(key)
        out.append({"name": name, "email": email, "phone": phone, "role": role})

    for c in (site or {}).get("contacts", []):
        if c.get("role") != "PRINCIPAL_INVESTIGATOR":
            _push(c)
    for c in (trial.get("centralContacts") or []):
        _push(c)
    return out[:limit]


def _coordinator(trial, site):
    for c in (site or {}).get("contacts", []):
        if c.get("role") != "PRINCIPAL_INVESTIGATOR":
            s = _fmt_contact(c)
            if s:
                return s
    for c in (trial.get("centralContacts") or []):
        s = _fmt_contact(c)
        if s:
            return s
    return ""


@app.route("/search", methods=["GET", "POST"])
@login_required
def search():
    if request.method == "GET":
        return render_template("search.html")
    note = request.form.get("note", "").strip()
    condition = request.form.get("condition", "").strip()
    country = request.form.get("country", "Canada").strip()
    require_site = bool(request.form.get("require_site"))
    include_obs = bool(request.form.get("include_observational"))
    location = request.form.get("location", "").strip()
    try:
        radius = int(request.form.get("radius", "50") or 0)
    except ValueError:
        radius = 50
    if not note:
        flash("Paste a de-identified patient note to search.", "error")
        return render_template("search.html")
    if not location:
        flash("Enter the patient's location so we only show trials near them.",
              "error")
        return render_template("search.html", note_value=note)

    coords, unit = None, "km"
    # Precise coordinates from "use my current location" take priority.
    lat_in = request.form.get("lat", "").strip()
    lon_in = request.form.get("lon", "").strip()
    if lat_in and lon_in:
        try:
            coords = (float(lat_in), float(lon_in))
            unit = units_for(request.form.get("cc", ""))
        except ValueError:
            coords = None
    if coords is None and location:
        try:
            geo = geocode(location)
        except Exception:
            geo = None
        if geo:
            coords = (geo[0], geo[1])
            unit = units_for(geo[2])
        else:
            flash(f"Couldn't find “{location}” - showing results by relevance "
                  "instead. Try a city, e.g. “Toronto, ON” or a postal code.",
                  "error")

    try:
        detected, results = run_search(note, condition, country, require_site,
                                       coords, radius, unit,
                                       interventional_only=not include_obs)
    except RuntimeError as e:
        flash(str(e), "error")
        return render_template("search.html", note_value=note)
    except Exception:
        app.logger.exception("search failed")
        flash("Search failed unexpectedly. Please try again.", "error")
        return render_template("search.html", note_value=note)

    if not detected:
        flash("Couldn't auto-detect a condition - type one in and search again.",
              "error")
        return render_template("search.html", note_value=note)

    profile = mt.patient_profile(note)
    return render_template("results.html", results=results, note=note,
                           condition=detected or condition, country=country,
                           require_site=require_site, profile=profile,
                           location=location, radius=radius, unit=unit,
                           located=bool(coords), include_obs=include_obs)


# --------------------------------------------------------------------------- #
# Import a note (upload/paste) -> de-identified text in the search box
# --------------------------------------------------------------------------- #
@app.route("/extract", methods=["POST"])
@login_required
def extract():
    """Import a note from an upload/paste. Extraction and AI de-identification
    are separate steps so we never lose the doctor's text: if de-id is off or
    fails, we still return the extracted text with a warning to redact."""
    f = request.files.get("file")
    raw = request.form.get("raw_text", "").strip()

    # 1) Get raw text (images require the vision model; there is no non-AI path).
    try:
        if f and f.filename:
            data = f.read()
            if ingest._ext(f.filename) in ingest.IMAGE_EXTS:
                text = ingest.vision_extract(data, ingest._ext(f.filename))
                flash("Read the image and de-identified it - review, then search.", "ok")
                return render_template("search.html", note_value=text)
            raw_text = ingest.extract_text(f.filename, data)
        elif raw:
            raw_text = raw
        else:
            flash("Choose a file or paste some text to import.", "error")
            return render_template("search.html")
    except ingest.IngestError as e:
        flash(str(e), "error")
        return render_template("search.html", note_value=raw)
    except Exception:
        app.logger.exception("extract (text) failed")
        flash("Couldn't read that. Please paste the note text instead.", "error")
        return render_template("search.html", note_value=raw)

    # 2) De-identify with the LLM if available; otherwise keep text + warn.
    if not mt.LLM_API_KEY:
        flash("Imported. AI de-identification is off - remove any names/identifiers "
              "before searching.", "error")
        return render_template("search.html", note_value=raw_text)
    try:
        cleaned = ingest.deidentify(raw_text)
        flash("Imported and de-identified - review the note, then search.", "ok")
        return render_template("search.html", note_value=cleaned)
    except ingest.IngestError:
        flash("Imported, but AI cleanup failed - please remove identifiers "
              "manually before searching.", "error")
        return render_template("search.html", note_value=raw_text)


# --------------------------------------------------------------------------- #
# Import a patient from a connected EHR (FHIR) -> de-identified note
# --------------------------------------------------------------------------- #
# Label shown once an EHR is linked. No real OAuth handshake yet - this connects
# to the public SMART/HAPI FHIR sandbox so the pull-by-ID flow can be used.
EHR_PROVIDER = os.environ.get("EHR_PROVIDER", "SMART Health IT (sandbox)")


@app.route("/ehr/connect", methods=["POST"])
@login_required
def ehr_connect():
    db.set_ehr_connection(g.user["id"], True, EHR_PROVIDER)
    flash(f"Connected to {EHR_PROVIDER}. You can now pull a patient by ID.", "ok")
    return redirect(request.form.get("next") or url_for("search"))


@app.route("/ehr/disconnect", methods=["POST"])
@login_required
def ehr_disconnect():
    db.set_ehr_connection(g.user["id"], False)
    flash("Disconnected your EHR.", "ok")
    return redirect(request.form.get("next") or url_for("search"))


@app.route("/ehr/import", methods=["POST"])
@login_required
def ehr_import():
    if not g.user["ehr_connected"]:
        flash("Connect your EHR first to pull patients by ID.", "error")
        return render_template("search.html")
    pid = request.form.get("patient_id", "").strip()
    want_sample = request.form.get("sample")
    try:
        if want_sample and not pid:
            pid = fhir.pick_sample_patient()
        summary, cond = fhir.fetch_patient_summary(pid)
    except fhir.FhirError as e:
        flash(str(e), "error")
        return render_template("search.html")
    except Exception:
        app.logger.exception("ehr import failed")
        flash("EHR import failed unexpectedly. Please try again.", "error")
        return render_template("search.html")
    flash("Loaded the patient from the EHR and de-identified it - review the "
          "note, then search.", "ok")
    return render_template("search.html", note_value=summary, condition_value=cond)


# --------------------------------------------------------------------------- #
# Refer + track
# --------------------------------------------------------------------------- #
_EMAIL_RE = _re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _extract_email(s):
    m = _EMAIL_RE.search(s or "")
    return m.group(0) if m else ""


@app.route("/refer", methods=["POST"])
@login_required
def refer():
    """Step 1: confirm the referral - capture consent + contact details before
    anything is saved or sent."""
    f = request.form
    nct = f.get("nct", "").strip()
    if not nct:
        flash("Couldn't start that referral (missing trial). Please try again.",
              "error")
        return redirect(request.referrer or url_for("dashboard"))
    label = f.get("patient_label", "").strip()
    if not label:
        p = mt.patient_profile(f.get("note", ""))
        label = f"{int(p['age']) if p['age'] else '?'}{(p['sex'] or '?')[0].upper()}"
    data = {k: f.get(k, "") for k in
            ("nct", "title", "condition", "country", "site", "coordinator",
             "verdict", "score", "rationale")}
    data["note"] = f.get("note", "")
    data["patient_label"] = label
    data["coordinator_email"] = _extract_email(f.get("coordinator", ""))
    return render_template("refer.html", d=data)


@app.route("/refer/confirm", methods=["POST"])
@login_required
def refer_confirm():
    """Step 2: consent recorded -> persist the referral and open its tracker."""
    f = request.form
    nct = f.get("nct", "").strip()
    if not nct:
        flash("Couldn't save that referral (missing trial).", "error")
        return redirect(url_for("dashboard"))
    if not f.get("consent"):
        flash("Please confirm patient consent before referring.", "error")
        return render_template("refer.html", d=request.form)
    ref_id = db.create_referral(g.user["id"], {
        "nct": nct, "title": f.get("title", ""),
        "patient_label": f.get("patient_label", "").strip() or "patient",
        "patient_summary": f.get("note", ""),
        "condition": f.get("condition", ""), "country": f.get("country", ""),
        "site": f.get("site", ""), "coordinator": f.get("coordinator", ""),
        "coordinator_email": f.get("coordinator_email", "").strip(),
        "patient_name": f.get("patient_name", "").strip(),
        "patient_contact": f.get("patient_contact", "").strip(),
        "consent": True,
        "verdict": f.get("verdict", ""), "score": f.get("score", 0),
        "rationale": f.get("rationale", ""),
    })
    flash("Referral saved. Send it to the site coordinator to close the loop.",
          "ok")
    return redirect(url_for("referral_detail", ref_id=ref_id))


@app.route("/app/invite", methods=["GET", "POST"])
@login_required
def invite_patient():
    """Clinician mints a 'your doctor referred you' link for a specific trial.
    They hand it to a patient; when the patient applies, it's credited back here
    so the clinician sees what happened - the same loop refer.py closes for sites."""
    if request.method == "POST":
        f = request.form
        nct = f.get("nct", "").strip()
        title = f.get("title", "").strip()
        if not (nct or title):
            flash("Add at least a trial NCT number or title.", "error")
        else:
            tok = db.create_invite(
                g.user["id"], g.user["name"] or "your physician",
                nct, title, f.get("condition", "").strip(),
                f.get("note", "").strip())
            flash("Invite link created - copy it below and send it to your patient.",
                  "ok")
            return redirect(url_for("invite_patient", new=tok))
    invites = db.list_invites(g.user["id"])
    return render_template("invite.html", invites=invites,
                           new_token=request.args.get("new", ""))


# --------------------------------------------------------------------------- #
# Recruitment campaigns (the marketing side, wired to the funnel + ATS)
# --------------------------------------------------------------------------- #
def _trial_dict_for_nct(nct):
    """Best-effort trial context for AI creative: prefer a site-posted study's
    fields, else fall back to the claim's stored title. Enough for the copy
    generator to write neutral, on-topic recruitment text."""
    nct = (nct or "").strip()
    posted = db.get_site_posted_study_by_nct(nct) if nct else None
    if posted:
        return {"title": posted["title"], "condition": posted["condition"],
                "brief_summary": posted["brief_summary"],
                "location": posted["location"], "nctId": nct}
    title = ""
    for c in db.list_study_claims(g.user["id"]):
        if db._norm_nct(c["nct"]) == db._norm_nct(nct):
            title = c["title"] or ""
            break
    return {"title": title, "condition": "", "brief_summary": "",
            "location": "", "nctId": nct}


def _own_campaign_or_404(cid):
    """Fetch a campaign and 404 unless it belongs to the current account - a
    campaign carries no PHI but still scopes to its owner."""
    camp = db.get_campaign(cid)
    if not camp or camp["user_id"] != g.user["id"]:
        abort(404)
    return camp


@app.route("/app/campaigns")
@login_required
def campaign_list():
    """All recruitment campaigns for the account, with live funnel + cost from the
    real pipeline (applicants -> enrolled, cost-per-enrolled). KPI: found ->
    contacted; the funnel numbers keep it honest (net throughput, not clicks)."""
    perf = {p["id"]: p for p in db.campaign_performance(g.user["id"], ncts=None)}
    camps = db.list_campaigns_for_user(g.user["id"])
    return render_template("campaigns.html", camps=camps, perf=perf,
                           claims=db.list_study_claims(g.user["id"]),
                           channels=db.CAMPAIGN_CHANNELS)


@app.route("/app/campaigns/new", methods=["POST"])
@login_required
def campaign_create():
    nct = request.form.get("nct", "").strip()
    name = request.form.get("name", "").strip()
    channel = request.form.get("channel", "other").strip()
    try:
        budget = float(request.form.get("budget", "") or 0)
    except ValueError:
        budget = 0
    if not name or not nct:
        flash("Give the campaign a name and pick the study it recruits for.",
              "error")
        return redirect(url_for("campaign_list"))
    if nct not in _site_claims():
        flash("You can only run campaigns for studies you've claimed.", "error")
        return redirect(url_for("campaign_list"))
    cid = db.create_campaign(g.user["id"], nct, name, channel=channel,
                             budget_usd=budget)
    flash("Campaign created. Draft the creative, then get it IRB-approved before "
          "it can go live.", "ok")
    return redirect(url_for("campaign_detail", cid=cid))


@app.route("/app/campaigns/<int:cid>")
@login_required
def campaign_detail(cid):
    """Campaign builder + placements + performance in one screen: draft/edit
    creative, attest IRB approval (hard gate), add the places you'll post (each
    gets a copy-ready blurb + trackable /go link), and see per-placement results."""
    camp = _own_campaign_or_404(cid)
    placements = db.list_placements(cid)
    base_url = request.url_root.rstrip("/")
    shares = [campaigns_mod.placement_share_payload(camp, p, base_url=base_url)
              for p in placements]
    perf = {p["id"]: p for p in db.campaign_performance(g.user["id"])}.get(cid)
    pl_perf = {p["id"]: p for p in db.placement_performance(g.user["id"])}
    return render_template(
        "campaign_detail.html", camp=camp, placements=placements,
        shares=shares, perf=perf, pl_perf=pl_perf,
        channels=db.CAMPAIGN_CHANNELS)


@app.route("/app/campaigns/<int:cid>/creative", methods=["POST"])
@login_required
def campaign_creative(cid):
    camp = _own_campaign_or_404(cid)
    action = request.form.get("action", "save")
    if action == "generate":
        trial = _trial_dict_for_nct(camp["nct"])
        creative = campaigns_mod.generate_creative(trial, camp["channel"])
        db.set_campaign_creative(cid, creative["headline"], creative["body"],
                                 creative["landing_copy"])
        flash("AI drafted a version. Review + edit it, then attest IRB approval. "
              "Copy must be neutral and truthful - no benefit or payment claims.",
              "ok")
    else:
        db.set_campaign_creative(
            cid, request.form.get("headline", ""), request.form.get("body", ""),
            request.form.get("landing_copy", ""))
        flash("Creative saved. Editing resets IRB approval - re-attest before "
              "going live.", "ok")
    return redirect(url_for("campaign_detail", cid=cid))


@app.route("/app/campaigns/<int:cid>/approve", methods=["POST"])
@login_required
def campaign_approve(cid):
    _own_campaign_or_404(cid)
    if not request.form.get("attest"):
        flash("Tick the box to attest the copy is IRB/REB-approved.", "error")
        return redirect(url_for("campaign_detail", cid=cid))
    db.approve_campaign(cid, approved=True)
    flash("Marked IRB-approved. You can activate this campaign now.", "ok")
    return redirect(url_for("campaign_detail", cid=cid))


@app.route("/app/campaigns/<int:cid>/status", methods=["POST"])
@login_required
def campaign_status(cid):
    _own_campaign_or_404(cid)
    ok, reason = db.set_campaign_status(cid, request.form.get("status", ""))
    flash("Campaign updated." if ok else reason, "ok" if ok else "error")
    return redirect(url_for("campaign_detail", cid=cid))


@app.route("/app/campaigns/<int:cid>/placements", methods=["POST"])
@login_required
def campaign_placement_add(cid):
    camp = _own_campaign_or_404(cid)
    label = request.form.get("label", "").strip()
    if not label:
        flash("Name the place you're posting (e.g. r/ADHD, UofT board).", "error")
        return redirect(url_for("campaign_detail", cid=cid))
    db.create_placement(cid, label=label,
                        channel=request.form.get("channel", "") or camp["channel"],
                        posted_url=request.form.get("posted_url", ""))
    flash("Placement added. Copy its blurb + tracked link and post it.", "ok")
    return redirect(url_for("campaign_detail", cid=cid))


@app.route("/app/intake")
@login_required
def intake_page():
    """Multi-source intake setup: each claimed study gets one inbound email
    address (forward applicants here from any source) + a CSV importer for an
    existing applicant list. KPI: speeds contacted -> screened by pulling every
    source into one queue. PHI: addresses are owner-scoped (see COMPLIANCE.md)."""
    claims = db.list_study_claims(g.user["id"])
    rows = []
    for c in claims:
        addr = db.get_or_create_intake_address(g.user["id"], c["nct"],
                                                label=c["title"] or "")
        rows.append({"claim": c, "address": addr,
                     "full": intake_mod.full_intake_address(addr["address"])})
    return render_template("intake.html", rows=rows,
                           domain=intake_mod.INTAKE_EMAIL_DOMAIN)


@app.route("/app/intake/import", methods=["POST"])
@login_required
def intake_import():
    nct = request.form.get("nct", "").strip()
    if nct not in _site_claims():
        flash("Pick a study you've claimed to import into.", "error")
        return redirect(url_for("intake_page"))
    up = request.files.get("file")
    if not up or not up.filename:
        flash("Choose a CSV file to import.", "error")
        return redirect(url_for("intake_page"))
    try:
        text = up.read().decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        norm = []
        for r in reader:
            low = {(k or "").strip().lower(): (v or "").strip()
                   for k, v in r.items()}
            norm.append({"name": low.get("name", ""), "email": low.get("email", ""),
                         "phone": low.get("phone", ""), "notes": low.get("notes", "")})
        res = db.import_leads_csv(g.user["id"], nct, norm, source="csv_import")
        flash(f"Imported: {res['created']} new, {res['matched']} matched, "
              f"{res['skipped']} skipped.", "ok")
    except Exception:
        app.logger.exception("csv import failed")
        flash("Couldn't read that CSV. Expected columns: name, email, phone, notes.",
              "error")
    return redirect(url_for("intake_page"))


@app.route("/app/campaign", methods=["GET", "POST"])
@login_required
def ad_campaign():
    """Legacy invite-link ad builder, superseded by the campaigns suite
    (/app/campaigns) which is backed by the real campaign + placement +
    attribution model. Kept as a redirect so old links don't 404."""
    return redirect(url_for("campaign_list"))


@app.route("/go/<token>")
def campaign_landing(token):
    """Public entrypoint for a tracked campaign/placement link. A site pastes this
    URL into wherever they post (Reddit, a campus board, an ad); a click records
    attribution, sets a cookie carried through to apply, and forwards to the
    study page. This is what closes the loop: applicants trace back to the exact
    post that produced them, all the way to enrolled."""
    tr = db.resolve_tracking_token(token)
    if not tr:
        return redirect(url_for("home"))
    if tr.get("placement_id"):
        db.bump_placement_clicks(token)
    _log_event("campaign_click", {"nct": tr.get("nct", "")})
    dest = (url_for("study_page", nct=tr["nct"]) if tr.get("nct")
            else url_for("home"))
    resp = make_response(redirect(dest))
    resp.set_cookie(CAMPAIGN_COOKIE, token, max_age=60 * 60 * 24 * 30,
                    samesite="Lax", httponly=True,
                    secure=bool(os.environ.get("BEHIND_PROXY")))
    return resp


@app.route("/i/<token>")
def invite_landing(token):
    """Public patient landing for a physician invite. Sets an attribution cookie,
    shows the trusted-messenger framing, and sends them to search + apply."""
    inv = db.get_invite(token)
    if not inv:
        flash("That invite link isn't valid. You can still search for trials below.",
              "error")
        return redirect(url_for("home"))
    db.bump_invite_clicks(token)
    resp = make_response(render_template("invite_landing.html", inv=inv))
    resp.set_cookie(INVITE_COOKIE, token, max_age=60 * 60 * 24 * 30,
                    samesite="Lax", httponly=True,
                    secure=bool(os.environ.get("BEHIND_PROXY")))
    return resp


@app.route("/referrals")
@login_required
def referrals():
    rows = db.list_referrals(g.user["id"])
    counts = db.status_counts(g.user["id"])
    enrolled = db.enrolled_count(g.user["id"])
    return render_template("referrals.html", referrals=rows, counts=counts,
                           statuses=db.STATUSES, enrolled=enrolled)


@app.route("/referrals.csv")
@login_required
def referrals_csv():
    import csv
    import io
    rows = db.list_referrals(g.user["id"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["patient", "nct", "trial", "site", "coordinator_email", "status",
                "consent", "sent_at", "created", "updated"])
    for r in rows:
        w.writerow([r["patient_label"], r["nct"], r["title"], r["site"],
                    r["coordinator_email"], r["status"],
                    "yes" if r["consent"] else "no", r["notified_at"],
                    r["created_at"], r["updated_at"]])
    from flask import Response
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=bridgemd_referrals.csv"})


@app.route("/referral/<int:ref_id>")
@login_required
def referral_detail(ref_id):
    ref = db.get_referral(ref_id, g.user["id"])
    if not ref:
        abort(404)
    share_url = url_for("coordinator_page", token=ref["token"], _external=True)
    subject, body = mailer.build_message(ref, share_url)
    mailto = "mailto:" + urllib.parse.quote(ref["coordinator_email"] or "") + \
        "?" + urllib.parse.urlencode({"subject": subject, "body": body})
    return render_template("referral_detail.html", ref=ref,
                           events=db.get_events(ref_id), statuses=db.STATUSES,
                           share_url=share_url, mailto=mailto,
                           email_subject=subject, email_body=body,
                           smtp_on=mailer.smtp_configured())


@app.route("/referral/<int:ref_id>/notify", methods=["POST"])
@login_required
def referral_notify(ref_id):
    ref = db.get_referral(ref_id, g.user["id"])
    if not ref:
        abort(404)
    email = request.form.get("coordinator_email", "").strip() or \
        ref["coordinator_email"]
    share_url = url_for("coordinator_page", token=ref["token"], _external=True)
    subject, body = mailer.build_message(ref, share_url)
    ok, msg = mailer.send_email(email, subject, body)
    if ok:
        db.mark_notified(ref_id, g.user["id"], email)
        flash("Referral emailed to the coordinator.", "ok")
    else:
        flash(msg + " Use the email/link below to send it manually.", "error")
    return redirect(url_for("referral_detail", ref_id=ref_id))


@app.route("/referral/<int:ref_id>/mark-sent", methods=["POST"])
@login_required
def referral_mark_sent(ref_id):
    """Doctor sent the referral out-of-band (mailto/copy) - record it."""
    if not db.mark_notified(ref_id, g.user["id"],
                            request.form.get("coordinator_email", "").strip()):
        abort(404)
    flash("Marked as sent to the coordinator.", "ok")
    return redirect(url_for("referral_detail", ref_id=ref_id))


# --------------------------------------------------------------------------- #
# Public candidate page (tokenized, no login) - lets a real study coordinator
# review a de-identified applicant and accept/decline. This is what closes the
# consumer loop without any cold email: you share the link with the site.
# --------------------------------------------------------------------------- #
def _site_token_lead_or_none(token):
    lead = db.get_lead_by_site_token(token)
    if not lead:
        return None
    return lead if db.site_token_active(lead) else None


@app.route("/c/<token>")
def candidate_page(token):
    lead = _site_token_lead_or_none(token)
    if not lead:
        abort(410)
    messages = db.get_messages(lead["id"])
    visits = db.get_visits(lead["id"])
    if lead["revealed"]:
        db.mark_thread_read(lead["id"], "site")
    return render_template("candidate_public.html", it=_decode_lead(lead),
                           lead=lead, labels=db.LEAD_LABELS,
                           screener_labels=SCREENER_LABELS,
                           messages=messages, visits=visits,
                           post_accept=db.LEAD_PIPELINE[3:])  # screening, enrolled


@app.route("/c/<token>/accept", methods=["POST"])
def candidate_accept(token):
    if db.accept_candidate_by_token(token, request.form.get("note", "").strip()):
        lead = db.get_lead_by_site_token(token)
        if lead:
            _notify_applicant(lead["token"], "accepted")
        flash("Accepted. The applicant's contact details are now unlocked below "
              "so you can invite them to a screening visit.", "ok")
    else:
        flash("Couldn't accept this candidate.", "error")
    return redirect(url_for("candidate_page", token=token))


@app.route("/c/<token>/decline", methods=["POST"])
def candidate_decline(token):
    if db.decline_candidate_by_token(token, request.form.get("reason", "").strip()):
        lead = db.get_lead_by_site_token(token)
        if lead:
            _notify_applicant(lead["token"], "declined")
        flash("Marked as not a match. No contact details were revealed.", "ok")
    else:
        flash("Couldn't update this candidate.", "error")
    return redirect(url_for("candidate_page", token=token))


@app.route("/c/<token>/message", methods=["POST"])
def candidate_message(token):
    """Study team messages the applicant. Only after acceptance (blinded model)."""
    lead = _site_token_lead_or_none(token)
    if not lead:
        abort(410)
    if not lead["revealed"]:
        flash("Accept the candidate first to start a conversation.", "error")
        return redirect(url_for("candidate_page", token=token))
    body = request.form.get("body", "").strip()
    if body:
        db.add_message(lead["id"], "site", body)
        _notify_applicant_message(lead, body)
        flash("Message sent to the applicant.", "ok")
    return redirect(url_for("candidate_page", token=token))


@app.route("/c/<token>/visit", methods=["POST"])
def candidate_visit(token):
    """Study team books a screening/follow-up visit for an accepted candidate."""
    lead = _site_token_lead_or_none(token)
    if not lead:
        abort(410)
    if not lead["revealed"]:
        flash("Accept the candidate first to book a visit.", "error")
        return redirect(url_for("candidate_page", token=token))
    raw = request.form.get("visit_at", "").strip()
    kind = request.form.get("kind", "screening").strip() or "screening"
    location = request.form.get("location", "").strip()
    note = request.form.get("note", "").strip()
    if raw:
        when = raw.replace("T", " ")            # datetime-local -> our format
        visit_id = db.add_visit(lead["id"], when, kind, location, note)
        visit = db.get_visit(visit_id)
        invite_url = _abs_url("visit_ics", token=lead["token"], visit_id=visit_id)
        db.update_lead_status(lead["id"], "screening",
                              f"{kind} visit booked for {when}", actor="site")
        sysmsg = f"Your {kind} visit is booked for {when}"
        sysmsg += f" at {location}." if location else "."
        sysmsg += f" Add to your calendar: {invite_url}"
        sysmsg += " We'll remind you beforehand."
        db.add_message(lead["id"], "system", sysmsg)
        _notify_applicant_visit(lead, when, location, invite_url=invite_url)
        sync = calendar_invites.maybe_sync_google_event(lead, visit, invite_url)
        if sync.get("attempted") and not sync.get("ok"):
            app.logger.warning("google calendar sync failed: %s",
                               sync.get("detail", "unknown"))
        flash("Visit booked and shared with the applicant.", "ok")
    else:
        flash("Pick a date and time for the visit.", "error")
    return redirect(url_for("candidate_page", token=token))


@app.route("/visit/<token>/<int:visit_id>.ics")
def visit_ics(token, visit_id):
    """Download a standard ICS invite for one visit."""
    lead = db.get_lead_by_token(token)
    if not lead:
        lead = db.get_lead_by_site_token(token)
        if not db.site_token_active(lead):
            abort(410)
    if not lead:
        abort(404)
    visit = db.get_visit(visit_id)
    if not visit or int(visit["lead_id"]) != int(lead["id"]):
        abort(404)
    invite_url = _abs_url("visit_ics", token=lead["token"], visit_id=visit_id)
    payload = calendar_invites.build_visit_ics(lead, visit, invite_url=invite_url)
    res = make_response(payload)
    res.headers["Content-Type"] = "text/calendar; charset=utf-8"
    res.headers["Content-Disposition"] = (
        f'attachment; filename="bridgemd-visit-{visit_id}.ics"')
    return res


@app.route("/c/<token>/status", methods=["POST"])
def candidate_advance(token):
    status = request.form.get("status", "").strip()
    note = request.form.get("note", "").strip()
    if status in ("screening", "enrolled", "closed") and \
            db.advance_by_token(token, status, note):
        lead = db.get_lead_by_site_token(token)
        if status in ("screening", "enrolled"):
            _notify_applicant(lead["token"], status)
        flash(f"Updated to \"{db.LEAD_LABELS.get(status, status)}\".", "ok")
    else:
        flash("Couldn't update status.", "error")
    return redirect(url_for("candidate_page", token=token))


# --------------------------------------------------------------------------- #
# Public coordinator page (tokenized, no login) - closes the referral loop.
# --------------------------------------------------------------------------- #
@app.route("/r/<token>")
def coordinator_page(token):
    ref = db.get_referral_by_token(token)
    if not ref:
        abort(404)
    return render_template("coordinator.html", ref=ref,
                           events=db.get_events(ref["id"]),
                           statuses=db.SITE_STATUSES)


@app.route("/r/<token>/status", methods=["POST"])
def coordinator_status(token):
    ref = db.get_referral_by_token(token)
    if not ref:
        abort(404)
    status = request.form.get("status", "")
    note = request.form.get("note", "").strip()
    if status not in db.SITE_STATUSES:
        flash("Please choose a valid status.", "error")
    elif db.update_status_by_token(token, status, note, actor="site"):
        flash(f"Thanks - marked as {status.replace('_', ' ')}.", "ok")
    return redirect(url_for("coordinator_page", token=token))


@app.route("/referral/<int:ref_id>/status", methods=["POST"])
@login_required
def referral_status(ref_id):
    status = request.form.get("status", "")
    note = request.form.get("note", "").strip()
    if status not in db.STATUSES:
        flash("Unknown status.", "error")
    elif db.update_status(ref_id, g.user["id"], status, note):
        flash(f"Status updated to {status.replace('_', ' ')}.", "ok")
    else:
        abort(404)
    return redirect(url_for("referral_detail", ref_id=ref_id))


if __name__ == "__main__":
    db.init_db()
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    # Hot-reload templates when developing so edits show up on refresh without a
    # restart. This is decoupled from Flask's debugger (which needs OS semaphores
    # some sandboxes block), so we get live templates without the crash.
    if debug:
        app.config["TEMPLATES_AUTO_RELOAD"] = True
        app.jinja_env.auto_reload = True
    print(f"BridgeMD on http://127.0.0.1:{port}  (LLM: "
          f"{'on' if mt.LLM_API_KEY else 'OFF - set LLM_API_KEY'})")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True,
            use_reloader=False)
