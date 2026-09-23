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
import base64
import difflib
import functools
import hashlib
import hmac
import html
import io
import json
import math
import os
import pathlib
import re
import secrets
import sys
import threading
import time
import datetime as dt
try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None
import csv
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
from html.parser import HTMLParser

from dotenv import load_dotenv
from flask import (Flask, Response, abort, flash, g, get_flashed_messages, jsonify,
                   make_response, redirect, render_template, request, send_file,
                   send_from_directory, session, url_for)
from markupsafe import Markup, escape
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# Reuse the matching engine + referral helpers from the parent package.
HERE = pathlib.Path(__file__).resolve().parent
# Local development keeps secrets in the ignored repository-root .env. Existing
# process variables win, which preserves normal production configuration.
load_dotenv(HERE.parent / ".env")
sys.path.insert(0, str(HERE.parent))
from copy_sanitize import sanitize_copy  # noqa: E402  (repo root, added above)
import match_trials as mt  # noqa: E402
import refer as rf  # noqa: E402

import adrender  # noqa: E402
import alerts as alerts_mod  # noqa: E402
import analytics  # noqa: E402
import calendar_invites  # noqa: E402
import campaigns as campaigns_mod  # noqa: E402
import clinic_lookup  # noqa: E402
import codes  # noqa: E402
import db  # noqa: E402
import fhir  # noqa: E402
import ingest  # noqa: E402
import intake as intake_mod  # noqa: E402
import logistics  # noqa: E402
import mailer  # noqa: E402
import notifications as notifications_mod  # noqa: E402
import payer  # noqa: E402
import payments as payments_mod  # noqa: E402
import records as records_mod  # noqa: E402
import redcap  # noqa: E402
import reminders as reminders_mod  # noqa: E402
import sites_features  # noqa: E402
import summarize  # noqa: E402
import trends  # noqa: E402
import ctis  # noqa: E402
import copilot  # noqa: E402
import omni_hub  # noqa: E402
import replies  # noqa: E402
import token_crypto  # noqa: E402

app = Flask(__name__)
trends.configure(app)
# Right-rail assistant (grounded, scoped to the study team's own data).
copilot.register(app)
# Short, plain-English card teaser (deterministic) available in every template.
app.jinja_env.globals["card_blurb"] = summarize.card_blurb
app.jinja_env.globals["patient_card_title"] = summarize.patient_card_title
# Display filter: normalizes "shouty acronym" CT.gov titles (e.g. "REducing
# ouTcOmes") without touching the stored value. Use as {{ title|tidy_title }}.
app.jinja_env.filters["tidy_title"] = summarize.tidy_title


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

# TEMP no-login / demo preview. It opens the study-team ATS to anonymous
# visitors and seeds fake patients into the DB. Normally MUST be OFF in
# production.
NO_LOGIN = os.environ.get("NO_LOGIN", "0") == "1"
# Pre-launch escape hatch: while NO real sites are onboarded there is no real
# PHI to protect, so we may want the study-team side to be a public, always-on
# demo even on the production host. PUBLIC_DEMO=1 is an EXPLICIT, deliberate
# acknowledgement of that trade-off - it lets NO_LOGIN run in prod.
#
# HARD COMPLIANCE GATE (see .cursor/rules/compliance.mdc + matcher/COMPLIANCE.md):
# the demo is scoped to a dedicated demo account with seeded fake data, so it
# does not expose any real applicant. BEFORE onboarding the first real site or
# ingesting any real patient data, PUBLIC_DEMO and NO_LOGIN MUST be turned off,
# or real PHI could be served to anonymous visitors.
PUBLIC_DEMO = os.environ.get("PUBLIC_DEMO", "0") == "1"
if NO_LOGIN and IS_PROD and not PUBLIC_DEMO:
    raise RuntimeError(
        "NO_LOGIN/demo mode is enabled while a production indicator is set "
        "(BEHIND_PROXY=1 or ENV=production). Refusing to start: demo mode "
        "exposes the study-team ATS and patient PHI without login. Unset "
        "NO_LOGIN for production, or set PUBLIC_DEMO=1 to intentionally run a "
        "public, data-seeded demo (only safe while NO real sites are onboarded).")
if NO_LOGIN and IS_PROD and PUBLIC_DEMO:
    app.logger.warning(
        "PUBLIC_DEMO is ON in production: the study-team side is a public "
        "no-login demo. This is only safe pre-launch (no real PHI). Turn "
        "PUBLIC_DEMO and NO_LOGIN OFF before onboarding any real site.")

# Study-team ("site") side public demo. Unlike NO_LOGIN (which fakes BOTH the
# patient and the site side), SITE_DEMO ONLY exposes the study-team side, and
# ONLY by impersonating a dedicated demo account on /app/* study-team paths -
# the patient side keeps its normal login gating. Default ON so prospects can
# click "See the demo" without any account.
#
# Why this is safe: the demo is scoped to one demo account seeded with fake
# candidates; real study-team accounts (and their applicant PHI) require login
# and are isolated by user_id, so an anonymous visitor only ever sees the demo
# account. Still a HARD COMPLIANCE GATE (see matcher/COMPLIANCE.md): never put
# real patient data on the demo account, and set SITE_DEMO=0 if you ever need
# the site side fully locked down.
SITE_DEMO = os.environ.get("SITE_DEMO", "1") == "1"

# Optional dedicated host for the site-side marketing site (e.g.
# "sites.bridgemd.health"). Only used to build the "For sites" link. Empty by
# default => the link stays on this domain at /inbox. The site root is the
# patient trial finder.
SITES_HOST = os.environ.get("SITES_HOST", "").strip().lower()

# Booking link used by the "Book a demo" CTA on the site-side site.
CAL_LINK = os.environ.get("CAL_LINK", "https://cal.com/harshil-shah-7tkvs7/30min").strip()

# Study-team paths where an anonymous visitor may be shown the demo account.
# (Excludes owner-only /app/analytics.) Patient/clinician/public paths are never
# auto-impersonated, so they stay public/gated exactly as before.
_STUDY_TEAM_DEMO_PREFIXES = (
    "/app/inbox", "/app/onboarding",
    "/app/leads", "/app/messages", "/app/site", "/app/dashboard",
    "/app/balance", "/app/campaign", "/app/intake", "/app/home",
    "/app/applicant", "/app/matching", "/app/documents", "/app/copilot",
    "/app/team", "/app/calendar", "/app/soe", "/app/payments", "/app/updates",
    "/app/irb", "/app/scope", "/app/mentions", "/app/away", "/marketing-hub",
    "/files/lead", "/files/team")

# Health-records / EHR sync (SMART Health IT) is hidden for now, the connector
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
# Omni: the prompt-configured inbox at /omni (own om_* tables, own routes).
omni_hub.register(app)

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
            "llm_configured": bool(mt.LLM_API_KEY),
            "llm_model": mt.LLM_MODEL if mt.LLM_API_KEY else "",
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
_DEMO_EMAIL = db._MARKETING_DEMO_EMAIL
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
MARKETING_GOOGLE_STATE_KEY = "marketing_google_state"
MARKETING_INSTAGRAM_STATE_KEY = "marketing_instagram_state"
CSRF_SESSION_KEY = "_csrf_token"
GOOGLE_CLIENT_ENV = "PROD" if IS_PROD else "DEV"
GOOGLE_CLIENT_ID = os.environ.get(
    f"GOOGLE_CLIENT_ID_{GOOGLE_CLIENT_ENV}", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get(
    f"GOOGLE_CLIENT_SECRET_{GOOGLE_CLIENT_ENV}", "").strip()
# Compatibility for existing deployments during the key-name migration. Only
# fall back as a complete pair so credentials from different clients never mix.
if not GOOGLE_CLIENT_ID and not GOOGLE_CLIENT_SECRET:
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
INSTAGRAM_APP_ID = (
    os.environ.get("INSTAGRAM_APP_ID", "").strip()
    or os.environ.get("META_APP_ID", "").strip())
INSTAGRAM_APP_SECRET = (
    os.environ.get("INSTAGRAM_APP_SECRET", "").strip()
    or os.environ.get("META_APP_SECRET", "").strip())
INSTAGRAM_WEBHOOK_VERIFY_TOKEN = os.environ.get(
    "INSTAGRAM_WEBHOOK_VERIFY_TOKEN", "").strip()
INSTAGRAM_WEBHOOK_MAX_BYTES = max(
    1024, min(5_000_000, int(os.environ.get(
        "INSTAGRAM_WEBHOOK_MAX_BYTES", "1000000"))))
INSTAGRAM_AUTH_URL = os.environ.get(
    "INSTAGRAM_BUSINESS_LOGIN_URL",
    "https://www.instagram.com/oauth/authorize").strip()
INSTAGRAM_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
INSTAGRAM_GRAPH_API_BASE_URL = "https://graph.instagram.com"
INSTAGRAM_GRAPH_API_VERSION = os.environ.get(
    "INSTAGRAM_GRAPH_API_VERSION", "v24.0").strip()
if not re.fullmatch(r"v\d+\.\d+", INSTAGRAM_GRAPH_API_VERSION):
    INSTAGRAM_GRAPH_API_VERSION = "v24.0"
INSTAGRAM_OAUTH_SCOPES = (
    "instagram_business_basic",
    "instagram_business_manage_messages",
)
GOOGLE_OAUTH_SCOPE = "openid email profile"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
GMAIL_API_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_INITIAL_THREAD_LIMIT = max(
    1, min(50, int(os.environ.get("GMAIL_INITIAL_THREAD_LIMIT", "20"))))
GMAIL_THREAD_MESSAGE_LIMIT = max(
    1, min(100, int(os.environ.get("GMAIL_THREAD_MESSAGE_LIMIT", "50"))))
GMAIL_OAUTH_SCOPES = (
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
)
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
    # Patient-portal password wall. The link is unguessable, but the password is
    # short enough to type, so the per-IP cap backs up the per-link lockout.
    "portal_login": max(1, int(os.environ.get("RATE_LIMIT_PORTAL_LOGIN_MAX", "12"))),
    # Inline "verify your email to apply" flow. Sending a code hits SMTP, so keep
    # it tighter than the verify check (which just compares a code).
    "apply_send_code": max(1, int(os.environ.get("RATE_LIMIT_APPLY_SEND_MAX", "6"))),
    "apply_verify_code": max(
        1, int(os.environ.get("RATE_LIMIT_APPLY_VERIFY_MAX", "12"))),
    # Public search is the one expensive public endpoint (LLM calls per query),
    # so cap it per IP to blunt bursts/bots. Generous for real users.
    "find": max(1, int(os.environ.get("RATE_LIMIT_FIND_MAX", "20"))),
    "application_public_message": max(
        1, int(os.environ.get("RATE_LIMIT_APP_MSG_MAX", "20"))),
}

# Global daily ceiling on LLM-backed searches so a traffic spike or abuse can't
# run up the model bill / exhaust the API key. When exceeded, search still works
# - it just returns matches without per-trial eligibility reasoning. 0 disables.
LLM_DAILY_SEARCH_CAP = max(0, int(os.environ.get("LLM_DAILY_SEARCH_CAP", "600")))

# Contact + last-updated shown on the Privacy / Terms pages and footer.
LEGAL_CONTACT = os.environ.get("LEGAL_CONTACT", "hello@bridgemd.health").strip()
LEGAL_UPDATED = os.environ.get("LEGAL_UPDATED", "July 2026").strip()


def _ensure_demo_user():
    db.migrate_demo_staff_identities()
    u = db.get_user_by_email(_DEMO_EMAIL)
    if not u:
        pw = generate_password_hash("demo-no-login", method="pbkdf2:sha256")
        db.create_user(_DEMO_EMAIL, pw, db._DEMO_LOGIN_NAME, "", "")
        u = db.get_user_by_email(_DEMO_EMAIL)
    elif (u["name"] or "").strip() != db._DEMO_LOGIN_NAME:
        conn = db.get_db()
        conn.execute("UPDATE users SET name = ? WHERE id = ?",
                     (db._DEMO_LOGIN_NAME, u["id"]))
        conn.commit()
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
    try:
        db.cleanup_demo_threads()
    except Exception:
        app.logger.exception("demo thread cleanup failed")
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
    # Claim the demo studies for this account FIRST - every study-team surface
    # (dashboard, applicants, campaigns, documents) hangs off claimed studies, so
    # without this they all show the "claim your studies" empty state.
    try:
        db.seed_demo_claims(user_id)
        db.ensure_demo_claim_volume(user_id, minimum_rows=108)
    except Exception:
        app.logger.exception("demo claim seeding failed")
    try:
        db.seed_demo_patient_matches(user_id)
    except Exception:
        app.logger.exception("demo match seeding failed")
    try:
        db.seed_demo_campaigns(user_id)
    except Exception:
        app.logger.exception("demo campaign seeding failed")
    try:
        db.seed_demo_lead_attribution(user_id)
    except Exception:
        app.logger.exception("demo lead attribution seeding failed")
    try:
        db.seed_demo_irb_submissions(user_id)
    except Exception:
        app.logger.exception("demo IRB submission seeding failed")
    try:
        db.seed_demo_engagement(user_id)
    except Exception:
        app.logger.exception("demo engagement seeding failed")
    try:
        # De-clone any existing demo DB seeded by the old (repetitive) copy.
        db.diversify_demo_replies()
    except Exception:
        app.logger.exception("demo reply diversify failed")
    try:
        db.seed_demo_team(user_id)
    except Exception:
        app.logger.exception("demo team seeding failed")
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
        db.seed_demo_case_notes(user_id)
    except Exception:
        app.logger.exception("demo case-note seeding failed")
    try:
        db.seed_demo_trial_documents(user_id)
    except Exception:
        app.logger.exception("demo trial-document seeding failed")
    try:
        _seed_demo_documents()
    except Exception:
        app.logger.exception("demo document seeding failed")
    try:
        db.cleanup_demo_threads()
    except Exception:
        app.logger.exception("demo thread cleanup failed")


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


def _site_demo_enabled():
    """True when the study-team side runs as a public demo (default on). This is
    independent of NO_LOGIN and never touches the patient side."""
    return SITE_DEMO


def _study_team_demo_path(path):
    return (path or "").startswith(_STUDY_TEAM_DEMO_PREFIXES)


def _is_demo_account(user):
    try:
        return bool(user) and (user["email"] or "").strip().lower() == _DEMO_EMAIL
    except (KeyError, TypeError):
        return False


# Seed the retention/engagement surfaces (messages, visits, a physician referral)
# on top of the demo leads so a fresh no-login demo shows the whole loop alive.
if NO_LOGIN:
    try:
        with app.test_request_context():
            _demo = _ensure_demo_user()
            _seed_demo_surfaces(_demo["id"] if _demo else None)
    except Exception:
        app.logger.exception("demo engagement seeding failed")


def _hash_password(raw):
    """Single place that decides how a password is stored. Same method as the
    study-team and patient accounts, so the portal never becomes the weak one."""
    return generate_password_hash(raw, method="pbkdf2:sha256")


def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return view(*a, **k)
    return wrapped


# Private owner-only analytics. Only this account sees the visitor dashboard;
# every other signed-in staff user gets a 404 (so its existence isn't leaked).
# Consent version for the opt-in "match me to future trials" registry. Bump this
# whenever the consent wording materially changes, so each stored consent record
# is tied to the exact text the person agreed to (auditable + revocable).
REGISTRY_CONSENT_VERSION = "2026-08-04.2"

OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "harshils2340@gmail.com").strip().lower()
# All accounts with owner-only admin access (internal inbox + analytics). The
# primary OWNER_EMAIL plus any founding operators, extendable via the
# OWNER_EMAILS env (comma-separated) without a code change.
OWNER_EMAILS = frozenset(
    {OWNER_EMAIL, "dhruv0704@gmail.com"}
    | {e.strip().lower() for e in os.environ.get("OWNER_EMAILS", "").split(",")
       if e.strip()})

# Analytics: don't count your own traffic. Set ANALYTICS_IGNORE_IPS to a
# comma-separated list of IPs to drop from the visitor funnel (e.g. your home /
# office IP). Events from these IPs, and from anyone signed in as OWNER_EMAIL, # are never logged. Find your current IP on the /app/analytics page.
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


# --------------------------------------------------------------------------- #
# Coarse geo for analytics. We resolve the visitor IP to an approximate area
# (city/region/country) so the owner can see WHERE demand is - but we NEVER
# store or log the raw IP, only the coarse area. Lookups are cached in-memory
# and run in a small background pool, so a request is never blocked on the geo
# provider: the event that triggered a cache miss simply logs with no area, and
# later events for that visitor pick it up once the cache warms.
# --------------------------------------------------------------------------- #
_GEO_CACHE = OrderedDict()                       # ip -> (ts, {city,region,country})
_GEO_INFLIGHT = set()                            # ips currently being resolved
_GEO_LOCK = threading.Lock()
_GEO_TTL = 12 * 3600
_GEO_MAX = 5000
_GEO_EMPTY = {"city": "", "region": "", "country": ""}
# Provider URL template (no API key needed by default). Override with GEOIP_URL;
# disable entirely with GEOIP_ENABLED=0.
_GEO_URL = os.environ.get("GEOIP_URL", "https://ipapi.co/{ip}/json/")
_GEO_ENABLED = os.environ.get("GEOIP_ENABLED", "1") == "1"
_GEO_POOL = ThreadPoolExecutor(max_workers=3)


def _is_private_ip(ip):
    """True for loopback/private/link-local addresses we shouldn't geo-locate."""
    ip = (ip or "").strip().lower()
    if not ip or ip in ("127.0.0.1", "::1", "localhost"):
        return True
    if ip.startswith(("10.", "192.168.", "169.254.", "127.", "::",
                      "fc", "fd", "fe80")):
        return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (ValueError, IndexError):
            return False
    return False


def _geo_resolve(ip):
    """Blocking IP -> area lookup that populates the cache. Runs ONLY in the
    background pool so it never delays a request. Best-effort: on any error the
    area is left blank."""
    geo = dict(_GEO_EMPTY)
    try:
        url = _GEO_URL.format(ip=urllib.parse.quote(ip))
        req = urllib.request.Request(url, headers={"User-Agent": "BridgeMD/1.0"})
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.load(r)
        if isinstance(data, dict) and not data.get("error"):
            geo = {
                "city": str(data.get("city") or "")[:80],
                "region": str(data.get("region")
                              or data.get("region_name") or "")[:80],
                "country": str(data.get("country_name")
                               or data.get("country") or "")[:80],
            }
    except Exception:
        pass
    with _GEO_LOCK:
        _GEO_CACHE[ip] = (time.time(), geo)
        _GEO_CACHE.move_to_end(ip)
        while len(_GEO_CACHE) > _GEO_MAX:
            _GEO_CACHE.popitem(last=False)
        _GEO_INFLIGHT.discard(ip)
    return geo


def _area_from_ip(ip):
    """Approximate area for the current visitor IP (analytics only). Never
    stores/returns the raw IP. Cache hit -> instant; cache miss -> kicks a
    background resolve and returns blank so the request isn't blocked."""
    ip = (ip or "").strip()
    if not _GEO_ENABLED or _is_private_ip(ip):
        return _GEO_EMPTY
    with _GEO_LOCK:
        hit = _GEO_CACHE.get(ip)
        if hit and (time.time() - hit[0]) < _GEO_TTL:
            _GEO_CACHE.move_to_end(ip)
            return hit[1]
        already = ip in _GEO_INFLIGHT
        if not already:
            _GEO_INFLIGHT.add(ip)
    if not already:
        try:
            _GEO_POOL.submit(_geo_resolve, ip)
        except Exception:
            with _GEO_LOCK:
                _GEO_INFLIGHT.discard(ip)
    return _GEO_EMPTY


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
        return bool(g.user and (g.user["email"] or "").strip().lower() in OWNER_EMAILS)
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
def _canonical_host_redirect():
    # Consolidate to one canonical hostname: 301 the app./www. subdomains to the
    # apex so search engines index (and cache the favicon for) a single host.
    # Only GET/HEAD are redirected - never bounce a POST (that would drop the
    # form body). Local dev (127.0.0.1) and the onrender.com host are untouched.
    if request.method not in ("GET", "HEAD"):
        return None
    host = (request.host or "").split(":")[0].lower()
    if host in _REDIRECT_HOSTS:
        qs = ("?" + request.query_string.decode()) if request.query_string else ""
        return redirect("https://" + CANONICAL_HOST + request.path + qs, code=301)
    return None


@app.before_request
def load_user():
    uid = session.get(USER_SESSION_KEY)
    g.user = db.get_user(uid) if uid else None
    # Full no-login demo (NO_LOGIN) impersonates the demo user everywhere; the
    # site-only demo (SITE_DEMO) only does so on study-team paths, so public and
    # patient pages keep their normal (public / logged-out) layout.
    _full_demo = _demo_mode_enabled()
    _site_demo = _site_demo_enabled() and _study_team_demo_path(request.path)
    if g.user is None and (_full_demo or _site_demo):
        g.user = _ensure_demo_user()
    # Seed demo data onto the demo account, OR onto a logged-in study-team account
    # that is still EMPTY (no claimed studies) while the demo is on - so the person
    # showing the product sees a populated dashboard/queue/campaigns/documents on
    # their own account. An account that already has claims/data is never touched.
    # Guard: SITE_DEMO must be OFF before onboarding real sites (see COMPLIANCE.md),
    # otherwise a brand-new real account would also get demo data.
    if (_full_demo or _site_demo) and g.user is not None:
        try:
            _seedable = _is_demo_account(g.user) or not db.user_claimed_ncts(
                g.user["id"])
        except Exception:
            _seedable = _is_demo_account(g.user)
        if _seedable:
            _seed_demo_surfaces(g.user["id"])
    pid = session.get(PATIENT_SESSION_KEY)
    g.patient_user = db.get_patient_user(pid) if pid else None
    # Patient side is auto-signed-in ONLY in the full no-login demo. Under the
    # site-only demo the patient side stays gated (real login required).
    if g.patient_user is None and _full_demo:
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


# The old physician-referral surface (a doctor pastes a note, searches trials,
# and refers a patient) is retired. BridgeMD is now two products: the patient
# search and the "For clinics" study-team app. These endpoints stay defined so
# url_for()/deep links resolve, but every request to them is redirected - so the
# physician side isn't reachable through any path. Signed-in accounts land on
# the study-team home; everyone else on the public patient search.
_HIDDEN_PHYSICIAN_ENDPOINTS = frozenset({
    "dashboard", "search", "refer", "refer_confirm",
    "invite_patient", "invite_landing",
    "referrals", "referrals_csv", "referral_detail", "referral_notify",
    "referral_mark_sent", "referral_status",
})


@app.before_request
def _hide_physician_surface():
    if request.endpoint in _HIDDEN_PHYSICIAN_ENDPOINTS:
        return redirect(url_for("marketing_hub") if g.user else url_for("home"))


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
        if (resp.headers.get("Content-Type", "").startswith("text/html")
                and "no-store" not in resp.headers.get("Cache-Control", "")):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate, max-age=0"
    except Exception:
        pass
    return resp


@app.after_request
def _no_em_dashes(resp):
    """Last line of defence for the no-em-dash rule.

    Drafts, chat answers, seed strings and the database are all sanitised at
    their source, and each of those sources has leaked at some point (a seed
    fixed after its rows were written, a string written as a unicode escape
    that a grep for the character never saw). Rewriting the rendered page catches whatever
    the next gap is. HTML, JSON and plain-text bodies only; streamed and
    passthrough responses (files, SSE) are left alone.
    """
    try:
        if resp.direct_passthrough or resp.is_streamed:
            return resp
        ctype = resp.headers.get("Content-Type", "")
        if not ctype.startswith(("text/html", "application/json", "text/plain")):
            return resp
        body = resp.get_data()
        if (b"\xe2\x80\x94" not in body and b"&mdash;" not in body
                and b"&#8212;" not in body and b"&#x2014;" not in body):  # noqa: dash
            return resp
        resp.set_data(sanitize_copy(body.decode("utf-8")))
    except Exception:
        pass
    return resp


@app.after_request
def _ajax_stay_json(resp):
    """In-place ("stay in the box") form submits: when a form is posted with the
    X-BridgeMD-Ajax header, turn the normal post->redirect->flash into a small
    JSON payload the client uses to show the toast and refresh just the box/modal
    - so texting or adding a note never kicks you out to the full page. Only
    redirects are transformed; 200 re-renders (validation errors) pass through so
    the client can fall back to a normal submit. Non-AJAX requests are untouched."""
    try:
        if request.headers.get("X-BridgeMD-Ajax") != "1":
            return resp
        if request.method != "POST":
            return resp
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp
        location = resp.headers.get("Location", "") or ""
        msgs = get_flashed_messages(with_categories=True)
        toast, category = ("", "info")
        if msgs:
            category, toast = msgs[-1]
        payload = json.dumps({
            "ok": True, "toast": toast, "category": category,
            "redirect": location,
        })
        return app.response_class(payload, mimetype="application/json")
    except Exception:
        return resp


def _log_event(name, detail=None):
    """Best-effort funnel event with the current visitor + attribution.
    Owner traffic (ignored IPs or the signed-in owner) is never logged."""
    try:
        if _analytics_ignored():
            return
        attr = getattr(g, "attr", {}) or {}
        area = _area_from_ip(_client_ip())
        db.log_web_event(
            name, visitor=getattr(g, "visitor_id", ""), path=request.path,
            source=attr.get("s", ""), medium=attr.get("m", ""),
            campaign=attr.get("c", ""), referrer=attr.get("r", ""),
            detail=detail, ua=request.headers.get("User-Agent", ""),
            city=area.get("city", ""), region=area.get("region", ""),
            country=area.get("country", ""))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Delivery / notifications for the consumer loop. Fully wired but OFF by default
# so nothing is emailed during testing. Go-live is ~2 min: set these env vars.
#   NOTIFY_LIVE=1
#   SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_FROM
#   NOTIFY_SMS=1 + TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER
#   SITE_NOTIFY_EMAIL=contact@sonicmedicaltrust.com
#   PUBLIC_BASE_URL=https://yourdomain.com      (optional, for correct email links)
# On apply we email the local clinic (looked up from the CT.gov site + PI
# when the listing has no address). Sponsor/central inboxes are skipped.
# --------------------------------------------------------------------------- #
NOTIFY_LIVE = os.environ.get("NOTIFY_LIVE", "0") == "1"
# "For now, we connect them ourselves": every live applicant gets a personal
# note from the founder with the study's own public contacts, because a new
# platform's reply lag must not cost anyone an enrollment. Set to 0 once study
# teams reply on the thread fast enough that the note is redundant.
FOUNDER_CONNECT = os.environ.get("FOUNDER_CONNECT", "1") == "1"
# Last-resort recipient when no real clinic address exists for a study. This
# must be an inbox a person actually reads, so it defaults to the brand inbox
# (which forwards to the operator). It used to default to an early partner
# clinic whose inbox bounced, which silently ate applicant handoffs.
SITE_NOTIFY_EMAIL = (
    os.environ.get("SITE_NOTIFY_EMAIL") or "hello@bridgemd.health"
).strip()
# Internal inbox(es) that get a heads-up on every new application, so the operator
# can confirm the funnel is producing real, legit applications. De-identified.
# Comma-separated; defaults to the brand inbox + your personal owner email so you
# get it directly. Override with OWNER_NOTIFY_EMAIL.
OWNER_NOTIFY_EMAIL = os.environ.get(
    "OWNER_NOTIFY_EMAIL", f"hello@bridgemd.health, {OWNER_EMAIL}").strip()
# One canonical hostname for SEO + favicon consolidation. Requests to the
# `app.` / `www.` subdomains 301-redirect here (see _canonical_host_redirect),
# and absolute links (emails, SEO canonicals) are pinned to it below - so Google
# stops splitting index/favicon cache across multiple hosts.
CANONICAL_HOST = os.environ.get("CANONICAL_HOST", "bridgemd.health").strip().lower()
_REDIRECT_HOSTS = {"app." + CANONICAL_HOST, "www." + CANONICAL_HOST}

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
# Keep absolute links on the canonical apex even if the env var still points at
# the app./www. subdomain.
for _rh in _REDIRECT_HOSTS:
    if _rh in PUBLIC_BASE_URL:
        PUBLIC_BASE_URL = PUBLIC_BASE_URL.replace(_rh, CANONICAL_HOST)
_NOTIFIER = notifications_mod.Notifier(
    live=NOTIFY_LIVE,
    send_email_fn=mailer.send_email if mailer.smtp_configured() else None,
)


def _abs_url(endpoint, **kw):
    """Absolute URL for links placed inside emails.

    Background notify threads have no HTTP request, so Flask url_for can raise
    without SERVER_NAME. Build the known public paths off PUBLIC_BASE_URL.
    """
    base = (PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        host = (CANONICAL_HOST or "bridgemd.health").strip()
        base = "https://" + host
    token = kw.get("token")
    if endpoint == "candidate_page" and token:
        return f"{base}/c/{token}"
    if endpoint == "application_public" and token:
        return f"{base}/a/{token}"
    lead_id = kw.get("lead_id")
    if endpoint == "applicant_detail" and lead_id is not None:
        return f"{base}/app/applicant/{lead_id}"
    try:
        return base + url_for(endpoint, **kw)
    except RuntimeError:
        return base


def _oauth_redirect_url(endpoint):
    """Use the configured public host in production and the request host locally."""
    if IS_PROD:
        return _abs_url(endpoint)
    return url_for(endpoint, _external=True)


def notifications_ready():
    return _NOTIFIER.email_ready()


def _notify(to_addr, subject, body):
    """Single switch for every consumer-loop email. No-op (never errors) unless
    go-live is on AND SMTP is configured AND there's a recipient."""
    return _NOTIFIER.send(to_email=to_addr, subject=subject, email_body=body,
                          allow_sms=False)


def _notify_async(to_addr, subject, body):
    """Fire-and-forget variant of _notify: the SMTP send runs on a daemon thread
    so heads-up emails (new candidate, owner alert, site DM copy) never make an
    interactive request wait on the mail provider. The message is already built
    in the request; only the network send is deferred."""
    def _run():
        try:
            _NOTIFIER.send(to_email=to_addr, subject=subject, email_body=body,
                           allow_sms=False)
        except Exception:
            app.logger.exception("async notify failed")
    threading.Thread(target=_run, daemon=True).start()
    return True


def _notify_patient(to_email, to_phone, subject, email_body, sms_body):
    """Patient-facing notify: email by default, optional SMS when env-enabled."""
    return _NOTIFIER.send(
        to_email=to_email,
        to_phone=to_phone,
        subject=subject,
        email_body=email_body,
        sms_body=sms_body,
    )


def _notify_patient_async(to_email, to_phone, subject, email_body, sms_body):
    """Fire the SMTP/Twilio send on a daemon thread so an interactive request
    (e.g. sending a chat message, booking a visit) never blocks on the mail/SMS
    provider. Message text is already built in the request; only the network send
    is deferred. The no-op path stays instant (notifier short-circuits when off)."""
    def _run():
        try:
            _NOTIFIER.send(to_email=to_email, to_phone=to_phone, subject=subject,
                           email_body=email_body, sms_body=sms_body)
        except Exception:
            app.logger.exception("async patient notify failed")
    threading.Thread(target=_run, daemon=True).start()


def _gcal_sync_async(lead, visit, invite_url):
    """Push the visit to the site's connected Google Calendar off the request
    path - it's a best-effort network POST and must never make booking feel slow."""
    def _run():
        try:
            sync = calendar_invites.maybe_sync_google_event(lead, visit, invite_url)
            if sync.get("attempted") and not sync.get("ok"):
                app.logger.warning("google calendar sync failed: %s",
                                   sync.get("detail", "unknown"))
        except Exception:
            app.logger.exception("async google calendar sync failed")
    threading.Thread(target=_run, daemon=True).start()


def _notify_site_new_candidate(token, trial=None, lat=None, lon=None,
                               radius=None, unit="km", wait=False,
                               require_real=False):
    """Email local study clinics. Looks up public site/PI addresses when CT.gov
    only lists a name. Sponsor inboxes are skipped. Lookup can take a few
    seconds, so apply fires this in a background thread unless wait=True.
    """
    if wait:
        return _notify_site_new_candidate_sync(
            token, trial=trial, lat=lat, lon=lon, radius=radius, unit=unit,
            require_real=require_real)

    def _run():
        with app.app_context():
            try:
                _notify_site_new_candidate_sync(
                    token, trial=trial, lat=lat, lon=lon, radius=radius,
                    unit=unit)
            except Exception:
                app.logger.exception("clinic notify failed")
    threading.Thread(target=_run, daemon=True).start()
    return []


def _notify_site_new_candidate_sync(token, trial=None, lat=None, lon=None,
                                    radius=None, unit="km", require_real=False):
    lead = db.get_lead_by_token(token)
    if not lead:
        return []
    print(
        f"clinic-notify start lead={lead['id']} nct={lead['nct']}",
        flush=True)
    lat, lon, radius, unit = _notify_geo_for_lead(
        lead, lat=lat, lon=lon, radius=radius, unit=unit)
    recipients = _resolve_clinic_notify_recipients(
        lead, trial=trial, lat=lat, lon=lon, radius=radius, unit=unit)
    if not recipients:
        print(
            f"clinic-notify lead={lead['id']} nct={lead['nct']} no recipients",
            flush=True)
        return []
    if require_real and all(r.get("source") == "fallback" for r in recipients):
        return []
    if all(r.get("source") == "fallback" for r in recipients):
        # No real clinic, PI, or central contact was found. Mailing the
        # founder letter to our own inbox would look like a successful
        # handoff while nobody at the study team ever hears about the
        # applicant. Alert ops instead so a person follows up by hand.
        subject, body = mailer.build_no_clinic_contact_alert(lead)
        ok = _notify(OWNER_NOTIFY_EMAIL, subject, body)
        print(
            f"clinic-notify lead={lead['id']} nct={lead['nct']} "
            f"no real contact, ops alerted ok={ok}",
            flush=True)
        return []
    # The email is the application: everything inline, no link (the study
    # team asked for something they can enroll from directly).
    # The letter names the site the applicant chose (or the nearest one),
    # never a central contact's "Study contact" label.
    clinic = {"facility": (lead["site"] or "").split(",")[0].strip()}
    try:
        cards = site_contact_cards(_get_study(lead["nct"]), lead, limit=1)
        if cards:
            clinic["facility"] = cards[0]["facility"] or clinic["facility"]
            clinic["phone"], clinic["pi"] = cards[0]["phone"], cards[0]["pi"]
    except Exception:
        app.logger.exception("site card lookup failed")
    subject, body = mailer.build_candidate_message(
        lead, None, clinic=clinic, reach=db.count_inbound_applications())
    delivered = []
    for rec in recipients:
        rec["subject"] = subject
        ok = _notify(rec["email"], subject, body)
        print(
            f"clinic-notify lead={lead['id']} nct={lead['nct']} "
            f"to={rec['email']} ok={ok}",
            flush=True)
        if ok:
            delivered.append(rec)
        else:
            app.logger.error(
                "clinic notify send failed lead=%s to=%s",
                lead["id"], rec["email"])
    if not delivered:
        return []
    try:
        db.record_clinic_notify(lead["id"], delivered)
    except Exception:
        app.logger.exception("record clinic notify failed")
    return [rec["email"] for rec in delivered]


_DEFAULT_NOTIFY_RADIUS_KM = 80
_clinic_backfill_started = False
_clinic_backfill_lock = threading.Lock()


def _notify_geo_for_lead(lead, lat=None, lon=None, radius=None, unit="km"):
    """Use apply-form coordinates when present; otherwise geocode the city."""
    unit = (unit or "km").strip() or "km"
    try:
        radius = float(radius) if radius not in (None, "") else None
    except (TypeError, ValueError):
        radius = None
    if lat is not None and lon is not None:
        return lat, lon, radius or _DEFAULT_NOTIFY_RADIUS_KM, unit
    loc = ""
    try:
        loc = (lead["location"] or "").strip()
    except (KeyError, IndexError, TypeError):
        loc = ""
    if loc:
        try:
            geo = geocode(loc)
        except Exception:
            geo = None
        if geo:
            return geo[0], geo[1], radius or _DEFAULT_NOTIFY_RADIUS_KM, unit
    return lat, lon, radius, unit


def _backfill_clinic_notify_existing():
    """Email local clinics for every real inbound apply on the current copy.

    Includes past applications (the operator inbox set), not only ones that
    also logged a browser apply event. After a successful SMTP send we stamp
    the current copy so worker restarts do not mail the same clinics twice.
    """
    if not notifications_ready():
        print("clinic-notify backfill skipped (notify not live)", flush=True)
        return 0
    sent = 0
    fallbacks = {SITE_NOTIFY_EMAIL, "hello@bridgemd.health"}
    pending = [
        lead for lead in db.leads_missing_clinic_notify()
        if _is_inbound_application(lead) and (lead["nct"] or "").strip()
    ]
    # A handoff that only ever reached the operator fallback has not told the
    # study team. Try those again, and send only if a real address exists now
    # (a listing gained a contact, or a filter that hid one was fixed).
    retry = [
        lead for lead in db.list_leads()
        if _is_inbound_application(lead) and (lead["nct"] or "").strip()
        and db.lead_clinic_notify_only_reached(lead, fallbacks)
    ]
    print(f"clinic-notify backfill pending {len(pending)} apply(s), "
          f"{len(retry)} fallback-only to retry", flush=True)
    for lead in pending + retry:
        try:
            emails = _notify_site_new_candidate(
                lead["token"], wait=True, require_real=lead in retry)
        except Exception:
            print(
                f"clinic-notify backfill crash lead={lead['id']}",
                flush=True)
            app.logger.exception(
                "clinic notify backfill failed for lead %s", lead["id"])
            continue
        if emails:
            sent += 1
            print(
                f"clinic-notify backfill lead={lead['id']} nct={lead['nct']} "
                f"to={', '.join(emails)}",
                flush=True)
            app.logger.info(
                "clinic notify backfill lead=%s nct=%s to=%s",
                lead["id"], lead["nct"], ", ".join(emails))
    return sent


def _run_clinic_notify_backfill():
    with app.app_context():
        try:
            print("clinic-notify backfill start", flush=True)
            n = _backfill_clinic_notify_existing()
            print(f"clinic-notify backfill emailed {n} apply(s)", flush=True)
            app.logger.info("clinic notify backfill emailed %s existing apply(s)", n)
        except Exception:
            print("clinic-notify backfill crashed", flush=True)
            app.logger.exception("clinic notify backfill failed")


@app.before_request
def _kick_clinic_notify_backfill():
    """First request after boot emails any live apply that missed the new send."""
    global _clinic_backfill_started
    if _clinic_backfill_started or not notifications_ready():
        return
    with _clinic_backfill_lock:
        if _clinic_backfill_started:
            return
        _clinic_backfill_started = True
    threading.Thread(target=_run_clinic_notify_backfill, daemon=True).start()


@app.route("/ops/clinic-notify-backfill")
def ops_clinic_notify_backfill():
    """Owner trigger: email clinics for every inbound apply missing the current copy."""
    if not _ops_key_ok():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    n = _backfill_clinic_notify_existing()
    return jsonify({"ok": True, "emailed": n,
                    "notify_live": notifications_ready()})


def _boot_clinic_notify_backfill():
    """Do not wait for an HTTP request. Health checks were not starting the send."""
    print(
        f"clinic-notify boot live={NOTIFY_LIVE} "
        f"smtp={mailer.smtp_configured()} ready={notifications_ready()}",
        flush=True)
    if not notifications_ready():
        print("clinic-notify backfill skipped (notify not live)", flush=True)
        return
    global _clinic_backfill_started
    with _clinic_backfill_lock:
        if _clinic_backfill_started:
            return
        _clinic_backfill_started = True

    def _run():
        time.sleep(2)
        _run_clinic_notify_backfill()

    threading.Thread(target=_run, daemon=True).start()


_boot_clinic_notify_backfill()


_connect_backfill_started = False
_connect_backfill_lock = threading.Lock()


def _backfill_applicant_connect_emails():
    """Email live applicants their /a/ thread once. No clinic addresses."""
    if not notifications_ready():
        print("connect-email backfill skipped (notify not live)", flush=True)
        return 0
    sent = 0
    live = db.live_apply_index()
    pending = [
        lead for lead in db.leads_missing_connect_email()
        if _is_live_application(lead, live)
    ]
    print(f"connect-email backfill pending {len(pending)} apply(s)", flush=True)
    for lead in pending:
        try:
            if _notify_applicant_clinic_connect(lead):
                sent += 1
                print(
                    f"connect-email backfill lead={lead['id']} nct={lead['nct']}",
                    flush=True)
        except Exception:
            app.logger.exception(
                "connect email backfill failed for lead %s", lead["id"])
    return sent


def _run_connect_email_backfill():
    with app.app_context():
        try:
            print("connect-email backfill start", flush=True)
            n = _backfill_applicant_connect_emails()
            print(f"connect-email backfill emailed {n} apply(s)", flush=True)
        except Exception:
            print("connect-email backfill crashed", flush=True)
            app.logger.exception("connect email backfill failed")


# Central contacts on a listing are the study's own front door ("Trial
# questions: 1-877-... or LillyTrials@Lilly.com"). Some are aimed at doctors
# who want to run a site, not at participants; those are left out.
_CENTRAL_NOT_FOR_PATIENTS = ("investigator", "physician", "becoming", "site staff",
                             "media", "press", "investor")


def central_contacts_for_patients(trial):
    """The listing's central contacts that a participant or a coordinator
    can actually use: {name, phone, email}, cleaned, never a non-human inbox."""
    out = []
    for c in ((trial or {}).get("centralContacts") or []):
        name = (c.get("name") or "").strip()
        if any(k in name.lower() for k in _CENTRAL_NOT_FOR_PATIENTS):
            continue
        phone = (c.get("phone") or "").strip()
        email = (c.get("email") or "").strip()
        if email and db.is_placeholder_site_email(email):
            email = ""
        if not (phone or email):
            continue
        out.append({"name": _clean_name(name), "phone": phone, "email": email})
    return out


def site_contact_cards(trial, lead, limit=3):
    """Nearby recruiting sites as cards a person can act on: facility, city,
    the listed phone, a real email if any, and the principal investigator's
    name. Straight from the ClinicalTrials.gov listing."""
    lat, lon, radius, unit = _notify_geo_for_lead(lead)
    site_label = ""
    try:
        site_label = (lead["site"] or "").strip()
    except (KeyError, IndexError, TypeError):
        site_label = ""
    cards = []
    for site in sites_for_patient_area(trial, site_label=site_label, lat=lat,
                                       lon=lon, radius=radius, unit=unit):
        phone, pi = "", ""
        for c in (site.get("contacts") or []):
            if _is_pi_contact(c) and not pi:
                pi = _clean_name(c.get("name"))
            elif not phone and (c.get("phone") or "").strip():
                phone = (c.get("phone") or "").strip()
        if not phone:
            for c in (site.get("contacts") or []):
                if (c.get("phone") or "").strip():
                    phone = (c.get("phone") or "").strip()
                    break
        email = ""
        for rec in clinic_emails_from_site(site):
            if not db.is_placeholder_site_email(rec["email"]):
                email = rec["email"]
                break
        if phone or email or pi:
            cards.append({"facility": site.get("facility") or "",
                          "city": site.get("city") or "",
                          "phone": phone, "email": email, "pi": pi})
        if len(cards) >= limit:
            break
    return cards


def _founder_connect_payload(lead):
    """The study's own public contacts for this applicant: up to three nearby
    recruiting sites (phone first) plus the study information line. Placeholder
    and garbled addresses never appear (db.is_placeholder_site_email)."""
    nct = (lead["nct"] or "").strip()
    if not nct:
        return [], []
    try:
        trial = _get_study(nct)
    except Exception:
        app.logger.exception("founder connect: study fetch failed for %s", nct)
        return [], []
    return site_contact_cards(trial, lead), central_contacts_for_patients(trial)


def _notify_applicant_founder_connect(token):
    """One-time personal email with the study's direct contacts. Sends only
    when there is something useful to say; stamps either way so the boot
    backfill does not retry a contactless study forever."""
    lead = db.get_lead_by_token(token)
    if not lead or not (lead["email"] or "").strip():
        return False
    sites, central = _founder_connect_payload(lead)
    if not sites and not central:
        app.logger.info("founder connect: no public contacts for lead %s (%s)",
                        lead["id"], lead["nct"])
        db.mark_founder_connect(lead["id"])
        return False
    subject, body = mailer.build_founder_connect_message(lead, sites, central)
    ok = _notify_patient(lead["email"], "", subject, body, "")
    if ok:
        db.mark_founder_connect(lead["id"])
        print(f"founder-connect lead={lead['id']} nct={lead['nct']}", flush=True)
    return ok


def _notify_applicant_founder_connect_async(token):
    def _run():
        with app.app_context():
            try:
                _notify_applicant_founder_connect(token)
            except Exception:
                app.logger.exception("founder connect failed")
    threading.Thread(target=_run, daemon=True).start()


_founder_backfill_started = False
_founder_backfill_lock = threading.Lock()


def _boot_founder_connect_backfill():
    """One pass over live applicants who never got the founder contacts email,
    so everyone already waiting is connected the moment this ships."""
    if not FOUNDER_CONNECT or not notifications_ready():
        return
    global _founder_backfill_started
    with _founder_backfill_lock:
        if _founder_backfill_started:
            return
        _founder_backfill_started = True

    def _run():
        time.sleep(8)
        with app.app_context():
            try:
                live = db.live_apply_index()
                pending = [
                    lead for lead in db.leads_missing_founder_connect()
                    if _is_live_application(lead, live)
                ]
                print(f"founder-connect backfill pending {len(pending)}",
                      flush=True)
                for lead in pending:
                    try:
                        _notify_applicant_founder_connect(lead["token"])
                    except Exception:
                        app.logger.exception(
                            "founder connect backfill failed for lead %s",
                            lead["id"])
            except Exception:
                app.logger.exception("founder connect backfill failed")

    threading.Thread(target=_run, daemon=True).start()


def _boot_connect_email_backfill():
    print(
        f"connect-email boot live={NOTIFY_LIVE} "
        f"smtp={mailer.smtp_configured()} ready={notifications_ready()}",
        flush=True)
    if not notifications_ready():
        print("connect-email backfill skipped (notify not live)", flush=True)
        return
    global _connect_backfill_started
    with _connect_backfill_lock:
        if _connect_backfill_started:
            return
        _connect_backfill_started = True

    def _run():
        time.sleep(4)
        _run_connect_email_backfill()

    threading.Thread(target=_run, daemon=True).start()


_boot_connect_email_backfill()
_boot_founder_connect_backfill()


def _boot_area_label_backfill():
    """Existing applications stored a bare postal code as their location.
    Resolve those to a city once so every email and inbox row reads like a
    place. Small, background, best-effort."""
    def _run():
        time.sleep(10)
        with app.app_context():
            try:
                n = 0
                for row in db.leads_with_postal_location(limit=50):
                    label = display_area(row["location"])
                    if label and label != (row["location"] or "").strip():
                        db.set_lead_location(row["id"], label)
                        n += 1
                print(f"area-label backfill updated {n}", flush=True)
            except Exception:
                app.logger.exception("area label backfill failed")
    threading.Thread(target=_run, daemon=True).start()


_boot_area_label_backfill()
print(f"llm boot key={bool(mt.LLM_API_KEY)} model={mt.LLM_MODEL} "
      f"base={mt.LLM_BASE_URL}", flush=True)


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
    # Deep-link straight to this applicant's full record (contact + screener +
    # eligibility) so the operator can act/forward in one click, plus the
    # cross-study inbox for the tabular view of everything.
    subject, body = mailer.build_owner_new_application(lead)
    return _notify_async(", ".join(recipients), subject, body)


def _applicant_thread_url(lead):
    """Secret link to this application. Guests can open it without signing in."""
    if not lead:
        return _abs_url("applications")
    tok = ""
    try:
        tok = lead["token"] or ""
    except (KeyError, IndexError, TypeError):
        tok = ""
    if not tok:
        try:
            tok = lead["lead_token"] or ""
        except (KeyError, IndexError, TypeError):
            tok = ""
    if not tok:
        return _abs_url("applications")
    return _abs_url("application_public", token=tok)


def _notify_applicant(token, kind):
    """Tell the applicant their status changed. kind in {accepted, declined,
    screening, enrolled}."""
    lead = db.get_lead_by_token(token)
    if not lead or (not lead["email"] and not lead["phone"]):
        return False
    link = _applicant_thread_url(lead)
    subject, body = mailer.build_applicant_message(lead, kind, link)
    _notify_patient_async(lead["email"], lead["phone"], subject, body, "")
    return True


def _notify_applicant_by_id(lead_id, kind):
    lead = db.get_lead(lead_id)
    return _notify_applicant(lead["token"], kind) if lead else False


def _notify_applicant_apply_confirmation(token, clinic_contacts=None):
    """BridgeMD emails the applicant their thread. No-op if notify is off."""
    _ = clinic_contacts
    lead = db.get_lead_by_token(token)
    if not lead or (not lead["email"] and not lead["phone"]):
        return False
    subject, body = mailer.build_apply_confirmation(
        lead, _applicant_thread_url(lead))
    ok = _notify_patient(lead["email"], lead["phone"], subject, body, "")
    if ok:
        db.mark_connect_emailed(lead["id"])
    return ok


def _notify_applicant_clinic_connect(lead, clinic_contacts=None):
    """BridgeMD emails an existing applicant their thread. No clinic address."""
    _ = clinic_contacts
    if not lead or (not lead["email"] and not lead["phone"]):
        return False
    subject, body = mailer.build_clinic_connect_message(
        lead, _applicant_thread_url(lead))
    ok = _notify_patient(lead["email"], lead["phone"], subject, body, "")
    if ok:
        db.mark_connect_emailed(lead["id"])
    return ok


_PLACEHOLDER_SCHEDULE_MARKERS = (
    "calendly.com/bridgemd-demo",
    "meet.google.com/bmd-demo",
)


def _is_placeholder_schedule_url(url):
    """Demo seed links that must never go to a real applicant."""
    u = (url or "").strip().lower()
    return any(marker in u for marker in _PLACEHOLDER_SCHEDULE_MARKERS)


def _notify_applicant_schedule(lead):
    """Never email an applicant a calendar / pick-a-time link."""
    if lead:
        app.logger.warning(
            "refusing calendar email for lead %s", lead["id"])
    return False


def _lead_source_key(lead):
    """The lead's normalized channel key (instagram|messenger|email_intake|...)."""
    try:
        return (lead["source"] or "").strip().lower()
    except (KeyError, IndexError, TypeError):
        return ""


def _connector_send_async(url, secret, lead, body):
    """POST an outbound reply to the site's connector so it lands back on the
    social channel the lead came from (IG DM, Messenger, ...). Best-effort and
    off-thread: the message is already saved in-thread, so a slow/broken
    connector never blocks the coordinator. Signed with the site's secret."""
    try:
        ext = lead["external_ref"]
    except (KeyError, IndexError, TypeError):
        ext = ""
    payload = json.dumps({
        "lead_id": lead["id"],
        "nct": lead["nct"],
        "channel": _lead_source_key(lead),
        "external_ref": ext or "",
        "to_name": lead["name"] or "",
        "body": body,
        "sent_at": db.now(),
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-BridgeMD-Signature"] = hmac.new(
            secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    def _go():
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read()
        except Exception:
            app.logger.warning("connector send failed for lead %s", lead["id"])

    threading.Thread(target=_go, daemon=True).start()
    return True


def _deliver_reply(lead, body, connector=None):
    """Route a coordinator's reply to wherever the lead can actually receive it:
    a social-channel lead (IG/FB/Messenger/WhatsApp) goes out through the site's
    connector; everyone else gets the usual email/SMS. Falls back to email if a
    social lead has no connector configured yet, so nothing is silently dropped."""
    src = _lead_source_key(lead)
    if src in db.CONNECTOR_CHANNELS:
        url, secret = connector if connector is not None \
            else db.get_site_connector(g.user["id"])
        if url:
            return _connector_send_async(url, secret, lead, body)
    return _notify_applicant_message(lead, body)


def _notify_applicant_message(lead, body):
    if not lead or (not lead["email"] and not lead["phone"]):
        return False
    link = _applicant_thread_url(lead)
    subject, msg = mailer.build_dm_message(lead, body, link, to="patient")
    sms = mailer.build_dm_sms(lead, link, to="patient")
    _notify_patient_async(lead["email"], lead["phone"], subject, msg, sms)
    return True


def _notify_site_message(lead, body):
    """An applicant wrote on their thread. Their words must always land in an
    inbox a person reads: the claimed site when it has a real address,
    otherwise the operator's fallback. site_contact_for_nct already refuses
    placeholder addresses."""
    if not lead:
        return False
    to_addr = db.site_contact_for_nct(lead["nct"]) or SITE_NOTIFY_EMAIL
    link = _abs_url("candidate_page", token=lead["site_token"])
    subject, msg = mailer.build_dm_message(lead, body, link, to="site")
    return _notify_async(to_addr, subject, msg)


def _notify_applicant_visit(lead, when, location, invite_url=""):
    if not lead or (not lead["email"] and not lead["phone"]):
        return False
    subject, body = mailer.build_visit_message(
        lead, when, location, _applicant_thread_url(lead), invite_url=invite_url)
    sms = mailer.build_reminder_sms(
        lead, when, location, _applicant_thread_url(lead))
    _notify_patient_async(lead["email"], lead["phone"], subject, body, sms)
    return True


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
    try:
        prep = visit["prep"]
    except (IndexError, KeyError):
        prep = ""
    thread = _applicant_thread_url(db.get_lead(visit["lead_id"]))
    subject, body = mailer.build_reminder_message(
        visit, visit["visit_at"], visit["location"], thread, prep=prep)
    sms = mailer.build_reminder_sms(
        visit, visit["visit_at"], visit["location"], thread)
    return _notify_patient(visit["email"], visit["phone"], subject, body, sms)


def _clinic_checkin(lead, prior=0):
    """A quiet application gets a status-check email to the study team that
    handled it, not another message to the applicant - they can't act on a
    "just checking in", but the team can tell us where things actually stand."""
    contacts = db.lead_clinic_notify(lead)
    if not contacts:
        return False
    subject, body = mailer.build_clinic_checkin_message(lead, prior)
    delivered = False
    for c in contacts:
        if c.get("email") and _notify(c["email"], subject, body):
            delivered = True
    return delivered


# Proactively remind patients about visits and check in with study teams on
# quiet applications (retention). In demo/local builds the message threads are
# curated seed data; letting the live background sweep run against a
# persistent demo DB just stacks repeated visit-reminders onto the same
# threads over calendar time. Default it OFF in demo - still runnable on
# demand via /reminders/run, or force it with REMINDERS_BACKGROUND=1.
if NO_LOGIN and "REMINDERS_BACKGROUND" not in os.environ:
    os.environ["REMINDERS_BACKGROUND"] = "0"
reminders_mod.configure(app, on_visit=_remind_visit, on_nudge=_clinic_checkin)


def _active_scope():
    """The single trial scope for the whole app, driven by the top switcher.

    Returns (active_nct, team_studies). ``active_nct == ""`` means "All studies"
    (an explicit choice the user made in the switcher). When the user has never
    chosen, we default to their first study so the dashboard opens as a workspace
    rather than an empty greeting. Every page reads scope from here so there is
    exactly one scoper - no per-page filter chips fighting the switcher."""
    studies = []
    if g.user:
        try:
            studies = db.list_team_studies(g.user["id"])
        except Exception:
            studies = []
    valid = {s["nct"] for s in studies}
    if "active_nct" not in session:
        # Demo builds open on the whole book of business (all trials) so the home
        # shows the full product at a glance; a real single-site user defaults to
        # their first study as a focused workspace.
        if studies and (_demo_mode_enabled() or _site_demo_enabled()
                        or _is_demo_account(g.user)):
            nct = ""
        else:
            nct = studies[0]["nct"] if studies else ""
    else:
        nct = session.get("active_nct") or ""   # "" == All studies (explicit)
        if nct and nct not in valid:
            nct = studies[0]["nct"] if studies else ""
    return nct, studies


def _scope_label(active_nct, nav_studies):
    """Human label for the switcher: study title, 'All studies', or empty."""
    if not nav_studies:
        return ""
    if not active_nct:
        return "All studies"
    return next((s["title"] or s["nct"] for s in nav_studies
                 if s["nct"] == active_nct), "")


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
        site_unread = (
            db.marketing_thread_counts(g.user["id"])["unread"]
            if g.user else 0
        )
    except Exception:
        site_unread = 0
    # In no-login testing mode we show a small switcher so you can preview all
    # three POVs (patient / clinician / study team) without signing in.
    path = request.path or "/"
    if (path.startswith("/app/leads") or path.startswith("/app/dashboard")
            or path.startswith("/app/inbox") or path.startswith("/app/onboarding")
            or path.startswith("/app/site") or path.startswith("/app/messages")
            or path.startswith("/app/analytics") or path.startswith("/app/home")
            or path.startswith("/app/balance")
            or path.startswith("/app/applicant") or path.startswith("/app/matching")
            or path.startswith("/app/documents")
            or path.startswith("/app/team") or path.startswith("/app/calendar")
            or path.startswith("/app/payments") or path.startswith("/app/updates")
            or path.startswith("/app/campaign") or path.startswith("/app/intake")
            or path.startswith("/app/soe") or path.startswith("/app/irb")
            or path.startswith("/app/mentions") or path.startswith("/app/away")
            or path.startswith("/marketing-hub")):
        pov = "study"
    elif path.startswith("/app") or path.startswith("/referral"):
        pov = "clinician"
    else:
        pov = "patient"
    nav_study_ncts = []
    nav_studies = []
    active_nct = ""
    active_study_label = ""
    my_role = ""
    my_role_label = ""
    can_manage_team = False
    matches_new = 0
    mentions_new = 0
    my_away = None
    am_covering = []
    cal_due = 0
    pay_due = 0
    upd_due = 0
    if g.user:
        try:
            nav_study_ncts = sorted(db.user_claimed_ncts(g.user["id"]))
        except Exception:
            nav_study_ncts = []
        try:
            matches_new = db.patient_match_counts(g.user["id"]).get("new", 0)
        except Exception:
            matches_new = 0
        # Sidebar badge: visits still to run in the next 7 days.
        try:
            _cw0 = db.now()
            _cw1 = (dt.datetime.now() + dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
            cal_due = sum(1 for v in db.list_calendar_visits(g.user["id"], _cw0, _cw1)
                          if (v["status"] or "scheduled") == "scheduled")
        except Exception:
            cal_due = 0
        # Sidebar badge: participant payments queued but not yet issued.
        try:
            pay_due = sum(1 for p in db.list_payments(g.user["id"])
                          if p["status"] == "queued")
        except Exception:
            pay_due = 0
        # Sidebar badge: sponsor updates/amendments with an open required step.
        try:
            upd_due = db.sponsor_updates_action_count(g.user["id"])
        except Exception:
            upd_due = 0
        try:
            active_nct, _studies = _active_scope()
            nav_studies = [{"nct": s["nct"],
                            "title": summarize.tidy_title(s["title"]) or s["nct"]}
                           for s in _studies]
            active_study_label = _scope_label(active_nct, nav_studies)
        except Exception:
            nav_studies = []
        # Per-study unread badges for the switcher, so the coordinator can see which
        # trial needs attention without opening each one. One grouped query.
        try:
            _unread_by_study = db.marketing_unread_by_study(g.user["id"])
            for _s in nav_studies:
                _s["unread"] = _unread_by_study.get(_s["nct"], 0)
        except Exception:
            pass
        try:
            my_role = db.member_role(g.user["id"])
            my_role_label = db.member_role_label(g.user["id"])
            can_manage_team = db.can_manage_team(g.user["id"])
        except Exception:
            pass
        # Collaboration state every screen needs: how many teammates pulled you
        # into something, whether you're away, and whose queue you're holding.
        try:
            mentions_new = db.unread_mention_count(g.user["id"])
        except Exception:
            mentions_new = 0
        try:
            my_away = db.active_away_for(g.user["id"])
            am_covering = db.covering_for(g.user["id"])
        except Exception:
            my_away, am_covering = None, []
    return {"user": g.user, "llm_on": bool(mt.LLM_API_KEY),
            "mentions_new": mentions_new, "my_away": my_away,
            "am_covering": am_covering,
            "member_role": my_role, "member_role_label": my_role_label,
            "can_manage_team": can_manage_team,
            "applications_count": apps_n, "pov_demo": _demo_mode_enabled(),
            "demo_available": NO_LOGIN, "pov": pov,
            "records_profile": rec, "records_provider": records_mod.provider_label(),
            "records_ui": RECORDS_UI,
            "alerts_new_count": alerts_new, "messages_unread": msgs_unread,
            "site_unread": site_unread, "patient_user": g.patient_user,
            "site_demo": _site_demo_enabled(),
            # Expose the guide only for the isolated seeded account. A signed-in
            # site should keep its normal, production-style workspace.
            "active_site_demo": bool(g.user and _site_demo_enabled()
                                     and _is_demo_account(g.user)),
            # Guided walkthrough (homepage tour banner + sidebar step guide) is
            # temporarily hidden while it's being reworked. Flip to re-enable.
            "walkthrough_enabled": False,
            "nav_study_ncts": nav_study_ncts,
            "nav_studies": nav_studies, "active_nct": active_nct,
            "active_study_label": active_study_label,
            "matches_new": matches_new, "cal_due": cal_due,
            "pay_due": pay_due, "upd_due": upd_due,
            "sites_home_url": _sites_home_url(),
            "cal_link": CAL_LINK,
            "sites_nav_features": sites_features.nav_items(),
            "guest_roles": db.GUEST_ROLES,
            "is_owner": _is_owner()}


def _sites_home_url():
    """Absolute URL of the research-site marketing page.

    Uses the dedicated subdomain when SITES_HOST is set, otherwise /inbox
    on the current host. The site root is the patient trial finder.
    """
    if SITES_HOST:
        scheme = "https" if os.environ.get("BEHIND_PROXY") else request.scheme
        return f"{scheme}://{SITES_HOST}/inbox"
    try:
        return url_for("inbox")
    except Exception:
        return "/inbox"


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
        return redirect(url_for(_home_endpoint()))
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
        return redirect(url_for(_home_endpoint()))
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
    session.pop(MARKETING_INSTAGRAM_STATE_KEY, None)
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
        "redirect_uri": _oauth_redirect_url("user_google_callback"),
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
app.jinja_env.globals["today_iso"] = lambda: dt.date.today().isoformat()


def _llm_allowed_for_request():
    """Model calls are for people. Outside a request (background jobs, tests)
    they are allowed; inside one, a crawler User-Agent turns them off."""
    from flask import has_request_context
    if not has_request_context():
        return True
    return not _is_bot()


summarize.ALLOW_LLM_HOOK = _llm_allowed_for_request


_ASSET_VER_CACHE = {}


def static_url(filename):
    """url_for('static', ...) with a ?v=<mtime> cache-buster so browsers/CDNs
    fetch a fresh copy whenever a static asset changes (otherwise long-lived
    caches keep serving stale CSS/JS/logo)."""
    try:
        ver = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
    except OSError:
        ver = _ASSET_VER_CACHE.get(filename, 0)
    _ASSET_VER_CACHE[filename] = ver
    return url_for("static", filename=filename) + (f"?v={ver}" if ver else "")


app.jinja_env.globals["static_url"] = static_url


@app.before_request
def _csrf_guard():
    _csrf_token()
    if request.method != "POST":
        return None
    if request.endpoint in {"alerts_run", "reminders_run", "redcap_webhook",
                            "instagram_webhook", "instagram_deauthorize",
                            "instagram_data_deletion",
                            "ops_verify_claim", "inbound_email_webhook",
                            "inbound_lead_webhook", "resend_webhook"}:
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


class InstagramAPIError(RuntimeError):
    def __init__(self, message, *, status=0, code=""):
        super().__init__(message)
        self.status = int(status or 0)
        self.code = str(code or "")[:80]


def _instagram_ready():
    return bool(INSTAGRAM_APP_ID and INSTAGRAM_APP_SECRET and INSTAGRAM_AUTH_URL)


def _instagram_json_request(url, *, data=None, method="GET", headers=None):
    """Call Instagram without leaking credential-bearing URLs into logs."""
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "BridgeMD-Instagram/1.0",
        **(headers or {}),
    }
    req = urllib.request.Request(
        url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code or 0)
        code = ""
        try:
            error_payload = json.loads(exc.read(65536).decode("utf-8"))
            error = error_payload.get("error", error_payload)
            if isinstance(error, dict):
                code = error.get("code") or error.get("error_subcode") or ""
        except Exception:
            pass
        app.logger.warning(
            "Instagram API rejected a request (status=%s code=%s)",
            status, code or "unknown")
        raise InstagramAPIError(
            "Instagram rejected the request.", status=status, code=code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise InstagramAPIError("Instagram could not be reached.") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise InstagramAPIError("Instagram returned an invalid response.") from None
    if not isinstance(payload, dict):
        raise InstagramAPIError("Instagram returned an invalid response.")
    return payload


def _instagram_authorization_url(state):
    parts = urllib.parse.urlsplit(INSTAGRAM_AUTH_URL)
    params = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    params.update({
        "client_id": INSTAGRAM_APP_ID,
        "redirect_uri": _oauth_redirect_url("marketing_instagram_callback"),
        "response_type": "code",
        "scope": ",".join(INSTAGRAM_OAUTH_SCOPES),
        "state": state,
        "enable_fb_login": "0",
        "force_authentication": "1",
    })
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc, parts.path,
        urllib.parse.urlencode(params), parts.fragment))


def _instagram_exchange_code(code):
    body = urllib.parse.urlencode({
        "client_id": INSTAGRAM_APP_ID,
        "client_secret": INSTAGRAM_APP_SECRET,
        "grant_type": "authorization_code",
        "redirect_uri": _oauth_redirect_url("marketing_instagram_callback"),
        "code": code,
    }).encode("utf-8")
    payload = _instagram_json_request(
        INSTAGRAM_TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    items = payload.get("data")
    token_data = items[0] if isinstance(items, list) and items else payload
    if not isinstance(token_data, dict):
        raise InstagramAPIError("Instagram returned no account authorization.")
    return token_data


def _instagram_exchange_long_lived_token(short_lived_token):
    query = urllib.parse.urlencode({
        "grant_type": "ig_exchange_token",
        "client_secret": INSTAGRAM_APP_SECRET,
        "access_token": short_lived_token,
    })
    return _instagram_json_request(
        f"{INSTAGRAM_GRAPH_API_BASE_URL}/access_token?{query}")


def _instagram_profile(access_token, instagram_user_id):
    account_id = urllib.parse.quote(str(instagram_user_id), safe="")
    query = urllib.parse.urlencode({
        "fields": "user_id,username,name,account_type",
    })
    return _instagram_json_request(
        f"{INSTAGRAM_GRAPH_API_BASE_URL}/{INSTAGRAM_GRAPH_API_VERSION}/"
        f"{account_id}?{query}",
        headers={"Authorization": f"Bearer {access_token}"})


def _instagram_subscribe_webhooks(access_token, instagram_user_id):
    account_id = urllib.parse.quote(str(instagram_user_id), safe="")
    body = urllib.parse.urlencode({"subscribed_fields": "messages"}).encode(
        "utf-8")
    payload = _instagram_json_request(
        f"{INSTAGRAM_GRAPH_API_BASE_URL}/{INSTAGRAM_GRAPH_API_VERSION}/"
        f"{account_id}/subscribed_apps",
        data=body, method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        })
    if payload.get("success") is not True:
        raise InstagramAPIError(
            "Instagram did not enable message notifications.")
    return True


def _instagram_unsubscribe_webhooks(access_token, instagram_user_id):
    if not access_token or not instagram_user_id:
        return False
    account_id = urllib.parse.quote(str(instagram_user_id), safe="")
    payload = _instagram_json_request(
        f"{INSTAGRAM_GRAPH_API_BASE_URL}/{INSTAGRAM_GRAPH_API_VERSION}/"
        f"{account_id}/subscribed_apps",
        method="DELETE",
        headers={"Authorization": f"Bearer {access_token}"})
    return payload.get("success") is True


def _instagram_send_reply(access_token, instagram_user_id, recipient_id, body):
    """Send a human-reviewed reply to a user who initiated an Instagram thread."""
    account_id = urllib.parse.quote(str(instagram_user_id), safe="")
    payload = json.dumps({
        "recipient": {"id": str(recipient_id)},
        "message": {"text": (body or "").strip()[:1000]},
    }).encode("utf-8")
    result = _instagram_json_request(
        f"{INSTAGRAM_GRAPH_API_BASE_URL}/{INSTAGRAM_GRAPH_API_VERSION}/"
        f"{account_id}/messages",
        data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        })
    message_id = str(result.get("message_id") or result.get("id") or "")
    if not message_id:
        raise InstagramAPIError("Instagram did not confirm message delivery.")
    return message_id


def _meta_signed_request_payload(signed_request):
    """Verify and decode a Meta signed_request using the Instagram app secret."""
    signed_request = (signed_request or "").strip()
    if not INSTAGRAM_APP_SECRET:
        raise RuntimeError("Instagram lifecycle callbacks are not configured.")
    if not signed_request or len(signed_request) > 16384:
        raise ValueError("Invalid signed request.")
    try:
        encoded_signature, encoded_payload = signed_request.split(".", 1)
        signature = base64.urlsafe_b64decode(
            encoded_signature + "=" * (-len(encoded_signature) % 4))
        payload_bytes = base64.urlsafe_b64decode(
            encoded_payload + "=" * (-len(encoded_payload) % 4))
        expected = hmac.new(
            INSTAGRAM_APP_SECRET.encode("utf-8"),
            encoded_payload.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid signed request.")
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError):
        raise ValueError("Invalid signed request.") from None
    if not isinstance(payload, dict):
        raise ValueError("Invalid signed request.")
    algorithm = str(payload.get("algorithm") or "").upper().replace("_", "-")
    if algorithm and algorithm != "HMAC-SHA256":
        raise ValueError("Invalid signed request.")
    return payload


def _google_exchange_code(code, redirect_endpoint="patient_google_callback"):
    payload = urllib.parse.urlencode({
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": _oauth_redirect_url(redirect_endpoint),
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


def _google_refresh_access_token(refresh_token):
    payload = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(
        GOOGLE_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _gmail_profile(access_token):
    req = urllib.request.Request(
        GMAIL_PROFILE_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _google_revoke_token(token):
    if not token:
        return False
    payload = urllib.parse.urlencode({"token": token}).encode("utf-8")
    req = urllib.request.Request(
        GOOGLE_REVOKE_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20):
        return True


def _oauth_token_expiry(expires_in):
    try:
        seconds = max(0, int(expires_in))
    except (TypeError, ValueError):
        seconds = 3600
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
    return expires.replace(microsecond=0).isoformat()


def _gmail_connection_access_token(user_id, source_id):
    """Return a usable token for Gmail sync/send operations."""
    connection = db.get_marketing_connection_for_source(user_id, source_id)
    if not connection or connection["provider"] != "gmail":
        raise RuntimeError("Gmail is not connected for this source.")
    if connection["status"] != "connected":
        raise RuntimeError("Gmail must be reconnected.")

    try:
        expires_at = dt.datetime.fromisoformat(connection["token_expires_at"] or "")
    except (TypeError, ValueError):
        expires_at = None
    now_utc = dt.datetime.now(dt.timezone.utc)
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.timezone.utc)
    try:
        access_token = token_crypto.decrypt_token(
            connection["access_token_encrypted"], app.secret_key)
        if access_token and expires_at and expires_at > now_utc + dt.timedelta(minutes=2):
            return access_token
        refresh_token = token_crypto.decrypt_token(
            connection["refresh_token_encrypted"], app.secret_key)
    except token_crypto.TokenEncryptionError:
        db.set_marketing_connection_error(
            user_id, connection["id"], "token_decryption_failed",
            "Stored credentials can no longer be decrypted.", "needs_reauth")
        raise RuntimeError("Gmail must be reconnected.") from None
    if not refresh_token:
        db.set_marketing_connection_error(
            user_id, connection["id"], "refresh_token_missing",
            "Google did not provide an offline refresh token.", "needs_reauth")
        raise RuntimeError("Gmail must be reconnected.")
    try:
        refreshed = _google_refresh_access_token(refresh_token)
        access_token = (refreshed or {}).get("access_token", "")
        if not access_token:
            raise RuntimeError("Google returned no access token.")
        encrypted = token_crypto.encrypt_token(access_token, app.secret_key)
        db.update_marketing_connection_tokens(
            user_id, connection["id"], access_token_encrypted=encrypted,
            token_expires_at=_oauth_token_expiry(refreshed.get("expires_in")),
            refresh_token_encrypted=(
                token_crypto.encrypt_token(refreshed["refresh_token"], app.secret_key)
                if refreshed.get("refresh_token") else None),
            granted_scopes=(refreshed.get("scope") or "").split() or None)
        return access_token
    except Exception:
        db.set_marketing_connection_error(
            user_id, connection["id"], "token_refresh_failed",
            "Google rejected the stored credentials.", "needs_reauth")
        raise RuntimeError("Gmail must be reconnected.") from None


class GmailSyncError(RuntimeError):
    def __init__(self, message, *, status=0, requires_reauth=False):
        super().__init__(message)
        self.status = status
        self.requires_reauth = requires_reauth


class GmailDeliveryError(RuntimeError):
    def __init__(self, message, *, requires_reauth=False, uncertain=False):
        super().__init__(message)
        self.requires_reauth = requires_reauth
        self.uncertain = uncertain


class _GmailAPIError(GmailSyncError):
    def __init__(self, status, code, message):
        super().__init__(message, status=status,
                         requires_reauth=status in (401, 403))
        self.code = (code or f"http_{status}") if status else "network_error"


def _gmail_api_get(access_token, resource, params=None):
    """Call one authenticated Gmail JSON endpoint without exposing the token."""
    if resource.startswith("https://"):
        url = resource
    else:
        url = f"{GMAIL_API_BASE_URL}/{resource.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access_token}"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = "Google could not complete the Gmail request."
        code = f"http_{exc.code}"
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = payload.get("error") or {}
            if isinstance(detail, dict):
                message = (detail.get("message") or message).strip()
                code = (detail.get("status") or code).strip().lower()
        except Exception:
            pass
        raise _GmailAPIError(exc.code, code, message) from None
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", None)
        message = str(reason or "Gmail is temporarily unreachable.")
        raise _GmailAPIError(0, "network_error", message) from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _GmailAPIError(502, "invalid_response",
                             "Google returned an invalid Gmail response.") from None


def _gmail_api_post(access_token, resource, payload):
    url = f"{GMAIL_API_BASE_URL}/{resource.lstrip('/')}"
    req = urllib.request.Request(
        url, data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = "Google could not complete the Gmail request."
        code = f"http_{exc.code}"
        try:
            result = json.loads(exc.read().decode("utf-8"))
            detail = result.get("error") or {}
            if isinstance(detail, dict):
                message = (detail.get("message") or message).strip()
                code = (detail.get("status") or code).strip().lower()
        except Exception:
            pass
        raise _GmailAPIError(exc.code, code, message) from None
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", None)
        message = str(reason or "Gmail is temporarily unreachable.")
        raise _GmailAPIError(0, "network_error", message) from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _GmailAPIError(502, "invalid_response",
                             "Google returned an invalid Gmail response.") from None


class _EmailHTMLTextParser(HTMLParser):
    _BLOCKS = {
        "address", "article", "blockquote", "br", "div", "footer", "h1",
        "h2", "h3", "h4", "h5", "h6", "header", "li", "p", "section",
        "table", "td", "th", "tr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        values = {key.lower(): value or "" for key, value in attrs}
        classes = values.get("class", "").lower().split()
        if self.suppressed:
            self.suppressed += 1
            return
        if tag in ("script", "style") or "gmail_quote" in classes:
            self.suppressed = 1
            return
        if tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.suppressed:
            self.suppressed -= 1
            return
        if tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)

    def text(self):
        return "".join(self.parts)


def _clean_email_text(value):
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "").replace("\u00a0", " ")
    quote_markers = (
        r"(?mi)^\s*On .{1,500} wrote:\s*$",
        r"(?mi)^\s*-{2,}\s*Original Message\s*-{2,}\s*$",
        r"(?mi)^\s*From:\s*.+\n\s*Sent:\s*.+$",
    )
    cut = len(text)
    for marker in quote_markers:
        match = re.search(marker, text)
        if match:
            cut = min(cut, match.start())
    text = text[:cut]
    lines = []
    blank = False
    for raw in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line:
            if lines and not blank:
                lines.append("")
            blank = True
            continue
        lines.append(line)
        blank = False
    return "\n".join(lines).strip()


def _gmail_decode_body_data(raw, content_type=""):
    if not raw:
        return ""
    try:
        padded = raw + "=" * (-len(raw) % 4)
        data = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return ""
    charset_match = re.search(
        r"charset\s*=\s*[\"']?([^;\"'\s]+)", content_type or "", re.I)
    charset = charset_match.group(1) if charset_match else "utf-8"
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _gmail_part_headers(part):
    values = {}
    for item in part.get("headers") or []:
        name = (item.get("name") or "").strip().lower()
        raw = item.get("value") or ""
        if not name:
            continue
        try:
            decoded = str(make_header(decode_header(raw)))
        except Exception:
            decoded = raw
        values.setdefault(name, []).append(decoded)
    return values


def _gmail_payload_text(payload):
    plain_parts = []
    html_parts = []

    def visit(part):
        mime_type = (part.get("mimeType") or "").lower()
        filename = (part.get("filename") or "").strip()
        headers = _gmail_part_headers(part)
        disposition = " ".join(headers.get("content-disposition", [])).lower()
        if filename or "attachment" in disposition:
            return
        for child in part.get("parts") or []:
            visit(child)
        data = (part.get("body") or {}).get("data") or ""
        if not data or mime_type not in ("text/plain", "text/html"):
            return
        content_type = " ".join(headers.get("content-type", []))
        decoded = _gmail_decode_body_data(data, content_type)
        if mime_type == "text/plain":
            plain_parts.append(decoded)
        else:
            html_parts.append(decoded)

    visit(payload or {})
    text = "\n".join(plain_parts)
    if not _clean_email_text(text) and html_parts:
        parser = _EmailHTMLTextParser()
        try:
            parser.feed("\n".join(html_parts))
            text = parser.text()
        except Exception:
            text = re.sub(r"<[^>]+>", " ", "\n".join(html_parts))
    return _clean_email_text(text)


def _gmail_header(headers, name):
    return (headers.get(name.lower()) or [""])[0]


def _gmail_message_time(message):
    try:
        seconds = int(message.get("internalDate") or 0) / 1000
        if seconds <= 0:
            raise ValueError
        return dt.datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _gmail_parse_thread(thread, account_identifier):
    parsed = []
    account_email = (account_identifier or "").strip().lower()
    raw_messages = (thread.get("messages") or [])[-GMAIL_THREAD_MESSAGE_LIMIT:]
    for message in raw_messages:
        message_id = str(message.get("id") or "").strip()
        payload = message.get("payload") or {}
        headers = _gmail_part_headers(payload)
        labels = set(message.get("labelIds") or [])
        if not message_id or labels.intersection({"DRAFT", "SPAM", "TRASH"}):
            continue
        from_name, from_email = parseaddr(_gmail_header(headers, "from"))
        from_email = from_email.strip().lower()
        recipients = getaddresses(
            (headers.get("to") or []) + (headers.get("cc") or []))
        recipients = [(name.strip(), email.strip().lower())
                      for name, email in recipients if email.strip()]
        outbound = "SENT" in labels or (
            bool(account_email) and from_email == account_email)
        if outbound:
            contact_name, contact_email = next(
                ((name, email) for name, email in recipients
                 if email != account_email), recipients[0] if recipients else ("", ""))
            author_name = account_identifier or from_name or "Gmail account"
        else:
            contact_name, contact_email = from_name, from_email
            author_name = from_name or from_email or "Email contact"
        body = _gmail_payload_text(payload)
        if not body:
            body = _clean_email_text(html.unescape(message.get("snippet") or ""))
        body = body or "(This email has no text body.)"
        parsed.append({
            "external_ref": message_id,
            "kind": "outbound" if outbound else "inbound",
            "body": body[:4000],
            "author_name": author_name,
            "delivery_status": "sent" if outbound else "received",
            "created_at": _gmail_message_time(message),
            "unread": "UNREAD" in labels,
            "contact_name": contact_name,
            "contact_email": contact_email,
            "subject": _gmail_header(headers, "subject").strip(),
            "subject_header_present": "subject" in headers,
        })
    if not parsed:
        return None
    parsed.sort(key=lambda item: (item["created_at"], item["external_ref"]))
    inbound = [item for item in parsed if item["kind"] == "inbound"]
    contact = (inbound[-1] if inbound else parsed[-1])
    canonical_subject = next(
        (item["subject"] for item in parsed
         if item["subject_header_present"]), "")
    return {
        "external_ref": str(thread.get("id") or "").strip(),
        "contact_name": contact["contact_name"] or contact["contact_email"],
        "contact_handle": contact["contact_email"],
        "subject": canonical_subject or "(no subject)",
        "messages": parsed,
        "latest_at": parsed[-1]["created_at"],
    }


def _gmail_full_thread_ids(access_token):
    payload = _gmail_api_get(access_token, "threads", {
        "labelIds": "INBOX",
        "includeSpamTrash": "false",
        "maxResults": GMAIL_INITIAL_THREAD_LIMIT,
    })
    return [str(item.get("id") or "").strip()
            for item in payload.get("threads") or [] if item.get("id")]


def _gmail_history_thread_ids(access_token, start_history_id):
    ids = []
    seen = set()
    page_token = ""
    history_id = str(start_history_id or "")
    while True:
        params = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
            "labelId": "INBOX",
            "maxResults": 500,
        }
        if page_token:
            params["pageToken"] = page_token
        payload = _gmail_api_get(access_token, "history", params)
        history_id = str(payload.get("historyId") or history_id)
        for record in payload.get("history") or []:
            for added in record.get("messagesAdded") or []:
                thread_id = str((added.get("message") or {}).get("threadId") or "")
                if thread_id and thread_id not in seen:
                    seen.add(thread_id)
                    ids.append(thread_id)
        page_token = str(payload.get("nextPageToken") or "")
        if not page_token:
            break
    return ids, history_id


def _gmail_fetch_threads(access_token, thread_ids):
    ids = list(dict.fromkeys(thread_id for thread_id in thread_ids if thread_id))
    if not ids:
        return []

    def fetch(thread_id):
        safe_id = urllib.parse.quote(thread_id, safe="")
        return _gmail_api_get(access_token, f"threads/{safe_id}", {"format": "full"})

    results = []
    with ThreadPoolExecutor(max_workers=min(6, len(ids))) as pool:
        futures = {pool.submit(fetch, thread_id): thread_id for thread_id in ids}
        for future in as_completed(futures):
            results.append(future.result())
    return results


def _gmail_clean_header(value, limit=998):
    return re.sub(r"[\r\n]+", " ", value or "").strip()[:limit]


def _gmail_reply_context(raw_thread, account_identifier, fallback_recipient):
    account_email = (account_identifier or "").strip().lower()
    messages = list(raw_thread.get("messages") or [])

    def message_order(message):
        try:
            return int(message.get("internalDate") or 0)
        except (TypeError, ValueError):
            return 0

    messages.sort(key=message_order)
    usable = [message for message in messages
              if not set(message.get("labelIds") or []).intersection(
                  {"DRAFT", "SPAM", "TRASH"})]
    if not usable:
        raise GmailDeliveryError("The Gmail conversation has no replyable messages.")

    contexts = []
    for message in usable:
        headers = _gmail_part_headers(message.get("payload") or {})
        _, from_email = parseaddr(_gmail_header(headers, "from"))
        from_email = from_email.strip().lower()
        outbound = "SENT" in set(message.get("labelIds") or []) or (
            account_email and from_email == account_email)
        contexts.append({
            "message": message,
            "headers": headers,
            "from_email": from_email,
            "outbound": outbound,
        })

    inbound = [context for context in contexts if not context["outbound"]]
    candidates = list(reversed(inbound))
    candidates.extend(
        context for context in reversed(contexts) if context["outbound"])
    reply_target = None
    reply_message_id = ""
    for context in candidates:
        candidate = _gmail_header(context["headers"], "message-id")
        match = re.search(r"<[^<>\s]+>", candidate or "")
        if match:
            reply_target = context
            reply_message_id = match.group(0)
            break
    if not reply_message_id:
        raise GmailDeliveryError(
            "Gmail did not return the original Message-ID needed for a threaded reply.")

    recipient = (fallback_recipient or "").strip().lower()
    for context in reversed(inbound):
        headers = context["headers"]
        _, reply_email = parseaddr(
            _gmail_header(headers, "reply-to") or _gmail_header(headers, "from"))
        if reply_email:
            recipient = reply_email.strip().lower()
            break
    if (not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", recipient)
            or recipient == account_email):
        raise GmailDeliveryError("This conversation has no valid external reply address.")

    target_headers = reply_target["headers"]
    subject = _gmail_clean_header(_gmail_header(target_headers, "subject"), 500)
    references = _gmail_header(target_headers, "references")
    reference_ids = re.findall(r"<[^<>\s]+>", references or "")
    if reply_message_id not in reference_ids:
        reference_ids.append(reply_message_id)
    return {
        "recipient": recipient,
        "subject": subject,
        "subject_header_present": "subject" in target_headers,
        "in_reply_to": reply_message_id,
        "references": " ".join(reference_ids[-30:]),
    }


def _send_gmail_reply(user_id, thread, body):
    connection = db.get_marketing_connection_for_source(
        user_id, thread["source_id"])
    if (not connection or connection["provider"] != "gmail"
            or connection["status"] != "connected"):
        raise GmailDeliveryError("Reconnect Gmail before sending this reply.",
                                 requires_reauth=True)
    if not (thread["external_ref"] or "").strip():
        raise GmailDeliveryError(
            "Only conversations imported from Gmail can be delivered as replies.")
    try:
        granted_scopes = set(json.loads(connection["granted_scopes"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        granted_scopes = set()
    if "https://www.googleapis.com/auth/gmail.send" not in granted_scopes:
        db.set_marketing_connection_error(
            user_id, connection["id"], "gmail_send_scope_missing",
            "The Gmail send permission was not granted.", "needs_reauth")
        raise GmailDeliveryError(
            "Reconnect Gmail and approve send permission before replying.",
            requires_reauth=True)
    try:
        access_token = _gmail_connection_access_token(
            user_id, connection["source_id"])
    except RuntimeError as exc:
        raise GmailDeliveryError(str(exc), requires_reauth=True) from None

    thread_ref = (thread["external_ref"] or "").strip()
    safe_thread_ref = urllib.parse.quote(thread_ref, safe="")
    try:
        raw_thread = _gmail_api_get(
            access_token, f"threads/{safe_thread_ref}", {"format": "full"})
        context = _gmail_reply_context(
            raw_thread, connection["account_identifier"],
            thread["contact_handle"])
        message = EmailMessage(policy=SMTP)
        message.set_content(body)
        message["To"] = context["recipient"]
        message["From"] = connection["account_identifier"]
        if context["subject_header_present"]:
            message["Subject"] = context["subject"]
        message["Date"] = formatdate(localtime=True)
        domain = connection["account_identifier"].rsplit("@", 1)[-1]
        domain = re.sub(r"[^A-Za-z0-9.-]", "", domain) or None
        message["Message-ID"] = make_msgid(domain=domain)
        message["In-Reply-To"] = context["in_reply_to"]
        message["References"] = context["references"]
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        sent = _gmail_api_post(access_token, "messages/send", {
            "raw": encoded,
            "threadId": thread_ref,
        })
    except _GmailAPIError as exc:
        if exc.requires_reauth:
            db.set_marketing_connection_error(
                user_id, connection["id"], exc.code, str(exc), "needs_reauth")
            message = "Google access expired. Reconnect Gmail before sending."
        else:
            db.record_marketing_connection_sync_error(
                user_id, connection["id"], exc.code, str(exc))
            message = (
                "Gmail did not confirm the send. Check Gmail Sent before retrying."
                if not exc.status or exc.status >= 500 else
                "Gmail rejected the reply. Nothing was sent or saved.")
        raise GmailDeliveryError(
            message, requires_reauth=exc.requires_reauth,
            uncertain=not exc.requires_reauth and (
                not exc.status or exc.status >= 500)) from None

    sent_id = str((sent or {}).get("id") or "").strip()
    if not sent_id:
        raise GmailDeliveryError(
            "Gmail did not confirm the send. Check Gmail Sent before retrying.",
            uncertain=True)
    return {
        "id": sent_id,
        "thread_id": str((sent or {}).get("threadId") or "").strip(),
        "expected_thread_id": thread_ref,
        "recipient": context["recipient"],
    }


def _sync_gmail_connection(user_id, connection_id, *, force_full=False):
    connection = db.get_marketing_connection(user_id, connection_id)
    if not connection or connection["provider"] != "gmail":
        raise GmailSyncError("Gmail connection not found.", status=404)
    if connection["status"] != "connected":
        raise GmailSyncError("Gmail must be reconnected.", status=409,
                             requires_reauth=True)
    try:
        access_token = _gmail_connection_access_token(
            user_id, connection["source_id"])
    except RuntimeError as exc:
        raise GmailSyncError(str(exc), status=409, requires_reauth=True) from None

    mode = "full" if (force_full or not connection["last_successful_sync_at"]
                      or not connection["gmail_history_id"]) else "incremental"
    try:
        if mode == "full":
            profile = _gmail_api_get(access_token, "profile")
            next_history_id = str(
                profile.get("historyId") or connection["gmail_history_id"] or "")
            thread_ids = _gmail_full_thread_ids(access_token)
        else:
            try:
                thread_ids, next_history_id = _gmail_history_thread_ids(
                    access_token, connection["gmail_history_id"])
            except _GmailAPIError as exc:
                if exc.status != 404:
                    raise
                mode = "full"
                profile = _gmail_api_get(access_token, "profile")
                next_history_id = str(
                    profile.get("historyId") or
                    connection["gmail_history_id"] or "")
                thread_ids = _gmail_full_thread_ids(access_token)

        imported_messages = 0
        imported_threads = 0
        newest_thread_id = None
        newest_at = ""
        for raw_thread in _gmail_fetch_threads(access_token, thread_ids):
            parsed = _gmail_parse_thread(raw_thread, connection["account_identifier"])
            if not parsed or not parsed["external_ref"]:
                continue
            result = db.upsert_gmail_thread(
                user_id, connection["source_id"],
                external_ref=parsed["external_ref"],
                contact_name=parsed["contact_name"],
                contact_handle=parsed["contact_handle"],
                subject=parsed["subject"], messages=parsed["messages"])
            if not result:
                continue
            imported_messages += result["inserted_messages"]
            imported_threads += int(result["inserted_thread"])
            if result["inserted_messages"] and result["latest_at"] >= newest_at:
                newest_at = result["latest_at"]
                newest_thread_id = result["thread_id"]

        db.mark_marketing_connection_synced(
            user_id, connection_id, gmail_history_id=next_history_id)
        return {
            "mode": mode,
            "imported_messages": imported_messages,
            "imported_threads": imported_threads,
            "newest_thread_id": newest_thread_id,
            "source_id": connection["source_id"],
        }
    except _GmailAPIError as exc:
        if exc.requires_reauth:
            db.set_marketing_connection_error(
                user_id, connection_id, exc.code, str(exc), "needs_reauth")
            message = "Google access expired. Reconnect Gmail to continue syncing."
        else:
            db.record_marketing_connection_sync_error(
                user_id, connection_id, exc.code, str(exc))
            message = "Gmail could not sync right now. Try again in a moment."
        raise GmailSyncError(
            message, status=exc.status or 502,
            requires_reauth=exc.requires_reauth) from None


def _post_patient_login_redirect():
    fresh = db.get_patient_user(session.get(PATIENT_SESSION_KEY))
    if fresh and not fresh["onboarding_done"]:
        return redirect(url_for("patient_onboarding"))
    nxt = session.pop(PATIENT_NEXT_KEY, "")
    return redirect(nxt if nxt.startswith("/") else url_for("home"))


def _needs_onboarding(user_id):
    """A brand-new workspace: no site profile and no studies yet. Such a user is
    sent through the quick setup (forward address + first study) instead of an
    empty inbox."""
    try:
        if db.get_site_profile(user_id):
            return False
        return not db.list_study_claims(user_id)
    except Exception:
        return False


def _home_endpoint():
    """Where a signed-in study-team user belongs: setup if their workspace is
    brand-new, otherwise the inbox (the product's home)."""
    if g.user and _needs_onboarding(g.user["id"]):
        return "onboarding"
    return "marketing_hub"


def _post_user_login_redirect():
    nxt = session.pop(USER_NEXT_KEY, "")
    if nxt.startswith("/"):
        return redirect(nxt)
    return redirect(url_for(_home_endpoint()))


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
        "redirect_uri": _oauth_redirect_url("patient_google_callback"),
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
    """Trending conditions: what people ACTUALLY search on our site first (live
    from search_stats, updates the moment someone searches), then external
    CT.gov/LLM trends, backfilled with seeds so it's never empty."""
    try:
        live = db.top_terms("condition", limit) + list(trends.get_trending("condition"))
        return _merge_terms(live, SEED_CONDITIONS, limit)
    except Exception:
        return SEED_CONDITIONS[:limit]


def trending_drugs(limit=6):
    """Trending drugs: real on-site searches first (live), then external
    CT.gov/LLM trends, backfilled with seeds so it's never empty."""
    try:
        live = db.top_terms("drug", limit) + list(trends.get_trending("drug"))
        return _merge_terms(live, SEED_DRUGS, limit)
    except Exception:
        return SEED_DRUGS[:limit]


# Condition typeahead: proxy ClinicalTrials.gov's own condition dictionary so the
# search box suggests the SAME full universe of conditions CT.gov does (every
# ADHD/lupus/etc. variant), not a short hardcoded list. Cached + falls back to
# our local lists if CT.gov is slow/unreachable, so the box always suggests
# something.
_COND_SUGGEST_CACHE = OrderedDict()
_COND_SUGGEST_TTL = 6 * 3600
# Cap kept modest: on a 512 MB box these per-worker caches must not balloon when
# a crawler walks thousands of SEO URLs. Env-tunable so it can be raised on a
# bigger instance without a code change.
_COND_SUGGEST_MAX = int(os.environ.get("COND_SUGGEST_CACHE_MAX", "400"))
_CT_SUGGEST_URL = "https://clinicaltrials.gov/api/int/suggest"


def _clean_suggest(s):
    """CT.gov returns regex-escaped strings (e.g. 'Lupus \\(LN\\)') for its own
    highlighting. Strip the escapes so suggestions read cleanly."""
    for a, b in (("\\(", "("), ("\\)", ")"), ("\\[", "["), ("\\]", "]"),
                 ("\\-", "-"), ("\\.", ".")):
        s = s.replace(a, b)
    return " ".join(s.split())


def _local_condition_matches(q, limit=25):
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


# NLM Clinical Tables "conditions" dictionary - a far richer source than CT.gov's
# int/suggest (which hard-caps at ~5). Returns cleaned condition names.
_NLM_COND_URL = "https://clinicaltables.nlm.nih.gov/api/conditions/v3/search"


def _ctgov_condition_suggest(q, limit=8):
    """Actual trial conditions from ClinicalTrials.gov (high-signal but few).

    Uses the retrying fetch so a transient throttle on the suggest endpoint
    doesn't silently break spelling correction ("athsma"->"asthma"). Retries
    cost nothing on the (normal) first-attempt success."""
    try:
        url = (_CT_SUGGEST_URL + "?dictionary=Condition&input="
               + urllib.parse.quote(q))
        data = mt._ct_get_json(url, timeout=6, attempts=3)
        if isinstance(data, list):
            return [_clean_suggest(str(x)) for x in data if str(x).strip()][:limit]
    except Exception:
        pass
    return []


def _nlm_condition_suggest(q, limit=25):
    """Broad condition coverage from NLM's conditions dictionary."""
    try:
        url = (_NLM_COND_URL + "?maxList=" + str(limit) + "&terms="
               + urllib.parse.quote(q))
        data = mt._ct_get_json(url, timeout=6, attempts=3)
        names = data[3] if isinstance(data, list) and len(data) > 3 else []
        out = []
        for row in names:
            name = row[0] if isinstance(row, list) and row else row
            name = _clean_suggest(str(name)).strip()
            if name:
                out.append(name)
        return out
    except Exception:
        return []


def _cond_search_value(label):
    """Drop a trailing "(ABBR)" so the CT.gov query term stays clean, e.g.
    "Diabetes mellitus (DM)" -> "Diabetes mellitus"; keep the label for display."""
    v = re.sub(r"\s*\([^)]*\)\s*$", "", label).strip()
    return v or label


# Curated lay-term / slang -> canonical mapping so casual, non-clinical searches
# still land on the right trials (accessibility). Medical dictionaries miss these
# because they aren't official synonyms. Each entry: (aliases, canonical, kind).
_LAY_SYNONYMS = [
    (["sugar disease", "high sugar", "high blood sugar", "blood sugar", "sugar"], "Diabetes", "condition"),
    (["water pill", "water pills", "water tablet"], "Diuretics", "drug"),
    (["blood thinner", "blood thinners"], "Anticoagulants", "drug"),
    (["high blood pressure", "hbp"], "Hypertension", "condition"),
    (["heart attack"], "Myocardial Infarction", "condition"),
    (["mini stroke", "tia"], "Transient Ischemic Attack", "condition"),
    (["hardening of the arteries"], "Atherosclerosis", "condition"),
    (["high cholesterol", "bad cholesterol"], "Hypercholesterolemia", "condition"),
    (["fatty liver"], "Nonalcoholic Fatty Liver Disease", "condition"),
    (["acid reflux", "heartburn"], "Gastroesophageal Reflux Disease", "condition"),
    (["add", "attention deficit"], "ADHD", "condition"),
    (["manic depression"], "Bipolar Disorder", "condition"),
    (["low thyroid", "underactive thyroid"], "Hypothyroidism", "condition"),
    (["overactive thyroid"], "Hyperthyroidism", "condition"),
    (["afib", "a-fib", "irregular heartbeat"], "Atrial Fibrillation", "condition"),
    (["lou gehrig", "lou gehrigs"], "Amyotrophic Lateral Sclerosis", "condition"),
    (["memory loss", "senility"], "Dementia", "condition"),
    (["shingles"], "Herpes Zoster", "condition"),
    (["flu"], "Influenza", "condition"),
    (["whooping cough"], "Pertussis", "condition"),
    (["chickenpox"], "Varicella", "condition"),
    (["pink eye"], "Conjunctivitis", "condition"),
    (["erectile", "impotence"], "Erectile Dysfunction", "condition"),
    (["low t", "low testosterone"], "Hypogonadism", "condition"),
    (["ringing in ears", "ringing in the ears"], "Tinnitus", "condition"),
    (["pins and needles", "numbness"], "Peripheral Neuropathy", "condition"),
    (["gluten"], "Celiac Disease", "condition"),
    (["male pattern baldness", "hair loss", "balding"], "Androgenetic Alopecia", "condition"),
    (["heavy periods"], "Menorrhagia", "condition"),
    (["marijuana", "weed", "cannabis"], "Cannabis", "drug"),
    (["birth control", "the pill", "contraceptive"], "Contraception", "condition"),
    (["kidney failure"], "Kidney Failure", "condition"),
    (["obesity", "overweight"], "Obesity", "condition"),
    (["sleep apnea"], "Sleep Apnea", "condition"),
    (["gout"], "Gout", "condition"),
]


def _lay_synonym_matches(q):
    """Return canonical trial terms for lay/slang searches (e.g. "water pill" ->
    Diuretics, "sugar" -> Diabetes). Matches on prefix or curated-alias substring
    so both partial and fully-typed casual terms resolve."""
    ql = q.lower().strip()
    if len(ql) < 2:
        return []
    out, seen = [], set()
    for aliases, canon, kind in _LAY_SYNONYMS:
        for a in aliases:
            if (a.startswith(ql) or ql.startswith(a) or ql == a
                    or (len(ql) >= 3 and ql in a)):
                key = (kind, canon.lower())
                if key in seen:
                    break
                seen.add(key)
                note = a if a != canon.lower() else ""
                out.append({"label": canon, "value": canon, "kind": kind,
                            "note": ("also: " + note) if note else ""})
                break
    return out


def _is_spell_fix(q, s):
    """True if suggestion `s` is a genuine spelling fix of query `q` (not a loose
    partial). Compares against the whole suggestion AND its individual tokens,
    and tolerates a plural 's', so "parkinsons"->"Parkinson Disease" and
    "brian"->"brain" both pass while "sugar disease"->"Blood Sugar" does not."""
    q = q.lower().strip()
    s = s.lower().strip()
    if not q or q == s:
        return False
    cands = [s] + re.split(r"[;\s]+", s)
    variants = [q, q.rstrip("s")]
    best = 0.0
    for a in variants:
        for b in cands:
            if a and b:
                best = max(best, difflib.SequenceMatcher(None, a, b).ratio())
    return best >= 0.8


def _lay_canonical(q):
    """Exact lay-term -> (canonical term, kind) for up-front query rewrite.
    Stricter than _lay_synonym_matches (which prefix-matches for the typeahead):
    the whole typed phrase must equal a known alias or the canonical term, so we
    only rewrite when we're confident ("sugar disease" -> Diabetes), never on a
    loose partial."""
    ql = q.lower().strip()
    if len(ql) < 3:
        return None
    for aliases, canon, kind in _LAY_SYNONYMS:
        if ql == canon.lower() or ql in [a.lower() for a in aliases]:
            return (canon, kind)
    return None


# NLM RxTerms drug autocomplete: drug-only (no procedures/behavioral arms like
# CT.gov's intervention dictionary returns), so the drug typeahead stays clean.
_RXTERMS_URL = "https://clinicaltables.nlm.nih.gov/api/rxterms/v3/search"
_DRUG_SKIP = ("pack", "starter", "package")


def _drug_suggest_matches(q, limit=10):
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

    def add(value, note="", curated=False):
        k = (value or "").lower()
        if value and k not in seen:
            seen.add(k)
            out.append({"label": value, "value": value, "note": note,
                        "kind": "drug", "curated": curated})

    # Curated brand/generic aliases -> canonical (keep the typed brand as a note).
    # These are high-intent (brands/trending peptides) - flagged so the suggest
    # merge keeps them above conditions, unlike the loose RxTerms backfill below.
    for alias, canon in _DRUG_ALIASES.items():
        if alias.startswith(ql) or canon.lower().startswith(ql):
            note = alias.title() if alias != canon.lower() else ""
            add(canon, note, curated=True)
    for d in SEED_DRUGS:
        if ql in d.lower():
            add(d, curated=True)
    # Backfill from NLM RxTerms (clean, drug-only, broad coverage of approved
    # drugs). Strip the "(Injectable)/(Oral Pill)" form suffix and skip packs.
    if len(out) < limit and len(ql) >= 2:
        try:
            url = (_RXTERMS_URL + "?maxList=20&terms=" + urllib.parse.quote(q))
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
    # Conditions: merge sources for breadth, deduped case-insensitively.
    #   0) lay/slang synonyms - casual searches ("sugar" -> Diabetes); pinned top
    #   1) ClinicalTrials.gov - real trial conditions (high-signal, but ~5 max)
    #   2) NLM conditions dictionary - broad coverage (the bulk of the list)
    #   3) our curated local list - clean, patient-friendly fallback
    ql = q.lower()
    lay = _lay_synonym_matches(q)
    conds, seen, cond_notes = [], set(), {}
    lay_cond_labels = set()

    def _push_cond(name, note=""):
        n = (name or "").strip()
        if not n:
            return
        # Dedup on the SEARCH value (parenthetical stripped) so near-duplicates
        # like "Fatty Liver Disease" and "Fatty liver disease (NAFLD/NASH)" don't
        # both show. First seen wins (CT.gov's clean labels lead).
        val = _cond_search_value(n).lower()
        if val in seen:
            return
        seen.add(val)
        conds.append(n)
        if note:
            cond_notes[n.lower()] = note

    for x in lay:
        if x["kind"] == "condition":
            lay_cond_labels.add(x["label"].lower())
            _push_cond(x["label"], x.get("note", ""))
    for c in _ctgov_condition_suggest(q):
        _push_cond(c)
    for c in _nlm_condition_suggest(q, 25):
        _push_cond(c)
    for c in _local_condition_matches(q, 25):
        _push_cond(c)

    def _cond_rank(n):
        nl = n.lower()
        if nl in lay_cond_labels:            # lay-mapped canonical -> pin to top
            return 0
        return 1 if nl.startswith(ql) else 2  # then prefix, then contains
    conds.sort(key=_cond_rank)
    conds = conds[:20]

    # Pinned lay-synonym matches (drug or condition) always lead - a casual term
    # like "the pill" or "sugar" should resolve at the very top, not get buried
    # under drug-name typeahead noise. Then the drug typeahead, then conditions.
    pinned, pinned_keys = [], set()
    for x in lay:
        val = (x["value"] if x["kind"] == "drug" else x["label"]).lower()
        key = (x["kind"], val)
        if key in pinned_keys:
            continue
        pinned_keys.add(key)
        pinned.append({
            "label": x["label"],
            "value": x["value"] if x["kind"] == "drug" else _cond_search_value(x["label"]),
            "note": x.get("note", ""),
            "kind": x["kind"],
        })
    drug_items = [d for d in _drug_suggest_matches(q)
                  if ("drug", (d.get("value") or "").lower()) not in pinned_keys]
    cond_items = [{"label": c, "value": _cond_search_value(c), "kind": "condition",
                   "note": cond_notes.get(c.lower(), "")}
                  for c in conds if ("condition", c.lower()) not in pinned_keys]

    # Ordering (accuracy first):
    #   1) exact matches, either kind - so a condition abbreviation like "aml"
    #      (Acute Myeloid Leukemia) leads, not a same-prefix drug ("Amlodipine").
    #   2) curated drugs (brands / trending peptides) - preserve the drug UX
    #      ("ozempic", "reta").
    #   3) conditions - this is a condition-first trial search, so real conditions
    #      outrank the loose RxTerms drug backfill ("pancreat" -> Pancreatic
    #      Cancer, not Pancreatin).
    #   4) remaining (RxTerms-backfill) drugs.
    def _exact(it):
        return ql in ((it.get("label") or "").lower(),
                      (it.get("value") or "").lower())
    exact = [c for c in cond_items if _exact(c)] + \
            [d for d in drug_items if _exact(d)]
    ex = {id(x) for x in exact}
    curated_drugs = [d for d in drug_items if id(d) not in ex and d.get("curated")]
    rest_conds = [c for c in cond_items if id(c) not in ex]
    rest_drugs = [d for d in drug_items
                  if id(d) not in ex and not d.get("curated")]
    items = pinned + exact + curated_drugs + rest_conds + rest_drugs
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
    """Default site: the patient trial finder. Most visitors come to search."""
    return _render_landing()


@app.route("/find-trial")
def find_trial():
    """Old finder URL. The search landing is now the site root."""
    return redirect(url_for("home"), code=301)


@app.route("/e/visit", methods=["POST"])
def track_visit():
    """Client-side visit beacon. The landing page fires this on load, so only
    real browsers that execute JavaScript are counted, most crawlers never run
    JS and so never reach here. Bot user-agents and owner traffic are dropped
    inside _log_event. Always returns 204 (best-effort, no body)."""
    _log_event("visit")
    return ("", 204)


# Homepage "trials near you" preview. The landing page is the most-hit surface,
# so we NEVER fetch CT.gov per visit: results are cached per ~metro grid cell
# (rounded lat/lon) for several hours, so every visitor from one city shares a
# single upstream call. Neutral registry facts only (no pay hooks, no PHI).
_NEARBY_CACHE = OrderedDict()
_NEARBY_CACHE_MAX = 300
_NEARBY_TTL_SECONDS = int(os.environ.get("NEARBY_PREVIEW_TTL", "21600"))  # 6h


def _nearby_preview(lat, lon, unit, radius, limit=4):
    """A few recruiting trials near (lat, lon), biggest actively-recruiting first
    (largest enrollment = most open slots), nearest as the tiebreak. Cached per
    metro grid cell so repeat visitors from one city share one CT.gov fetch."""
    key = (round(lat, 1), round(lon, 1), unit, radius)
    now = time.time()
    hit = _NEARBY_CACHE.get(key)
    if hit and now - hit["ts"] < _NEARBY_TTL_SECONDS:
        _NEARBY_CACHE.move_to_end(key)
        return hit["trials"]
    geo = f"distance({lat},{lon},{radius}{unit})"
    raw = mt.fetch_trials("", max_n=60, geo=geo, statuses="RECRUITING")
    out = []
    for t in raw:
        near = nearby_sites(t, lat, lon, unit, radius)
        if not near or not t.get("nctId"):
            continue
        try:
            enroll = int(t.get("enrollment") or 0)
        except (TypeError, ValueError):
            enroll = 0
        site = near[0]
        out.append({
            "nct": t.get("nctId", ""),
            "title": (t.get("title") or "").strip(),
            "condition": (t.get("conditions") or [""])[0] or "",
            "phase": t.get("phase") or "",
            "city": site.get("city") or "",
            "distance": int(round(site["distance"])),
            "enrollment": enroll,
        })
    out.sort(key=lambda r: (-r["enrollment"], r["distance"]))
    out = out[:limit]
    _NEARBY_CACHE[key] = {"ts": now, "trials": out}
    _NEARBY_CACHE.move_to_end(key)
    while len(_NEARBY_CACHE) > _NEARBY_CACHE_MAX:
        _NEARBY_CACHE.popitem(last=False)
    return out


@app.route("/api/nearby-preview")
def nearby_preview():
    """JSON feed for the homepage 'trials near you' card. GET-only, neutral
    ClinicalTrials.gov facts, heavily cached so the landing page never triggers a
    live CT.gov call per visit. Returns {ok, unit, trials:[...]}. Fails soft."""
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except (TypeError, ValueError):
        return jsonify({"ok": False})
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({"ok": False})
    unit = units_for(request.args.get("cc", ""))
    radius = 100 if unit == "km" else 60
    try:
        trials = _nearby_preview(lat, lon, unit, radius)
    except Exception:
        app.logger.exception("nearby preview failed")
        return jsonify({"ok": False})
    return jsonify({"ok": True, "unit": unit, "trials": trials})


# Homepage credibility stats. Baked from a ClinicalTrials.gov scrape (run
# tools/refresh_home_stats.py to recompute) instead of hitting the API on every
# page load. Floors are rounded DOWN so the "+" stays truthful.
#   Refreshed 2026-07-20 against https://clinicaltrials.gov/api/v2/studies:
#     overallStatus=RECRUITING ............................. 65,213  -> 65,000+
#     RECRUITING + (Phase 1 OR compensation wording OR healthy volunteers)
#       -> thousands of studies that pay or reimburse participants. This is a
#       CONSERVATIVE floor: CT.gov barely indexes pay wording and most trials
#       reimburse time/travel, so the true number is much higher. We show
#       "Thousands" instead of a small exact count, which otherwise sits next to
#       65,000 and misleads people into thinking almost no trials compensate.
HOME_STATS = {
    "recruiting": "65,000+",
    "paid": "Thousands",
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
_PRESCREEN_CACHE_MAX = int(os.environ.get("PRESCREEN_CACHE_MAX", "200"))
_PRESCREEN_TTL_SECONDS = int(os.environ.get("PRESCREEN_CACHE_TTL", "86400"))


def _structured_prescreen(trial):
    try:
        return mt.structured_prescreen_questions(trial) or []
    except Exception:
        return []


def _prescreen_for_trial(trial):
    """Return cached/generated patient-answerable pre-screen questions for a
    trial. Prefers the LLM list, then structured CT.gov fields, then []."""
    nct = (trial or {}).get("nctId") or ""
    if not nct:
        return _structured_prescreen(trial)
    hit = _PRESCREEN_CACHE.get(nct)
    if hit and (time.time() - float(hit.get("ts") or 0)) <= _PRESCREEN_TTL_SECONDS:
        _PRESCREEN_CACHE.move_to_end(nct)
        return hit["questions"]
    questions = []
    # Only a person on the apply form earns a model call. Crawlers hitting
    # study pages were burning the provider quota (429s) and taking the
    # tailored questions away from real applicants. While the provider is
    # cooling us down, serve the structured questions at once.
    if mt.llm_available() and not _is_bot():
        try:
            questions = mt.prescreen_questions(trial)
        except mt.LLMCoolingDown:
            questions = []
        except Exception:
            app.logger.exception("prescreen question generation failed")
            questions = []
    if not questions:
        # A failed or empty model call must not be remembered for a day: the
        # next visitor should get another try at the tailored questions. Serve
        # the structured fallback now without caching it.
        return _structured_prescreen(trial)
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
        "widened": False,
        "location_only": False,
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


def _exact_trial_for_query(q):
    """If the query names ONE specific study - an NCT id, or a near-exact trial
    title - return that study's NCT so we can guarantee it surfaces even when
    it's outside the searcher's radius (single-site studies usually are).
    Returns "" otherwise. Cheap for the common case: the NCT check is free, and
    a network lookup only happens for long, title-like phrases."""
    q = (q or "").strip()
    if not q:
        return ""
    m = re.search(r"NCT\d{8}", q.upper())
    if m:
        return m.group(0)
    # Only treat long, title-like phrases as a possible exact-title lookup, so
    # ordinary short condition searches don't pay for an extra API call.
    if len(q.split()) < 5:
        return ""
    try:
        cand = mt.fetch_trials("", max_n=20, term=q)
    except Exception:
        return ""
    ql = q.lower()
    best, best_ratio = "", 0.0
    for t in cand:
        title = (t.get("title") or "").strip()
        if not title:
            continue
        tl = title.lower()
        if ql == tl or ql in tl:
            return t.get("nctId", "")
        ratio = difflib.SequenceMatcher(None, ql, tl).ratio()
        if ratio > best_ratio:
            best_ratio, best = ratio, t.get("nctId", "")
    return best if best_ratio >= 0.85 else ""


@app.route("/find", methods=["GET", "POST"])
def find():
    """Public, no-login patient search. The search form lives on the homepage;
    this endpoint handles the POST and renders patient-friendly results. A GET
    just bounces back to the homepage (carrying any prefill)."""
    if request.method == "GET":
        # "Find a trial" is always the patient search - never bounce a signed-in
        # account to the (now-hidden) physician referral surface.
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
    # Lay/slang normalization: rewrite casual wording to the canonical medical
    # term up front so it searches broadly ("sugar disease" -> Diabetes, "water
    # pill" -> Diuretics) instead of dead-ending on CT.gov's literal term match.
    # Only for structured (non-freeform) single-term input.
    if not intervention and not freeform and condition:
        lay = _lay_canonical(condition)
        if lay:
            canon, kind = lay
            if kind == "drug":
                intervention, condition = canon, ""
            else:
                condition = canon
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
    location_only = False
    if not label and not about:
        if location:
            location_only = True          # browse everything recruiting nearby
        else:
            flash("Tell us the condition, describe what you're looking for, or "
                  "enter a location to see all trials nearby.", "error")
            return redirect(url_for("home", location=location))
    if not location:
        # Bounce back to the page they came from, not the homepage: if this was a
        # known condition page, return there with ?needloc=1 so the location box +
        # "use my current location" are focused right in front of them (the page's
        # own JS shows the prompt). Fall back to home - with a flash - for freeform
        # conditions that have no dedicated page.
        cond_slug = slugify(condition_label) if condition_label else ""
        if cond_slug and cond_slug in _COND_BY_SLUG:
            return redirect(url_for("condition_page", slug=cond_slug,
                                    needloc=1))
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
        # Log the true result count on a cache hit too. Without this the event
        # carries no "results" field, so the recent-searches feed and funnel
        # stats coerce it to 0 - making a cached search that actually returned
        # dozens of trials look like a zero-result search.
        cached_n = len((_load_search(cached_sid) or {}).get("results") or [])
        _log_event("search", {"q": label, "results": cached_n, "cached": 1,
                              "loc": (location or "").strip()})
        return _finish_find_redirect(cached_sid, nct)
    try:
        # For a structured drug/condition query (not free-text), show WHERE the
        # searched term appears in each trial. We skip this for freeform searches
        # (condition_terms is empty there) since there's no single clean term.
        if intervention:
            evidence_terms = [intervention]
            typed_raw = request.form.get("condition", "").strip()
            if typed_raw and typed_raw.lower() != intervention.lower():
                evidence_terms.append(typed_raw)
            evidence_kind = "drug"
        elif condition_terms:
            evidence_terms = list(condition_terms)
            evidence_kind = "condition"
        else:
            evidence_terms, evidence_kind = None, ""

        # Only produce fit / not-a-fit verdicts when the searcher actually
        # described their situation (free-text or the "about" details). A bare
        # condition or drug search has nothing to judge specific eligibility
        # criteria against, so we show a neutral "you might qualify" instead of
        # falsely flagging everything "probably not a fit".
        detected, results = run_search(note, condition_query, "", False, coords,
                                       radius, unit, interventional_only=True,
                                       intervention=intervention,
                                       assess=bool(about.strip()),
                                       evidence_terms=evidence_terms,
                                       evidence_kind=evidence_kind,
                                       location_only=location_only)
    except RuntimeError as e:
        flash(str(e), "error")
        return redirect(url_for("home", condition=condition_label, location=location))
    except Exception:
        app.logger.exception("public find failed")
        flash("Search failed unexpectedly. Please try again.", "error")
        return redirect(url_for("home", condition=condition_label, location=location))

    # Stage 1 - spelling / phrasing correction via ClinicalTrials.gov's own
    # suggest (fast + accurate: "brian"->"brain", "diabetis"->"diabetes"). The
    # actual search (query.cond) does NOT self-correct, so a single-word typo
    # dead-ends without this. Only for short (<=2 word) queries - longer phrases
    # are vague situations better handled by the LLM stage below.
    if (not results and not intervention and not freeform
            and condition_label and len(condition_label.split()) <= 2):
        try:
            sugg = _ctgov_condition_suggest(condition_label, limit=3)
        except Exception:
            sugg = []
        # Only accept a suggestion that's a genuine spelling fix, so
        # "brian"->"brain" and "parkinsons"->"Parkinson Disease" apply but a
        # loose partial like "sugar disease"->"Blood Sugar" doesn't hijack the
        # LLM stage below.
        corrected = next((s for s in sugg
                          if _is_spell_fix(condition_label, s)), "")
        if corrected:
            try:
                det_c, res_c = run_search(
                    build_patient_note(corrected, age, sex, about, pregnant,
                                       other_trial),
                    corrected, "", False, coords, radius, unit,
                    interventional_only=True, assess=bool(about.strip()),
                    evidence_terms=[corrected], evidence_kind="condition")
            except Exception:
                det_c, res_c = "", []
            if res_c:
                results = res_c
                label = corrected
                condition_label = corrected
                condition_terms = [corrected]
                detected = corrected

    # Stage 2 - niche / messy / multi-concept fallback: a plain condition search
    # that STILL found nothing may be a phrase CT.gov can't match ("stage 4 lung
    # ca with brain mets", "trouble breathing at night", a rare-disease nickname).
    # Let the LLM interpret what the person means and search again, so odd or
    # niche searches still surface the best-matching trials instead of a dead end.
    if (not results and not intervention and not freeform
            and condition_label and mt.LLM_API_KEY):
        interp_about = condition_label + (("\n" + about) if about else "")
        interp_note = build_patient_note("", age, sex, interp_about,
                                         pregnant, other_trial)
        try:
            # Multi-condition interpretation + a full-text pass on the raw phrase
            # so trials mentioning the symptom (incl. in eligibility criteria)
            # surface too - don't overfit a vague search to one condition.
            detected2, results2 = run_search(
                interp_note, "", "", False, coords, radius, unit,
                interventional_only=True, assess=bool(about.strip()),
                broad_term=condition_label)
        except Exception:
            detected2, results2 = "", []
            app.logger.exception("LLM interpretation fallback failed")
        if results2:
            results = results2
            detected = detected2 or detected
            freeform = True            # treat as an interpreted (free-text) search
            condition_terms = []       # don't log the raw phrase as a condition
            if detected:
                label = detected
                condition_label = detected

    # Stage 3 - a matching trial exists but fell outside the search radius (a
    # single-site study like a university trial), or a brand-new trial the
    # condition search missed. Rather than dead-ending on "no trials", widen the
    # net: do a full-text (query.term) pass at a much larger radius (still
    # distance-ranked, so a trial ~95 km away shows as such), then finally drop
    # the location filter entirely as a last resort. Runs even without an LLM
    # key (plain term search). `widened` lets the results page say so.
    widened = False
    if not results and not intervention and not location_only and condition_label:
        attempts = []
        if coords:
            attempts.append((coords, max(int(radius or 0), 500)))  # widen radius
        attempts.append((None, radius))                            # anywhere
        for use_coords, use_radius in attempts:
            try:
                d3, r3 = run_search(
                    build_patient_note("", age, sex, about, pregnant, other_trial),
                    "", "", False, use_coords, use_radius, unit,
                    interventional_only=True, assess=bool(about.strip()),
                    broad_term=condition_label)
            except Exception:
                d3, r3 = "", []
                app.logger.exception("title/keyword fallback failed")
            if r3:
                results = r3
                detected = d3 or detected
                freeform = True
                condition_terms = []
                widened = True
                break

    # Exact study reference: someone typed a full trial name or pasted an NCT id.
    # They want THAT study - so guarantee it shows even if its only site is
    # outside their radius (which is why a plain search can miss it). Pull it in
    # via a no-geo lookup and pin it to the top, distance and all.
    if (not location_only and not intervention
            and request.form.get("freeform", "") != "1"):
        target = _exact_trial_for_query(request.form.get("condition", ""))
        if target and target not in {r["trial"].get("nctId") for r in results}:
            try:
                _, exact_res = run_search(
                    build_patient_note("", age, sex, about, pregnant, other_trial),
                    "", "", False, None, radius, unit, interventional_only=True,
                    assess=bool(about.strip()),
                    broad_term=request.form.get("condition", "").strip())
            except Exception:
                exact_res = []
                app.logger.exception("exact-trial lookup failed")
            pinned = next((r for r in exact_res
                           if r["trial"].get("nctId") == target), None)
            if pinned:
                results = [pinned] + results
                widened = True

    # Location-only browse: give it a readable label for the results page + logs.
    if location_only:
        label = "Trials near " + location

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
                          "intervention": 1 if intervention else 0,
                          "loc": (location or "").strip()})

    ctx = {"condition": label, "location": location, "unit": unit,
           "q_condition": condition_label or (label if freeform else ""),
           "q_intervention": intervention, "widened": widened,
           "location_only": location_only,
           "q_age": age, "q_sex": sex, "q_about": about, "q_radius": radius,
           "q_lat": lat_in, "q_lon": lon_in, "q_cc": cc_in}
    search_id = _cache_search(results, ctx)
    # Only memoize non-empty result sets. A transient upstream hiccup (CT.gov
    # throttle/timeout) can yield zero trials for a query that normally has many;
    # caching that would keep serving "no trials" on every retry until the TTL
    # expires. Let empty searches re-run instead.
    if results:
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
    cached_prescreen = _prescreen_cached(r["trial"])
    prescreen = (cached_prescreen if cached_prescreen
                 else _structured_prescreen(r["trial"]))
    prescreen_ai = bool(mt.LLM_API_KEY) and bool((r["trial"] or {}).get("criteria"))
    plain_terms = summarize.plain_terms(r["trial"])
    return render_template("trial_detail.html", r=r, search_id=search_id,
                           applied=applied, summary=summary, plain_terms=plain_terms,
                           prescreen=prescreen, prescreen_ai=prescreen_ai,
                           cached_prescreen=cached_prescreen, **ctx)


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
    # Date of birth is what a study team actually screens on. It beats the
    # age carried over from the search form, which may be a guess or blank.
    dob = f.get("dob", "").strip()[:10]
    if dob:
        dob_age = mailer.age_from_dob(dob)
        if not dob_age:
            flash("Enter a valid date of birth.", "error")
            return redirect(_safe_next(request.referrer) or url_for("home"))
        age = dob_age

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

    # Double-submit guard. A real applicant clicked apply three times in seven
    # seconds and became three applications, three threads, and three clinic
    # handoffs. The same email applying to the same study within minutes is one
    # application: show the same thank-you page and send nothing again.
    _dup = db.find_recent_duplicate_lead(email, f.get("nct", "").strip())
    if _dup:
        _log_event("apply_duplicate", {"nct": f.get("nct", "").strip()})
        return render_template(
            "thanks.html", title=f.get("title", ""), nct=f.get("nct", ""),
            alert_prefill={
                "condition": f.get("condition", "").strip(),
                "location": f.get("location", "").strip(),
                "lat": f.get("lat", "").strip(), "lon": f.get("lon", "").strip(),
                "cc": f.get("cc", "").strip(),
                "radius": f.get("radius", "").strip() or "50",
                "unit": f.get("unit", "km").strip() or "km",
                "email": email,
            })

    token = db.create_lead({
        "applicant_token": applicant,
        "nct": f.get("nct", "").strip(), "title": f.get("title", "").strip(),
        "condition": f.get("condition", "").strip(),
        "location": display_area(f.get("location", "")),
        "site": f.get("site", "").strip(), "name": name,
        "email": email, "phone": f.get("phone", "").strip(),
        "age": age, "sex": sex, "dob": dob,
        "notes": f.get("about", "").strip(), "consent": 1, "source": source,
        "screener": json.dumps(screener) if screener else "",
        "eligibility": json.dumps(elig) if elig else "",
        "prescreen_readiness": readiness,
        "records_connected": records_connected, "record_summary": record_summary,
        "referred_by": referred_by, "invite_token": invite_token,
        # Separate, optional opt-in to the consented cross-study matching pool.
        # Unchecked by default; never required to apply (freely-given consent).
        "registry_opt_in": 1 if f.get("registry_opt_in") else 0,
        "registry_consent_version": REGISTRY_CONSENT_VERSION,
    })
    def _form_float(key):
        raw = (f.get(key) or "").strip()
        try:
            return float(raw) if raw else None
        except (TypeError, ValueError):
            return None
    apply_lat = _form_float("lat")
    apply_lon = _form_float("lon")
    apply_radius = _form_float("radius")
    apply_unit = (f.get("unit") or "km").strip() or "km"
    # Email every public study address we can find (clinic, then sponsor) in
    # one send. No-op while NOTIFY_LIVE is off, but the chosen addresses are
    # still stored on the lead.
    new_lead = db.get_lead_by_token(token)
    clinic_recs = []
    if new_lead:
        geo_lat, geo_lon, geo_r, geo_u = _notify_geo_for_lead(
            new_lead, lat=apply_lat, lon=apply_lon, radius=apply_radius,
            unit=apply_unit)
        try:
            clinic_recs = _resolve_clinic_notify_recipients(
                new_lead, lat=geo_lat, lon=geo_lon, radius=geo_r, unit=geo_u)
        except Exception:
            app.logger.exception("clinic resolve failed")
            clinic_recs = []
    site_emails = _notify_site_new_candidate(
        token, lat=apply_lat, lon=apply_lon, radius=apply_radius, unit=apply_unit)
    # Internal heads-up so the operator can confirm real applications are landing.
    # Skip addresses that already got the clinic email so one apply is not two
    # near-duplicate messages in the same inbox.
    _notify_owner_new_application(token, exclude_emails=site_emails)
    # Confirm receipt to the applicant (job-application style). The in-app system
    # message always shows in their thread; the email sends only when go-live is
    # on, so both surfaces stay in sync.
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
            "Thanks for applying. We emailed you a link to message the study "
            "team. They will reply on that same thread.")
    _notify_applicant_apply_confirmation(token)
    if FOUNDER_CONNECT:
        _notify_applicant_founder_connect_async(token)
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
    """Legacy B2B page for sites/clinics/sponsors. Superseded by the /for-sites
    coordinator-OS page, which now covers sites, clinics and sponsors. Redirect so
    old links, ads and bookmarks land on the current site-side page instead."""
    _log_event("view_for_clinicians")
    return redirect(_sites_home_url(), code=301)


# Self-hosted marketing shell (Framer-exported, no Framer runtime/fee).
_LANDING_DIR = HERE / "landing"
_LANDING_HERO_IMAGE_RE = re.compile(
    r'<img\b(?=[^>]*\balt="Hero Image")[^>]*>', re.IGNORECASE)
# The hero slot IS the product: the study-team app in its public demo mode
# (SITE_DEMO, see load_user) embedded live, instead of a recorded video.
#
# How it moves. The Framer hero keeps only a placeholder (.landing-demo-slot).
# The real frame (.landing-demo) is a portal appended to <body>, so Framer's
# transformed / overflow-hidden wrappers can never clip it, and the iframe is
# never re-parented (moving an iframe reloads it). A scroll runway is inserted
# after the hero section, and progress p across it drives the frame:
#   p < .5     grows from the hero slot into a stage ~96% of the screen wide
#   .5 - .8    holds there, fully interactive (pointer events on)
#   .8 - 1     shrinks back toward the centre and fades; the next section is
#              already rising underneath it, so no blank runway is left
# p reaches 1 when the next section's top is 25% down the viewport.
# On phones the frame is phone-shaped (the app's own mobile layout at a 390px
# viewport, ~72vh tall) and goes through the same stage, expanding to the
# width of the screen under the nav.
#
# How it navigates. Every thread click is a full document load inside the
# frame, and the blank between documents read as a flicker. So the frame is
# double-buffered: an in-app link click is intercepted, loaded in a second
# hidden iframe, and that one is swapped in only after it has loaded and
# painted. The old page stays on screen the whole time.
_LANDING_VIEW_W, _LANDING_VIEW_H = 1180, 626   # app viewport at the hero slot's aspect
_LANDING_INBOX = "/app/inbox?embed=1&owner=unassigned"
_LANDING_DEMO_BAR = 36   # window bar height (px) above the app
_LANDING_DEMO_SLOT = '<div class="landing-demo-slot" style="position:absolute;inset:0"></div>'
_LANDING_DEMO_PORTAL = (
    '<style>'
    '.landing-demo{position:absolute;z-index:40;border-radius:16px;box-sizing:border-box;'
    'background:#fff;border:1px solid rgba(18,87,176,.28);'
    'box-shadow:0 0 0 5px rgba(18,87,176,.07),0 24px 60px rgba(2,2,18,.14)}'
    # Clickable state: solid brand outline plus a one-time halo pulse to draw the eye.
    '.landing-demo.is-live{border-color:#1257b0;'
    'box-shadow:0 0 0 4px rgba(18,87,176,.18),0 24px 60px rgba(2,2,18,.14);'
    'animation:landing-demo-pulse 1.4s ease-out 1}'
    '@keyframes landing-demo-pulse{'
    '0%{box-shadow:0 0 0 4px rgba(18,87,176,.18),0 24px 60px rgba(2,2,18,.14)}'
    '45%{box-shadow:0 0 0 16px rgba(18,87,176,.10),0 24px 60px rgba(2,2,18,.14)}'
    '100%{box-shadow:0 0 0 4px rgba(18,87,176,.18),0 24px 60px rgba(2,2,18,.14)}}'
    # Window bar: says what this is and what to do with it, in the frame itself.
    f'.landing-demo-bar{{position:absolute;left:0;right:0;top:0;height:{_LANDING_DEMO_BAR}px;'
    'display:flex;align-items:center;gap:10px;padding:0 14px;'
    'border-bottom:1px solid rgba(18,87,176,.14);border-radius:15px 15px 0 0;'
    'background:#f5f7fb;font:500 13px/1 "Figtree",system-ui,sans-serif;color:#42506a;'
    'white-space:nowrap;overflow:hidden}'
    '.landing-demo-bar .dot{width:8px;height:8px;border-radius:50%;background:#22a06b;flex:0 0 auto}'
    '.landing-demo-bar b{color:#12122b;font-weight:700}'
    '.landing-demo-bar .hint{overflow:hidden;text-overflow:ellipsis}'
    '.landing-demo-bar [data-live],.landing-demo-bar [data-live-m],.landing-demo-bar [data-idle-m],'
    '.landing-demo.is-live .landing-demo-bar [data-idle]{display:none}'
    '.landing-demo.is-live .landing-demo-bar [data-live]{display:inline}'
    '@media(max-width:809px){.landing-demo-bar [data-idle],.landing-demo-bar [data-live]{display:none!important}'
    '.landing-demo-bar [data-idle-m]{display:inline}.landing-demo.is-live .landing-demo-bar [data-idle-m]{display:none}'
    '.landing-demo.is-live .landing-demo-bar [data-live-m]{display:inline}}'
    '.landing-demo-bar .demo-open{margin-left:auto;color:#1257b0;font-weight:600;'
    'text-decoration:none;flex:0 0 auto}'
    '.landing-demo-bar .demo-open:hover{text-decoration:underline}'
    f'.landing-demo-clip{{position:absolute;left:0;right:0;top:{_LANDING_DEMO_BAR}px;bottom:0;'
    'overflow:hidden;border-radius:0 0 15px 15px}'
    '.landing-demo-scale{position:absolute;top:0;left:0;transform-origin:top left}'
    '.landing-demo iframe{position:absolute;top:0;left:0;display:block;border:0;'
    'background:#fff;pointer-events:none}'
    '.landing-demo iframe[data-buffer]{visibility:hidden}'
    '.landing-demo.is-live iframe{pointer-events:auto}'
    '.landing-demo-runway{height:150vh}'
    '@media(max-width:809px){.landing-demo-runway{height:130vh}}'
    '</style>'
    '<div class="landing-demo">'
    '<div class="landing-demo-bar"><span class="dot"></span><b>Live demo</b>'
    '<span class="hint"><span data-idle>Scroll down to expand it and try it yourself</span>'
    '<span data-live>This is the real product. Click anything.</span>'
    '<span data-idle-m>Scroll to try it</span><span data-live-m>Tap anything</span></span>'
    f'<a class="demo-open" href="{_LANDING_INBOX}" target="_blank" rel="noopener">'
    'Open full size &#8599;</a></div>'
    '<div class="landing-demo-clip"><div class="landing-demo-scale">'
    f'<iframe data-src="{_LANDING_INBOX}" title="BridgeMD live demo" loading="eager" '
    'tabindex="-1"></iframe>'
    '<iframe data-buffer title="" tabindex="-1" aria-hidden="true"></iframe>'
    '</div></div>'
    '</div>'
    '<script>(function(){'
    'var demo=document.querySelector(".landing-demo"),'
    'slot=document.querySelector(".landing-demo-slot");if(!demo||!slot)return;'
    'var box=slot.closest("[data-framer-name=image]")||slot.parentElement;'
    'var sc=demo.querySelector(".landing-demo-scale"),'
    'f=demo.querySelector("iframe:not([data-buffer])"),g=demo.querySelector("iframe[data-buffer]");'
    f'var VW0={_LANDING_VIEW_W},VW=VW0,VH={_LANDING_VIEW_H},AR=VW/VH,NAV=96,BAR={_LANDING_DEMO_BAR},'
    'raf=0,x0=0,y0=0,w0=0,h0=0;'
    'f.src=f.getAttribute("data-src");'
    'var runway=document.createElement("div");runway.className="landing-demo-runway";'
    '(slot.closest("section")||slot.parentElement).insertAdjacentElement("afterend",runway);'
    'function clamp(v,a,b){return v<a?a:v>b?b:v}'
    'function ease(t){return t<.5?2*t*t:1-Math.pow(-2*t+2,2)/2}'
    'function size(){sc.style.width=f.style.width=g.style.width=VW+"px";'
    'sc.style.height=f.style.height=g.style.height=VH+"px"}'
    'function layout(){raf=0;'
    'var vw=window.innerWidth,vh=window.innerHeight,sy=window.scrollY||0,sx=window.scrollX||0,'
    'phone=vw<810;'
    # Phone slot: Framer's hero image box is a 180px banner; make it a phone
    # screen (~72vh, no wider than 9:17) and let the page flow around it.
    'if(phone){var bw=box.getBoundingClientRect().width,bh=Math.round(Math.min(vh*.72,bw*1.9));'
    'if(box.style.height!==bh+"px")box.style.height=bh+"px"}else if(box.style.height)box.style.height="";'
    'var sr=box.getBoundingClientRect(),p=0,NAVP=phone?80:NAV;'
    'if(runway.offsetHeight>0){'
    'var start=sr.top+sy-vh*.18,end=runway.getBoundingClientRect().bottom+sy-vh*.25;'
    'p=clamp((sy-start)/Math.max(1,end-start),0,1)}'
    'var x,y,w,h,fixed=p>0;'
    'if(!fixed){x=sr.left+sx;y=sr.top+sy;w=sr.width;h=sr.height;'
    # Remember where the stage starts from (the slot, with its top at .18vh) and
    # size the app viewport to the slot's real aspect (minus the bar), so the
    # frame never letterboxes and the expansion eases from a fixed start rect.
    # Phones get the app's own mobile layout (390px viewport at ~.9x).
    'x0=sr.left;y0=vh*.18;w0=sr.width;h0=sr.height;AR=w0/Math.max(1,h0-BAR);'
    'VW=phone?390:VW0;VH=Math.round(VW/AR);size()}'
    # Stage: nearly the full width of the screen, from under the nav to a small
    # bottom margin. The frame's aspect is free to change on the way there.
    'else{var tw=phone?vw-16:Math.min(vw*.96,1920),th=vh-NAVP-(phone?12:24),'
    'tx=(vw-tw)/2,ty=NAVP,e=ease(clamp(p/.5,0,1));'
    'x=x0+(tx-x0)*e;y=y0+(ty-y0)*e;w=w0+(tw-w0)*e;h=h0+(th-h0)*e;'
    # The app's viewport widens with the frame so it lands at native 1:1 scale
    # (largest, crispest, most room). Rounded to 8px to limit reflow churn.
    'var vwp=phone?390:Math.round((VW0+(tw-VW0)*e)/8)*8,vhp=Math.round((h-BAR)/(w/vwp));'
    'if(vwp!==VW||vhp!==VH){VW=vwp;VH=vhp;size()}}'
    # Exit mirrors the entrance: shrink toward the centre while fading.
    'if(fixed&&p>.8){var q=ease(clamp((p-.8)/.2,0,1)),k=1-.4*q,w2=w*k,h2=h*k;'
    'x+=(w-w2)/2;y+=(h-h2)/2;w=w2;h=h2}'
    'demo.style.position=fixed?"fixed":"absolute";'
    'demo.style.left=x+"px";demo.style.top=y+"px";demo.style.width=w+"px";demo.style.height=h+"px";'
    'var ah=h-BAR,s=Math.min(w/VW,ah/VH);sc.style.transform="scale("+s+")";'
    'sc.style.left=Math.round((w-VW*s)/2)+"px";sc.style.top=Math.round((ah-VH*s)/2)+"px";'
    'demo.style.opacity=p>.8?String(1-(p-.8)/.2):"1";'
    'demo.style.visibility=p>=1?"hidden":"visible";'
    'demo.classList.toggle("is-live",p>=.45&&p<.85)}'
    'function req(){if(!raf)raf=requestAnimationFrame(layout)}'
    'window.addEventListener("scroll",req,{passive:true});'
    'window.addEventListener("resize",req);'
    'if(window.ResizeObserver)new ResizeObserver(req).observe(document.body);'
    # Double-buffered navigation (see the note at the top of this block).
    'var loading=null;'
    'function embedUrl(href,base){try{var u=new URL(href,base);'
    'if(u.origin!==location.origin||u.pathname.indexOf("/app/")!==0)return null;'
    'u.searchParams.set("embed","1");u.hash="";return u.pathname+u.search}catch(e){return null}}'
    'function wire(fr){var w=fr.contentWindow;if(!w)return;'
    'w.addEventListener("click",function(e){'
    'if(e.defaultPrevented||e.button||e.metaKey||e.ctrlKey||e.shiftKey)return;'
    'var a=e.target&&e.target.closest&&e.target.closest("a[href]");'
    'if(!a||a.target==="_blank"||a.hasAttribute("download"))return;'
    'var raw=a.getAttribute("href");if(!raw||raw.charAt(0)==="#")return;'
    'var next=embedUrl(raw,w.location.href);if(!next)return;'
    'e.preventDefault();e.stopImmediatePropagation();'
    'if(next!==w.location.pathname+w.location.search)swapTo(next)},true)}'
    'function swapTo(url){var old=f,nu=g;loading=url;'
    'var st=null;try{st=old.contentDocument.querySelector(".mh-thread-stack")}catch(e){}'
    'nu.dataset.stackTop=st?String(st.scrollTop):"0";nu.src=url}'
    'function finishSwap(nu){var url=loading,old=f;'
    # Carry the thread list's scroll across so the clicked row doesn't jump.
    'try{var s2=nu.contentDocument.querySelector(".mh-thread-stack");'
    'if(s2)s2.scrollTop=parseInt(nu.dataset.stackTop,10)||0}catch(e){}'
    'requestAnimationFrame(function(){requestAnimationFrame(function(){'
    'if(loading!==url)return;'
    'nu.removeAttribute("data-buffer");nu.removeAttribute("aria-hidden");'
    'old.setAttribute("data-buffer","");old.setAttribute("aria-hidden","true");'
    'f=nu;g=old;loading=null;wire(f);'
    'try{g.contentWindow.location.replace("about:blank")}catch(e){g.src="about:blank"}})})}'
    '[f,g].forEach(function(fr){fr.addEventListener("load",function(){'
    'if(fr.hasAttribute("data-buffer")){if(loading&&fr.src!=="about:blank")finishSwap(fr)}'
    'else wire(fr)})});'
    # The app focuses an input as it loads, and a same-origin iframe's focus
    # scrolls the PARENT to bring it into view: the landing would jump down to
    # the hero frame on load. Put the page back where it was - but only on the
    # first load, and only if the visitor hasn't started scrolling themselves.
    'var moved=false,py0=sy0(),first=true;function sy0(){return window.scrollY||0}'
    '["wheel","touchstart","keydown","pointerdown"].forEach(function(ev){'
    'window.addEventListener(ev,function(){moved=true},{passive:true,capture:true})});'
    'function unjump(){if(!moved&&Math.abs(sy0()-py0)>2)window.scrollTo(0,py0);req()}'
    'f.addEventListener("load",function(){if(!first)return;first=false;'
    'unjump();setTimeout(unjump,300)});'
    'layout()})()</script>'
)


def _serve_landing():
    """Serve the marketing shell with its live token and product demo injected."""
    html = (_LANDING_DIR / "index.html").read_text(encoding="utf-8")
    meta = f'<meta name="csrf-token" content="{_csrf_token()}">'
    html = html.replace("<head>", "<head>" + meta, 1)
    html = _LANDING_HERO_IMAGE_RE.sub(_LANDING_DEMO_SLOT, html, count=1)
    # The live frame is a body-level portal (see _LANDING_DEMO_PORTAL).
    html = html.replace("</body>", _LANDING_DEMO_PORTAL + "</body>", 1)
    resp = Response(html, mimetype="text/html")
    # The shell is rebuilt out-of-band by clean_landing.py; without this the browser
    # serves a stale cached copy and edits look like they didn't apply.
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/inbox")
def inbox():
    """Research-site product site (inbox + marketing). / is the patient finder."""
    _log_event("view_for_sites")
    return _serve_landing()


@app.route("/for-sites")
def for_sites():
    """Old research-site URL. The site-side home is now /inbox."""
    return redirect(url_for("inbox"), code=301)


@app.route("/landing/assets/<path:filename>")
def landing_assets(filename):
    return send_from_directory(_LANDING_DIR / "assets", filename)


def _marketing_time_label(raw):
    """Compact relative timestamp for the inbox, falling back to the raw value."""
    try:
        stamp = dt.datetime.strptime((raw or "").strip(), "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return raw or ""
    delta = dt.datetime.now() - stamp
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return "Now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    if seconds < 172800:
        return "Yesterday"
    if stamp.year == dt.datetime.now().year:
        return stamp.strftime("%b %d").replace(" 0", " ")
    return stamp.strftime("%b %d, %Y").replace(" 0", " ")


def _marketing_thread_url(thread_id=None, anchor="conversation"):
    values = {}
    if thread_id:
        values["thread"] = thread_id
    url = url_for("marketing_hub", **values)
    return f"{url}#{anchor}" if anchor else url


# One-tap reply snippets for the composer (label shown on the chip, text inserted
# into the reply box). The template (marketing_hub.html) expected `quick_replies`
# but nothing ever provided it, so the chips silently never rendered -- this is the
# missing source. Kept neutral and NON-committal on purpose (no promised
# reimbursement, placebo odds, or eligibility guarantees a coordinator must
# confirm), since a chip is inserted verbatim and a rushed send must not state an
# unapproved claim (see COMPLIANCE.md). Edit before sending.
_MARKETING_QUICK_REPLIES = [
    ("Offer a screening call",
     "Would you be open to a short screening call this week? Send me a few times "
     "that work and I'll get it booked."),
    ("Ask best time to reach",
     "What days and times generally work best to reach you? I'll make sure someone "
     "follows up then."),
    ("Still interested?",
     "Just checking in. Are you still interested in learning more about this "
     "study? No pressure either way, just let me know."),
    ("What to expect",
     "Happy to walk you through what taking part involves and answer any questions "
     "on a quick call. Would that help?"),
    ("Thanks + next steps",
     "Thanks for the details, this is really helpful. I'll review and follow up "
     "with the next steps shortly."),
]


@app.route("/app/inbox")
@login_required
def marketing_hub():
    """The product's home: one team-scoped inbox across every channel - marketing
    email, social DMs, ad lead forms, ClinicalTrials.gov, referrals. Canonical at
    /app/inbox; /app/home and /marketing-hub redirect here (see below)."""
    _log_event("view_marketing_hub")
    if _is_demo_account(g.user):
        db.seed_demo_marketing_hub(g.user["id"])

    status = (request.args.get("status") or "open").strip().lower()
    if status not in ("open", "resolved", "all"):
        status = "open"
    channel = (request.args.get("channel") or "").strip().lower()
    if channel not in db.MARKETING_CHANNELS:
        channel = ""
    query = (request.args.get("q") or "").strip()[:100]
    stage_filter = (request.args.get("stage") or "all").strip().lower()
    if stage_filter not in ("all",) + db.MARKETING_PIPELINE_STAGES:
        stage_filter = "all"
    # Private inbox by default: you only see conversations assigned to you
    # ("mine"), plus the shared "unassigned" queue of new inquiries nobody owns
    # yet so intake never disappears. There is deliberately no "see everyone"
    # view - a teammate's conversation only reaches you when it is assigned to you
    # (directly or via away/coverage routing). Persisted in session like the study
    # scope so it survives status/stage/search navigation.
    owner_param = request.args.get("owner")
    if owner_param is not None:
        session["mh_owner"] = (owner_param
                               if owner_param in ("mine", "unassigned") else "mine")
    owner_filter = session.get("mh_owner", "mine")
    if owner_filter not in ("mine", "unassigned"):
        owner_filter = "mine"

    # Scope the whole inbox to the trial chosen in the top switcher, so switching
    # studies shows a different set of people - each trial reads as its own inbox.
    active_nct, _studies = _active_scope()

    sources = [dict(row) for row in db.list_marketing_sources(
        g.user["id"], nct=active_nct)]
    for source in sources:
        last_sync = source.get("last_successful_sync_at") or ""
        source["sync_time_label"] = (
            _marketing_time_label(last_sync) if last_sync else "Not synced yet")
    members = []
    for row in db.list_org_members(g.user["id"]):
        member = dict(row)
        member["display_name"] = member.get("name") or member.get("email") or "Teammate"
        member["initial"] = member["display_name"][:1].upper()
        members.append(member)
    member_by_id = {member["user_id"]: member for member in members}

    source_filter = request.args.get("source", type=int)
    if source_filter not in {source["id"] for source in sources}:
        source_filter = None
    # Searching means "find this person", and a coordinator rarely knows - or
    # should have to know - which study, stage, or queue they are currently
    # scoped to. Applying the scope on top of a query made the common case
    # silently return nothing: search a real name while scoped to another study
    # and you get zero rows, with no hint the person exists one scope over. So a
    # query searches EVERYTHING and the template says so, with one click back.
    searching = bool(query)
    thread_rows = db.list_marketing_threads(
        g.user["id"], status=("" if searching else status), channel=channel,
        query=query, source_id=(None if searching else source_filter),
        nct=("" if searching else active_nct),
        assignee=("" if searching else owner_filter))
    threads = []
    for row in thread_rows:
        item = dict(row)
        item["time_label"] = _marketing_time_label(item.get("updated_at"))
        item["channel_label"] = db.MARKETING_CHANNEL_LABELS.get(
            item.get("channel"), "Message")
        threads.append(item)
    stage_counts = {"all": len(threads)}
    for item in threads:
        key = item.get("pipeline_stage") or "new"
        stage_counts[key] = stage_counts.get(key, 0) + 1
    if stage_filter != "all" and not searching:
        threads = [
            item for item in threads
            if (item.get("pipeline_stage") or "new") == stage_filter
        ]

    selected_id = request.args.get("thread", type=int)
    active = db.get_marketing_thread(g.user["id"], selected_id) if selected_id else None
    if (active and stage_filter != "all"
            and (active["pipeline_stage"] or "new") != stage_filter):
        active = None
    # Nothing is selected until someone clicks a row (`?thread=`). Auto-opening
    # the first conversation made the landing embed and default inbox look busy.
    # Unread only clears on an explicit thread click so reloads do not walk the queue.
    if active and active["unread"] and selected_id:
        db.mark_marketing_thread_read(g.user["id"], active["id"])
        active = db.get_marketing_thread(g.user["id"], active["id"])
        for item in threads:
            if item["id"] == active["id"]:
                item["unread"] = 0

    messages = []
    applicant = None
    eligibility = {}
    records_profile = None
    records_data = {}
    checklist = []
    if active:
        for row in db.list_marketing_messages(g.user["id"], active["id"]):
            item = dict(row)
            item["time_label"] = _marketing_time_label(item.get("created_at"))
            item["display_author"] = (
                item.get("user_name") or item.get("author_name") or "Contact")
            messages.append(item)
        if active["linked_lead_id"]:
            row = db.get_lead(active["linked_lead_id"])
            if row:
                applicant = dict(row)
                try:
                    eligibility = json.loads(applicant.get("eligibility") or "{}")
                except (TypeError, ValueError):
                    eligibility = {}
                if applicant.get("applicant_token"):
                    profile_row = db.get_records_profile(
                        applicant["applicant_token"])
                    records_profile = dict(profile_row) if profile_row else None
                    if records_profile:
                        records_data = records_profile
                checklist = [dict(task) for task in db.list_tasks(applicant["id"])]
        # The reply draft is NOT built here any more. The composer asks for
        # one when the coordinator asks for one (draft.json), so a page load no
        # longer pays for a draft nobody requested - with LLM_API_KEY set that
        # was a synchronous model call on every open of every conversation.

    settings = db.get_marketing_handoff(g.user["id"])
    active_owner_id = db.marketing_active_owner_id(settings)
    active_owner = member_by_id.get(active_owner_id, {
        "display_name": g.user["name"] or g.user["email"],
        "initial": (g.user["name"] or g.user["email"] or "?")[:1].upper(),
    })
    primary = member_by_id.get(settings["primary_user_id"])
    cover = member_by_id.get(settings["cover_user_id"])
    counts = db.marketing_thread_counts(g.user["id"], nct=active_nct,
                                        assignee=owner_filter)
    counts["sources"] = sum(1 for source in sources
                            if source["status"] == "connected")
    # Count of unclaimed inquiries so the "Unassigned" toggle can badge new intake.
    unassigned_count = db.marketing_thread_counts(
        g.user["id"], nct=active_nct, assignee="unassigned")["open"]

    # Coverage state the inbox renders: who is covering, how much would move if
    # you left right now, and the one-time "what happened while you were out".
    my_open_count = db.open_queue_count(g.user["id"])
    _away = db.active_away_for(g.user["id"])
    portal_reveal = None
    applicant_portal = None
    portal_url = ""
    if applicant:
        applicant_portal = db.get_portal_by_lead(applicant["id"])
        reveal = session.get("portal_reveal")
        if (reveal and reveal.get("lead_id") == applicant["id"]):
            portal_reveal = session.pop("portal_reveal", None)
        if applicant_portal and applicant_portal["status"] == "active":
            portal_url = url_for(
                "portal", token=applicant_portal["token"], _external=True)
    return render_template(
        "marketing_hub.html", sources=sources, threads=threads,
        my_open_count=my_open_count,
        cover_name=(db.display_name(_away["cover_user_id"]) if _away else ""),
        away_recap=(None if _away else db.away_recap(g.user["id"])),
        active=dict(active) if active else None, messages=messages,
        members=members, member_by_id=member_by_id, settings=dict(settings),
        active_owner=active_owner, primary=primary, cover=cover, counts=counts,
        status_filter=status, channel_filter=channel, search_query=query,
        searching=searching,
        source_filter=source_filter, active_nct=active_nct,
        stage_filter=stage_filter, stage_counts=stage_counts,
        owner_filter=owner_filter, unassigned_count=unassigned_count,
        applicant=applicant, eligibility=eligibility,
        applicant_portal=applicant_portal,
        portal_reveal=portal_reveal, portal_url=portal_url,
        records_profile=records_profile, records_data=records_data,
        checklist=checklist,
        is_demo=_is_demo_account(g.user),
        quick_replies=_MARKETING_QUICK_REPLIES,
        channel_labels=db.MARKETING_CHANNEL_LABELS,
        gmail_oauth_ready=_google_ready(),
        instagram_oauth_ready=_instagram_ready(),
        today=dt.date.today().isoformat())


def _private_marketing_workspace_required(provider_name="external account"):
    session_user_id = session.get(USER_SESSION_KEY)
    if not session_user_id:
        session[USER_NEXT_KEY] = url_for("marketing_hub")
        flash(f"Sign in to your own workspace before connecting {provider_name}.",
              "error")
        return redirect(url_for("login", next=url_for("marketing_hub")))
    if _is_demo_account(g.user):
        flash("External accounts cannot be attached to the public demo workspace.",
              "error")
        return redirect(url_for("marketing_hub"))
    return None


@app.route("/marketing-hub/connect/instagram")
@login_required
def marketing_instagram_connect():
    blocked = _private_marketing_workspace_required("Instagram")
    if blocked:
        return blocked
    if not _instagram_ready():
        flash("Instagram Business Login is not configured.", "error")
        return redirect(url_for("marketing_hub"))
    state = secrets.token_urlsafe(32)
    session[MARKETING_INSTAGRAM_STATE_KEY] = {
        "state": state,
        "user_id": g.user["id"],
        "created_at": int(time.time()),
    }
    return redirect(_instagram_authorization_url(state))


@app.route("/integrations/instagram/callback")
@login_required
def marketing_instagram_callback():
    pending = session.pop(MARKETING_INSTAGRAM_STATE_KEY, None)
    if not _instagram_ready():
        flash("Instagram Business Login is not configured.", "error")
        return redirect(url_for("marketing_hub"))
    state = request.args.get("state", "")
    expected = pending.get("state", "") if isinstance(pending, dict) else ""
    pending_user_id = pending.get("user_id") if isinstance(pending, dict) else None
    created_at = pending.get("created_at", 0) if isinstance(pending, dict) else 0
    try:
        state_age = int(time.time()) - int(created_at)
        state_is_fresh = 0 <= state_age <= 10 * 60
    except (TypeError, ValueError):
        state_is_fresh = False
    pending_user_is_valid = bool(
        g.user and pending_user_id == g.user["id"]
        and session.get(USER_SESSION_KEY) == pending_user_id
        and not _is_demo_account(g.user))
    if (not state or not expected or not hmac.compare_digest(state, expected)
            or not state_is_fresh or not pending_user_is_valid):
        flash("Instagram connection failed state validation. Please try again.",
              "error")
        return redirect(url_for("marketing_hub"))
    if request.args.get("error"):
        flash("Instagram connection was cancelled. No account was added.", "error")
        return redirect(url_for("marketing_hub"))
    code = request.args.get("code", "").strip()
    if not code:
        flash("Instagram did not return an authorization code.", "error")
        return redirect(url_for("marketing_hub"))

    try:
        short_payload = _instagram_exchange_code(code)
        short_token = str(short_payload.get("access_token") or "").strip()
        instagram_user_id = str(
            short_payload.get("user_id") or short_payload.get("id") or "").strip()
        if not short_token or not instagram_user_id:
            raise InstagramAPIError(
                "Instagram returned no account authorization.")

        permissions = short_payload.get("permissions") or []
        if isinstance(permissions, str):
            granted_scopes = set(permissions.replace(",", " ").split())
        else:
            granted_scopes = {
                str(scope).strip() for scope in permissions if str(scope).strip()}
        if not granted_scopes:
            granted_scopes = set(INSTAGRAM_OAUTH_SCOPES)
        if not set(INSTAGRAM_OAUTH_SCOPES).issubset(granted_scopes):
            flash("Instagram needs profile and message permissions to connect.",
                  "error")
            return redirect(url_for("marketing_hub"))

        long_payload = _instagram_exchange_long_lived_token(short_token)
        access_token = str(long_payload.get("access_token") or "").strip()
        if not access_token:
            raise InstagramAPIError("Instagram returned no long-lived token.")
        profile = _instagram_profile(access_token, instagram_user_id)
        profile_id = str(
            profile.get("user_id") or profile.get("id") or "").strip()
        username = str(profile.get("username") or "").strip().lstrip("@")
        account_type = str(profile.get("account_type") or "").strip().upper()
        if profile_id and profile_id != instagram_user_id:
            raise InstagramAPIError("Instagram returned an inconsistent profile.")
        if not username:
            raise InstagramAPIError("Instagram returned no professional username.")
        if account_type and account_type not in {
                "BUSINESS", "CREATOR", "MEDIA_CREATOR"}:
            raise InstagramAPIError(
                "Only Instagram professional accounts can be connected.")

        _instagram_subscribe_webhooks(access_token, instagram_user_id)
        result = db.connect_marketing_account(
            g.user["id"], provider="instagram", channel="instagram",
            external_account_id=instagram_user_id,
            account_identifier=f"@{username}",
            access_token_encrypted=token_crypto.encrypt_token(
                access_token, app.secret_key),
            granted_scopes=granted_scopes,
            token_expires_at=_oauth_token_expiry(
                long_payload.get("expires_in") or 60 * 24 * 60 * 60),
            instagram_account_id=instagram_user_id,
            label="Instagram DMs")
        if not result:
            raise RuntimeError("Connection storage rejected the account.")
    except InstagramAPIError as exc:
        app.logger.warning(
            "Instagram OAuth callback failed (status=%s code=%s)",
            exc.status, exc.code or "unknown")
        flash("Instagram could not be connected. Check the account permissions "
              "and try again.", "error")
        return redirect(url_for("marketing_hub"))
    except Exception:
        app.logger.exception("Instagram OAuth callback failed")
        flash("Instagram could not be connected. Please try again.", "error")
        return redirect(url_for("marketing_hub"))

    _log_event("instagram_connected")
    flash(f"Instagram connected for @{username} and subscribed to message "
          "notifications.", "ok")
    return redirect(url_for("marketing_hub", source=result["source_id"]))


@app.route("/marketing-hub/connect/gmail")
def marketing_gmail_connect():
    if not _google_ready():
        flash("Gmail OAuth is not configured. Add the Google client ID and secret.",
              "error")
        return redirect(url_for("marketing_hub"))
    session_user_id = session.get(USER_SESSION_KEY)
    if not g.user or _is_demo_account(g.user):
        session_user_id = None
    state = secrets.token_urlsafe(32)
    session[MARKETING_GOOGLE_STATE_KEY] = {
        "state": state,
        "user_id": session_user_id,
        "created_at": int(time.time()),
    }
    qs = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": _oauth_redirect_url("marketing_gmail_callback"),
        "response_type": "code",
        "scope": " ".join(GMAIL_OAUTH_SCOPES),
        "state": state,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent select_account",
    })
    return redirect(f"{GOOGLE_AUTH_URL}?{qs}")


@app.route("/integrations/gmail/callback")
def marketing_gmail_callback():
    pending = session.pop(MARKETING_GOOGLE_STATE_KEY, None)
    if not _google_ready():
        flash("Gmail OAuth is not configured.", "error")
        return redirect(url_for("marketing_hub"))
    state = request.args.get("state", "")
    expected = pending.get("state", "") if isinstance(pending, dict) else ""
    pending_user_id = pending.get("user_id") if isinstance(pending, dict) else None
    created_at = pending.get("created_at", 0) if isinstance(pending, dict) else 0
    try:
        state_age = int(time.time()) - int(created_at)
        state_is_fresh = 0 <= state_age <= 10 * 60
    except (TypeError, ValueError):
        state_is_fresh = False
    pending_user_is_valid = (
        pending_user_id is None
        or (g.user is not None and session.get(USER_SESSION_KEY) == pending_user_id
            and g.user["id"] == pending_user_id
            and not _is_demo_account(g.user)))
    if (not state or not expected or not hmac.compare_digest(state, expected)
            or not pending_user_is_valid or not state_is_fresh):
        flash("Gmail connection failed state validation. Please try again.", "error")
        return redirect(url_for("marketing_hub"))
    if request.args.get("error"):
        flash("Gmail connection was cancelled. No account was added.", "error")
        return redirect(url_for("marketing_hub"))
    code = request.args.get("code", "").strip()
    if not code:
        flash("Google did not return an authorization code.", "error")
        return redirect(url_for("marketing_hub"))

    try:
        token_payload = _google_exchange_code(code, "marketing_gmail_callback")
        access_token = (token_payload or {}).get("access_token", "")
        if not access_token:
            raise RuntimeError("Google returned no access token.")
        identity = _google_userinfo(access_token)
        gmail_profile = _gmail_profile(access_token)

        external_account_id = (identity.get("sub") or "").strip()
        identity_email = (identity.get("email") or "").strip().lower()
        identity_name = (identity.get("name") or "").strip()
        identity_picture = (identity.get("picture") or "").strip()
        gmail_email = (gmail_profile.get("emailAddress") or "").strip().lower()
        if (not external_account_id or not gmail_email
                or identity.get("email_verified") is False
                or (identity_email and identity_email != gmail_email)):
            raise RuntimeError("Google returned an inconsistent account profile.")

        granted_scopes = set(
            ((token_payload or {}).get("scope") or " ".join(GMAIL_OAUTH_SCOPES)).split())
        required_scopes = {
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
        }
        if not required_scopes.issubset(granted_scopes):
            flash("Gmail needs both read and send permission to connect.", "error")
            return redirect(url_for("marketing_hub"))

        workspace_user = db.get_user(pending_user_id) if pending_user_id else None
        if not workspace_user:
            workspace_user = db.get_user_by_oauth("google", external_account_id)
        if not workspace_user and not pending_user_id:
            workspace_user = db.get_user_by_email(gmail_email)
        if _is_demo_account(workspace_user):
            flash("This account belongs to the shared public demo and cannot "
                  "store external credentials.", "error")
            return redirect(url_for("marketing_hub"))
        connection_user_id = workspace_user["id"] if workspace_user else None
        existing = (db.get_marketing_connection_by_account(
            connection_user_id, "gmail", external_account_id)
            if connection_user_id else None)
        refresh_token = (token_payload or {}).get("refresh_token", "")
        if not refresh_token and not (
                existing and existing["refresh_token_encrypted"]):
            flash("Google did not grant offline access. Reconnect and approve all "
                  "requested permissions.", "error")
            return redirect(url_for("marketing_hub"))

        if not workspace_user:
            connection_user_id = db.create_user(
                email=gmail_email,
                password_hash=generate_password_hash(
                    secrets.token_urlsafe(32), method="pbkdf2:sha256"),
                name=identity_name or gmail_email.split("@", 1)[0],
                verified=True,
                oauth_provider="google",
                oauth_sub=external_account_id,
                oauth_picture=identity_picture)
            workspace_user = db.get_user(connection_user_id)
        elif not pending_user_id and (
                workspace_user["oauth_provider"] != "google"
                or workspace_user["oauth_sub"] != external_account_id):
            db.link_user_oauth(
                workspace_user["id"], "google", external_account_id,
                identity_name, identity_picture)
            workspace_user = db.get_user(workspace_user["id"])
        connection_user_id = workspace_user["id"]
        if pending_user_id is None:
            session[USER_SESSION_KEY] = connection_user_id
            session.pop(USER_PENDING_KEY, None)
            session.pop(USER_PENDING_PURPOSE_KEY, None)
            g.user = workspace_user

        result = db.connect_marketing_account(
            connection_user_id, provider="gmail", channel="email",
            external_account_id=external_account_id,
            account_identifier=gmail_email,
            access_token_encrypted=token_crypto.encrypt_token(
                access_token, app.secret_key),
            refresh_token_encrypted=token_crypto.encrypt_token(
                refresh_token, app.secret_key) if refresh_token else "",
            granted_scopes=granted_scopes,
            token_expires_at=_oauth_token_expiry(
                (token_payload or {}).get("expires_in")),
            gmail_history_id=str(gmail_profile.get("historyId") or ""),
            label="Gmail inbox")
        if not result:
            raise RuntimeError("Connection storage rejected the account.")
    except Exception:
        app.logger.exception("gmail oauth callback failed")
        flash("Gmail could not be connected. Please try again.", "error")
        return redirect(url_for("marketing_hub"))

    sync_result = None
    try:
        sync_result = _sync_gmail_connection(
            connection_user_id, result["connection_id"], force_full=True)
    except GmailSyncError:
        app.logger.warning("initial gmail sync failed", exc_info=True)

    _log_event("gmail_connected")
    if sync_result and sync_result["imported_messages"]:
        session["active_nct"] = ""
        count = sync_result["imported_messages"]
        flash(f"Gmail connected. Imported {count} message{'s' if count != 1 else ''}.",
              "ok")
    elif sync_result:
        flash(f"Gmail connected for {gmail_email}. Your inbox is up to date.", "ok")
    else:
        flash("Gmail was authorized, but the first inbox sync failed. Use Sync now "
              "to retry.", "error")
    return redirect(url_for(
        "marketing_hub", source=result["source_id"],
        thread=(sync_result or {}).get("newest_thread_id")))


@app.route("/marketing-hub/connections/<int:connection_id>/sync",
           methods=["POST"])
@login_required
def marketing_connection_sync(connection_id):
    blocked = _private_marketing_workspace_required()
    if blocked:
        return blocked
    connection = db.get_marketing_connection(g.user["id"], connection_id)
    if not connection or connection["provider"] != "gmail":
        abort(404)
    try:
        result = _sync_gmail_connection(g.user["id"], connection_id)
    except GmailSyncError as exc:
        if _is_json_request():
            return jsonify({
                "ok": False,
                "error": "gmail_reconnect_required" if exc.requires_reauth
                         else "gmail_sync_failed",
                "message": str(exc),
                "reconnect_url": url_for("marketing_gmail_connect"),
            }), 409 if exc.requires_reauth else 502
        flash(str(exc), "error")
        return redirect(url_for(
            "marketing_hub", source=connection["source_id"]))

    imported = result["imported_messages"]
    if imported:
        # Gmail cannot infer a study reliably. Show new general mail immediately
        # instead of hiding it under whichever study happened to be selected.
        session["active_nct"] = ""
    target = url_for(
        "marketing_hub", source=result["source_id"],
        thread=result["newest_thread_id"])
    if _is_json_request():
        return jsonify({"ok": True, **result, "redirect_url": target})
    if imported:
        flash(f"Imported {imported} new Gmail message"
              f"{'s' if imported != 1 else ''}.", "ok")
    else:
        flash("Gmail is up to date.", "ok")
    return redirect(target)


@app.route("/marketing-hub/connections/<int:connection_id>/disconnect",
           methods=["POST"])
@login_required
def marketing_connection_disconnect(connection_id):
    blocked = _private_marketing_workspace_required()
    if blocked:
        return blocked
    connection = db.get_marketing_connection(g.user["id"], connection_id)
    if not connection:
        abort(404)
    if connection["provider"] == "gmail":
        try:
            encrypted = (connection["refresh_token_encrypted"]
                         or connection["access_token_encrypted"])
            token = token_crypto.decrypt_token(encrypted, app.secret_key)
            if token:
                _google_revoke_token(token)
        except Exception:
            # Local credentials are still erased even if Google has already
            # revoked the grant or its revocation endpoint is unavailable.
            app.logger.warning("google token revocation failed", exc_info=True)
    elif connection["provider"] == "instagram":
        try:
            access_token = token_crypto.decrypt_token(
                connection["access_token_encrypted"], app.secret_key)
            _instagram_unsubscribe_webhooks(
                access_token,
                connection["instagram_account_id"]
                or connection["external_account_id"])
        except Exception:
            # Deleting local credentials takes priority if Meta is unavailable or
            # the user has already revoked the grant remotely.
            app.logger.warning(
                "Instagram webhook unsubscription failed", exc_info=True)
    result = db.disconnect_marketing_connection(
        g.user["id"], connection_id, purge_imported_threads=True)
    if not result:
        abort(404)
    provider_label = (
        "Instagram" if connection["provider"] == "instagram" else "Gmail")
    removed = result["deleted_threads"]
    conversation_label = "conversation" if removed == 1 else "conversations"
    flash(
        f"{provider_label} disconnected. Stored credentials and {removed} "
        f"imported {conversation_label} were removed.", "ok")
    return redirect(url_for("marketing_hub"))


@app.route("/marketing-hub")
@login_required
def marketing_hub_legacy():
    """Back-compat: the marketing hub is now the main inbox at /app/inbox. Keep
    the old URL working for bookmarks/links by redirecting (carrying filters)."""
    return redirect(url_for("marketing_hub", **request.args.to_dict()))


@app.route("/marketing-hub/sources", methods=["POST"])
@login_required
def marketing_source_add():
    channel = (request.form.get("channel") or "").strip().lower()
    label = (request.form.get("label") or "").strip()
    identifier = (request.form.get("identifier") or "").strip()
    if channel not in db.MARKETING_CHANNELS:
        flash("Choose email, Instagram, or Google Ads.", "error")
        return redirect(url_for("marketing_hub"))
    if channel == "email":
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", identifier):
            flash("Enter a valid email address.", "error")
            return redirect(url_for("marketing_hub"))
    elif channel == "instagram":
        identifier = identifier.lstrip("@")
        if not identifier or re.search(r"\s", identifier):
            flash("Enter an Instagram handle without spaces.", "error")
            return redirect(url_for("marketing_hub"))
        identifier = "@" + identifier
    elif not identifier:
        flash("Enter the Google Ads account name or customer ID.", "error")
        return redirect(url_for("marketing_hub"))
    if not label:
        label = db.MARKETING_CHANNEL_LABELS[channel]
    source_id = db.create_marketing_source(
        g.user["id"], channel, label, identifier, connection_mode="demo")
    if source_id:
        flash("Account added. It is ready for test conversations; OAuth is still "
              "needed for live syncing.", "ok")
    else:
        flash("That account could not be added.", "error")
    return redirect(url_for("marketing_hub", source=source_id) if source_id
                    else url_for("marketing_hub"))


@app.route("/marketing-hub/sources/<int:source_id>/status", methods=["POST"])
@login_required
def marketing_source_status(source_id):
    status = (request.form.get("status") or "").strip().lower()
    source = db.get_marketing_source(g.user["id"], source_id)
    if not source:
        abort(404)
    if source["connection_mode"] == "live":
        flash("Use the Gmail reconnect or disconnect control for live accounts.",
              "error")
        return redirect(url_for("marketing_hub", source=source_id))
    if not db.set_marketing_source_status(g.user["id"], source_id, status):
        abort(404)
    flash("Channel connected." if status == "connected" else "Channel disconnected.",
          "ok")
    return redirect(url_for("marketing_hub"))


@app.route("/marketing-hub/threads", methods=["POST"])
@login_required
def marketing_thread_create():
    source_id = request.form.get("source_id", type=int)
    thread_id = db.create_marketing_thread(
        g.user["id"], source_id,
        request.form.get("contact_name"), request.form.get("contact_handle"),
        request.form.get("subject"), request.form.get("body"))
    if not thread_id:
        flash("Choose a connected channel and enter a message.", "error")
        return redirect(url_for("marketing_hub"))
    flash("Test conversation added to the shared inbox.", "ok")
    return redirect(_marketing_thread_url(thread_id))


@app.route("/marketing-hub/threads/<int:thread_id>/reply", methods=["POST"])
@login_required
def marketing_thread_reply(thread_id):
    thread = db.get_marketing_thread(g.user["id"], thread_id)
    if not thread:
        abort(404)
    body = (request.form.get("body") or "").strip()[:4000]
    if not body:
        flash("Write a reply before sending.", "error")
        return redirect(_marketing_thread_url(thread_id, "composer"))

    if thread["connection_mode"] == "demo":
        if not db.add_marketing_message(
                g.user["id"], thread_id, body, kind="outbound"):
            flash("The reply could not be sent.", "error")
        return redirect(_marketing_thread_url(thread_id, "composer"))

    blocked = _private_marketing_workspace_required()
    if blocked:
        return blocked
    if thread["provider"] == "instagram":
        inbound = next(
            (row for row in reversed(
                db.list_marketing_messages(g.user["id"], thread_id))
             if row["kind"] == "inbound"), None)
        try:
            inbound_at = dt.datetime.strptime(
                (inbound["created_at"] or "")[:16], "%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            inbound_at = None
        if not inbound_at or dt.datetime.now() - inbound_at > dt.timedelta(hours=24):
            flash("Instagram's reply window has closed. Ask the person to send "
                  "a new message before replying.", "error")
            return redirect(_marketing_thread_url(thread_id, "composer"))
        try:
            access_token = token_crypto.decrypt_token(
                thread["access_token_encrypted"], app.secret_key)
            message_id = _instagram_send_reply(
                access_token,
                thread["instagram_account_id"] or thread["external_account_id"],
                thread["external_ref"], body)
        except (InstagramAPIError, token_crypto.TokenEncryptionError):
            app.logger.warning("Instagram reply failed", exc_info=True)
            flash("Instagram could not deliver this reply. Reconnect the account "
                  "or try again.", "error")
            return redirect(_marketing_thread_url(thread_id, "composer"))
        stored = db.add_marketing_message(
            g.user["id"], thread_id, body, kind="outbound",
            delivery_status="sent", external_ref=message_id)
        if not stored:
            flash("Instagram sent the reply, but the local copy could not be "
                  "saved. Do not resend it.", "error")
        else:
            _log_event("instagram_reply_sent")
        return redirect(_marketing_thread_url(thread_id, "composer"))
    if thread["provider"] != "gmail":
        flash("External delivery is not available for this account.", "error")
        return redirect(_marketing_thread_url(thread_id, "composer"))
    try:
        sent = _send_gmail_reply(g.user["id"], thread, body)
    except GmailDeliveryError as exc:
        flash(str(exc), "error")
        return redirect(_marketing_thread_url(thread_id, "composer"))

    stored = db.add_marketing_message(
        g.user["id"], thread_id, body, kind="outbound",
        delivery_status="sent", external_ref=sent["id"])
    _log_event("gmail_reply_sent")
    if not stored:
        app.logger.error(
            "gmail reply sent but local persistence failed: thread=%s message=%s",
            thread_id, sent["id"])
        flash("Gmail sent the reply, but the workspace could not save its local "
              "copy. Do not resend it.", "error")
    elif sent["thread_id"] and sent["thread_id"] != sent["expected_thread_id"]:
        flash("Reply sent through Gmail, but Gmail started a separate conversation.",
              "error")
    return redirect(_marketing_thread_url(thread_id, "composer"))


@app.route("/marketing-hub/threads/<int:thread_id>/note", methods=["POST"])
@login_required
def marketing_thread_note(thread_id):
    body = request.form.get("body")
    msg_id = db.add_marketing_message(g.user["id"], thread_id, body, kind="note")
    if not msg_id:
        flash("Write a note before adding it.", "error")
        return redirect(_marketing_thread_url(thread_id, "composer"))
    author = db.display_name(g.user["id"]) or "A teammate"
    people = db.record_mentions(g.user["id"], "marketing_thread", thread_id,
                                "marketing_message", _int_or(msg_id, 0), body)
    if people:
        _mention_notify(people, author, "a conversation",
                        url_for("marketing_hub", thread=thread_id,
                                _external=True))
        flash("Internal note added. " + _mention_flash(people), "ok")
    else:
        flash("Internal note added for the team.", "ok")
    return redirect(_marketing_thread_url(thread_id, "composer"))


@app.route("/marketing-hub/threads/<int:thread_id>/assign", methods=["POST"])
@login_required
def marketing_thread_assign(thread_id):
    raw = (request.form.get("assignee_id") or "").strip()
    try:
        assignee_id = int(raw) if raw else None
    except ValueError:
        assignee_id = None
    if not db.assign_marketing_thread(g.user["id"], thread_id, assignee_id):
        abort(404)
    flash("Conversation assignment updated.", "ok")
    return redirect(_marketing_thread_url(thread_id))


@app.route("/marketing-hub/threads/<int:thread_id>/status", methods=["POST"])
@login_required
def marketing_thread_status(thread_id):
    status = (request.form.get("status") or "").strip().lower()
    if not db.set_marketing_thread_status(g.user["id"], thread_id, status):
        abort(404)
    flash("Conversation resolved." if status == "resolved"
          else "Conversation reopened.", "ok")
    return redirect(_marketing_thread_url(thread_id))


# One conversation stage <-> one applicant status. The inbox and the Applicants
# queue are two views of the same person, so this mapping is defined once and
# used by both the stage control and by applicant creation - otherwise a thread
# marked "Screening" could produce an applicant sitting in "Pre-screen".
_INBOX_STAGE_TO_LEAD_STATUS = {
    "new": "submitted", "outreach": "prescreen", "prescreen": "prescreen",
    "screening": "screening", "enrolled": "enrolled",
    "disqualified": "closed", "archived": "closed",
}


@app.route("/marketing-hub/threads/<int:thread_id>/stage", methods=["POST"])
@login_required
def marketing_thread_stage(thread_id):
    stage = (request.form.get("stage") or "").strip().lower()
    thread = db.get_marketing_thread(g.user["id"], thread_id)
    if not thread or not db.set_marketing_thread_stage(
            g.user["id"], thread_id, stage):
        abort(404)
    if thread["linked_lead_id"]:
        lead_stage = _INBOX_STAGE_TO_LEAD_STATUS.get(stage, "prescreen")
        db.update_lead_status(
            thread["linked_lead_id"], lead_stage,
            note="stage updated from shared inbox", actor="site")
        if stage in ("screening", "enrolled") and not db.list_tasks(
                thread["linked_lead_id"]):
            for title in (
                    "Review patient-authorized records",
                    "Confirm coordinator eligibility review",
                    "Book screening visit",
                    "Document consent discussion"):
                db.add_task(
                    thread["linked_lead_id"], title,
                    assigned_to="site", created_by="site")
    flash("Recruitment stage updated.", "ok")
    return redirect(_marketing_thread_url(thread_id))


@app.route("/marketing-hub/threads/<int:thread_id>/applicant", methods=["POST"])
@login_required
def marketing_thread_create_applicant(thread_id):
    thread = db.get_marketing_thread(g.user["id"], thread_id)
    if not thread:
        abort(404)
    if thread["linked_lead_id"]:
        flash("This conversation is already linked to an applicant.", "ok")
        return redirect(_marketing_thread_url(thread_id))
    if request.form.get("consent") != "1":
        flash("Confirm the person's permission before creating an applicant.",
              "error")
        return redirect(_marketing_thread_url(thread_id))
    nct = (thread["nct"] or "").strip()
    if not nct or nct not in set(db.user_claimed_ncts(g.user["id"])):
        flash("Choose a verified study before creating an applicant.", "error")
        return redirect(_marketing_thread_url(thread_id))
    handle = (thread["contact_handle"] or "").strip()
    token = db.create_lead({
        "applicant_token": "inbox-" + secrets.token_urlsafe(12),
        "nct": nct,
        "title": thread["study_label"] or nct,
        "name": thread["contact_name"] or "Prospective participant",
        "email": handle if "@" in handle and not handle.startswith("@") else "",
        "consent": 1,
        "source": thread["channel"] or "inbox",
        "owner_user_id": g.user["id"],
        "eligibility": json.dumps({
            "verdict": "possible",
            "met": [],
            "unknown": ["Review study inclusion and exclusion criteria"],
            "not_met": [],
        }),
    })
    lead = db.get_lead_by_token(token)
    if not lead or not db.link_marketing_thread_lead(
            g.user["id"], thread_id, lead["id"]):
        flash("The applicant could not be linked.", "error")
        return redirect(_marketing_thread_url(thread_id))
    # Un-blind immediately. Candidate codes exist to protect someone who applied
    # THROUGH BridgeMD until the site accepts them; this person wrote to the site
    # directly, so the site already holds their name and contact details. Leaving
    # the record blinded hid nothing and broke the rest of the flow: they showed
    # in Applicants as "Candidate #0117" with no email, weren't selectable, and
    # db.blast_audience skipped them - so a coordinator could never message the
    # person they were already mid-conversation with. Review is untouched: the
    # lead still lands in "Awaiting review" with no decision recorded.
    db.reveal_lead_from_inbox(
        lead["id"],
        f"permission to contact confirmed by {db.display_name(g.user['id']) or 'a coordinator'} "
        f"from the {thread['channel'] or 'inbox'} conversation")
    # The conversation's stage and the applicant's pipeline status are the same
    # fact shown in two places, so seed the lead from the thread instead of
    # letting them start out disagreeing.
    stage = (thread["pipeline_stage"] or "new").strip().lower()
    lead_status = _INBOX_STAGE_TO_LEAD_STATUS.get(stage, "prescreen")
    # Only ever carry the stage FORWARD. A new lead already starts at
    # "prescreen"; mapping an early thread back to "submitted" would drop it out
    # of the Awaiting-review bucket, and a closed stage would create an applicant
    # that is dead on arrival.
    if (lead_status in db.LEAD_PIPELINE
            and db.LEAD_PIPELINE.index(lead_status)
            > db.LEAD_PIPELINE.index("prescreen")):
        db.update_lead_status(lead["id"], lead_status,
                              note="stage carried over from the inbox",
                              actor="site")
    _log_event("marketing_applicant_linked", {
        "thread_id": thread_id, "lead_id": lead["id"], "nct": nct})
    flash(f"{lead['name']} is now an applicant on "
          f"{thread['study_label'] or nct}. Permission to contact was recorded "
          f"from this conversation.", "ok")
    return redirect(_marketing_thread_url(thread_id))


@app.route("/marketing-hub/threads/<int:thread_id>/records", methods=["POST"])
@login_required
def marketing_thread_records(thread_id):
    thread = db.get_marketing_thread(g.user["id"], thread_id)
    if not thread or not thread["linked_lead_id"]:
        abort(404)
    if not _is_demo_account(g.user):
        flash("Records aren't connected for this workspace yet.", "error")
        return redirect(_marketing_thread_url(thread_id, "records"))
    lead = db.get_lead(thread["linked_lead_id"])
    if not lead or request.form.get("authorization_confirmed") != "1":
        flash("Confirm they agreed to share records before continuing.",
              "error")
        return redirect(_marketing_thread_url(thread_id, "records"))
    db.authorize_lead_records(lead["id"])
    try:
        prof = records_mod.connect(None, lead["applicant_token"])
        db.set_records_profile(lead["applicant_token"], prof)
        summary = records_mod.summary_text(prof)
        db.attach_records_to_open_leads(lead["applicant_token"], summary)
        try:
            elig = json.loads(lead["eligibility"] or "{}")
        except (TypeError, ValueError):
            elig = {}
        elig, _ = records_mod.autofill_eligibility(prof, elig)
        db.set_lead_prescreen(lead["id"], json.dumps(elig),
                              lead["prescreen_readiness"] or "")
        _log_event("demo_records_retrieved", {
            "thread_id": thread_id, "lead_id": lead["id"],
            "provider": records_mod.provider()})
        flash("Records updated.", "ok")
    except Exception:
        app.logger.exception("demo record retrieval failed")
        flash("Couldn't get records. Try again.", "error")
    return redirect(_marketing_thread_url(thread_id, "records"))


@app.route("/marketing-hub/threads/<int:thread_id>/checklist/<int:task_id>",
           methods=["POST"])
@login_required
def marketing_thread_checklist(thread_id, task_id):
    thread = db.get_marketing_thread(g.user["id"], thread_id)
    if not thread or not thread["linked_lead_id"] or not _is_demo_account(g.user):
        abort(404)
    tasks = {task["id"]: task for task in db.list_tasks(thread["linked_lead_id"])}
    if task_id not in tasks:
        abort(404)
    status = "done" if request.form.get("done") == "1" else "open"
    db.set_task_status(task_id, status)
    return redirect(_marketing_thread_url(thread_id, "checklist"))


@app.route("/marketing-hub/threads/<int:thread_id>/draft.json",
           methods=["GET", "POST"])
@login_required
def marketing_thread_draft(thread_id):
    """Bridget writes the reply the coordinator asked for.

    POST an ``instruction`` ("offer a screening call next week") and the draft
    follows it; with no instruction it answers the last inbound message, which
    is what the GET form does. An instruction is enough on its own, so an
    outreach thread with nothing inbound can still be drafted. Nothing here
    sends: the draft lands in the composer for a human to edit."""
    if not db.get_marketing_thread(g.user["id"], thread_id):
        abort(404)
    data = request.get_json(silent=True) or {}
    instruction = (data.get("instruction")
                   or request.form.get("instruction") or "").strip()[:400]
    # Same writer the rail uses (Bridget with this conversation open), so the
    # two entry points can never drift apart.
    res = copilot.agent.draft_for_thread(g.user["id"], thread_id, instruction)
    if res.get("hold") or res.get("blocked"):
        # Triage held it for a person, or the draft broke a rule. Nothing is
        # written; the reason is the message.
        return jsonify({"ok": False, "message": res["reason"],
                        "hold": bool(res.get("hold")),
                        "blocked": bool(res.get("blocked")),
                        "category": res.get("category")}), 409
    if res.get("error"):
        return jsonify({"ok": False, "message": res["error"]}), 400
    return jsonify({"ok": True, "draft": res["draft"],
                    "category": res.get("category"),
                    "flags": res.get("flags", []),
                    "human_review_required": True})


@app.route("/marketing-hub/coverage", methods=["POST"])
@login_required
def marketing_coverage():
    primary_id = request.form.get("primary_user_id", type=int) or g.user["id"]
    cover_id = request.form.get("cover_user_id", type=int)
    vacation_mode = request.form.get("vacation_mode") == "1"
    away_until = (request.form.get("away_until") or "").strip()
    if away_until:
        try:
            parsed = dt.date.fromisoformat(away_until)
        except ValueError:
            flash("Choose a valid return date.", "error")
            return redirect(url_for("marketing_hub"))
        if vacation_mode and parsed < dt.date.today():
            flash("The return date cannot be in the past.", "error")
            return redirect(url_for("marketing_hub"))
    ok = db.set_marketing_handoff(
        g.user["id"], primary_id, cover_id, vacation_mode, away_until,
        request.form.get("note"))
    if not ok:
        flash("Choose a different teammate to cover the inbox.", "error")
    elif vacation_mode:
        flash("Vacation coverage is on. New conversations route to your cover.",
              "ok")
    else:
        flash("Vacation coverage is off. The primary owner is back on duty.", "ok")
    return redirect(url_for("marketing_hub"))


@app.route("/for-sites/<slug>")
def for_sites_feature(slug):
    """Retired. These four pages pitched the old "coordinator OS" (intake,
    pre-screen, scheduling, documents) and directly contradicted what the
    product now is: one inbox. Rather than leave contradictory pages reachable
    (and indexed), every known slug 301s to the hub. `sites_features.py`
    remains the canonical slug registry used by navigation and the sitemap."""
    if sites_features.get(slug) is None:
        abort(404)
    return redirect(url_for("inbox") + "#how", code=301)


# The public blog / resources section was removed from the product. The route
# names are kept as permanent (301) redirects to the sites home so old inbound
# links and search-indexed URLs don't 404, and so any stray url_for("blog_index")
# still builds instead of crashing a page.
@app.route("/blog")
def blog_index():
    return redirect(url_for("inbox"), code=301)


@app.route("/blog/<slug>")
def blog_post(slug):
    return redirect(url_for("inbox"), code=301)


@app.route("/privacy")
def privacy():
    return render_template("privacy.html", legal_contact=LEGAL_CONTACT,
                           legal_updated=LEGAL_UPDATED)


@app.route("/terms")
def terms():
    return render_template("terms.html", legal_contact=LEGAL_CONTACT,
                           legal_updated=LEGAL_UPDATED)


@app.route("/a/<token>")
def application_public(token):
    """Guest-safe thread for one application. The token is the secret; no
    patient login. This is what message emails link to so someone who applied
    without an account can still talk to the study team."""
    lead = db.get_lead_by_token(token)
    if not lead:
        abort(410)
    db.mark_thread_read(lead["id"], "patient")
    return render_template(
        "application_public.html",
        lead=lead,
        messages=db.get_messages(lead["id"]),
        labels=db.LEAD_LABELS,
        closed=lead["status"] in db.LEAD_CLOSED)


@app.route("/a/<token>/message", methods=["POST"])
def application_public_message(token):
    """Applicant replies from the secret link. Same thread the site/operator
    already sees."""
    blocked = _guard_ip_rate_limit("application_public_message")
    if blocked:
        return blocked
    lead = db.get_lead_by_token(token)
    if not lead:
        abort(410)
    body = request.form.get("body", "").strip()
    if body:
        db.add_message(lead["id"], "patient", body)
        _notify_site_message(lead, body)
        flash("Message sent to the study team.", "success")
    else:
        flash("Write a message first.", "error")
    return redirect(url_for("application_public", token=token))


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


def _meta_callback_signed_request():
    signed_request = request.form.get("signed_request", "")
    if not signed_request and request.is_json:
        payload = request.get_json(silent=True) or {}
        if isinstance(payload, dict):
            signed_request = payload.get("signed_request", "")
    return str(signed_request or "").strip()


@app.route("/integrations/instagram/deauthorize", methods=["GET", "POST"])
def instagram_deauthorize():
    """Erase Instagram credentials when Meta reports app deauthorization."""
    if request.method == "GET":
        return jsonify({"ok": True, "callback": "instagram_deauthorize"})
    try:
        payload = _meta_signed_request_payload(_meta_callback_signed_request())
        instagram_user_id = str(payload.get("user_id") or "").strip()
        if not instagram_user_id:
            raise ValueError("Invalid signed request.")
    except RuntimeError:
        return jsonify({"ok": False, "error": "callback_not_configured"}), 503
    except ValueError:
        return jsonify({"ok": False, "error": "invalid_signed_request"}), 403
    db.deauthorize_marketing_account("instagram", instagram_user_id)
    return jsonify({"success": True}), 200


@app.route("/integrations/instagram/data-deletion", methods=["GET", "POST"])
def instagram_data_deletion():
    """Process Meta deletion requests and return a public status receipt."""
    if request.method == "GET":
        return render_template(
            "instagram_data_deletion.html", receipt=None,
            legal_contact=LEGAL_CONTACT)
    signed_request = _meta_callback_signed_request()
    try:
        payload = _meta_signed_request_payload(signed_request)
        instagram_user_id = str(payload.get("user_id") or "").strip()
        if not instagram_user_id:
            raise ValueError("Invalid signed request.")
    except RuntimeError:
        return jsonify({"ok": False, "error": "callback_not_configured"}), 503
    except ValueError:
        return jsonify({"ok": False, "error": "invalid_signed_request"}), 403

    receipt = db.delete_marketing_account_data(
        "instagram", instagram_user_id,
        request_key=hashlib.sha256(signed_request.encode("utf-8")).hexdigest(),
        confirmation_code=secrets.token_hex(16))
    if not receipt:
        return jsonify({"ok": False, "error": "deletion_failed"}), 500
    confirmation_code = receipt["confirmation_code"]
    return jsonify({
        "url": _abs_url(
            "instagram_data_deletion_status",
            confirmation_code=confirmation_code),
        "confirmation_code": confirmation_code,
    }), 200


@app.route(
    "/integrations/instagram/data-deletion/status/<confirmation_code>")
def instagram_data_deletion_status(confirmation_code):
    receipt = db.get_marketing_data_deletion(confirmation_code)
    if not receipt:
        abort(404)
    response = make_response(render_template(
        "instagram_data_deletion.html", receipt=dict(receipt),
        legal_contact=LEGAL_CONTACT))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/integrations/instagram/webhook", methods=["GET", "POST"])
def instagram_webhook():
    """Verify Meta's subscription and persist signed Instagram events."""
    if request.method == "GET":
        mode = request.args.get("hub.mode", "")
        provided = request.args.get("hub.verify_token", "")
        challenge = request.args.get("hub.challenge", "")
        configured = INSTAGRAM_WEBHOOK_VERIFY_TOKEN
        if (mode == "subscribe" and configured and provided
                and hmac.compare_digest(provided, configured)):
            return app.response_class(
                challenge, status=200, mimetype="text/plain")
        return app.response_class(
            "Webhook verification failed", status=403, mimetype="text/plain")

    if request.content_length and request.content_length > \
            INSTAGRAM_WEBHOOK_MAX_BYTES:
        return jsonify({"ok": False, "error": "payload_too_large"}), 413

    raw_payload = request.get_data(cache=True)
    if len(raw_payload) > INSTAGRAM_WEBHOOK_MAX_BYTES:
        return jsonify({"ok": False, "error": "payload_too_large"}), 413
    if not INSTAGRAM_APP_SECRET:
        return jsonify({"ok": False, "error": "webhook_not_configured"}), 503

    provided_signature = request.headers.get("X-Hub-Signature-256", "")
    expected_signature = "sha256=" + hmac.new(
        INSTAGRAM_APP_SECRET.encode("utf-8"), raw_payload,
        hashlib.sha256).hexdigest()
    if not (provided_signature and hmac.compare_digest(
            provided_signature, expected_signature)):
        return jsonify({"ok": False, "error": "invalid_signature"}), 403

    try:
        payload = json.loads(raw_payload)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid_json"}), 400
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid_payload"}), 400

    account_external_id = ""
    entries = payload.get("entry")
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        account_external_id = str(entries[0].get("id") or "")
    event_key = hashlib.sha256(raw_payload).hexdigest()
    inserted = db.record_marketing_webhook_event(
        "instagram", event_key, raw_payload.decode("utf-8", errors="replace"),
        account_external_id=account_external_id)
    if not inserted:
        return jsonify({"ok": True, "duplicate": True}), 200
    imported = 0
    try:
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            account_id = str(entry.get("id") or "")
            for event in entry.get("messaging") or []:
                if not isinstance(event, dict):
                    continue
                message = event.get("message") or {}
                if not isinstance(message, dict) or message.get("is_echo"):
                    continue
                sender_id = str((event.get("sender") or {}).get("id") or "")
                message_id = str(message.get("mid") or message.get("id") or "")
                text = str(message.get("text") or "").strip()
                if not sender_id or sender_id == account_id or not text:
                    continue
                try:
                    stamp = dt.datetime.fromtimestamp(
                        int(event.get("timestamp") or 0) / 1000)
                    created_at = stamp.strftime("%Y-%m-%d %H:%M")
                except (TypeError, ValueError, OSError):
                    created_at = db.now()
                result = db.ingest_instagram_message(
                    account_id, sender_id, message_id, text, created_at)
                if result and result["inserted"]:
                    imported += 1
        db.finish_marketing_webhook_event(
            "instagram", event_key, "processed" if imported else "ignored")
    except Exception as exc:
        app.logger.exception("Instagram webhook processing failed")
        db.finish_marketing_webhook_event(
            "instagram", event_key, "error", str(exc))
        # Meta should not retry a valid signed event forever because of an
        # internal processing problem; the stored event remains auditable.
        return jsonify({"ok": False, "error": "processing_failed"}), 200
    return jsonify({"ok": True, "imported_messages": imported}), 200


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


def _svix_ok(secret, headers, raw):
    """Verify a Svix-signed webhook (what Resend sends). The signed content is
    "<id>.<timestamp>.<body>", the secret is base64 after "whsec_", and the
    header may carry several space-separated "v1,<sig>" values."""
    try:
        sid = headers.get("svix-id", "")
        ts = headers.get("svix-timestamp", "")
        sigs = headers.get("svix-signature", "")
        if not (sid and ts and sigs):
            return False
        if abs(time.time() - int(ts)) > 300:
            return False
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_")
                               else secret)
        want = base64.b64encode(hmac.new(
            key, f"{sid}.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()
        return any(hmac.compare_digest(want, part.split(",", 1)[1])
                   for part in sigs.split() if "," in part)
    except Exception:
        return False


def _resend_get(path):
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if not key:
        raise RuntimeError("RESEND_API_KEY not set")
    # Resend sits behind Cloudflare, which refuses urllib's default
    # User-Agent outright (error 1010), so name ourselves.
    req = urllib.request.Request("https://api.resend.com" + path,
                                 headers={"Authorization": f"Bearer {key}",
                                          "User-Agent": "BridgeMD/1.0 (+https://bridgemd.health)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _send_reply_mail(to_addr, subject, body, reply_to=None):
    """Outbound for the reply handler: same gate as every other email (go-live
    on and SMTP configured), with an optional Reply-To."""
    if not notifications_ready():
        return False
    ok, _ = mailer.send_email(to_addr, subject, body, reply_to=reply_to)
    return bool(ok)


def _inbound_forward_target():
    """Where a received email is forwarded for a person to read: the first
    operator address that is not on our own domain. Forwarding to hello@
    would loop once hello@ itself is received through Resend."""
    ours = ("bridgemd.health", "bridgemd.local")
    for addr in (os.environ.get("INBOUND_FORWARD_TO") or OWNER_NOTIFY_EMAIL).split(","):
        addr = addr.strip()
        if addr and not any(addr.lower().endswith("@" + d) for d in ours):
            return addr
    return OWNER_EMAIL


@app.route("/hooks/resend", methods=["POST"])
def resend_webhook():
    """Resend posts here for every email received on reply.bridgemd.health.
    Signature-checked with RESEND_WEBHOOK_SECRET; fails closed without it. The
    full message is fetched from Resend and handled off the request thread:
    forwarded to the operator, and if it is an out-of-office reply naming
    someone else, the application is re-sent to them (replies.py)."""
    secret = os.environ.get("RESEND_WEBHOOK_SECRET", "").strip()
    if not secret:
        abort(403)
    if not _svix_ok(secret, request.headers, request.get_data()):
        abort(403)
    event = request.get_json(silent=True) or {}
    if event.get("type") != "email.received":
        return jsonify({"ok": True, "ignored": event.get("type")}), 200
    email_id = ((event.get("data") or {}).get("email_id")
                or (event.get("data") or {}).get("id") or "")
    if not email_id:
        return jsonify({"ok": False, "error": "no email id"}), 200

    def _run():
        with app.app_context():
            try:
                msg = _resend_get(f"/emails/receiving/{email_id}")
                msg["id"] = msg.get("id") or email_id
                res = replies.handle_received(
                    msg, _send_reply_mail, _inbound_forward_target(),
                    reach=db.count_inbound_applications())
                print(f"inbound reply {email_id}: {res}", flush=True)
            except Exception:
                app.logger.exception("inbound reply handling failed for %s", email_id)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True}), 200


@app.route("/integrations/lead", methods=["POST"])
def inbound_lead_webhook():
    """Generic ad / lead-form capture (Instagram-Meta, Google, Reddit, Zapier,
    Make). An automation posts {to: <intake address>, name, email, phone,
    message, channel} and it lands in the same triaged inbox as email. One
    endpoint covers every ad channel without per-platform OAuth. Same secret +
    PHI/BAA rules as the email webhook (see matcher/COMPLIANCE.md)."""
    secret = os.environ.get("INTAKE_WEBHOOK_SECRET", "")
    if not secret:
        abort(403)
    provided = (request.headers.get("X-Intake-Token", "")
                or request.args.get("secret", ""))
    if not (provided and hmac.compare_digest(provided, secret)):
        abort(403)
    payload = request.get_json(silent=True) or {}
    if not payload:
        payload = {k: v for k, v in request.form.items()}
    # Map a lead-form message onto the field the handler threads as the body.
    if payload.get("message") and not payload.get("text"):
        payload["text"] = payload.get("message")
    payload.setdefault("channel", "meta")
    result = intake_mod.handle_inbound_email(payload)
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
        out = reminders_mod.run_all()
        # Hand a returning teammate their queue back on the day they said they'd
        # be back. active_away_for() also expires lazily on read, so this only
        # makes it punctual - correctness never depends on cron running.
        out["aways_ended"] = db.sweep_expired_aways()
        return jsonify(out)
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
# Distinct conditions held in memory. Small on a 512 MB box (each entry is a
# list of full CT.gov trial dicts); TTL + shared DB make refetches cheap.
_SEO_TRIAL_MAX = int(os.environ.get("SEO_TRIAL_CACHE_MAX", "200"))


def _fetch_recruiting(condition, geo=None, limit=12):
    raw = mt.fetch_trials(condition, max_n=40, geo=geo)
    raw = [t for t in raw
           if (t.get("overallStatus") or "RECRUITING").upper() == "RECRUITING"
           and (t.get("studyType") or "").upper() != "OBSERVATIONAL"]
    return raw[:limit]


def _seo_trials(condition, city=None, limit=12):
    """Recruiting trials for an SEO page, cached per (condition, city).

    Returns (trials, is_local). When a city is given we geo-filter to trials near
    it so each city page has UNIQUE local content. If none are local we fall back
    to the national list but return is_local=False - the city page then noindexes
    and labels these as the nearest studies, so we never publish near-duplicate
    'doorway' pages (which Google penalizes across the whole programmatic surface).
    """
    cond_key = (condition or "").strip().lower()
    if not cond_key:
        return [], True
    key = (cond_key, (city or "").strip().lower())
    now_ts = time.time()
    hit = _SEO_TRIAL_CACHE.get(key)
    if hit and (now_ts - hit[0]) < _SEO_TRIAL_TTL:
        _SEO_TRIAL_CACHE.move_to_end(key)
        return hit[1][:limit], hit[2]
    trials = []
    is_local = True
    try:
        geo = None
        coords = SEO_CITY_COORDS.get(city) if city else None
        if coords:
            geo = f"distance({coords[0]},{coords[1]},100mi)"
        trials = _fetch_recruiting(condition, geo=geo, limit=limit)
        if not trials and geo:                      # no local trials -> national
            trials = _fetch_recruiting(condition, geo=None, limit=limit)
            is_local = False                        # -> city page will noindex
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
    _SEO_TRIAL_CACHE[key] = (now_ts, trials, is_local)
    _SEO_TRIAL_CACHE.move_to_end(key)
    while len(_SEO_TRIAL_CACHE) > _SEO_TRIAL_MAX:
        _SEO_TRIAL_CACHE.popitem(last=False)
    # Persist whether this city page is indexable (has local trials) so the
    # sitemap can list only these - not the full condition x city grid, most of
    # which self-noindexes and just burns crawl budget.
    if city:
        try:
            db.record_seo_city_page(slugify(condition), slugify(city),
                                    is_local and bool(trials), len(trials))
        except Exception:
            app.logger.exception("seo city page record failed")
    return trials[:limit], is_local


_SEO_COUNT_CACHE = OrderedDict()
_SEO_COUNT_TTL = 12 * 3600     # real recruiting total, refreshed at most every 12h
_SEO_COUNT_MAX = 600


def _seo_trial_count(condition, city=None):
    """Real number of currently-recruiting trials for a condition (+ optional
    city), via CT.gov's cheap countTotal. Cached per (condition, city) so crawled
    SEO pages never trigger a live count per hit. Powers the "N trials near X"
    title/snippet that drives click-through. Returns 0 on any error."""
    cond_key = (condition or "").strip().lower()
    if not cond_key:
        return 0
    key = (cond_key, (city or "").strip().lower())
    now_ts = time.time()
    hit = _SEO_COUNT_CACHE.get(key)
    if hit and (now_ts - hit[0]) < _SEO_COUNT_TTL:
        _SEO_COUNT_CACHE.move_to_end(key)
        return hit[1]
    n = 0
    try:
        geo = None
        coords = SEO_CITY_COORDS.get(city) if city else None
        if coords:
            geo = f"distance({coords[0]},{coords[1]},100mi)"
        n = mt.count_trials(condition, geo=geo)
    except Exception:
        app.logger.exception("seo count failed for %s / %s", condition, city)
        n = 0
    _SEO_COUNT_CACHE[key] = (now_ts, n)
    _SEO_COUNT_CACHE.move_to_end(key)
    while len(_SEO_COUNT_CACHE) > _SEO_COUNT_MAX:
        _SEO_COUNT_CACHE.popitem(last=False)
    return n


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


def _condition_faqs(condition, city=None):
    """Neutral, truthful FAQ pairs for a condition (+ optional city). Powers both
    the on-page FAQ and the FAQPage JSON-LD (rich results + what ChatGPT/Perplexity
    quote). Deliberately factual: no sponsor recruitment claims and no promises
    about pay or eligibility (see compliance guardrails)."""
    c = (condition or "clinical").strip()
    cl = c.lower()
    where = f" near {city}" if city else ""
    faqs = [
        (f"Are {cl} clinical trials{where} free to join?",
         "Taking part is generally free to you, and many trials cover "
         "study-related visits, tests, and the study drug at no cost. What's "
         "covered varies by study - the study team confirms the details."),
        (f"Do you get paid for {cl} clinical trials?",
         "Some studies offer compensation for your time and travel; the amount "
         "varies by study and some offer none. Any compensation is set by the "
         "study, not by BridgeMD."),
        (f"How do I know if I qualify for a {cl} trial?",
         "Each study sets its own eligibility criteria - things like age, health "
         "history, and current medications. You share a few details and the study "
         "team reviews and confirms whether you're a fit."),
        ("Is BridgeMD the study sponsor?",
         "No. BridgeMD is a free tool that helps you find recruiting trials and "
         "apply online. Trial information comes from ClinicalTrials.gov, and the "
         "study team - not BridgeMD - decides eligibility."),
    ]
    return [{"q": q, "a": a} for q, a in faqs]


# High-demand conditions featured on the /trials hub (each must resolve to a
# real condition page). Ordered roughly by consumer search demand.
TRIALS_HUB_POPULAR = [
    "Type 2 diabetes", "Weight loss", "Obesity", "Prediabetes",
    "Fatty liver disease (NAFLD/NASH)", "High cholesterol",
    "Chronic kidney disease", "Depression", "Anxiety", "Migraine",
    "Rheumatoid arthritis", "COPD", "Asthma", "Psoriasis",
    "Breast cancer", "Alzheimer's disease",
]
# High-intent condition+city landing pages people actually search for. Only
# rendered if both the condition and city are part of the curated SEO surface.
TRIALS_HUB_CITY_PICKS = [
    ("Type 2 diabetes", "New York, NY"), ("Weight loss", "Chicago, IL"),
    ("Type 2 diabetes", "Los Angeles, CA"), ("Weight loss", "Houston, TX"),
    ("Fatty liver disease (NAFLD/NASH)", "New York, NY"),
    ("High cholesterol", "Chicago, IL"), ("Obesity", "Miami, FL"),
    ("Prediabetes", "Toronto, ON"), ("Chronic kidney disease", "Phoenix, AZ"),
    ("Depression", "Boston, MA"), ("Asthma", "Atlanta, GA"),
    ("Migraine", "Seattle, WA"),
]


@app.route("/trials")
def trials_index():
    """Browseable hub that links every condition page. Gives crawlers one
    indexable entry point into the whole programmatic surface (crawl discovery +
    internal PageRank to the leaf pages) and targets broad 'browse clinical
    trials by condition' intent. Real navigational content, not a thin doorway."""
    popular = [c for c in TRIALS_HUB_POPULAR if slugify(c) in _COND_BY_SLUG]
    city_picks = [(c, city) for (c, city) in TRIALS_HUB_CITY_PICKS
                  if slugify(c) in _COND_BY_SLUG and slugify(city) in _CITY_BY_SLUG]
    return render_template(
        "trials_index.html",
        conditions=sorted(SEO_CONDITIONS, key=str.lower),
        popular=popular, city_picks=city_picks, slugify=slugify,
        canonical_url=_abs_url("trials_index"))


@app.route("/trials/<slug>")
def condition_page(slug):
    # Curated conditions only. An unknown slug 404s (don't render a thin page for
    # an arbitrary string - that's the kind of low-value content that drags the
    # whole programmatic SEO surface down).
    condition = _COND_BY_SLUG.get(slug)
    if not condition:
        abort(404)
    try:
        db.log_search_term(condition, "condition")
    except Exception:
        pass
    trials, _ = _seo_trials(condition)
    return render_template(
        "condition.html", condition=condition, city=None, local=True,
        trials=trials, trial_count=_seo_trial_count(condition), cities=SEO_CITIES[:16],
        eu_trials=_seo_ctis_trials(condition), faqs=_condition_faqs(condition),
        conditions=_related_conditions(condition), slugify=slugify,
        canonical_url=_abs_url("condition_page", slug=slugify(condition)))


@app.route("/trials/<slug>/<city_slug>")
def condition_city_page(slug, city_slug):
    condition = _COND_BY_SLUG.get(slug)
    city = _CITY_BY_SLUG.get(city_slug)
    # Both must be curated: condition in our list, city a real metro we know.
    if not condition or not city:
        abort(404)
    trials, local = _seo_trials(condition, city=city)
    trial_count = _seo_trial_count(condition, city=city) if local else 0
    return render_template(
        "condition.html", condition=condition, city=city, local=local,
        trials=trials, trial_count=trial_count,
        cities=SEO_CITIES[:16], faqs=_condition_faqs(condition, city),
        conditions=_related_conditions(condition), slugify=slugify,
        canonical_url=_abs_url("condition_city_page",
                               slug=slugify(condition), city_slug=city_slug))


_STUDY_CACHE = OrderedDict()
_STUDY_TTL = 6 * 3600          # re-fetch a study from CT.gov at most every 6h
# Full CT.gov/CTIS study objects are the single biggest per-worker memory user
# (long descriptions + eligibility text). Keep the cap low on 512 MB.
_STUDY_MAX = int(os.environ.get("STUDY_CACHE_MAX", "250"))
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
    # Scrub sponsor efficacy/safety claims from the raw brief summary before we
    # show it on the page (indexed) - never republish an unproven claim as our own.
    summary_text = summarize.scrub_claims(summarize.tidy(trial.get("briefSummary") or ""))
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


# --------------------------------------------------------------------------- #
# Study-team UI view helpers (verdict / stage / source / time). Display-only:
# they turn the pipeline's stored data into the labels + tones the "BridgeMD
# for sites" screens render. De-identification is preserved (name shows only
# when a lead is revealed; otherwise the candidate code).
# --------------------------------------------------------------------------- #
_VERDICT_VIEW = {
    "likely_eligible": ("Likely eligible", "ok"),
    "possible": ("Possible", "warn"),
    "needs_review": ("Needs review", "warn"),
    "unlikely": ("Unlikely", "danger"),
    "error": ("Needs review", "warn"),
}
_STAGE_VIEW = {
    "submitted": ("Submitted", "info"),
    "prescreen": ("Pre-screen", "neutral"),
    "eligible": ("Eligible", "ok"),
    "screening": ("Screening", "brand"),
    "enrolled": ("Enrolled", "ok"),
    "closed": ("Closed", "neutral"),
    "withdrawn": ("Withdrawn", "neutral"),
}
# Deterministic realistic-looking source labels for seeded demo leads whose raw
# source is the generic "demo" tag - so the queue's Source column reads real.
_DEMO_SOURCES = ["Meta campaign", "Reddit r/ADHD", "Email intake", "Google",
                 "CSV import", "Web form"]
_SOURCE_LABELS = {
    "referral": "Physician referral", "invite": "Physician referral",
    "physician": "Physician referral", "emr": "EMR referral",
    "csv_import": "CSV import", "intake": "Email intake",
    "email": "Email intake", "email_intake": "Email intake", "web": "Web form",
    "instagram": "Instagram DM", "instagram_dm": "Instagram DM",
    "messenger": "Messenger", "facebook": "Facebook", "whatsapp": "WhatsApp",
    "sms": "SMS", "reddit": "Reddit", "meta": "Meta ad", "google": "Google",
}


def _initials(name):
    # Drop trailing credentials ("Elena Vargas, JD, CCRC" -> "Elena Vargas") so
    # avatars read as first+last initials, not the credential letters.
    base = (name or "").split(",")[0]
    parts = [p for p in base.replace("#", "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _verdict_view(elig):
    """Return {verdict,label,tone,score} for a lead's stored eligibility read,
    deriving a sensible verdict/score when the raw read didn't include one."""
    elig = elig or {}
    verdict = (elig.get("verdict") or "").strip()
    score = int(elig.get("score") or 0)
    met = len(elig.get("met") or [])
    not_met = len(elig.get("not_met") or [])
    unknown = len(elig.get("unknown") or [])
    if not verdict:
        if not_met:
            verdict = "unlikely" if not_met >= 2 else "possible"
        elif unknown:
            verdict = "possible"
        elif met:
            verdict = "likely_eligible"
        else:
            verdict = "needs_review"
    if not score:
        base = {"likely_eligible": 90, "possible": 64, "needs_review": 52,
                "unlikely": 26, "error": 50}.get(verdict, 55)
        score = max(5, min(99, base - 7 * not_met + (4 if met else 0) - 2 * unknown))
    label, tone = _VERDICT_VIEW.get(verdict, ("Needs review", "warn"))
    return {"verdict": verdict, "label": label, "tone": tone, "score": score}


def _stage_view(status):
    label, tone = _STAGE_VIEW.get(status, (status.title(), "neutral"))
    return {"label": label, "tone": tone}


def _source_view(lead):
    s = (lead["source"] or "").strip().lower()
    if s in _SOURCE_LABELS:
        return _SOURCE_LABELS[s]
    if s in ("demo", ""):
        return _DEMO_SOURCES[int(lead["id"] or 0) % len(_DEMO_SOURCES)]
    return s.replace("_", " ").title()


def _rel_time(ts_str):
    """Compact 'time ago' for an app timestamp ('YYYY-MM-DD HH:MM')."""
    if not ts_str:
        return ""
    try:
        t = dt.datetime.strptime(str(ts_str)[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return str(ts_str)
    secs = (dt.datetime.now() - t).total_seconds()
    if secs < 0:
        return "just now"
    if secs < 3600:
        return f"{max(1, int(secs // 60))}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    days = int(secs // 86400)
    if days < 7:
        return f"{days}d ago"
    if days < 30:
        return f"{max(1, days // 7)}w ago"
    return _short_date(t)


def _short_date(value, with_year=False, with_weekday=False):
    """Render a portable human date (Windows does not support strftime %-d)."""
    prefix = value.strftime("%A, ") if with_weekday else ""
    suffix = f", {value.year}" if with_year else ""
    return f"{prefix}{value.strftime('%b')} {value.day}{suffix}"


def _clock_time(value):
    """Render a portable 12-hour time (Windows does not support strftime %-I)."""
    hour = value.hour % 12 or 12
    return f"{hour}:{value.strftime('%M %p')}"


def _queue_item(it):
    """Flatten a decoded lead into the fields the queue/dashboard render."""
    l = it["lead"]
    v = _verdict_view(it.get("elig"))
    last = ""
    if it.get("events"):
        last = it["events"][-1]["created_at"]
    last = last or l["updated_at"] or l["created_at"]
    return {
        "id": l["id"],
        "name": (l["name"] if l["revealed"] else it["code"]),
        "initials": (_initials(l["name"]) if l["revealed"]
                     else f"#{l['id'] % 100:02d}"),
        "full_name": l["name"] or it["code"],
        "email": l["email"] if l["revealed"] else "",
        "phone": l["phone"] if l["revealed"] else "",
        "revealed": bool(l["revealed"]),
        "code": it["code"],
        "verdict": v,
        "stage": _stage_view(l["status"]),
        "status": l["status"],
        "source": _source_view(l),
        "nct": l["nct"],
        "title": l["title"],
        "unread": it.get("unread", 0),
        "last_activity": _rel_time(last),
        "last_activity_at": last or "",
        "flags": it.get("flags") or [],
        "elig": it.get("elig") or {},
    }


def _forward_draft(lead, it):
    """Pre-draft the handoff email to every public study address we have:
    clinic contacts first, then sponsor/central, then the fallback inbox."""
    nct = (lead["nct"] or "").strip()
    if not nct:
        return None
    recipients = _resolve_clinic_notify_recipients(lead)
    to_email = ", ".join(r["email"] for r in recipients if r.get("email"))
    to_name = (recipients[0].get("name") or "") if recipients else ""
    ctgov_url = (f"https://clinicaltrials.gov/study/{nct}"
                 if _NCT_RE.match(nct.upper()) else "")
    sender_name = ""
    try:
        if g.user:
            sender_name = (g.user["name"] or g.user["email"] or "").strip()
    except (KeyError, IndexError, TypeError):
        sender_name = ""
    subject, body = mailer.build_coordinator_forward(
        lead, it.get("elig"), it.get("screener"), it.get("flags"),
        to_name=to_name, sender_name=sender_name)
    return {"to": to_email, "to_name": to_name, "subject": subject,
            "body": body, "ctgov_url": ctgov_url, "found": bool(to_email),
            "clinics": recipients}


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
            db.ensure_demo_claim_volume(g.user["id"], minimum_rows=108)
        except Exception:
            app.logger.exception("demo claim volume seeding failed")
    rows = db.list_leads_for_user(g.user["id"])
    recon = db.latest_reconciliation_for_leads([r["id"] for r in rows])
    claims = db.list_team_studies(g.user["id"])
    site_cfg = redcap.config_from_profile(db.get_site_profile(g.user["id"]))
    counts = db.lead_counts_for_ncts([c["nct"] for c in claims])
    # Study switcher (top bar) + search box both drive this list.
    active_nct = _set_active_study(claims)
    q = request.args.get("q", "").strip()
    review, active, done = [], [], []
    queue = []
    for r in rows:
        if active_nct and r["nct"] != active_nct:
            continue
        item = _decode_lead(r, recon.get(r["id"]))
        qi = _queue_item(item)
        if q and not _queue_matches(qi, r, q):
            continue
        queue.append(qi)
        if r["status"] == "prescreen" and not r["decision"]:
            review.append(item)
        elif r["revealed"] and r["status"] not in db.LEAD_CLOSED:
            active.append(item)
        else:
            done.append(item)
    # Most-recent-first by default, so brand-new applications sit at the top and
    # are easy to spot. Column-header clicks can still re-sort client-side.
    queue.sort(key=lambda x: x.get("last_activity_at") or "", reverse=True)
    enrolled_n = sum(1 for q in queue if q["status"] == "enrolled")
    return render_template("leads.html", queue=queue, review=review, active=active,
                           done=done, counts=counts, enrolled_n=enrolled_n,
                           claims=claims, q=q, active_nct=active_nct,
                           # The blast composer's audience options come from the
                           # same constants the resolver validates against, so
                           # the UI can never offer a filter the server rejects.
                           pipeline=db.LEAD_PIPELINE, conv_tags=db.CONV_TAGS,
                           labels=db.LEAD_LABELS)


@app.route("/app/applicants/export.csv")
@login_required
def leads_export():
    """Download the current applicant list as CSV - the export coordinators/CROs
    live on. Scoped to the active study (top switcher) and the current search, so
    what you export matches what you see."""
    import csv
    import io
    rows = db.list_leads_for_user(g.user["id"])
    recon = db.latest_reconciliation_for_leads([r["id"] for r in rows])
    claims = db.list_team_studies(g.user["id"])
    active_nct = _set_active_study(claims)
    q = request.args.get("q", "").strip()
    out = []
    for r in rows:
        if active_nct and r["nct"] != active_nct:
            continue
        item = _decode_lead(r, recon.get(r["id"]))
        qi = _queue_item(item)
        if q and not _queue_matches(qi, r, q):
            continue
        out.append((r, qi))
    out.sort(key=lambda t: t[1].get("last_activity_at") or "", reverse=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "Email", "Phone", "Verdict", "Score", "Stage", "Source",
                "NCT", "Study", "Applied", "Last activity"])
    for r, qi in out:
        w.writerow([qi["full_name"], r["email"] or "", r["phone"] or "",
                    qi["verdict"]["label"], qi["verdict"]["score"],
                    qi["stage"]["label"], qi["source"], r["nct"] or "",
                    r["title"] or "", (r["created_at"] or "")[:16],
                    (qi.get("last_activity_at") or "")[:16]])
    fname = f"applicants-{(active_nct or 'all')}-{dt.date.today().isoformat()}.csv"
    return app.response_class(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{fname}"'})


def _is_recent_apply(created_at, hours=48):
    """True if an application timestamp is within the last `hours`. Tolerant of
    the few timestamp shapes the DB may hold; returns False if it can't parse."""
    s = (created_at or "").strip()
    if not s:
        return False
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            t = dt.datetime.strptime(s[:len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
        return (dt.datetime.utcnow() - t) <= dt.timedelta(hours=hours)
    return False


def _operator_inbox_row(r):
    """Flatten a lead row into the columns the operator needs to triage and
    forward an application, without loading the full detail view."""
    keys = set(r.keys())

    def g_(k, default=""):
        return (r[k] if k in keys else default)

    def _json(k):
        raw = g_(k, "")
        if not raw:
            return {}
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except (ValueError, TypeError):
            return {}

    elig = _json("eligibility")
    screener = _json("screener")
    readiness = _json("prescreen_readiness")
    flags = screener.get("_flags") or []
    contact = []
    if g_("email"):
        contact.append("email")
    if g_("phone"):
        contact.append("phone")
    return {
        "id": g_("id"),
        "created_at": g_("created_at") or g_("updated_at"),
        "name": g_("name"),
        "email": g_("email"),
        "phone": g_("phone"),
        "contact": ", ".join(contact) or "none",
        "age": g_("age"),
        "dob": g_("dob"),
        "sex": g_("sex"),
        "condition": g_("condition"),
        "title": g_("title"),
        "nct": g_("nct"),
        "location": g_("location"),
        "status": g_("status") or "new",
        "source": (g_("source") or "web").replace("_", " "),
        "verdict": (elig.get("verdict") or "").lower(),
        "flags": len(flags),
        "readiness": readiness.get("score") or readiness.get("label") or "",
        "records": bool(g_("records_connected", 0)),
        "referred_by": g_("referred_by"),
        "registry_opt_in": bool(g_("registry_opt_in", 0)),
    }


# Sources that represent a real, patient-initiated application the operator can
# act on: the patient applied through the site (web) or arrived via a physician
# invite link (referral). Everything else - seeded demo data (source='demo'),
# clinic-side EMR matches (source='emr', a separate demo-only flow with no
# contact to forward), and manual imports (intake/csv) - is excluded from the
# concierge inbox.
_INBOUND_SOURCES = {"web", "referral"}
_SEED_TOKEN_PREFIXES = ("demo-", "inbox-", "seeded-", "demo-inbox-")


def _is_inbound_application(r):
    """True only for real inbound applications. Allowlisting the inbound sources
    is more robust and future-proof than blocklisting each demo seeder: seeded
    demo leads, EMR internal matches, and imports all fall outside the allowlist
    automatically. Placeholder email domains and seed tokens are a safety net."""
    keys = set(r.keys())

    def g_(k):
        return (r[k] if k in keys else "") or ""

    if str(g_("source")).strip().lower() not in _INBOUND_SOURCES:
        return False
    email = str(g_("email")).strip().lower()
    if email.endswith("@example.com") or email.endswith("@bridgemd.local"):
        return False
    tok = str(g_("applicant_token")).strip().lower()
    if tok.startswith(_SEED_TOKEN_PREFIXES):
        return False
    return True


def _live_apply_key(r):
    keys = set(r.keys())
    nct = ((r["nct"] if "nct" in keys else "") or "").strip().upper()
    ts = ((r["created_at"] if "created_at" in keys else "") or "")[:16]
    return (nct, ts)


def _is_live_application(r, live_index=None):
    """A live apply is a form submit that also logged a browser apply event.
    Seeds never write that event, so this is the list you can act on."""
    if not _is_inbound_application(r):
        return False
    idx = live_index if live_index is not None else db.live_apply_index()
    return _live_apply_key(r) in idx


def _live_operator_apps():
    """Live applications for the owner inbox and internal counts."""
    live = db.live_apply_index()
    rows = [r for r in db.list_leads() if _is_live_application(r, live)]
    apps = [_operator_inbox_row(r) for r in rows]
    for a in apps:
        att = live.get(((a.get("nct") or "").upper(), (a.get("created_at") or "")[:16]), {})
        a["found_via"] = att.get("found_via") or "Direct"
        a["search_q"] = att.get("search_q") or ""
        a["is_live"] = True
        a["is_new"] = _is_recent_apply(a.get("created_at"))
    apps.sort(key=lambda a: a.get("created_at") or "", reverse=True)
    return apps


@app.route("/internal")
@owner_required
def internal_hub():
    """Owner-only internal hub, reachable directly at /internal. One entry point
    to the operator inbox (everyone who applied) and visitor analytics (searches,
    locations, funnel, applications over time). Access is limited to OWNER_EMAILS;
    any other signed-in user gets a 404 (see owner_required)."""
    apps = _live_operator_apps()
    stats = {
        "total": len(apps),
        "new": sum(1 for a in apps if a.get("is_new")),
        "with_flags": sum(1 for a in apps if a["flags"]),
        "records": sum(1 for a in apps if a["records"]),
    }
    return render_template("internal.html", stats=stats)


@app.route("/internal/registry")
@owner_required
def internal_registry():
    """Owner-only view of the consented matching pool: people who opted in to be
    matched to FUTURE studies. Deduped to one row per person, with the conditions
    they've shown interest in. This is the moat asset - and it only ever holds
    people who gave explicit, separate, revocable consent (registry_opt_in)."""
    rows = db.list_registry_leads()
    members = {}
    for r in rows:
        key = ((r["applicant_token"] or r["email"] or r["token"]) or "").lower()
        cond = (r["condition"] or "").strip()
        m = members.get(key)
        if not m:
            members[key] = {
                "name": r["name"] or "Anonymous",
                "email": r["email"] or "",
                "phone": r["phone"] or "",
                "location": r["location"] or "",
                "age": r["age"] or "",
                "sex": r["sex"] or "",
                "records": bool(r["records_connected"]),
                "consent_at": (r["registry_consent_at"] or r["created_at"] or "")[:16],
                "applicant_token": r["applicant_token"] or "",
                "conditions": {cond} if cond else set(),
                "applications": 1,
            }
        else:
            if cond:
                m["conditions"].add(cond)
            m["applications"] += 1
            if r["records_connected"]:
                m["records"] = True
    pool = []
    for m in members.values():
        m["conditions"] = sorted(m["conditions"])
        pool.append(m)
    pool.sort(key=lambda x: x["consent_at"] or "", reverse=True)
    stats = {
        "members": len(pool),
        "conditions": len({c for m in pool for c in m["conditions"]}),
        "with_records": sum(1 for m in pool if m["records"]),
    }
    return render_template("registry.html", pool=pool, stats=stats)


@app.route("/internal/registry/opt-out", methods=["POST"])
@owner_required
def internal_registry_opt_out():
    """Honour a withdrawal request: remove a person from the consented pool.
    Leaves their underlying application intact; only clears the future-matching
    opt-in (right to withdraw)."""
    tok = (request.form.get("applicant_token") or "").strip()
    email = (request.form.get("email") or "").strip()
    n = db.set_registry_opt_out(applicant_token=tok or None, email=email or None)
    flash(f"Removed {n} record(s) from the matching pool." if n
          else "Nothing to remove.", "success" if n else "error")
    return redirect(url_for("internal_registry"))


# --- Recruitment tracker: source mix + demographic quota balance ------------- #
# Coordinators are graded on hitting an enrollment target with a BALANCED sample
# (e.g. not all applicants in 18-40 and none in 40-64). This view makes target vs
# progress per age band, sex, and source visible so under-filled buckets are
# obvious. KPI: Tier-1 (found/contacted per stratum) + Tier-2 (coordinator sees
# missing buckets fast, redirects effort into the stage/segment that's behind).

_SOURCE_LABELS = {
    "web": "Website", "referral": "Physician referral", "emr": "Records match",
    "intake": "Inbox / forwarded", "import": "CSV import", "ads": "Ad campaign",
    "ctgov": "ClinicalTrials.gov", "campaign": "Campaign",
}


def _recruit_targets_key(nct):
    return f"recruit_targets:{nct or '__all__'}"


def _parse_boundaries(raw, default=(18, 30, 45, 65)):
    """Parse a comma list of age boundaries into a sorted, deduped int list.
    Boundaries define the bands (18,30,45,65 -> 18-29, 30-44, 45-64, 65+)."""
    try:
        b = sorted({int(x) for x in str(raw).replace(" ", "").split(",") if x != ""})
    except (ValueError, TypeError):
        b = []
    return b if len(b) >= 2 else list(default)


def _age_bands(boundaries):
    """(label, lo, hi) bands from boundaries; final band is open-ended (hi=None)."""
    bands = []
    for i in range(len(boundaries) - 1):
        bands.append((f"{boundaries[i]}\u2013{boundaries[i + 1] - 1}",
                      boundaries[i], boundaries[i + 1] - 1))
    bands.append((f"{boundaries[-1]}+", boundaries[-1], None))
    return bands


def _recruitment_balance_ctx(all_leads, nct):
    """Demographic-balance + source-mix view for a set of leads. Shared by the
    owner tracker (global) and the coordinator page (scoped to claimed studies),
    so the exact same math backs both - no duplicated logic, no drift."""
    studies = sorted({(l["nct"], (l["title"] or l["nct"]))
                      for l in all_leads if l["nct"]}, key=lambda x: (x[1] or "").lower())
    nct = (nct or "").strip()
    leads = [l for l in all_leads if l["nct"] == nct] if nct else all_leads

    try:
        cfg = json.loads(db.get_kv(_recruit_targets_key(nct), "") or "{}")
    except (ValueError, TypeError):
        cfg = {}
    boundaries = _parse_boundaries(cfg.get("boundaries", "18,30,45,65"))
    total_target = int(cfg.get("total_target") or 0)
    # "All studies" has no target of its own - roll up each study's target so the
    # combined view shows real progress vs goal instead of a blank "–".
    if not nct and not total_target:
        agg = 0
        for sid, _t in studies:
            try:
                agg += int((json.loads(
                    db.get_kv(_recruit_targets_key(sid), "") or "{}")
                ).get("total_target") or 0)
            except (ValueError, TypeError):
                pass
        total_target = agg
    bands = _age_bands(boundaries)
    n_bands = len(bands)

    age_counts = [0] * n_bands
    age_unknown = 0
    for l in leads:
        try:
            a = int(str(l["age"]).strip())
        except (ValueError, TypeError):
            age_unknown += 1
            continue
        idx = next((i for i, (_, lo, hi) in enumerate(bands)
                    if a >= lo and (hi is None or a <= hi)), None)
        if idx is None:
            age_unknown += 1
        else:
            age_counts[idx] += 1

    total_known = sum(age_counts)
    per_band_target = (total_target // n_bands) if total_target else 0
    even_share = (total_known / n_bands) if n_bands else 0
    age_rows = []
    for (label, _lo, _hi), c in zip(bands, age_counts):
        if per_band_target:
            pct = int(round(100 * c / per_band_target))
            status = ("over" if c >= per_band_target
                      else "behind" if c < 0.6 * per_band_target else "on")
            bar = min(pct, 100)
        else:
            pct = int(round(100 * c / total_known)) if total_known else 0
            status = ("behind" if even_share and c < 0.6 * even_share
                      else "over" if even_share and c > 1.4 * even_share else "on")
            bar = pct
        age_rows.append({"label": label, "count": c, "target": per_band_target,
                         "pct": pct, "bar": bar, "status": status})

    skew = None
    if total_known and n_bands > 1:
        top = max(age_rows, key=lambda r: r["count"])
        share = round(100 * top["count"] / total_known)
        if share >= 50:
            skew = {"label": top["label"], "share": share}

    sex = {"female": 0, "male": 0, "other": 0}
    for l in leads:
        s = (l["sex"] or "").strip().lower()
        if s in ("female", "f"):
            sex["female"] += 1
        elif s in ("male", "m"):
            sex["male"] += 1
        elif s:
            sex["other"] += 1
    sex_total = sum(sex.values())
    sex_rows = [{"label": k.capitalize(), "count": v,
                 "bar": int(round(100 * v / sex_total)) if sex_total else 0}
                for k, v in sex.items() if v or k in ("female", "male")]

    src, campaign_tracked = {}, 0
    for l in leads:
        key = ((l["source"] or "web").strip().lower()) or "web"
        src[key] = src.get(key, 0) + 1
        if l["campaign_id"]:
            campaign_tracked += 1
    src_max = max(src.values()) if src else 0
    source_rows = sorted(
        [{"label": _SOURCE_LABELS.get(k, k.title()), "count": v,
          "bar": int(round(100 * v / src_max)) if src_max else 0}
         for k, v in src.items()], key=lambda r: r["count"], reverse=True)

    stats = {"total": len(leads), "known_age": total_known,
             "sources": len(src), "campaign_tracked": campaign_tracked,
             "target": total_target}
    return {
        "studies": studies, "nct": nct, "stats": stats,
        "age_rows": age_rows, "age_unknown": age_unknown, "skew": skew,
        "sex_rows": sex_rows, "source_rows": source_rows,
        "boundaries": ",".join(str(b) for b in boundaries),
        "total_target": total_target,
    }


def _seed_demo_targets_if_demo():
    """In the demo, assume the site is fully set up: give every claimed study a
    sensible enrollment target so the workspace shows real progress instead of a
    "set this up" prompt. Idempotent; demo-only (COMPLIANCE.md)."""
    if not g.user:
        return
    if not (_demo_mode_enabled() or _is_demo_account(g.user)
            or _site_demo_enabled()):
        return
    try:
        ncts = db.user_claimed_ncts(g.user["id"])
    except Exception:
        return
    for nct in ncts:
        key = _recruit_targets_key(nct)
        try:
            if json.loads(db.get_kv(key, "") or "{}").get("total_target"):
                continue
        except (ValueError, TypeError):
            pass
        db.set_kv(key, json.dumps({"boundaries": "18,30,45,65",
                                   "total_target": 30}))


def _save_recruitment_targets(nct):
    """Persist enrollment target + age boundaries for a study (shared by owner
    and coordinator save routes). Target is per-NCT (a protocol's own goal)."""
    boundaries = _parse_boundaries(request.form.get("boundaries", "18,30,45,65"))
    try:
        total_target = max(0, int(request.form.get("total_target") or 0))
    except (ValueError, TypeError):
        total_target = 0
    db.set_kv(_recruit_targets_key(nct), json.dumps({
        "boundaries": ",".join(str(b) for b in boundaries),
        "total_target": total_target}))


@app.route("/internal/recruitment")
@owner_required
def internal_recruitment():
    """Owner-only recruitment tracker (global): applications by source and
    demographic balance (age bands + sex) against a target."""
    ctx = _recruitment_balance_ctx(db.list_leads(), request.args.get("nct"))
    return render_template(
        "internal_recruitment.html", page_endpoint="internal_recruitment",
        targets_endpoint="internal_recruitment_targets", allow_all_studies=True,
        **ctx)


@app.route("/internal/recruitment/targets", methods=["POST"])
@owner_required
def internal_recruitment_targets():
    """Save the enrollment target + age boundaries for a study (or all studies)."""
    nct = (request.form.get("nct") or "").strip()
    _save_recruitment_targets(nct)
    flash("Recruitment targets saved.", "success")
    return redirect(url_for("internal_recruitment", nct=nct or None))


@app.route("/app/balance")
@login_required
def recruitment_balance():
    """Coordinator enrollment-balance page: demographic + source mix vs target,
    SCOPED to the account's claimed studies (not global). Same math as the owner
    tracker; lets a coordinator keep the cohort balanced per protocol."""
    _seed_demo_targets_if_demo()
    claims = _site_claims()
    leads = analytics._scoped_leads(sorted(claims)) if claims else []
    ctx = _recruitment_balance_ctx(leads, request.args.get("nct"))
    return render_template(
        "internal_recruitment.html", page_endpoint="recruitment_balance",
        targets_endpoint="recruitment_balance_targets", allow_all_studies=True,
        coordinator=True, **ctx)


@app.route("/app/balance/targets", methods=["POST"])
@login_required
def recruitment_balance_targets():
    """Save enrollment target + boundaries for a claimed study (coordinator)."""
    nct = (request.form.get("nct") or "").strip()
    if nct and nct not in _site_claims():
        abort(403)
    _save_recruitment_targets(nct)
    flash("Enrollment targets saved.", "success")
    return redirect(url_for("recruitment_balance", nct=nct or None))


@app.route("/internal/inbox")
@owner_required
def operator_inbox():
    """Cross-study operator inbox: EVERY real application that lands, in one
    tabular view that updates as people apply. This is what the concierge loop
    runs on - the study-team dashboard (/app/leads) is scoped to claimed
    studies, so web applications to unclaimed public trials only show here.
    Owner-only, and only real patient-initiated applications are shown."""
    apps = _live_operator_apps()
    stats = {
        "total": len(apps),
        "with_flags": sum(1 for a in apps if a["flags"]),
        "records": sum(1 for a in apps if a["records"]),
        "eligible": sum(1 for a in apps if a["verdict"] == "eligible"),
        "pool": sum(1 for a in apps if a.get("registry_opt_in")),
    }
    return render_template("internal_inbox.html", apps=apps, stats=stats)


# Where an applicant came from -> a friendly channel label + badge tone. The
# unified inbox groups every channel (a site's forwarded mail, ad lead forms,
# ClinicalTrials.gov, physician referrals, imports) into one triage list.
_CHANNEL_META = {
    "web": ("Website", "brand"),
    "email_intake": ("Email", "info"),
    "email": ("Email", "info"),
    "intake": ("Email", "info"),
    "referral": ("Physician", "violet"),
    "csv_import": ("Import", "neutral"),
    # The Meta family, kept distinct so a coordinator sees whether someone filled
    # a paid ad form, slid into the IG DMs, or messaged the Facebook page.
    "meta": ("Meta ad", "violet"),
    "instagram": ("Instagram DM", "violet"),
    "instagram_dm": ("Instagram DM", "violet"),
    "messenger": ("Messenger", "info"),
    "facebook": ("Facebook", "info"),
    "whatsapp": ("WhatsApp", "ok"),
    "sms": ("SMS", "ok"),
    "google": ("Google Ads", "warn"),
    "reddit": ("Reddit", "warn"),
    "ctgov": ("ClinicalTrials.gov", "info"),
    "campus": ("Campus", "neutral"),
    "emr": ("EMR match", "neutral"),
    "demo": ("Demo", "neutral"),
}

def _channel_meta(source):
    key = (source or "web").strip().lower()
    return _CHANNEL_META.get(key, (key.replace("_", " ").title() or "Other",
                                    "neutral"))


# Maps a channel's display label to a brand/product icon in _icons.html, so the
# inbox shows where a lead came from at a glance (the IG/FB mark, the CT.gov tile,
# an envelope for email). Keyed on the label so it lines up with what's rendered.
_CHANNEL_ICON = {
    "Instagram DM": "instagram", "Messenger": "messenger", "Facebook": "facebook",
    "WhatsApp": "whatsapp", "Meta ad": "meta", "Google Ads": "google",
    "Google": "google", "Reddit": "reddit", "ClinicalTrials.gov": "ctgov",
    "Email intake": "mail", "Email": "mail", "SMS": "phone", "Web form": "grid",
    "Physician": "user", "Physician referral": "user", "EMR match": "activity",
    "EMR referral": "activity",
}


def _channel_icon(label):
    return _CHANNEL_ICON.get(label, "inbox")


@app.route("/legacy/inbox")
@login_required
def team_inbox():
    """Redirect legacy inbox links to the canonical team inbox."""
    return redirect(url_for("marketing_hub", nct=(request.args.get("nct") or None)))


@app.route("/app/search-index.json")
@login_required
def search_index():
    """Client-side index for the top-bar live search (the inline omnibox).
    Same visibility rules as the Applicants queue: scoped to the team's claimed
    studies, and de-identified - names/emails are only included once a lead has
    been accepted (revealed), otherwise just the coded reference is searchable."""
    rows = db.list_leads_for_user(g.user["id"])
    recon = db.latest_reconciliation_for_leads([r["id"] for r in rows])
    out = []
    for r in rows:
        item = _decode_lead(r, recon.get(r["id"]))
        qi = _queue_item(item)
        code = qi.get("code", "")
        revealed = bool(r["revealed"])
        name = (r["name"] or "") if revealed else ""
        email = (r["email"] or "") if revealed else ""
        stage = (qi.get("stage") or {}).get("label", "")
        study = r["nct"] or ""
        label = name or code
        sub = " · ".join(p for p in [email, study, stage] if p)
        kw = " ".join([label, email, r["nct"] or "", r["title"] or "",
                       stage, code]).lower()
        out.append({"label": label, "sub": sub, "kw": kw, "g": "Applicants",
                    "url": url_for("applicant_detail", lead_id=r["id"])})
    return jsonify(out)


def _set_active_study(claims):
    """Resolve the study-switcher selection, matching `_active_scope`. A valid
    `nct` query param focuses that study; anything else (incl. empty) means
    "All studies" (`""`). When the user has never chosen, demo/multi-study builds
    default to All studies so pages open on the whole book of business; a real
    single-site account focuses its first study as a workspace."""
    valid = {c["nct"] for c in claims}
    param = request.args.get("nct")
    if param is not None:
        session["active_nct"] = param if param in valid else ""
        return session["active_nct"]
    if "active_nct" in session:
        cur = session.get("active_nct") or ""
        return cur if cur in valid else ""
    if claims and (_demo_mode_enabled() or _site_demo_enabled()
                   or _is_demo_account(g.user)):
        return ""  # demo opens on All studies
    first = claims[0]["nct"] if claims else ""
    if first:
        session["active_nct"] = first
    return first


def _queue_matches(qi, row, q):
    """Free-text search over a queue row. Matches the candidate code, study, and
    title always; name/email only when the lead is revealed (de-identified)."""
    ql = q.lower()
    hay = [qi.get("code", ""), row["nct"] or "", row["title"] or "",
           qi.get("stage", {}).get("label", ""), qi.get("source", "")]
    if row["revealed"]:
        hay += [row["name"] or "", row["email"] or ""]
    return any(ql in (h or "").lower() for h in hay)


@app.route("/app/home")
@login_required
def study_home():
    """Redirect the retired study home to the canonical team inbox."""
    return redirect(url_for("marketing_hub", nct=(request.args.get("nct") or None)))


@app.route("/app/scope")
@login_required
def set_scope():
    """The one place trial scope is set (from the top switcher). nct='' (or any
    unknown value) selects 'All studies'; a valid nct scopes the whole app to it.
    Redirects back to wherever the user was so scope applies in place."""
    nct = (request.args.get("nct") or "").strip()
    try:
        valid = {s["nct"] for s in db.list_team_studies(g.user["id"])}
    except Exception:
        valid = set()
    session["active_nct"] = nct if nct in valid else ""
    nxt = request.args.get("next") or url_for("marketing_hub")
    if not (nxt.startswith("/") and not nxt.startswith("//")):
        nxt = url_for("marketing_hub")
    return redirect(nxt)


# --------------------------------------------------------------------------- #
# Calendar: the trial-aware scheduling surface. Unlike a generic calendar it
# understands PROTOCOL VISIT WINDOWS (e.g. "Day 28 +/-3"), so it can flag a
# visit whose window is closing (a protocol deviation / dropout risk you can
# still prevent) and one booked outside its window. It aggregates every visit
# across the trials a team runs onto one month grid + agenda, with one-click
# reschedule (which re-arms the patient reminder) and prep checklists that flow
# into those reminders. KPI: Tier-2 Operational Efficiency -> fewer missed
# visits (retained) and faster screening close (screened -> enrolled).
# --------------------------------------------------------------------------- #
# Distinct, token-based colours cycled per trial for the calendar (no raw hex).
_TRIAL_PALETTE = ["var(--accent)", "var(--violet)", "var(--info)", "var(--green)",
                  "var(--amber)", "var(--red)", "var(--brand-2, var(--accent))"]

_VISIT_KINDS = [
    ("screening", "Screening"),
    ("baseline", "Baseline / enrollment"),
    ("treatment", "Treatment / dosing"),
    ("followup", "Follow-up"),
    ("reconsent", "Re-consent"),
    ("phone", "Phone check-in"),
    ("close", "Close-out"),
]
_VISIT_KIND_LABELS = dict(_VISIT_KINDS)


def _cal_parse_dt(ts):
    try:
        return dt.datetime.strptime(str(ts)[:16], "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None


def _cal_fmt_hour(h):
    """'7 AM', '12 PM', '5 PM' for the week/day time-grid gutter."""
    ap = "AM" if h < 12 else "PM"
    hh = h % 12 or 12
    return f"{hh} {ap}"


def _cal_visit_min(v):
    """Minutes-since-midnight for a visit's start time (defaults to 9:00)."""
    try:
        hh, mm = (int(x) for x in (v["time"] or "09:00").split(":")[:2])
        return hh * 60 + mm
    except Exception:
        return 9 * 60


def _cal_layout(visits, day_start_min, px_per_hour):
    """Position timed visits in a day column with overlap lanes.
    Returns [{v, top, height, left, width}] (top/height px, left/width %)."""
    ev = []
    for v in visits:
        s = _cal_visit_min(v)
        e = s + max(15, int(v["duration_min"] or 30))
        ev.append({"v": v, "s": s, "e": e, "lane": 0, "lanes": 1})
    ev.sort(key=lambda x: (x["s"], x["e"]))
    # Cluster overlapping events, then pack each cluster into lanes.
    i, n = 0, len(ev)
    while i < n:
        cluster_end, k = ev[i]["e"], i + 1
        while k < n and ev[k]["s"] < cluster_end:
            cluster_end = max(cluster_end, ev[k]["e"])
            k += 1
        cluster = ev[i:k]
        lane_ends = []
        for it in cluster:
            placed = False
            for li, lend in enumerate(lane_ends):
                if it["s"] >= lend:
                    lane_ends[li] = it["e"]
                    it["lane"] = li
                    placed = True
                    break
            if not placed:
                it["lane"] = len(lane_ends)
                lane_ends.append(it["e"])
        for it in cluster:
            it["lanes"] = len(lane_ends)
        i = k
    out = []
    for it in ev:
        w = 100.0 / it["lanes"]
        out.append({
            "v": it["v"],
            "top": round((it["s"] - day_start_min) / 60.0 * px_per_hour, 1),
            "height": round(max(24.0, (it["e"] - it["s"]) / 60.0 * px_per_hour), 1),
            "left": round(it["lane"] * w, 3),
            "width": round(w, 3),
        })
    return out


def _visit_flag(v, now_dt):
    """Compute a calendar status flag for a visit row.
    Returns (code, label). Codes: done|cancelled|deviation|overdue|closing|ok."""
    status = (v["status"] or "scheduled")
    if status == "completed":
        return "done", "Completed"
    if status == "cancelled":
        return "cancelled", "Cancelled"
    if status == "missed":
        return "overdue", "Missed"
    va = _cal_parse_dt(v["visit_at"])
    we = _cal_parse_dt(v["window_end"]) if v["window_end"] else None
    if va and va < now_dt:
        return "overdue", "Past due - mark done or reschedule"
    if we and va and va > we:
        return "deviation", "Booked outside protocol window"
    if we:
        days = (we.date() - now_dt.date()).days
        if 0 <= days <= 3:
            return "closing", ("Window closes today" if days == 0
                               else f"Window closes in {days}d")
    return "ok", ""


def _cal_rel_day(va, now_dt):
    """Human relative day label, e.g. 'Today', 'Yesterday', '3 days ago', 'in 2 days'."""
    days = (va.date() - now_dt.date()).days
    if days == 0:
        return "Today"
    if days == -1:
        return "Yesterday"
    if days == 1:
        return "Tomorrow"
    if days < 0:
        return f"{-days} days ago"
    return f"in {days} days"


def _visit_view(v, now_dt):
    code, label = _visit_flag(v, now_dt)
    va = _cal_parse_dt(v["visit_at"])
    prep = [p.strip() for p in (v["prep"] or "").splitlines() if p.strip()]
    agenda = [a.strip() for a in ((v["agenda"] if "agenda" in v.keys() else "") or "").splitlines() if a.strip()]
    return {
        "id": v["id"], "lead_id": v["lead_id"],
        "agenda": agenda,
        "series_id": (v["series_id"] if "series_id" in v.keys() else "") or "",
        "recurrence": (v["recurrence"] if "recurrence" in v.keys() else "") or "",
        "lead_name": (v["lead_name"] if "lead_name" in v.keys() else "") or "Applicant",
        "nct": (v["nct"] if "nct" in v.keys() else "") or "",
        "trial_title": (v["trial_title"] if "trial_title" in v.keys() else "") or "",
        "lead_token": (v["lead_token"] if "lead_token" in v.keys() else "") or "",
        "kind": v["kind"] or "screening",
        "kind_label": _VISIT_KIND_LABELS.get(v["kind"] or "screening",
                                             (v["kind"] or "Visit").title()),
        "title": (v["title"] or "").strip(),
        "location": v["location"] or "",
        "note": v["note"] or "",
        "prep": prep,
        "visit_at": v["visit_at"] or "",
        "date": va.strftime("%Y-%m-%d") if va else "",
        "date_label": _short_date(va) if va else "",
        "rel": _cal_rel_day(va, now_dt) if va else "",
        "time": va.strftime("%H:%M") if va else "",
        "time_label": _clock_time(va) if va else "",
        "duration_min": v["duration_min"] or 30,
        "window_start": v["window_start"] or "",
        "window_end": v["window_end"] or "",
        "status": v["status"] or "scheduled",
        "flag": code, "flag_label": label,
    }


_VISIT_STATUS_TONE = {"completed": "ok", "missed": "danger",
                      "cancelled": "neutral", "scheduled": "info"}


def _lead_case_context(lead_id, current_visit_id, series_id=""):
    """Assemble a participant's case context for the visit overlay: prior visits
    (with outcome/notes), team notes anyone added, and recurring-series position.
    Read-only; used so the coordinator walks in knowing the history."""
    now = dt.datetime.now()
    history = []
    for r in db.get_visits(lead_id):
        if r["id"] == current_visit_id:
            continue
        va = _cal_parse_dt(r["visit_at"])
        st = (r["status"] or "scheduled")
        if not ((va and va < now) or st in ("completed", "missed", "cancelled")):
            continue  # only finished / past visits are "history"
        history.append({
            "date": _short_date(va, with_year=True) if va else "",
            "kind": _VISIT_KIND_LABELS.get(r["kind"] or "screening",
                                           (r["kind"] or "Visit").title()),
            "status": st, "tone": _VISIT_STATUS_TONE.get(st, "neutral"),
            "note": (r["note"] or "").strip(),
        })
    history = history[-6:][::-1]  # most-recent first
    notes = [{"body": n["body"], "author": n["author"] or "Team",
              "when": (n["created_at"] or "")[:10]}
             for n in db.list_notes(lead_id)[:6]]
    series = None
    if series_id:
        sv = db.get_series_visits(series_id)
        idx = next((k for k, r in enumerate(sv, start=1)
                    if r["id"] == current_visit_id), 0)
        remaining = sum(1 for r in sv
                        if (_cal_parse_dt(r["visit_at"]) or now) >= now
                        and (r["status"] or "scheduled") not in ("completed", "cancelled"))
        series = {"total": len(sv), "index": idx, "remaining": remaining}
    return {"history": history, "notes": notes, "series": series}


def _build_visit_card(v, now):
    """Full data for a visit-detail overlay (shared by the Today timeline and the
    Schedule rail so a click anywhere opens the SAME in-place card - no page
    bounce). `v` is a _visit_view dict."""
    vd = _cal_parse_dt(v["visit_at"]) or now
    dur = max(20, int(v["duration_min"] or 30))
    end = vd + dt.timedelta(minutes=dur)
    ws, we = _cal_parse_dt(v["window_start"]), _cal_parse_dt(v["window_end"])
    win = (f"{_short_date(ws)} – {_short_date(we)}"
           if ws and we else "")
    ctx = _lead_case_context(v["lead_id"], v["id"], v["series_id"])
    # Per-visit calendar invite link (add-to-calendar / send), like a real
    # calendar. Uses the patient's private token; empty if we can't resolve it.
    invite_url = ""
    try:
        _lead = db.get_lead(v["lead_id"])
        if _lead and _lead["token"]:
            invite_url = _abs_url("visit_ics", token=_lead["token"], visit_id=v["id"])
    except Exception:
        invite_url = ""
    return {
        "visit_id": v["id"], "id": v["lead_id"], "name": v["lead_name"],
        "invite_url": invite_url,
        "kind": v["kind_label"], "trial_full": v["trial_title"] or v["nct"] or "",
        "time": v["time_label"], "flag": v["flag"], "flag_label": v["flag_label"],
        "location": v["location"], "note": v["note"], "prep": v["prep"],
        "agenda": v["agenda"], "recurrence": v["recurrence"],
        "series_id": v["series_id"], "duration": dur, "status": v["status"],
        "date_long": _short_date(vd, with_weekday=True),
        "end_time": _clock_time(end),
        "date_iso": vd.strftime("%Y-%m-%d"), "time_iso": vd.strftime("%H:%M"),
        "window": win, "history": ctx["history"], "notes": ctx["notes"],
        "series": ctx["series"],
        "guests": db.list_visit_guests(v["id"]),
    }


def _visit_owned(visit_id):
    """Return (visit_row, lead_row) if the visit belongs to a trial this user's
    team runs, else (None, None). Authorization guard for calendar actions."""
    v = db.get_visit(visit_id)
    if not v:
        return None, None
    lead = db.get_lead(v["lead_id"])
    if not lead:
        return None, None
    try:
        if lead["nct"] not in db.user_claimed_ncts(g.user["id"]):
            return None, None
    except Exception:
        return None, None
    return v, lead


def _user_now():
    """Current wall-clock time in the *visitor's* timezone.

    The browser writes its IANA zone (e.g. "America/New_York") to the `tz`
    cookie, so day boundaries, "today" and the now-line are correct for every
    user no matter what timezone the server runs in. Returns a naive datetime in
    that local zone; falls back to server-local time if the cookie is missing or
    invalid."""
    if request:
        # Preferred: IANA zone name (handles DST correctly).
        try:
            tzname = request.cookies.get("tz")
            if tzname and ZoneInfo is not None:
                return dt.datetime.now(ZoneInfo(tzname)).replace(tzinfo=None)
        except Exception:
            pass
        # Fallback: raw UTC offset in minutes (east of UTC). Works even if the
        # server image has no IANA tz database, so the day never silently
        # reverts to UTC.
        try:
            off = int(request.cookies.get("tzoff"))
            if -900 <= off <= 900:
                return (dt.datetime.now(dt.timezone.utc)
                        + dt.timedelta(minutes=off)).replace(tzinfo=None)
        except (TypeError, ValueError):
            pass
    return dt.datetime.now()


@app.route("/app/calendar")
@login_required
def calendar_page():
    rows = db.list_leads_for_user(g.user["id"])
    items = [{"lead": r} for r in rows]
    _seed_demo_calendar_if_demo(items)

    now_dt = _user_now()
    today = now_dt.date()
    # Which month? ?m=YYYY-MM (default current). ?nct= scopes to one trial.
    try:
        y, m = (int(x) for x in (request.args.get("m") or "").split("-"))
        anchor = dt.date(y, m, 1)
    except Exception:
        anchor = today.replace(day=1)
    view = (request.args.get("view") or "week").lower()
    if view not in ("month", "week", "day", "agenda"):
        view = "week"
    # Scope follows the single global switcher (no per-page chip row). "" = all.
    nct_filter, _ = _active_scope()

    import calendar as _pycal
    cal = _pycal.Calendar(firstweekday=6)  # Sunday-first
    weeks = cal.monthdatescalendar(anchor.year, anchor.month)
    grid_start = weeks[0][0]
    grid_end = weeks[-1][-1] + dt.timedelta(days=1)

    # Day anchor for week/day views (?d=YYYY-MM-DD; default today). Week = Sun-Sat.
    try:
        dp = (request.args.get("d") or "").split("-")
        day_anchor = dt.date(int(dp[0]), int(dp[1]), int(dp[2]))
    except Exception:
        day_anchor = today
    week_start = day_anchor - dt.timedelta(days=(day_anchor.weekday() + 1) % 7)
    week_end = week_start + dt.timedelta(days=6)

    # Pull a wide range so every view + agenda have data.
    range_start = min(grid_start, week_start, day_anchor, today)
    range_end = max(grid_end, week_end + dt.timedelta(days=1),
                    day_anchor + dt.timedelta(days=1), today + dt.timedelta(days=45))
    raw = db.list_calendar_visits(
        g.user["id"],
        range_start.strftime("%Y-%m-%d %H:%M"),
        range_end.strftime("%Y-%m-%d 23:59"))
    visits = [_visit_view(v, now_dt) for v in raw]
    if nct_filter:
        visits = [v for v in visits if v["nct"] == nct_filter]

    by_day = {}
    for v in visits:
        by_day.setdefault(v["date"], []).append(v)

    # Month grid: list of weeks, each a list of day cells.
    grid = []
    for wk in weeks:
        cells = []
        for d in wk:
            key = d.strftime("%Y-%m-%d")
            cells.append({
                "date": key, "day": d.day,
                "in_month": d.month == anchor.month,
                "is_today": d == today,
                "visits": by_day.get(key, []),
            })
        grid.append(cells)

    # Week / Day time-grid: full 24h (12 AM -> 12 AM) like Google Calendar, so the
    # grid always scrolls the whole day rather than cropping to a dynamic window.
    tl_dates = ([week_start + dt.timedelta(days=i) for i in range(7)]
                if view == "week" else [day_anchor])
    hour_lo, hour_hi = 0, 24
    PX_PER_HOUR = 54
    day_start_min = hour_lo * 60
    cal_hours = [{"h": h, "label": _cal_fmt_hour(h)}
                 for h in range(hour_lo, hour_hi)]
    grid_px = (hour_hi - hour_lo) * PX_PER_HOUR
    tl_days = []
    for d in tl_dates:
        key = d.strftime("%Y-%m-%d")
        tl_days.append({
            "date": key, "dow": d.strftime("%a"), "day": d.day,
            "full": d.strftime("%A"), "is_today": d == today,
            "count": len(by_day.get(key, [])),
            "events": _cal_layout(by_day.get(key, []), day_start_min, PX_PER_HOUR),
        })
    now_min = now_dt.hour * 60 + now_dt.minute
    now_top = (round((now_min - day_start_min) / 60.0 * PX_PER_HOUR, 1)
               if hour_lo * 60 <= now_min <= hour_hi * 60 else None)

    # Agenda: everything from today forward, soonest first.
    agenda = sorted(
        [v for v in visits if v["date"] >= today.strftime("%Y-%m-%d")],
        key=lambda v: v["visit_at"])

    # Attention rail: overdue / deviations / closing windows across the range.
    attention = [v for v in visits
                 if v["flag"] in ("overdue", "deviation", "closing")]
    attention.sort(key=lambda v: (v["flag"] != "overdue", v["visit_at"]))

    # One stable colour per trial so an all-trials calendar stays legible.
    all_ncts = sorted({r["nct"] for r in rows if r["nct"]})
    trial_color = {n: _TRIAL_PALETTE[i % len(_TRIAL_PALETTE)]
                   for i, n in enumerate(all_ncts)}

    # Trials for the legend + the "new visit" applicant picker.
    trials = []
    seen = set()
    for r in rows:
        if r["nct"] and r["nct"] not in seen:
            seen.add(r["nct"])
            trials.append({"nct": r["nct"], "title": r["title"] or r["nct"],
                           "color": trial_color.get(r["nct"], "var(--accent)")})
    # Applicants bookable for a visit. When the page is scoped to one trial, the
    # picker shows only that trial's people; unscoped, it searches across all.
    bookable = [{"id": r["id"], "name": r["name"] or "Applicant",
                 "nct": r["nct"], "title": r["title"] or r["nct"],
                 "phone": (r["phone"] if "phone" in r.keys() else "") or "",
                 "color": trial_color.get(r["nct"], "var(--accent)")}
                for r in rows if r["revealed"] and r["status"] not in db.LEAD_CLOSED
                and (not nct_filter or r["nct"] == nct_filter)]

    prev_m = (anchor - dt.timedelta(days=1)).replace(day=1)
    next_m = (anchor + dt.timedelta(days=32)).replace(day=1)
    month_key = anchor.strftime("%Y-%m")
    day_key = day_anchor.strftime("%Y-%m-%d")

    # Prev/next + "Today" + view-switcher URLs, per current view.
    if view == "month":
        cal_prev = url_for("calendar_page", m=prev_m.strftime("%Y-%m"), view="month")
        cal_next = url_for("calendar_page", m=next_m.strftime("%Y-%m"), view="month")
        cal_today = url_for("calendar_page", view="month")
        range_label = anchor.strftime("%B %Y")
        show_today = month_key != today.strftime("%Y-%m")
    elif view == "week":
        cal_prev = url_for("calendar_page",
                           d=(week_start - dt.timedelta(days=7)).strftime("%Y-%m-%d"), view="week")
        cal_next = url_for("calendar_page",
                           d=(week_start + dt.timedelta(days=7)).strftime("%Y-%m-%d"), view="week")
        cal_today = url_for("calendar_page", view="week")
        if week_start.month == week_end.month:
            range_label = f"{_short_date(week_start)}\u2013{week_end.day}, {week_end.year}"
        else:
            range_label = f"{_short_date(week_start)} \u2013 {_short_date(week_end, with_year=True)}"
        show_today = not (week_start <= today <= week_end)
    elif view == "day":
        cal_prev = url_for("calendar_page",
                           d=(day_anchor - dt.timedelta(days=1)).strftime("%Y-%m-%d"), view="day")
        cal_next = url_for("calendar_page",
                           d=(day_anchor + dt.timedelta(days=1)).strftime("%Y-%m-%d"), view="day")
        cal_today = url_for("calendar_page", view="day")
        range_label = _short_date(day_anchor, with_weekday=True)
        show_today = day_anchor != today
    else:  # agenda
        cal_prev = url_for("calendar_page", m=prev_m.strftime("%Y-%m"), view="agenda")
        cal_next = url_for("calendar_page", m=next_m.strftime("%Y-%m"), view="agenda")
        cal_today = url_for("calendar_page", view="agenda")
        range_label = anchor.strftime("%B %Y")
        show_today = month_key != today.strftime("%Y-%m")

    switch_urls = {
        "day": url_for("calendar_page", d=day_key, view="day"),
        "week": url_for("calendar_page", d=day_key, view="week"),
        "month": url_for("calendar_page", m=month_key, view="month"),
        "agenda": url_for("calendar_page", m=month_key, view="agenda"),
    }

    # Rich overlay card per visit (same component as the dashboard), keyed by
    # visit id so any click on the grid/agenda/attention opens the same card.
    visit_cards = [_build_visit_card(v, now_dt) for v in visits]

    sync = _calendar_sync_view()

    return render_template(
        "calendar.html",
        grid=grid, agenda=agenda, attention=attention, visit_cards=visit_cards,
        view=view, nct_filter=nct_filter, trials=trials, bookable=bookable,
        visit_kinds=_VISIT_KINDS, sync=sync,
        tl_days=tl_days, cal_hours=cal_hours, grid_px=grid_px,
        px_per_hour=PX_PER_HOUR, now_top=now_top, trial_color=trial_color,
        range_label=range_label, show_today=show_today,
        cal_prev=cal_prev, cal_next=cal_next, cal_today=cal_today,
        switch_urls=switch_urls,
        month_label=anchor.strftime("%B %Y"),
        month_key=month_key, day_key=day_key,
        prev_month=prev_m.strftime("%Y-%m"),
        next_month=next_m.strftime("%Y-%m"),
        today_key=today.strftime("%Y-%m"),
        weekday_labels=["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"])


# --------------------------------------------------------------------------- #
# Calendar sync: publish every study visit as a live iCalendar (.ics) feed the
# coordinator subscribes to from Google/Outlook/Apple. One-way, read-only,
# auto-refreshing - so visits land in the calendar they already use (frictionless
# onboarding), with events DE-IDENTIFIED by default so no PHI leaves for a
# non-BAA calendar vendor. KPI: Tier-2 efficiency + fewer missed visits.
# --------------------------------------------------------------------------- #
_PROVIDER_LABELS = {"google": "Google Calendar", "outlook": "Outlook",
                    "apple": "Apple Calendar", "ics": "your calendar"}


def _seed_demo_sync_if_demo(feed):
    """In the demo, arrive already connected - never show setup as pending."""
    if not _demo_mode_enabled():
        return feed
    if not feed["provider"] and not feed["connected_at"] and not feed["last_synced_at"]:
        db.set_calendar_feed(g.user["id"], provider="google")
        db.touch_calendar_feed_synced(g.user["id"])
        feed = db.get_or_create_calendar_feed(g.user["id"])
    return feed


def _calendar_sync_view():
    """Assemble the calendar-sync panel state: the secret feed URL plus one-click
    subscribe deep-links for Google/Outlook and connection status."""
    from urllib.parse import quote
    feed = db.get_or_create_calendar_feed(g.user["id"])
    feed = _seed_demo_sync_if_demo(feed)
    feed_url = url_for("calendar_feed_ics", token=feed["token"], _external=True)
    webcal_url = re.sub(r"^https?://", "webcal://", feed_url)
    google_add = ("https://calendar.google.com/calendar/r?cid="
                  + quote(webcal_url, safe=""))
    outlook_add = ("https://outlook.live.com/calendar/0/addfromweb?url="
                   + quote(feed_url, safe="") + "&name=" + quote("BridgeMD study visits"))
    return {
        "connected": bool(feed["provider"]),
        "provider": feed["provider"],
        "provider_label": _PROVIDER_LABELS.get(feed["provider"], ""),
        "connected_at": (feed["connected_at"] or "")[:16],
        "last_synced_at": (feed["last_synced_at"] or "")[:16],
        "deidentify": bool(feed["deidentify"]),
        "feed_url": feed_url, "webcal_url": webcal_url,
        "google_add": google_add, "outlook_add": outlook_add,
    }


def _ics_escape(text):
    return (str(text or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n"))


def _ics_fold(line):
    """RFC 5545 line folding (keep lines <=75 octets; continuation starts w/ space)."""
    if len(line) <= 73:
        return line
    out, rest = [], line
    while len(rest) > 73:
        out.append(rest[:73])
        rest = " " + rest[73:]
    out.append(rest)
    return "\r\n".join(out)


def _lead_code(name, lead_id):
    """De-identified label: initials + record code (never the full name)."""
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    initials = "".join(p[0].upper() for p in parts[:2]) or "PT"
    return f"{initials} #{lead_id}"


def _build_ics(user_id, feed):
    now_dt = dt.datetime.now()
    start = (now_dt - dt.timedelta(days=45)).strftime("%Y-%m-%d %H:%M")
    end = (now_dt + dt.timedelta(days=365)).strftime("%Y-%m-%d %H:%M")
    rows = db.list_calendar_visits(user_id, start, end)
    deident = bool(feed["deidentify"])
    stamp = now_dt.strftime("%Y%m%dT%H%M%S")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//BridgeMD//Calendar Sync//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:BridgeMD study visits",
             "X-WR-TIMEZONE:America/Toronto",
             "X-PUBLISHED-TTL:PT30M", "REFRESH-INTERVAL;VALUE=DURATION:PT30M"]
    for r in rows:
        va = _cal_parse_dt(r["visit_at"])
        if not va:
            continue
        dur = max(10, int(r["duration_min"] or 30))
        end_dt = va + dt.timedelta(minutes=dur)
        kind_label = _VISIT_KIND_LABELS.get(r["kind"] or "screening",
                                            (r["kind"] or "Visit").title())
        who = (_lead_code(r["lead_name"], r["lead_id"]) if deident
               else (r["lead_name"] or "Participant"))
        summary = f"{kind_label} \u00b7 {who}"
        trial = r["trial_title"] or r["nct"] or ""
        desc = [f"Study: {trial}"] if trial else []
        agenda = [a.strip() for a in ((r["agenda"] or "")).splitlines() if a.strip()]
        if agenda:
            desc.append("Agenda: " + "; ".join(agenda))
        desc.append("Full details in BridgeMD.")
        if deident:
            desc.append("(Patient de-identified for calendar sync.)")
        status = (r["status"] or "scheduled")
        ical_status = "CANCELLED" if status == "cancelled" else "CONFIRMED"
        ev = ["BEGIN:VEVENT", f"UID:visit-{r['id']}@bridgemd",
              f"DTSTAMP:{stamp}",
              f"DTSTART:{va.strftime('%Y%m%dT%H%M%S')}",
              f"DTEND:{end_dt.strftime('%Y%m%dT%H%M%S')}",
              f"SUMMARY:{_ics_escape(summary)}",
              f"DESCRIPTION:{_ics_escape(chr(10).join(desc))}",
              f"STATUS:{ical_status}"]
        if r["location"]:
            ev.append(f"LOCATION:{_ics_escape(r['location'])}")
        ev += ["BEGIN:VALARM", "TRIGGER:-PT1H", "ACTION:DISPLAY",
               "DESCRIPTION:Study visit reminder", "END:VALARM", "END:VEVENT"]
        lines.extend(ev)
    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(ln) for ln in lines) + "\r\n"


@app.route("/cal/feed/<token>.ics")
def calendar_feed_ics(token):
    """Public, secret-by-URL iCalendar feed. The token IS the credential, so this
    is deliberately outside login (Google/Outlook fetch it server-side)."""
    feed = db.get_calendar_feed_by_token(token)
    if not feed:
        abort(404)
    ics = _build_ics(feed["user_id"], feed)
    try:
        db.touch_calendar_feed_synced(feed["user_id"])
    except Exception:
        pass
    resp = make_response(ics)
    resp.headers["Content-Type"] = "text/calendar; charset=utf-8"
    resp.headers["Content-Disposition"] = "inline; filename=bridgemd.ics"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/app/calendar/sync/connect", methods=["POST"])
@login_required
def calendar_sync_connect():
    provider = (request.form.get("provider") or "ics").strip().lower()
    if provider not in ("google", "outlook", "apple", "ics"):
        provider = "ics"
    view = _calendar_sync_view()          # feed URLs + deep-links (also seeds demo)
    db.set_calendar_feed(g.user["id"], provider=provider)
    flash(f"Connected to {_PROVIDER_LABELS[provider]}. New and changed visits "
          "sync automatically - no re-import.", "ok")
    dest = {"google": view["google_add"], "outlook": view["outlook_add"],
            "apple": view["webcal_url"]}.get(provider)
    return redirect(dest or url_for("calendar_page"))


@app.route("/app/calendar/sync/disconnect", methods=["POST"])
@login_required
def calendar_sync_disconnect():
    db.set_calendar_feed(g.user["id"], provider="")
    flash("Calendar sync turned off. Remove the BridgeMD calendar in your "
          "calendar app to clear it there.", "info")
    return redirect(url_for("calendar_page"))


@app.route("/app/calendar/sync/deidentify", methods=["POST"])
@login_required
def calendar_sync_deidentify():
    on = request.form.get("deidentify") == "1"
    db.set_calendar_feed(g.user["id"], deidentify=on)
    if on:
        flash("Synced events now show patient initials only.", "ok")
    else:
        flash("Synced events will show full patient names. Only do this if a BAA "
              "covers your calendar provider.", "warn")
    return redirect(url_for("calendar_page"))


@app.route("/app/calendar/sync/rotate", methods=["POST"])
@login_required
def calendar_sync_rotate():
    db.rotate_calendar_feed_token(g.user["id"])
    flash("New calendar link generated. Re-add it in your calendar app - the old "
          "link no longer works.", "warn")
    return redirect(url_for("calendar_page"))


@app.route("/app/calendar/book", methods=["POST"])
@login_required
def calendar_book():
    lead_id = request.form.get("lead_id", type=int)
    lead = db.get_lead(lead_id) if lead_id else None
    if not lead or lead["nct"] not in db.user_claimed_ncts(g.user["id"]):
        flash("Pick a valid applicant to book.", "error")
        return redirect(_cal_back())
    when = _cal_form_dt("date", "time")
    if not when:
        flash("Enter a valid date and time.", "error")
        return redirect(_cal_back())
    kind = request.form.get("kind") or "screening"
    location = request.form.get("location", "").strip()
    visit_id = db.add_visit(
        lead_id, when, kind=kind, location=location,
        note=request.form.get("note", "").strip(),
        window_start=_cal_form_dt("window_start", None) or "",
        window_end=_cal_form_dt("window_end", None) or "",
        duration_min=request.form.get("duration_min", type=int) or 30,
        prep=request.form.get("prep", "").strip())
    # Auto-create the calendar invite and send it to the patient - like a real
    # calendar: an .ics they can add in one tap, plus a Google Calendar sync when
    # the site has it connected. KPI: Tier-2 efficiency + retention (fewer no-shows
    # when the visit is actually on the patient's calendar with a reminder).
    _share_visit_invite(lead, visit_id, when, kind, location)
    flash("Visit booked. Calendar invite created and sent to the patient.", "ok")
    return redirect(_cal_back())


def _share_visit_invite(lead, visit_id, when, kind, location):
    """Create the per-visit ICS invite link, message it to the patient, and sync
    to the site's connected Google Calendar. Best-effort - booking still succeeds
    if messaging/sync is unavailable."""
    try:
        visit = db.get_visit(visit_id)
        invite_url = _abs_url("visit_ics", token=lead["token"], visit_id=visit_id)
        sysmsg = f"Your {kind} visit is booked for {when}"
        sysmsg += f" at {location}." if location else "."
        sysmsg += f" Add it to your calendar: {invite_url} We'll remind you beforehand."
        db.add_message(lead["id"], "system", sysmsg)
        _notify_applicant_visit(lead, when, location, invite_url=invite_url)
        _gcal_sync_async(lead, visit, invite_url)
    except Exception:
        app.logger.exception("visit invite share failed")


@app.route("/app/calendar/visit/<int:visit_id>/reschedule", methods=["POST"])
@login_required
def calendar_reschedule(visit_id):
    v, lead = _visit_owned(visit_id)
    if not v:
        abort(404)
    old_at = v["visit_at"]
    when = _cal_form_dt("date", "time")
    if not when:
        flash("Enter a valid date and time.", "error")
        return redirect(_cal_back())
    fields = {
        "visit_at": when,
        "kind": request.form.get("kind") or v["kind"],
        "location": request.form.get("location", "").strip(),
        "note": request.form.get("note", "").strip(),
        "prep": request.form.get("prep", "").strip(),
        "duration_min": request.form.get("duration_min", type=int) or (v["duration_min"] or 30),
    }
    ws = _cal_form_dt("window_start", None)
    we = _cal_form_dt("window_end", None)
    if ws is not None:
        fields["window_start"] = ws
    if we is not None:
        fields["window_end"] = we
    db.update_visit(visit_id, **fields)
    # Optionally slide the rest of a recurring series by the same delta, so the
    # whole cadence moves in one action instead of rescheduling each visit.
    series_moved = 0
    series_id = v["series_id"] if "series_id" in v.keys() else ""
    if request.form.get("apply_series") and series_id:
        new_dt0, old_dt0 = _cal_parse_dt(when), _cal_parse_dt(old_at)
        if new_dt0 and old_dt0:
            delta = int((new_dt0 - old_dt0).total_seconds())
            series_moved = db.shift_series_after(
                series_id, old_at, delta, exclude_id=visit_id)
    # Warn (don't block) if the new time is outside the protocol window.
    we_dt = _cal_parse_dt(fields.get("window_end") or v["window_end"])
    ws_dt = _cal_parse_dt(fields.get("window_start") or v["window_start"])
    new_dt = _cal_parse_dt(when)
    tail = (f" {series_moved} later visit{'s' if series_moved != 1 else ''} in the "
            "series moved to match." if series_moved else "")
    if new_dt and ((we_dt and new_dt > we_dt) or (ws_dt and new_dt < ws_dt)):
        flash("Saved - heads up: this time is outside the protocol window "
              "(possible deviation)." + tail, "warn")
    else:
        flash("Visit updated. A fresh reminder is queued for the patient." + tail, "ok")
    return redirect(_cal_back())


@app.route("/app/calendar/visit/<int:visit_id>/status", methods=["POST"])
@login_required
def calendar_visit_status(visit_id):
    v, lead = _visit_owned(visit_id)
    if not v:
        abort(404)
    status = (request.form.get("status") or "").strip()
    if status not in ("scheduled", "completed", "missed", "cancelled"):
        flash("Unknown status.", "error")
        return redirect(_cal_back())
    db.set_visit_status(visit_id, status)
    msg = {"completed": "Marked completed.", "missed": "Marked missed.",
           "cancelled": "Visit cancelled.", "scheduled": "Reopened."}[status]
    # Auto-queue the participant stipend for a completed visit, if the protocol
    # has an IRB-approved payment rule for this visit kind (and one isn't already
    # queued for this visit). Compliance: subjects only, fixed IRB amount.
    if status == "completed":
        queued = _auto_queue_visit_payment(v, lead)
        if queued:
            msg += (f" {payments_mod.format_cents(queued['amount_cents'], queued['currency'])}"
                    " stipend queued for the participant.")
        # If this completes the whole protocol schedule, queue any completion
        # lump sum too (idempotent per participant).
        done = _maybe_queue_completion_payment(lead)
        if done:
            msg += (f" {payments_mod.format_cents(done['amount_cents'], done['currency'])}"
                    " completion stipend queued.")
    flash(msg, "ok")
    return redirect(_cal_back())


@app.route("/app/calendar/visit/<int:visit_id>/recur", methods=["POST"])
@login_required
def calendar_recur(visit_id):
    """Turn a one-off visit into a recurring series (this visit becomes the
    anchor; N-1 more are generated at the chosen cadence). KPI: Tier-2 - book a
    whole schedule of visits once instead of one-at-a-time."""
    v, lead = _visit_owned(visit_id)
    if not v:
        abort(404)
    recurrence = (request.form.get("recurrence") or "weekly").strip()
    if recurrence not in ("weekly", "biweekly", "monthly"):
        recurrence = "weekly"
    try:
        count = max(2, min(24, int(request.form.get("count") or 4)))
    except (ValueError, TypeError):
        count = 4
    import uuid as _uuid
    sid = (v["series_id"] if "series_id" in v.keys() else "") or _uuid.uuid4().hex[:12]
    db.update_visit(visit_id, series_id=sid, recurrence=recurrence)
    base = _cal_parse_dt(v["visit_at"])
    step = db._RECUR_DAYS.get(recurrence, 7)
    made = 0
    if base:
        for k in range(1, count):
            when = (base + dt.timedelta(days=step * k)).strftime("%Y-%m-%d %H:%M")
            try:
                db.add_visit(
                    lead["id"], when, kind=v["kind"],
                    location=v["location"], note="",
                    duration_min=v["duration_min"],
                    title=v["title"], prep=v["prep"],
                    agenda=(v["agenda"] if "agenda" in v.keys() else ""),
                    series_id=sid, recurrence=recurrence)
                made += 1
            except Exception:
                app.logger.exception("recurring visit creation failed")
    flash(f"Recurring set created - {made} more {recurrence} "
          f"visit{'s' if made != 1 else ''} added. Reminders queued.", "ok")
    return redirect(_cal_back())


@app.route("/app/calendar/visit/<int:visit_id>/note", methods=["POST"])
@login_required
def calendar_visit_note(visit_id):
    """Add a team note to the participant from the visit overlay (context anyone
    on the team can see next time)."""
    v, lead = _visit_owned(visit_id)
    if not v:
        abort(404)
    body = (request.form.get("body") or "").strip()
    if body:
        db.add_note(lead["id"], body, author=(g.user["name"] or "You"))
        flash("Note added.", "ok")
    else:
        flash("Nothing to save.", "warn")
    return redirect(_cal_back())


@app.route("/app/calendar/visit/<int:visit_id>/agenda", methods=["POST"])
@login_required
def calendar_visit_agenda(visit_id):
    """Save the coordinator agenda ('what to go over') for a visit."""
    v, lead = _visit_owned(visit_id)
    if not v:
        abort(404)
    db.update_visit(visit_id, agenda=(request.form.get("agenda") or "").strip())
    flash("Agenda saved.", "ok")
    return redirect(_cal_back())


@app.route("/app/calendar/visit/<int:visit_id>/guest", methods=["POST"])
@login_required
def calendar_visit_guest_add(visit_id):
    """Add an extra attendee (guest) to a visit - sub-I, PI, interpreter,
    caregiver, monitor. Record-only scheduling metadata; guests are not emailed
    automatically. KPI: Tier-2 efficiency (coordinator manages who's in the room
    without leaving the visit)."""
    v, lead = _visit_owned(visit_id)
    if not v:
        abort(404)
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Add a name for the guest.", "warn")
        return redirect(_cal_back())
    db.add_visit_guest(visit_id, name, email=request.form.get("email") or "",
                       role=request.form.get("role") or "guest")
    flash(f"{name} added to the visit.", "ok")
    return redirect(_cal_back())


@app.route("/app/calendar/visit/<int:visit_id>/guest/<int:guest_id>",
           methods=["POST"])
@login_required
def calendar_visit_guest_update(visit_id, guest_id):
    """Edit a guest or switch their role inline."""
    v, lead = _visit_owned(visit_id)
    if not v:
        abort(404)
    fields = {}
    if "role" in request.form:
        fields["role"] = request.form.get("role")
    if request.form.get("name"):
        fields["name"] = request.form.get("name")
    if "email" in request.form:
        fields["email"] = request.form.get("email")
    db.update_visit_guest(guest_id, visit_id, **fields)
    flash("Guest updated.", "ok")
    return redirect(_cal_back())


@app.route("/app/calendar/visit/<int:visit_id>/guest/<int:guest_id>/remove",
           methods=["POST"])
@login_required
def calendar_visit_guest_remove(visit_id, guest_id):
    """Remove a guest from a visit."""
    v, lead = _visit_owned(visit_id)
    if not v:
        abort(404)
    db.remove_visit_guest(guest_id, visit_id)
    flash("Guest removed.", "ok")
    return redirect(_cal_back())


def _auto_queue_visit_payment(visit, lead):
    """If an active IRB-approved payment rule matches this completed visit's
    trial + kind, queue a participant payment (idempotent per visit). Returns
    the created payment row dict, or None."""
    try:
        if db.payment_for_visit(visit["id"]):
            return None
        rule = db.find_active_payment_rule(g.user["id"], lead["nct"], visit["kind"])
        if not rule or not rule["irb_approved"] or (rule["amount_cents"] or 0) <= 0:
            return None
        pid = db.create_payment(
            lead["id"], lead["nct"], rule["amount_cents"],
            kind=visit["kind"], label=rule["label"] or "Visit stipend",
            visit_id=visit["id"], rule_id=rule["id"],
            currency=rule["currency"], method=rule["method"],
            status="queued", created_by="system",
            note="Auto-queued on completed visit")
        return {"id": pid, "amount_cents": rule["amount_cents"],
                "currency": rule["currency"]}
    except Exception:
        app.logger.exception("auto payment queue failed")
        return None


def _completion_payment_exists(lead_id):
    """A completion lump sum already logged for this participant (idempotency)."""
    try:
        for p in db.list_payments(g.user["id"]):
            if p["lead_id"] == lead_id and p["kind"] == "completion" \
                    and p["status"] != "void":
                return True
    except Exception:
        pass
    return False


def _maybe_queue_completion_payment(lead, withdrawn=False):
    """Queue a lump-sum completion stipend for a participant.

    Fires when a participant has completed every protocol visit, or - if the rule
    is set to prorate - a partial amount when they withdraw early (completed /
    total protocol visits). Idempotent per participant. Requires an active,
    IRB-approved completion rule for the trial. Returns the created payment dict
    or None. Compliance: pays for participation actually completed, never a bonus
    for enrolling (COMPLIANCE.md sec 5)."""
    try:
        rule = db.find_active_completion_rule(g.user["id"], lead["nct"])
        if not rule or not rule["irb_approved"] or (rule["amount_cents"] or 0) <= 0:
            return None
        if _completion_payment_exists(lead["id"]):
            return None
        total = db.soe_visit_count(g.user["id"], lead["nct"])
        done = db.completed_visit_count(lead["id"])
        full = rule["amount_cents"] or 0
        if withdrawn:
            if not rule["prorate"] or total <= 0 or done <= 0:
                return None  # no full payout on early withdrawal unless prorating
            amount = int(round(full * min(done, total) / total))
            label = f"{rule['label']} (prorated {done}/{total} visits)"
        else:
            # Full completion: only once the whole schedule is done.
            if total <= 0 or done < total:
                return None
            amount, label = full, rule["label"]
        if amount <= 0:
            return None
        pid = db.create_payment(
            lead["id"], lead["nct"], amount, kind="completion", label=label,
            rule_id=rule["id"], currency=rule["currency"], method=rule["method"],
            status="queued", created_by="system",
            note=("Auto-queued on early withdrawal (prorated)" if withdrawn
                  else "Auto-queued on study completion"))
        return {"id": pid, "amount_cents": amount, "currency": rule["currency"]}
    except Exception:
        app.logger.exception("completion payment queue failed")
        return None


def _cal_back():
    """Redirect target after a calendar action: back to the same month/view.
    Honors a same-origin ?next= (e.g. actions fired from the dashboard overlay)."""
    nxt = (request.form.get("next") or request.args.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    m = request.form.get("m") or request.args.get("m") or ""
    view = request.form.get("view") or request.args.get("view") or ""
    nct = request.form.get("nct") or request.args.get("nct") or ""
    q = {k: val for k, val in (("m", m), ("view", view), ("nct", nct)) if val}
    return url_for("calendar_page", **q)


def _cal_form_dt(date_field, time_field):
    """Combine a date + optional time form field into a stored '%Y-%m-%d %H:%M'
    string. Returns '' when a date field is present-but-empty, None when the
    field is absent, and the combined string when valid."""
    d = request.form.get(date_field)
    if d is None:
        return None
    d = d.strip()
    if not d:
        return ""
    t = (request.form.get(time_field) or "").strip() if time_field else ""
    if not t:
        t = "09:00"
    try:
        dt.datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M")
    except ValueError:
        return ""
    return f"{d} {t}"


def _seed_demo_calendar_if_demo(items):
    """Populate a realistic, protocol-shaped visit calendar for the demo account
    so the calendar reads as a live schedule (screening + follow-ups with
    windows, one closing window, one overdue). Idempotent: only fills when the
    org has very few visits. Demo-only (see COMPLIANCE.md)."""
    if not g.user:
        return
    if not (_demo_mode_enabled() or _is_demo_account(g.user)
            or _site_demo_enabled()):
        return
    # Wait until the browser has reported its timezone (set on first paint, then a
    # one-time reload). Seeding before that would anchor "today" to the server's
    # UTC day and leave the viewer's timeline empty. On a non-UTC dev host,
    # server-local time already matches the viewer, so seed right away.
    if request and not (request.cookies.get("tz") or request.cookies.get("tzoff")):
        server_is_utc = abs(
            (dt.datetime.now() - dt.datetime.utcnow()).total_seconds()) < 60
        if server_is_utc:
            return
    # Seed relative to the VIEWER's clock so "today's" visits land on the day the
    # viewer actually sees (a UTC server would otherwise seed onto the wrong day
    # near midnight, leaving the home timeline empty).
    now_dt = _user_now()
    cands = [it["lead"] for it in items
             if it["lead"]["revealed"] and it["lead"]["status"] not in db.LEAD_CLOSED]
    if not cands:
        cands = [it["lead"] for it in items]
    if not cands:
        return

    # Always keep at least one UPCOMING visit today, so the live "Today" timeline,
    # the Next-up card and the "in X min" countdown never go blank as the demo DB
    # ages past its seeded hours. Only tops up when nothing is still ahead today.
    try:
        sod = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        eod = now_dt.replace(hour=23, minute=59, second=0, microsecond=0)
        todays = [v for v in db.list_calendar_visits(
            g.user["id"], sod.strftime("%Y-%m-%d %H:%M"),
            eod.strftime("%Y-%m-%d %H:%M"))
            if (v["status"] or "scheduled") == "scheduled"]
        # Fill TODAY with a believable spread (a few done, one imminent, a few
        # ahead) so the home "Today" timeline reads like a live clinic day rather
        # than one lonely block. Tops up only what's missing; idempotent once
        # today already has enough. These are TODAY-only (no future rows) so they
        # can't pile up over time. Relative to the viewer's clock.
        TODAY_TARGET = 6
        if len(todays) < TODAY_TARGET:
            taken = set()
            for v in todays:
                d = _cal_parse_dt(v["visit_at"])
                if d:
                    taken.add(d.hour)
            # Absolute working-day slots (9am-5pm) so the day is full regardless of
            # when the viewer opens it: earlier ones read as done, later ones as
            # upcoming. Not now-relative, so an evening viewer still sees a real
            # clinic day rather than an empty track.
            _fill = [
                (9.0, "screening", "Bring a photo ID and insurance card"),
                (10.5, "treatment", "Arrive 15 minutes early for vitals"),
                (12.0, "followup", "Bring your symptom diary"),
                (13.5, "screening", "Bring a photo ID and insurance card"),
                (15.0, "baseline", "Wear loose sleeves for a blood draw"),
                (16.5, "followup", "Bring your symptom diary"),
                (17.5, "treatment", "Arrive 15 minutes early for vitals"),
            ]
            _ag = {
                "screening": "Confirm inclusion/exclusion criteria\nReview medical & med history\nCollect baseline vitals",
                "treatment": "Administer study drug + record dose\nPre/post vitals\nLog any adverse events",
                "followup": "Review symptom diary\nAssess adverse events since last visit\nConfirm next visit",
                "baseline": "Verify signed consent on file\nBaseline labs, ECG, and vitals\nDispense study diary",
            }
            ci = 0
            for h, knd, prep in _fill:
                if len(todays) >= TODAY_TARGET:
                    break
                hh = int(h)
                mm = 30 if (h - hh) >= 0.5 else 0
                if hh in taken:
                    continue
                taken.add(hh)
                lead = cands[ci % len(cands)]
                ci += 1
                when = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
                dur = 60 if knd in ("baseline", "treatment") else 30
                db.add_visit(
                    lead["id"], when.strftime("%Y-%m-%d %H:%M"), kind=knd,
                    location=lead["site"] or "Study site", prep=prep,
                    agenda=_ag.get(knd, ""), duration_min=dur)
                todays.append({"visit_at": when.strftime("%Y-%m-%d %H:%M"),
                               "status": "scheduled"})
    except Exception:
        app.logger.exception("demo today-fill failed")

    try:
        # Big protocol-shaped seed only when few future visits exist, so the
        # calendar stays alive relative to today even after the demo DB ages.
        future = db.list_calendar_visits(
            g.user["id"],
            now_dt.strftime("%Y-%m-%d %H:%M"),
            (now_dt + dt.timedelta(days=60)).strftime("%Y-%m-%d %H:%M"))
        if sum(1 for v in future if (v["status"] or "scheduled") == "scheduled") >= 4:
            return
    except Exception:
        return

    def _at(days, hour):
        return (now_dt + dt.timedelta(days=days)).replace(
            hour=hour, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")

    # A few TODAY, placed relative to the current hour so the home "Today"
    # timeline always reads as a live day (some done, one happening, one ahead)
    # regardless of when the demo is opened. Clamped to the 7am–7pm track.
    # Spread + distinct by construction so blocks never collide, and skewed so at
    # least one sits after "now" (populates Next up) at typical work hours.
    _h_past = min(14, max(8, now_dt.hour - 2))
    _h_soon = min(16, max(_h_past + 2, now_dt.hour + 1))
    _h_late = min(18, max(_h_soon + 2, now_dt.hour + 3))
    # Coordinator-facing "what to go over" agendas per visit kind (distinct from
    # the patient-facing prep). These populate the overlay's context checklist.
    _AGENDA = {
        "screening": "Confirm inclusion/exclusion criteria\nReview medical & med history\nCollect baseline vitals + weight",
        "baseline": "Verify signed consent on file\nBaseline labs, ECG, and vitals\nDispense study diary + first kit",
        "treatment": "Administer study drug + record dose\nPre/post vitals\nLog any adverse events",
        "followup": "Review symptom diary\nAssess adverse events since last visit\nConfirm next visit + drug accountability",
        "reconsent": "Walk through the amended consent\nAnswer questions\nCollect signature + file to source",
    }
    # (days_ahead, hour, kind, window +/- days, prep, recurring?). One overdue,
    # one closing, one recurring (weekly treatment series).
    specs = [
        (0, _h_past, "screening", 0,
         "Bring a photo ID and insurance card\nAllow ~90 minutes", False),
        (0, _h_soon, "treatment", 2, "Arrive 15 minutes early for vitals", True),
        (0, _h_late, "followup", 3, "Bring your symptom diary", False),
        (-2, 10, "screening", 0,
         "Bring a photo ID and insurance card\nAllow ~90 minutes", False),
        (1, 9, "screening", 0,
         "Fast 8 hours before the visit\nBring your current medication list", False),
        (2, 14, "baseline", 3,
         "Bring completed consent packet\nWear loose sleeves for a blood draw", False),
        (3, 11, "treatment", 2, "Arrive 15 minutes early for vitals", False),
        (9, 10, "followup", 3, "Bring your symptom diary", False),
        (16, 13, "followup", 3, "Bring your symptom diary", False),
    ]
    i = 0
    for days, hour, kind, win, prep, recurring in specs:
        lead = cands[i % len(cands)]
        i += 1
        when = _at(days, hour)
        ws = we = ""
        if win:
            base = _cal_parse_dt(when)
            ws = (base - dt.timedelta(days=win)).strftime("%Y-%m-%d %H:%M")
            we = (base + dt.timedelta(days=win)).strftime("%Y-%m-%d %H:%M")
        _dur = 60 if kind in ("baseline", "treatment") else 30
        try:
            if recurring:
                # A live weekly treatment series (this + 3 more) so the overlay
                # shows recurrence + "move the rest of the series" behaviour.
                db.add_visit_series(
                    lead["id"], when, 4, recurrence="weekly", kind=kind,
                    location=lead["site"] or "Study site", prep=prep,
                    agenda=_AGENDA.get(kind, ""), duration_min=_dur)
            else:
                db.add_visit(lead["id"], when, kind=kind,
                             location=lead["site"] or "Study site",
                             window_start=ws, window_end=we, prep=prep,
                             agenda=_AGENDA.get(kind, ""), duration_min=_dur)
        except Exception:
            app.logger.exception("demo calendar seeding failed")

    # ── Case context: a couple of PAST visits (with outcomes) + team notes per
    # participant, so the overlay reads like a real chart, not a blank slate. ──
    _hist = [
        (-28, "screening", "completed",
         "Eligible - BMI 31, A1c 7.8. Consented. Labs drawn."),
        (-14, "baseline", "completed",
         "Baseline vitals normal. First kit dispensed. Tolerated well."),
        (-7, "treatment", "missed",
         "No-show; reached by phone, rebooked. Watch adherence."),
    ]
    # Team notes attributed to the demo coordinators so the case chart shows the
    # staff collaborating, not an anonymous "Coordinator".
    _notes = [
        ("Avery Kim", "Prefers morning visits; works afternoons."),
        ("Riley Patel", "Daughter (caregiver) usually attends - add to reminders."),
        ("Avery Kim", "Mild nausea reported week 1; resolved. Monitor at next dose."),
    ]
    for lead in cands[:3]:
        try:
            if db.list_notes(lead["id"]):
                continue  # already has context; keep idempotent
            for days, kind, status, note in _hist:
                vid = db.add_visit(
                    lead["id"], _at(days, 10), kind=kind,
                    location=lead["site"] or "Study site", note=note,
                    agenda=_AGENDA.get(kind, ""),
                    duration_min=60 if kind in ("baseline", "treatment") else 30)
                db.set_visit_status(vid, status)
            for author, body in _notes[:2]:
                db.add_note(lead["id"], body, author=author)
        except Exception:
            app.logger.exception("demo context seeding failed")


# --------------------------------------------------------------------------- #
# Participant payments (stipends / reimbursement). Subjects only, IRB-approved
# amounts, auto-queued from completed visits, one-click issue through a pluggable
# provider, with a 1099/W-9 tax gate and an audit trail. See COMPLIANCE.md §5.
# KPI: Tier-2 efficiency (kills the gift-card spreadsheet) + retention (prompt,
# reliable stipends keep enrolled participants from dropping out).
# --------------------------------------------------------------------------- #
def _dollars_to_cents(raw):
    try:
        return int(round(float(str(raw).replace("$", "").replace(",", "").strip()) * 100))
    except (TypeError, ValueError):
        return 0


def _payment_owned(payment_id):
    p = db.get_payment(payment_id)
    if not p:
        return None, None
    lead = db.get_lead(p["lead_id"])
    if not lead:
        return None, None
    try:
        if lead["nct"] not in db.user_claimed_ncts(g.user["id"]):
            return None, None
    except Exception:
        return None, None
    return p, lead


@app.route("/app/payments")
@login_required
def payments_page():
    _seed_demo_payments_if_demo()
    # Scope follows the single global switcher (no per-page chip row). "" = all.
    nct_filter, _ = _active_scope()
    year = dt.datetime.now().year
    thr = payments_mod.IRS_1099_THRESHOLD_CENTS
    rows = db.list_payments(g.user["id"], nct_filter or None)

    # Per-participant YTD totals (tax basis) + W-9 state, computed once per lead.
    lead_ids = {r["lead_id"] for r in rows}
    ytd = {lid: db.participant_year_total_cents(lid, year) for lid in lead_ids}
    w9 = {lid: db.get_payment_recipient(lid) for lid in lead_ids}

    payments, queued_cents, issued_month_cents = [], 0, 0
    month_prefix = dt.datetime.now().strftime("%Y-%m")
    paid_leads, w9_needed = set(), set()
    for r in rows:
        lid = r["lead_id"]
        total = ytd.get(lid, 0)
        rec = w9.get(lid, {})
        over = total >= thr
        near = (not over) and total >= int(thr * 0.8)
        needs_w9 = (over or near) and rec.get("w9_status") != "collected"
        if needs_w9:
            w9_needed.add(lid)
        if r["status"] == "queued":
            queued_cents += r["amount_cents"] or 0
        if r["status"] in ("issued", "paid"):
            paid_leads.add(lid)
            if str(r["created_at"])[:7] == month_prefix:
                issued_month_cents += r["amount_cents"] or 0
        payments.append({
            "id": r["id"], "lead_id": lid,
            "lead_name": r["lead_name"] or "Participant",
            "lead_token": r["lead_token"] or "",
            "nct": r["nct"], "trial_title": r["trial_title"] or r["nct"],
            "kind": r["kind"], "label": r["label"] or "Visit stipend",
            "amount": payments_mod.format_cents(r["amount_cents"], r["currency"]),
            "amount_cents": r["amount_cents"] or 0, "currency": r["currency"],
            "status": r["status"], "method": r["method"],
            "method_label": payments_mod.method_label(r["method"]),
            "ptype": ("travel" if r["kind"] == "travel"
                      else "completion" if r["kind"] == "completion"
                      else "stipend"),
            "provider_ref": r["provider_ref"] or "",
            "date": str(r["created_at"])[:10],
            "ytd": payments_mod.format_cents(total, r["currency"]),
            "over_1099": over, "near_1099": near,
            "w9_status": rec.get("w9_status", "not_needed"),
            "needs_w9": needs_w9,
            "undue": (r["amount_cents"] or 0) > payments_mod.UNDUE_INDUCEMENT_CENTS,
        })

    # Needs action = every queued/failed payment, plus one W-9 prompt per flagged
    # participant (not one per payment row, or a long-term participant floods it).
    needs_action, _w9_shown = [], set()
    for p in payments:
        if p["status"] in ("queued", "failed"):
            needs_action.append(p)
            if p["needs_w9"]:
                _w9_shown.add(p["lead_id"])
        elif p["needs_w9"] and p["lead_id"] not in _w9_shown:
            _w9_shown.add(p["lead_id"])
            needs_action.append(p)
    rules = [dict(x) for x in db.list_payment_rules(g.user["id"], nct_filter or None)]
    for rl in rules:
        rl["amount"] = payments_mod.format_cents(rl["amount_cents"], rl["currency"])
        rl["rule_type"] = rl.get("rule_type") or "visit"
        rl["type_label"] = payments_mod.payout_mode_label(rl["rule_type"])
        rl["method_label"] = payments_mod.method_label(rl["method"])

    team_studies = db.list_team_studies(g.user["id"])
    trials = [{"nct": s["nct"], "title": s["title"] or s["nct"]} for s in team_studies]

    # Participants for the ad-hoc reimbursement picker (open leads in the scoped
    # study/studies).
    participants = []
    for lr in db.list_leads_for_user(g.user["id"]):
        if lr["status"] in db.LEAD_CLOSED:
            continue
        if nct_filter and lr["nct"] != nct_filter:
            continue
        participants.append({"id": lr["id"],
                             "name": lr["name"] or f"Participant #{lr['id']}",
                             "nct": lr["nct"]})

    # Whether the active study has a Schedule of Events (enables one-click setup).
    soe_ready = bool(nct_filter) and db.soe_visit_count(g.user["id"], nct_filter) > 0

    return render_template(
        "payments.html",
        payments=payments, needs_action=needs_action, rules=rules,
        trials=trials, nct_filter=nct_filter, visit_kinds=_VISIT_KINDS,
        methods=payments_mod.METHODS, payout_modes=payments_mod.PAYOUT_MODES,
        phase_templates=PHASE_TEMPLATES, participants=participants,
        soe_ready=soe_ready,
        provider_label=payments_mod.provider_label(),
        provider_live=payments_mod.provider_is_live(),
        threshold=payments_mod.format_cents(thr),
        summary={
            "queued": payments_mod.format_cents(queued_cents),
            "queued_n": sum(1 for p in payments if p["status"] == "queued"),
            "issued_month": payments_mod.format_cents(issued_month_cents),
            "participants": len(paid_leads),
            "w9_needed": len(w9_needed),
        })


@app.route("/app/payments/rule", methods=["POST"])
@login_required
def payments_rule_save():
    rule_id = request.form.get("rule_id", type=int)
    nct = (request.form.get("nct") or "").strip()
    if nct and nct not in db.user_claimed_ncts(g.user["id"]):
        flash("Pick a study you run.", "error")
        return redirect(url_for("payments_page"))
    rule_type = request.form.get("rule_type") or "visit"
    if rule_type not in ("visit", "completion", "travel"):
        rule_type = "visit"
    # Completion lump sums are not tied to a specific visit kind; tag them so they
    # don't collide with per-visit auto-queue lookups.
    kind = ("completion" if rule_type == "completion"
            else "travel" if rule_type == "travel"
            else (request.form.get("kind") or "screening"))
    _default_label = {
        "completion": "Study completion stipend",
        "travel": "Travel & expense reimbursement",
    }.get(rule_type, f"{_VISIT_KIND_LABELS.get(kind, kind).title()} - time & travel")
    label = (request.form.get("label") or "").strip() or _default_label
    amount_cents = _dollars_to_cents(request.form.get("amount"))
    currency = request.form.get("currency") or "USD"
    method = request.form.get("method") or "gift_card"
    if method not in payments_mod.METHOD_LABELS:
        method = "gift_card"
    prorate = 1 if (rule_type == "completion" and request.form.get("prorate")) else 0
    irb = 1 if request.form.get("irb_approved") else 0
    irb_note = (request.form.get("irb_note") or "").strip()
    if amount_cents <= 0:
        flash("Enter an amount greater than zero.", "error")
        return redirect(url_for("payments_page"))
    if rule_id:
        rl = db.get_payment_rule(rule_id)
        if not rl or rl["user_id"] != g.user["id"]:
            abort(404)
        db.update_payment_rule(rule_id, kind=kind, label=label,
                               amount_cents=amount_cents, currency=currency,
                               method=method, irb_approved=irb, irb_note=irb_note,
                               rule_type=rule_type, prorate=prorate)
        flash("Payment rule updated.", "ok")
    else:
        db.add_payment_rule(g.user["id"], nct, kind, label, amount_cents,
                            currency=currency, method=method, irb_approved=irb,
                            irb_note=irb_note, rule_type=rule_type,
                            prorate=prorate)
        flash("Payment rule added." + ("" if irb else
              " Attest IRB approval before it can auto-issue."), "ok")
    return redirect(url_for("payments_page", nct=nct or None))


@app.route("/app/payments/rule/<int:rule_id>/delete", methods=["POST"])
@login_required
def payments_rule_delete(rule_id):
    rl = db.get_payment_rule(rule_id)
    if not rl or rl["user_id"] != g.user["id"]:
        abort(404)
    db.update_payment_rule(rule_id, active=0)
    flash("Payment rule deactivated.", "ok")
    return redirect(url_for("payments_page"))


@app.route("/app/payments/<int:payment_id>/issue", methods=["POST"])
@login_required
def payments_issue(payment_id):
    p, lead = _payment_owned(payment_id)
    if not p:
        abort(404)
    if p["status"] not in ("queued", "failed"):
        flash("This payment isn't queued.", "error")
        return redirect(url_for("payments_page"))
    year = dt.datetime.now().year
    thr = payments_mod.IRS_1099_THRESHOLD_CENTS
    prior = db.participant_year_total_cents(lead["id"], year, exclude_payment_id=payment_id)
    projected = prior + (p["amount_cents"] or 0)
    rec = db.get_payment_recipient(lead["id"])
    # Tax gate: don't disburse across the 1099 threshold without a W-9 on file.
    if projected >= thr and rec.get("w9_status") != "collected":
        db.set_w9_status(lead["id"], "requested")
        flash("Held: this payment reaches the $600 1099 threshold for the year. "
              "Collect a W-9 from the participant first, then issue.", "error")
        return redirect(url_for("payments_page"))
    res = payments_mod.issue_payment(p)
    if res.get("ok"):
        db.update_payment_status(payment_id, res["status"], actor="you",
                                 provider=res["provider"],
                                 provider_ref=res["provider_ref"])
        flash(f"Issued {payments_mod.format_cents(p['amount_cents'], p['currency'])} "
              f"to {lead['name'] or 'the participant'} "
              f"({payments_mod.provider_label()}).", "ok")
    else:
        db.update_payment_status(payment_id, "failed", actor="you",
                                 note=res.get("error", ""))
        flash("Payment failed: " + (res.get("error") or "provider error"), "error")
    return redirect(url_for("payments_page"))


@app.route("/app/payments/<int:payment_id>/void", methods=["POST"])
@login_required
def payments_void(payment_id):
    p, lead = _payment_owned(payment_id)
    if not p:
        abort(404)
    db.update_payment_status(payment_id, "void", actor="you",
                             note=request.form.get("note", ""))
    flash("Payment voided.", "ok")
    return redirect(url_for("payments_page"))


@app.route("/app/payments/recipient/<int:lead_id>/w9", methods=["POST"])
@login_required
def payments_w9(lead_id):
    lead = db.get_lead(lead_id)
    if not lead or lead["nct"] not in db.user_claimed_ncts(g.user["id"]):
        abort(404)
    status = request.form.get("status") or "collected"
    if status not in ("not_needed", "requested", "collected"):
        status = "collected"
    db.set_w9_status(lead_id, status)
    flash({"collected": "W-9 marked on file.", "requested": "W-9 requested.",
           "not_needed": "W-9 cleared."}[status], "ok")
    return redirect(url_for("payments_page"))


# Starter per-visit amounts (USD) by trial phase, so a coordinator can one-click
# a sensible baseline and then adjust to their IRB-approved schedule. These are
# prefill suggestions only - the IRB-approved amount is always the source of
# truth and must be attested before a rule can auto-issue.
PHASE_TEMPLATES = [
    {"id": "phase1", "label": "Phase 1 (healthy volunteer)",
     "amounts": {"screening": 75, "baseline": 150, "treatment": 250,
                 "followup": 100, "reconsent": 50, "phone": 25, "close": 100}},
    {"id": "phase2", "label": "Phase 2 (patient)",
     "amounts": {"screening": 75, "baseline": 100, "treatment": 125,
                 "followup": 75, "reconsent": 50, "phone": 25, "close": 75}},
    {"id": "phase3", "label": "Phase 3 (patient)",
     "amounts": {"screening": 50, "baseline": 75, "treatment": 100,
                 "followup": 60, "reconsent": 50, "phone": 20, "close": 60}},
    {"id": "device", "label": "Device / observational",
     "amounts": {"screening": 40, "baseline": 60, "treatment": 75,
                 "followup": 50, "reconsent": 40, "phone": 20, "close": 50}},
]


@app.route("/app/payments/rules/from-soe", methods=["POST"])
@login_required
def payments_rules_from_soe():
    """One-click: draft a per-visit stipend rule for every visit in the study's
    Schedule of Events. Amounts start at 0 and IRB is unattested, so the
    coordinator sets the approved amount and attests before anything auto-issues.
    KPI: Tier-2 - stand up a whole payment schedule in one click instead of one
    rule at a time (setup cycle time)."""
    nct = (request.form.get("nct") or "").strip()
    if not nct or nct not in db.user_claimed_ncts(g.user["id"]):
        flash("Pick a study you run.", "error")
        return redirect(url_for("payments_page"))
    if db.soe_visit_count(g.user["id"], nct) == 0:
        flash("Build the Schedule of Events for this study first, then generate "
              "payment rules from it.", "error")
        return redirect(url_for("payments_page", nct=nct or None))
    n = db.generate_payment_rules_from_soe(g.user["id"], nct)
    if n:
        flash(f"Added {n} draft rule{'s' if n != 1 else ''} from the Schedule of "
              "Events. Set each amount and attest IRB approval to activate.", "ok")
    else:
        flash("Every Schedule of Events visit already has a rule.", "ok")
    return redirect(url_for("payments_page", nct=nct or None))


@app.route("/app/payments/reimbursement", methods=["POST"])
@login_required
def payments_reimbursement():
    """Log an ad-hoc travel/expense reimbursement for a participant (receipt
    based). Goes through the same ledger + 1099/W-9 + issue path as a stipend, but
    is tagged 'travel' so it is clearly a reimbursement, never a pay-to-enroll
    incentive (COMPLIANCE.md sec 5)."""
    lead_id = request.form.get("lead_id", type=int)
    lead = db.get_lead(lead_id) if lead_id else None
    if not lead or lead["nct"] not in db.user_claimed_ncts(g.user["id"]):
        flash("Pick a participant in a study you run.", "error")
        return redirect(url_for("payments_page"))
    amount_cents = _dollars_to_cents(request.form.get("amount"))
    if amount_cents <= 0:
        flash("Enter a reimbursement amount greater than zero.", "error")
        return redirect(url_for("payments_page"))
    method = request.form.get("method") or "manual"
    if method not in payments_mod.METHOD_LABELS:
        method = "gift_card"
    receipt = (request.form.get("receipt") or "").strip()
    label = (request.form.get("label") or "").strip() or "Travel & expense reimbursement"
    note = "Receipt: " + receipt if receipt else "Ad-hoc reimbursement"
    db.create_payment(
        lead["id"], lead["nct"], amount_cents, kind="travel", label=label,
        currency=request.form.get("currency") or "USD", method=method,
        status="queued", created_by="you", note=note)
    flash(f"Reimbursement of {payments_mod.format_cents(amount_cents)} queued for "
          f"{lead['name'] or 'the participant'}.", "ok")
    return redirect(url_for("payments_page"))


def _seed_demo_payments_if_demo():
    """Seed IRB-approved payment rules + a realistic ledger (queued, issued, one
    participant near the 1099 threshold) so Payments reads as a live workflow.
    Idempotent; demo-only (COMPLIANCE.md)."""
    if not g.user:
        return
    if not (_demo_mode_enabled() or _is_demo_account(g.user)
            or _site_demo_enabled()):
        return
    try:
        ncts = sorted(db.user_claimed_ncts(g.user["id"]))
    except Exception:
        return
    if not ncts:
        return
    # Rules: a stipend schedule per trial (IRB-approved). Seed for EVERY claimed
    # trial (not just the first few) and only for kinds a trial is missing, so the
    # schedule is never empty when the page is scoped to a single study.
    # Show the range of payout structures + methods a real site uses: per-visit
    # stipends on varied rails, plus a completion lump sum that prorates on early
    # withdrawal. (kind, label, cents, method, rule_type, prorate)
    rule_specs = [
        ("screening", "Screening visit - time & travel", 7500, "gift_card",
         "visit", 0),
        ("baseline", "Baseline visit - time & travel", 7500, "prepaid_card",
         "visit", 0),
        ("followup", "Follow-up visit - time & travel", 5000, "ach", "visit", 0),
        ("completion", "Study completion stipend", 10000, "check",
         "completion", 1)]
    for nct in ncts:
        try:
            have = {r["kind"] for r in db.list_payment_rules(g.user["id"], nct)}
        except Exception:
            have = set()
        for kind, label, cents, method, rtype, prorate in rule_specs:
            if kind in have:
                continue
            try:
                db.add_payment_rule(g.user["id"], nct, kind, label, cents,
                                    method=method, irb_approved=1,
                                    irb_note="Amount per IRB-approved consent, sec. 12",
                                    rule_type=rtype, prorate=prorate)
            except Exception:
                app.logger.exception("demo payment rule seeding failed")
    # The ledger below is seeded once; if payments already exist, we're done.
    try:
        if db.list_payments(g.user["id"]):
            return
    except Exception:
        return
    # A realistic, live ledger: a site several months into multiple trials.
    # Mostly paid history, some issued this month, a few queued (owed now), one
    # void (corrected duplicate), and one long-term participant near the $600/yr
    # 1099 line so the W-9 flag is real - not a two-row demo.
    rows = db.list_leads_for_user(g.user["id"])
    _placeholder = ("patient profile", "participant", "")

    def _real_name(r):
        return (r["name"] or "").strip().lower() not in _placeholder

    cands = [r for r in rows if r["revealed"]
             and r["status"] not in db.LEAD_CLOSED and _real_name(r)]
    if not cands:
        cands = [r for r in rows if _real_name(r)] or rows
    if not cands:
        return

    def _pdt(days):
        return (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M")

    amt = {"screening": 7500, "baseline": 7500, "followup": 5000,
           "travel": 4200, "completion": 10000}
    lbl = {"screening": "Screening visit - time & travel",
           "baseline": "Baseline visit - time & travel",
           "followup": "Follow-up visit - time & travel",
           "travel": "Travel & expense reimbursement",
           "completion": "Study completion stipend"}
    # Method per kind mirrors the seeded rules (varied rails read as real).
    meth = {"screening": "gift_card", "baseline": "prepaid_card",
            "followup": "ach", "travel": "check", "completion": "check"}
    # (participant idx, kind, status, days_ago)
    ledger = [
        # idx 0 - long-term participant, ~6 months in, near the 1099 threshold,
        # who then completed the study (lump sum paid).
        (0, "screening", "paid", 132), (0, "baseline", "paid", 118),
        (0, "followup", "paid", 104), (0, "followup", "paid", 90),
        (0, "followup", "paid", 76), (0, "followup", "paid", 62),
        (0, "followup", "paid", 48), (0, "followup", "paid", 34),
        (0, "followup", "issued", 11), (0, "completion", "queued", 0),
        # active participants with real, varied histories
        (1, "screening", "paid", 88), (1, "baseline", "paid", 74),
        (1, "followup", "paid", 32), (1, "followup", "void", 32),
        (1, "travel", "paid", 74),
        (2, "screening", "paid", 70), (2, "followup", "issued", 8),
        (3, "screening", "paid", 45), (3, "followup", "queued", 0),
        (4, "screening", "paid", 60), (4, "baseline", "issued", 6),
        (5, "screening", "issued", 10), (5, "travel", "queued", 1),
        (6, "screening", "paid", 52), (6, "travel", "paid", 52),
        (7, "screening", "issued", 4),
        (8, "screening", "queued", 0),
        (9, "screening", "queued", 0),
        (10, "screening", "paid", 38),
        (11, "followup", "issued", 3),
        (12, "screening", "paid", 22),
    ]
    for idx, kind, status, days in ledger:
        if idx >= len(cands):
            continue
        lead = cands[idx]
        try:
            db.create_payment(
                lead["id"], lead["nct"], amt[kind], kind=kind, label=lbl[kind],
                currency="USD", method=meth.get(kind, "gift_card"), status=status,
                created_by="system", created_at=_pdt(days))
        except Exception:
            app.logger.exception("demo payment seeding failed")
    # A couple of participants have chosen how they want to be paid, so the
    # method column reads as real (informational only; no live rail wired).
    try:
        if len(cands) > 0:
            db.set_payout_method(cands[0]["id"], "check")
        if len(cands) > 1:
            db.set_payout_method(cands[1]["id"], "ach")
        if len(cands) > 3:
            db.set_payout_method(cands[3]["id"], "prepaid_card")
    except Exception:
        app.logger.exception("demo payout method seeding failed")


# --------------------------------------------------------------------------- #
# Sponsor -> site UPDATES (amendments, safety letters, doc requests, bulletins).
# The heaviest recurring site burden is a protocol amendment: one sponsor change
# forces every site to acknowledge, take it to the IRB, update the consent form,
# RE-CONSENT already-enrolled participants, and retrain staff - and any missed
# step is a finding/deviation and a retention risk. This turns that into one
# tracked checklist per update, with the re-consent step booking real visits on
# the calendar so nothing is lost between "sponsor emailed us" and "everyone
# re-signed". KPI: Tier-2 efficiency (cuts amendment cycle time) feeding
# retention (staying compliant avoids dropouts/holds mid-study).
# --------------------------------------------------------------------------- #
_UPDATE_TYPES = [
    ("amendment", "Protocol amendment"),
    ("safety", "Safety letter / IB update"),
    ("doc_request", "Document request"),
    ("bulletin", "Sponsor bulletin"),
]
_UPDATE_TYPE_LABELS = dict(_UPDATE_TYPES)

# Which checklist steps apply to each update type, in the order a site works them.
_UPDATE_STEPS = {
    "amendment": ["ack", "irb", "icf", "reconsent", "retrain"],
    "safety": ["ack", "irb", "retrain"],
    "doc_request": ["ack"],
    "bulletin": ["ack"],
}
_STEP_LABELS = {
    "ack": "Acknowledge receipt",
    "irb": "Submit to IRB / REB",
    "icf": "Update consent form (ICF)",
    "reconsent": "Re-consent participants",
    "retrain": "Retrain study staff",
}


def _step_state(u, key):
    """Return (state, detail) for a checklist step. state in
    done|partial|todo|na. detail is a short human string (dates/counts)."""
    if key == "ack":
        if u["ack_status"] == "done":
            return "done", ("Acknowledged " + str(u["ack_at"])[:10]) if u["ack_at"] else "Acknowledged"
        return "todo", ""
    if key == "irb":
        s = u["irb_status"]
        if s == "not_required":
            return "na", "Not required"
        if s == "approved":
            return "done", ("Approved " + str(u["irb_approved_at"])[:10]) if u["irb_approved_at"] else "Approved"
        if s == "submitted":
            return "partial", ("Submitted " + str(u["irb_submitted_at"])[:10]) if u["irb_submitted_at"] else "Submitted - awaiting approval"
        return "todo", ""
    if key == "icf":
        if u["icf_status"] == "not_required":
            return "na", "Not required"
        if u["icf_status"] == "done":
            return "done", (u["icf_note"] or "Consent form updated")
        return "todo", ""
    if key == "reconsent":
        s = u["reconsent_status"]
        if s == "not_required":
            return "na", "Not required"
        if s == "complete":
            return "done", "All participants re-consented"
        if s == "in_progress":
            return "partial", "In progress"
        return "todo", ""
    if key == "retrain":
        if u["retrain_status"] == "not_required":
            return "na", "Not required"
        if u["retrain_status"] == "done":
            return "done", "Staff retrained"
        return "todo", ""
    return "todo", ""


def _update_view(u, recon=None):
    """Flatten a sponsor_updates row for templates: checklist steps + progress.
    Pass `recon` = {"booked":n,"done":n,"candidates":n} to fill re-consent counts."""
    typ = u["type"] or "amendment"
    keys = _UPDATE_STEPS.get(typ, ["ack"])
    steps, done_n, total_n, next_action = [], 0, 0, ""
    for k in keys:
        state, detail = _step_state(u, k)
        if state == "na":
            continue
        total_n += 1
        if state == "done":
            done_n += 1
        elif not next_action:
            next_action = _STEP_LABELS[k]
        steps.append({"key": k, "label": _STEP_LABELS[k],
                      "state": state, "detail": detail})
    due = str(u["due_at"] or "")[:10]
    overdue = False
    if due:
        try:
            overdue = (dt.datetime.strptime(due, "%Y-%m-%d").date()
                       < dt.date.today()) and done_n < total_n
        except ValueError:
            overdue = False
    return {
        "id": u["id"], "nct": u["nct"] or "",
        "type": typ, "type_label": _UPDATE_TYPE_LABELS.get(typ, typ.title()),
        "version": u["version"] or "", "title": u["title"] or "",
        "summary": u["summary"] or "", "source": u["source"] or "",
        "received": str(u["received_at"] or "")[:10],
        "due": due, "overdue": overdue,
        "ack_status": u["ack_status"], "irb_status": u["irb_status"],
        "icf_status": u["icf_status"], "reconsent_status": u["reconsent_status"],
        "retrain_status": u["retrain_status"],
        "steps": steps, "done_n": done_n, "total_n": total_n,
        "complete": total_n > 0 and done_n >= total_n,
        "next_action": next_action or "Done",
        "recon": recon or {},
    }


def _reconsent_counts(update_id):
    visits = db.reconsent_visits_for_update(update_id)
    booked = {v["lead_id"] for v in visits}
    done = {v["lead_id"] for v in visits if (v["status"] or "") == "completed"}
    return visits, booked, done


def _update_owned(update_id):
    u = db.get_sponsor_update(update_id)
    if not u or u["user_id"] != g.user["id"]:
        return None
    return u


# Controlled-document types shown in the "Documents & versions" panel, in the
# order a site cares about them.
_DOC_LABELS = {
    "protocol": "Protocol",
    "icf": "Consent form (ICF)",
    "ib": "Investigator brochure",
    "other": "Document",
}
_DOC_ICONS = {"protocol": "file", "icf": "file", "ib": "file", "other": "file"}
_DOC_STATUS_TONE = {"current": "ok", "pending": "warn", "superseded": "neutral"}


def _doc_versions_view(user_id, nct, update_id=None):
    """Group a study's controlled-document versions by type (Protocol/ICF/IB),
    newest first, flagging the version this amendment introduces. Read-only view
    for templates."""
    if not nct:
        return []
    rows = db.list_doc_versions(user_id, nct)
    groups = {}
    for r in rows:
        groups.setdefault(r["doc_type"], []).append({
            "version": r["version"] or "-",
            "status": r["status"] or "current",
            "status_tone": _DOC_STATUS_TONE.get(r["status"] or "current", "neutral"),
            "effective": str(r["effective_at"] or "")[:10],
            "is_new": update_id is not None and r["update_id"] == update_id,
            "note": r["note"] or "",
        })
    out = []
    for t in ("protocol", "icf", "ib", "other"):
        if groups.get(t):
            current = next((v for v in groups[t] if v["status"] == "current"), None)
            pending = next((v for v in groups[t] if v["status"] == "pending"), None)
            out.append({
                "type": t, "label": _DOC_LABELS.get(t, t.title()),
                "icon": _DOC_ICONS.get(t, "file"),
                "current": current, "pending": pending,
                "versions": groups[t],
            })
    return out


_SOE_SYS = ("You are a clinical research coordinator building a study Schedule of "
            "Events (the visit schedule / schedule of assessments) from a protocol. "
            "Return STRICT JSON only, no prose.")


def _soe_template():
    """Sensible generic Phase-2 skeleton used when there's no LLM key or protocol
    text - so the feature is always usable and the coordinator just edits it."""
    return [
        {"name": "Screening", "day_offset": -14, "window_before": 7,
         "window_after": 0, "duration_min": 90,
         "procedures": "Informed consent\nEligibility review\nMedical history\n"
                       "Vitals\nLabs (CBC, chemistry)\nECG"},
        {"name": "Baseline / Day 1", "day_offset": 0, "window_before": 0,
         "window_after": 0, "duration_min": 90,
         "procedures": "Confirm eligibility\nRandomize\nDispense study drug\n"
                       "Vitals\nPRO questionnaires"},
        {"name": "Week 2", "day_offset": 14, "window_before": 3, "window_after": 3,
         "duration_min": 45,
         "procedures": "Vitals\nAdverse event review\nCon-meds review\n"
                       "Drug accountability"},
        {"name": "Week 4", "day_offset": 28, "window_before": 3, "window_after": 3,
         "duration_min": 60,
         "procedures": "Vitals\nLabs\nAE review\nPRO questionnaires\n"
                       "Dispense study drug"},
        {"name": "Week 8", "day_offset": 56, "window_before": 5, "window_after": 5,
         "duration_min": 60, "procedures": "Vitals\nLabs\nAE review\nPRO questionnaires"},
        {"name": "End of Treatment / Week 12", "day_offset": 84, "window_before": 5,
         "window_after": 5, "duration_min": 90,
         "procedures": "Vitals\nLabs\nECG\nAE review\nFinal PRO\nDrug accountability"},
        {"name": "Safety Follow-up", "day_offset": 112, "window_before": 7,
         "window_after": 7, "duration_min": 30,
         "procedures": "AE review\nCon-meds review"},
    ]


def _soe_template_for(title="", condition=""):
    """A realistic, indication-shaped schedule of events per study, so each demo
    trial's Protocol schedule reads like its real protocol - not one generic
    skeleton. Grounded in the actual designs of Fieve's active trials (antidepressant
    RCTs with MADRS/C-SSRS, a single-dose psilocybin trial with prep + dosing +
    integration, and acute-migraine trials with an e-diary run-in). Coordinators
    still edit freely. Demo-only shaping (COMPLIANCE.md)."""
    t = (str(title) + " " + str(condition)).lower()

    # Single-dose psychedelic (COMP360 psilocybin in TRD): prep -> dosing day ->
    # integration -> follow-ups. Distinct from a daily-drug RCT.
    if "psilocybin" in t or "comp360" in t or "treatment-resistant" in t:
        return [
            {"name": "Screening", "day_offset": -21, "window_before": 7,
             "window_after": 0, "duration_min": 120,
             "procedures": "Informed consent\nMINI / diagnosis\nMADRS\nC-SSRS\n"
                           "Antidepressant taper plan\nLabs (CBC, chemistry)\nECG"},
            {"name": "Preparation session", "day_offset": -1, "window_before": 3,
             "window_after": 0, "duration_min": 90,
             "procedures": "Prep session with study therapist\nMADRS\nC-SSRS\n"
                           "Confirm washout complete"},
            {"name": "Dosing day", "day_offset": 0, "window_before": 0,
             "window_after": 0, "duration_min": 420,
             "procedures": "Administer COMP360\n6-8h monitored session (2 therapists)\n"
                           "Vitals hourly\nC-SSRS post-session"},
            {"name": "Integration (Day 2)", "day_offset": 1, "window_before": 0,
             "window_after": 1, "duration_min": 90,
             "procedures": "Integration session\nMADRS\nC-SSRS\nAdverse-event review"},
            {"name": "Week 1", "day_offset": 8, "window_before": 2, "window_after": 2,
             "duration_min": 60,
             "procedures": "Integration session\nMADRS\nC-SSRS\nAE review"},
            {"name": "Week 3 (primary endpoint)", "day_offset": 21,
             "window_before": 3, "window_after": 3, "duration_min": 75,
             "procedures": "MADRS (primary endpoint)\nCGI-S\nC-SSRS\nAE review"},
            {"name": "Week 6", "day_offset": 42, "window_before": 3, "window_after": 3,
             "duration_min": 60, "procedures": "MADRS\nC-SSRS\nAE review"},
            {"name": "Week 12 follow-up", "day_offset": 84, "window_before": 5,
             "window_after": 5, "duration_min": 60,
             "procedures": "MADRS\nC-SSRS\nFinal AE review"},
        ]

    # Acute migraine (Elismetrep, Ubrogepant): screening -> e-diary run-in ->
    # treat an attack -> post-treatment diary review.
    if "migraine" in t:
        if "long-term" in t or "safety" in t:
            return [
                {"name": "Screening", "day_offset": -14, "window_before": 7,
                 "window_after": 0, "duration_min": 75,
                 "procedures": "Informed consent\nMigraine history (IHS)\n"
                               "Eligibility review\nLabs\nDispense e-diary"},
                {"name": "Baseline / Day 1", "day_offset": 0, "window_before": 0,
                 "window_after": 0, "duration_min": 60,
                 "procedures": "Confirm attack frequency\nDispense study medication "
                               "for intermittent use\nTrain on e-diary"},
                {"name": "Month 1", "day_offset": 28, "window_before": 5,
                 "window_after": 5, "duration_min": 45,
                 "procedures": "e-diary review\nAE review\nMedication accountability"},
                {"name": "Month 3", "day_offset": 84, "window_before": 7,
                 "window_after": 7, "duration_min": 60,
                 "procedures": "e-diary review\nLabs\nAE review"},
                {"name": "Month 6", "day_offset": 168, "window_before": 7,
                 "window_after": 7, "duration_min": 60,
                 "procedures": "e-diary review\nLabs\nECG\nAE review"},
                {"name": "Month 12 / End of Treatment", "day_offset": 364,
                 "window_before": 7, "window_after": 7, "duration_min": 75,
                 "procedures": "Final e-diary review\nLabs\nECG\nAE review\n"
                               "Medication accountability"},
                {"name": "Safety Follow-up", "day_offset": 392, "window_before": 7,
                 "window_after": 7, "duration_min": 30,
                 "procedures": "AE review\nCon-meds review"},
            ]
        return [
            {"name": "Screening", "day_offset": -21, "window_before": 7,
             "window_after": 0, "duration_min": 75,
             "procedures": "Informed consent\nMigraine history (IHS criteria)\n"
                           "Eligibility review\nLabs\nDispense e-diary"},
            {"name": "Baseline / run-in", "day_offset": 0, "window_before": 0,
             "window_after": 0, "duration_min": 60,
             "procedures": "Confirm ~2-8 attacks/month\nTrain on e-diary + study-med "
                           "use\nDispense study medication"},
            {"name": "Treat an attack", "day_offset": 14, "window_before": 14,
             "window_after": 14, "duration_min": 30,
             "procedures": "Dose at onset of a moderate/severe attack\n"
                           "Record pain + most-bothersome symptom at 2h and 24h"},
            {"name": "Post-treatment", "day_offset": 21, "window_before": 3,
             "window_after": 5, "duration_min": 45,
             "procedures": "e-diary review\nPain freedom at 2h\nAE review"},
            {"name": "End of Study", "day_offset": 35, "window_before": 5,
             "window_after": 5, "duration_min": 45,
             "procedures": "Final e-diary review\nAE review\nMedication accountability"},
        ]

    # Long-term open-label extension in MDD: monthly-ish visits out to ~1 year.
    if "open-label" in t or "extension" in t or "x-nova-ole" in t or " ole" in t:
        return [
            {"name": "OLE enrollment / Day 1", "day_offset": 0, "window_before": 0,
             "window_after": 0, "duration_min": 75,
             "procedures": "Open-label consent\nMADRS\nC-SSRS\n"
                           "Dispense open-label study drug"},
            {"name": "Month 1", "day_offset": 28, "window_before": 5,
             "window_after": 5, "duration_min": 45,
             "procedures": "MADRS\nC-SSRS\nAE review\nDrug accountability"},
            {"name": "Month 3", "day_offset": 84, "window_before": 7,
             "window_after": 7, "duration_min": 60,
             "procedures": "MADRS\nC-SSRS\nLabs\nAE review"},
            {"name": "Month 6", "day_offset": 168, "window_before": 7,
             "window_after": 7, "duration_min": 60,
             "procedures": "MADRS\nC-SSRS\nLabs\nECG\nAE review"},
            {"name": "Month 9", "day_offset": 252, "window_before": 7,
             "window_after": 7, "duration_min": 45,
             "procedures": "MADRS\nC-SSRS\nAE review"},
            {"name": "Month 12 / End of Treatment", "day_offset": 364,
             "window_before": 7, "window_after": 7, "duration_min": 90,
             "procedures": "MADRS\nC-SSRS\nLabs\nECG\nFinal PRO\nDrug accountability"},
            {"name": "Safety Follow-up", "day_offset": 392, "window_before": 7,
             "window_after": 7, "duration_min": 30,
             "procedures": "AE review\nC-SSRS\nCon-meds review"},
        ]

    # Default: antidepressant RCT (Azetukalner X-NOVA3, Seltorexant) - a 6-week
    # double-blind schedule with MADRS/C-SSRS at each visit.
    return [
        {"name": "Screening", "day_offset": -28, "window_before": 14,
         "window_after": 0, "duration_min": 120,
         "procedures": "Informed consent\nMINI / eligibility\nMADRS\nC-SSRS\n"
                       "Medical history\nLabs (CBC, chemistry)\nECG\n"
                       "Urine drug screen"},
        {"name": "Baseline / Randomization (Day 1)", "day_offset": 0,
         "window_before": 0, "window_after": 0, "duration_min": 90,
         "procedures": "Confirm eligibility\nMADRS\nC-SSRS\nRandomize\n"
                       "Dispense study drug\nDispense e-diary"},
        {"name": "Week 1", "day_offset": 7, "window_before": 2, "window_after": 2,
         "duration_min": 45,
         "procedures": "MADRS\nC-SSRS\nAE review\nCompliance check"},
        {"name": "Week 2", "day_offset": 14, "window_before": 2, "window_after": 2,
         "duration_min": 45,
         "procedures": "MADRS\nC-SSRS\nAE review\nDrug accountability"},
        {"name": "Week 4", "day_offset": 28, "window_before": 3, "window_after": 3,
         "duration_min": 60,
         "procedures": "MADRS\nC-SSRS\nLabs\nAE review\nDispense study drug"},
        {"name": "Week 6 / End of double-blind (primary)", "day_offset": 42,
         "window_before": 3, "window_after": 3, "duration_min": 90,
         "procedures": "MADRS (primary endpoint)\nCGI-S\nC-SSRS\nLabs\nECG\n"
                       "Drug accountability"},
        {"name": "Safety Follow-up", "day_offset": 70, "window_before": 7,
         "window_after": 7, "duration_min": 30,
         "procedures": "AE review\nC-SSRS\nCon-meds review"},
    ]


def _soe_normalize(v):
    procs = v.get("procedures")
    if isinstance(procs, list):
        procs = "\n".join(str(p).strip() for p in procs if str(p).strip())

    def _int(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return 0
    return {
        "name": str(v.get("name") or "").strip()[:80],
        "day_offset": _int(v.get("day_offset")),
        "window_before": abs(_int(v.get("window_before"))),
        "window_after": abs(_int(v.get("window_after"))),
        "duration_min": _int(v.get("duration_min")) or 30,
        "procedures": (procs or "").strip(),
    }


def _soe_from_protocol(text, title=""):
    """Draft a Schedule of Events from protocol text via the LLM. Falls back to a
    generic editable template when there's no key or no usable text."""
    text = (text or "").strip()
    if mt.LLM_API_KEY and text:
        prompt = (
            f"STUDY: {title}\n\nPROTOCOL TEXT (may be partial):\n{text[:8000]}\n\n"
            "Extract the visit schedule as JSON exactly:\n"
            '{"visits":[{"name":str,"day_offset":int,"window_before":int,'
            '"window_after":int,"duration_min":int,"procedures":[str,...]}]}\n'
            "Rules: day_offset is days relative to Day 1 (baseline = 0, screening "
            "negative). If a window isn't stated use 0. procedures are short visit "
            "activities (assessments, labs, dosing). Order visits chronologically.")
        try:
            obj = mt._extract_json(mt.llm_chat(_SOE_SYS, prompt))
            rows = [_soe_normalize(v) for v in (obj.get("visits") or [])[:40]]
            rows = [r for r in rows if r["name"]]
            if rows:
                return rows
        except Exception:
            app.logger.exception("SoE LLM generation failed")
    return _soe_template()


def _seed_demo_soe_if_demo():
    """Seed a standard schedule of events for each demo study so the Schedule
    page reads as a live workflow. Idempotent; demo-only (COMPLIANCE.md)."""
    if not g.user:
        return
    if not (_demo_mode_enabled() or _is_demo_account(g.user)
            or _site_demo_enabled()):
        return
    try:
        studies = db.list_team_studies(g.user["id"])
    except Exception:
        return
    for s in studies:
        nct = s["nct"]
        if not nct:
            continue
        try:
            if not db.soe_visit_count(g.user["id"], nct):
                tmpl = _soe_template_for(s["title"] or "", s["condition"]
                                         if "condition" in s.keys() else "")
                db.replace_soe_visits(g.user["id"], nct, tmpl)
        except Exception:
            app.logger.exception("demo SoE seeding failed")


@app.route("/app/soe")
@login_required
def soe_page():
    _seed_demo_soe_if_demo()
    nct_filter, studies = _active_scope()
    trials = [{"nct": s["nct"], "title": s["title"] or s["nct"]} for s in studies]
    # A schedule belongs to ONE study. Let this page pick a study on its own (chips)
    # so it's never blank under the global "All studies" scope: on-page pick wins,
    # then the global scope, then just default to the first trial.
    valid = {t["nct"] for t in trials}
    req_nct = (request.args.get("nct") or "").strip()
    active = (req_nct if req_nct in valid else "") or nct_filter or (
        trials[0]["nct"] if trials else "")
    active_title = next((t["title"] for t in trials if t["nct"] == active), active)
    # Only offer the on-page study picker as a fallback when the top-bar switcher is
    # on "All studies". If a specific trial is already selected there, assume it and
    # don't show a redundant legend, switching happens from the top bar.
    show_picker = (not nct_filter) and len(trials) > 1
    visits = db.list_soe_visits(g.user["id"], active) if active else []
    # Enrolled/in-progress participants we can apply the schedule to.
    apply_leads = []
    if active:
        for r in db.list_leads_for_user(g.user["id"]):
            if (r["nct"] or "") == active and (r["status"] or "") in (
                    "enrolled", "screening", "eligible", "accepted"):
                apply_leads.append(
                    {"id": r["id"], "name": r["name"] or f"Applicant #{r['id']}"})
    return render_template(
        "soe.html", trials=trials, nct=active, active_title=active_title,
        show_picker=show_picker,
        visits=visits, apply_leads=apply_leads, has_llm=bool(mt.LLM_API_KEY),
        today=dt.date.today().strftime("%Y-%m-%d"))


@app.route("/app/soe/generate", methods=["POST"])
@login_required
def soe_generate():
    nct = (request.form.get("nct") or "").strip()
    if not nct:
        flash("Pick a study first.", "error")
        return redirect(url_for("soe_page"))
    if nct not in _site_claims():
        abort(403)
    title = next((s["title"] for s in db.list_team_studies(g.user["id"])
                  if s["nct"] == nct), nct)
    rows = _soe_from_protocol(request.form.get("protocol_text") or "", title)
    db.replace_soe_visits(g.user["id"], nct, rows)
    session["active_nct"] = nct
    ai = " with AI" if (mt.LLM_API_KEY and (request.form.get("protocol_text") or "").strip()) else ""
    flash(f"Drafted a {len(rows)}-visit schedule{ai}. Review and edit below.",
          "success")
    return redirect(url_for("soe_page"))


@app.route("/app/soe/save", methods=["POST"])
@login_required
def soe_save():
    nct = (request.form.get("nct") or "").strip()
    if not nct or nct not in _site_claims():
        abort(403)
    names = request.form.getlist("name")
    days = request.form.getlist("day_offset")
    wb = request.form.getlist("window_before")
    wa = request.form.getlist("window_after")
    dur = request.form.getlist("duration_min")
    procs = request.form.getlist("procedures")

    def gi(lst, j):
        try:
            return int(lst[j])
        except (IndexError, ValueError, TypeError):
            return 0
    rows = []
    for i, nm in enumerate(names):
        if not (nm or "").strip():
            continue
        rows.append({
            "name": nm.strip(), "day_offset": gi(days, i),
            "window_before": abs(gi(wb, i)), "window_after": abs(gi(wa, i)),
            "duration_min": gi(dur, i) or 30,
            "procedures": (procs[i] if i < len(procs) else "").strip(),
        })
    db.replace_soe_visits(g.user["id"], nct, rows)
    session["active_nct"] = nct
    flash(f"Saved schedule ({len(rows)} visits).", "success")
    return redirect(url_for("soe_page"))


@app.route("/app/soe/apply", methods=["POST"])
@login_required
def soe_apply():
    nct = (request.form.get("nct") or "").strip()
    try:
        lead_id = int(request.form.get("lead_id"))
    except (TypeError, ValueError):
        flash("Pick a participant.", "error")
        return redirect(url_for("soe_page"))
    _ensure_site_access_for_lead(lead_id)
    n = db.materialize_soe_for_lead(
        lead_id, g.user["id"], nct, request.form.get("anchor") or "")
    if n:
        flash(f"Booked {n} protocol visits on the calendar.", "success")
        return redirect(url_for("calendar_page"))
    flash("No schedule to apply yet - build it first.", "warn")
    return redirect(url_for("soe_page"))


@app.route("/app/documents")
@login_required
def documents_page():
    """Regulatory binder + patient forms: the ICH-GCP essential documents a site
    routes, reviews, and signs, grouped by category with a full audit trail. The
    whole shared workspace sees the same binder, so any teammate can pick up where
    another left off. Read + approve/return; e-signatures happen in the validated
    vendor and the approval is recorded here (see COMPLIANCE.md)."""
    active_nct, _team = _active_scope()
    docs = db.list_documents(g.user["id"], nct=active_nct or None)
    counts = db.document_counts(g.user["id"])
    # name -> email so the audit trail shows the real teammate who acted.
    staff = {}
    for m in db.list_org_members(g.user["id"]):
        if m["name"]:
            staff[m["name"]] = m["email"]
    order = ["consent", "patient", "regulatory", "site"]
    groups = []
    for cat in order:
        items = [d for d in docs if d["category"] == cat]
        if not items:
            continue
        rows = []
        for d in items:
            rows.append({
                "doc": d,
                "events": db.list_document_events(d["id"]),
            })
        groups.append({
            "key": cat,
            "label": db.DOC_CATEGORY_LABELS.get(cat, cat.title()),
            "rows": rows,
        })
    return render_template(
        "documents.html", groups=groups, counts=counts, staff=staff,
        status_labels=db.DOC_STATUS_LABELS, has_any=bool(docs))


@app.route("/app/documents/<int:doc_id>/action", methods=["POST"])
@login_required
def document_action(doc_id):
    """Advance a document's status (review / approve / send back) and append an
    audit event attributed to the acting teammate. Stays on the Documents page."""
    doc = db.get_document(doc_id)
    if not doc or doc["user_id"] not in db.org_member_ids(g.user["id"]):
        abort(404)
    status = (request.form.get("status") or "").strip()
    note = (request.form.get("note") or "").strip()
    if status not in db.DOC_STATUSES:
        flash("Unknown document action.", "error")
        return redirect(url_for("documents_page"))
    role_label = db.ORG_ROLE_LABELS.get(db.member_role(g.user["id"]), "Team")
    for m in db.list_org_members(g.user["id"]):
        if m["user_id"] == g.user["id"] and m["role_label"]:
            role_label = m["role_label"]
            break
    meaning = "Approval" if status in ("approved", "signed") else ""
    db.set_document_status(
        doc_id, status, actor=(g.user["name"] or "You"),
        actor_role=role_label, meaning=meaning, note=note)
    msg = {"approved": "Approved and logged to the binder.",
           "returned": "Sent back for changes.",
           "in_review": "Marked in review."}.get(status, "Document updated.")
    flash(msg, "success")
    return redirect(url_for("documents_page"))


def _current_member_role_label():
    """The acting teammate's display title for an audit trail (falls back to the
    base role)."""
    label = db.ORG_ROLE_LABELS.get(db.member_role(g.user["id"]), "Team")
    for m in db.list_org_members(g.user["id"]):
        if m["user_id"] == g.user["id"] and m["role_label"]:
            return m["role_label"]
    return label


def _irb_or_404(sub_id):
    sub = db.get_irb_submission(sub_id)
    if not sub or sub["user_id"] not in db.org_member_ids(g.user["id"]):
        abort(404)
    return sub


def _irb_days_left(expires_at):
    exp = (expires_at or "").strip()[:10]
    if not exp:
        return None
    try:
        d = dt.datetime.strptime(exp, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (d - dt.date.today()).days


@app.route("/app/irb")
@login_required
def irb_page():
    """IRB & approvals: recruitment materials are advertising and need ethics-board
    approval BEFORE use (COMPLIANCE.md §5). This is where a coordinator assembles a
    submission package, submits it (initial / modification / continuing review),
    tracks the review, and records the approved version + expiry. Approval here
    flips the linked campaign's gate so - and only so - it can go live."""
    active_nct, studies = _active_scope()
    subs = db.list_irb_submissions_for_user(g.user["id"],
                                            ncts=[active_nct] if active_nct else None)
    title_by_nct = {s["nct"]: (s["title"] or s["nct"]) for s in studies}
    rows = []
    for s in subs:
        days = _irb_days_left(s["expires_at"])
        rows.append({
            "s": s,
            "study": title_by_nct.get(s["nct"], s["nct"]),
            "mats": db.list_irb_items(s["id"]),
            "days_left": days,
            "expiring": (days is not None and days <= 45 and s["status"] == "approved"),
        })
    return render_template(
        "irb.html", rows=rows, counts=db.irb_counts(g.user["id"]),
        studies=studies, status_labels=db.IRB_STATUS_LABELS,
        status_tone=db.IRB_STATUS_TONE, sub_types=db.IRB_SUBMISSION_TYPES,
        known_irbs=db.KNOWN_IRBS, has_any=bool(subs))


@app.route("/app/irb/new", methods=["POST"])
@login_required
def irb_create():
    nct = (request.form.get("nct") or "").strip()
    title = (request.form.get("title") or "").strip()
    if not title or not nct:
        flash("Name the package and pick the study it recruits for.", "error")
        return redirect(url_for("irb_page"))
    if nct not in _site_claims():
        flash("You can only submit materials for studies you've claimed.", "error")
        return redirect(url_for("irb_page"))
    irb_name = (request.form.get("irb_name") or "").strip()
    irb_kind = "local" if irb_name.lower().startswith("local") else "central"
    sub_type = (request.form.get("submission_type") or "initial").strip()
    if sub_type not in db.IRB_SUBMISSION_TYPES:
        sub_type = "initial"
    pi = ""
    for m in db.list_org_members(g.user["id"]):
        if (m["role"] or "") == "pi":
            pi = m["name"] or ""
            break
    sid = db.create_irb_submission(
        g.user["id"], nct, title, irb_name=irb_name, irb_kind=irb_kind,
        submission_type=sub_type, pi_name=pi,
        protocol_version=(request.form.get("protocol_version") or "").strip())
    flash("Package created. Add your materials, then submit it for review.", "success")
    return redirect(url_for("irb_detail", sub_id=sid))


@app.route("/app/irb/<int:sub_id>")
@login_required
def irb_detail(sub_id):
    sub = _irb_or_404(sub_id)
    items = db.list_irb_items(sub_id)
    events = db.list_irb_events(sub_id)
    staff = {m["name"]: m["email"] for m in db.list_org_members(g.user["id"])
             if m["name"]}
    # Campaign creatives on this study you can pull straight into the package.
    linked = {i["campaign_id"] for i in items if i["campaign_id"]}
    camps = [c for c in db.list_campaigns_for_user(g.user["id"], ncts=[sub["nct"]])
             if c["id"] not in linked]
    return render_template(
        "irb_detail.html", sub=sub, items=items, events=events, staff=staff,
        campaigns=camps, days_left=_irb_days_left(sub["expires_at"]),
        status_labels=db.IRB_STATUS_LABELS, status_tone=db.IRB_STATUS_TONE,
        sub_types=db.IRB_SUBMISSION_TYPES, item_kinds=db.IRB_ITEM_KINDS,
        today=dt.date.today().strftime("%Y-%m-%d"),
        one_year=(dt.date.today() + dt.timedelta(days=365)).strftime("%Y-%m-%d"))


@app.route("/app/irb/<int:sub_id>/item", methods=["POST"])
@login_required
def irb_item(sub_id):
    sub = _irb_or_404(sub_id)
    action = (request.form.get("action") or "add").strip()
    if action == "remove":
        try:
            db.remove_irb_item(int(request.form.get("item_id") or 0), sub_id)
        except (TypeError, ValueError):
            pass
        return redirect(url_for("irb_detail", sub_id=sub_id))
    camp_id = request.form.get("campaign_id")
    if camp_id:
        c = db.get_campaign(int(camp_id))
        if c and c["user_id"] in db.org_member_ids(g.user["id"]):
            db.add_irb_item(
                sub_id, kind="campaign", campaign_id=c["id"],
                label=(c["name"] or "Campaign creative"),
                version="v1.0",
                detail=(c["headline"] or "")[:200] or "Campaign ad creative.")
            flash("Added the campaign creative to the package.", "success")
        return redirect(url_for("irb_detail", sub_id=sub_id))
    label = (request.form.get("label") or "").strip()
    if not label:
        flash("Give the material a name.", "error")
        return redirect(url_for("irb_detail", sub_id=sub_id))
    kind = (request.form.get("kind") or "material").strip()
    if kind not in db.IRB_ITEM_KINDS:
        kind = "material"
    db.add_irb_item(sub_id, kind=kind, label=label,
                    version=(request.form.get("version") or "v1.0").strip(),
                    detail=(request.form.get("detail") or "").strip())
    flash("Material added to the package.", "success")
    return redirect(url_for("irb_detail", sub_id=sub_id))


@app.route("/app/irb/<int:sub_id>/status", methods=["POST"])
@login_required
def irb_status(sub_id):
    sub = _irb_or_404(sub_id)
    status = (request.form.get("status") or "").strip().lower()
    note = (request.form.get("note") or "").strip()
    ok, reason = db.advance_irb_status(
        sub_id, status, actor=(g.user["name"] or "You"),
        actor_role=_current_member_role_label(), note=note,
        submission_ref=(request.form.get("submission_ref") or "").strip(),
        approved_version=(request.form.get("approved_version") or "").strip(),
        expires_at=(request.form.get("expires_at") or "").strip())
    if not ok:
        flash(reason or "Could not update the submission.", "error")
    else:
        msg = {
            "submitted": "Marked submitted to the IRB.",
            "in_review": "Marked under review.",
            "revisions": "Logged the IRB's requested revisions.",
            "approved": "Approved and logged. Linked campaigns are now cleared to "
                        "go live.",
            "expired": "Marked expired. Linked campaigns were paused.",
        }.get(status, "Submission updated.")
        flash(msg, "success")
    return redirect(url_for("irb_detail", sub_id=sub_id))


@app.route("/app/irb/<int:sub_id>/packet")
@login_required
def irb_packet(sub_id):
    """The submission packet as a clean, printable cover sheet + materials list +
    version history - the format you attach or upload to the IRB (print to PDF)."""
    sub = _irb_or_404(sub_id)
    prof = db.get_site_profile(g.user["id"]) or {}
    try:
        org_name = (prof["org_name"] or "").strip()
    except (KeyError, TypeError):
        org_name = ""
    return render_template(
        "irb_packet.html", sub=sub, items=db.list_irb_items(sub_id),
        events=db.list_irb_events(sub_id), org_name=org_name,
        sub_types=db.IRB_SUBMISSION_TYPES, item_kinds=db.IRB_ITEM_KINDS,
        generated=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))


@app.route("/app/updates")
@login_required
def updates_page():
    _seed_demo_updates_if_demo()
    # Scope follows the single global switcher. "" = all studies.
    nct_filter, _ = _active_scope()
    rows = db.list_sponsor_updates(g.user["id"], nct_filter or None)
    updates = []
    for u in rows:
        recon = {}
        if (u["type"] or "") == "amendment":
            _, booked, done = _reconsent_counts(u["id"])
            cands = db.reconsent_candidates(u["user_id"], u["nct"])
            recon = {"booked": len(booked), "done": len(done),
                     "candidates": len(cands)}
        updates.append(_update_view(u, recon))

    open_updates = [u for u in updates if not u["complete"]]
    done_updates = [u for u in updates if u["complete"]]

    team_studies = db.list_team_studies(g.user["id"])
    trials = [{"nct": s["nct"], "title": s["title"] or s["nct"]}
              for s in team_studies]
    return render_template(
        "updates.html", open_updates=open_updates, done_updates=done_updates,
        trials=trials, nct_filter=nct_filter, update_types=_UPDATE_TYPES)


@app.route("/app/updates/<int:update_id>")
@login_required
def update_detail(update_id):
    u = _update_owned(update_id)
    if not u:
        abort(404)
    recon = {}
    cands = []
    booked_visits = []
    if (u["type"] or "") == "amendment":
        visits, booked, done = _reconsent_counts(update_id)
        booked_visits = [_visit_view(v, dt.datetime.now()) for v in visits]
        raw_cands = db.reconsent_candidates(u["user_id"], u["nct"])
        for c in raw_cands:
            cands.append({
                "id": c["id"], "name": c["name"] or "Participant",
                "status": c["status"], "booked": c["id"] in booked,
                "done": c["id"] in done})
        recon = {"booked": len(booked), "done": len(done),
                 "candidates": len(raw_cands),
                 "remaining": len(raw_cands) - len(booked)}
    view = _update_view(u, recon)
    events = db.list_update_events(update_id)
    trial_title = ""
    for s in db.list_team_studies(g.user["id"]):
        if s["nct"] == u["nct"]:
            trial_title = s["title"] or s["nct"]
    visit_cards = [_build_visit_card(v, dt.datetime.now()) for v in booked_visits]
    doc_groups = _doc_versions_view(u["user_id"], u["nct"], update_id)
    return render_template(
        "update_detail.html", u=view, nct=u["nct"], trial_title=trial_title,
        candidates=cands, booked_visits=booked_visits, visit_cards=visit_cards,
        events=events, visit_kinds=_VISIT_KINDS, doc_groups=doc_groups)


@app.route("/app/updates/new", methods=["POST"])
@login_required
def updates_new():
    nct = (request.form.get("nct") or "").strip()
    if nct and nct not in db.user_claimed_ncts(g.user["id"]):
        flash("Pick a study you run.", "error")
        return redirect(url_for("updates_page"))
    typ = request.form.get("type") or "amendment"
    if typ not in _UPDATE_TYPE_LABELS:
        typ = "amendment"
    title = (request.form.get("title") or "").strip()
    if not title:
        flash("Give the update a short title.", "error")
        return redirect(url_for("updates_page"))
    version = (request.form.get("version") or "").strip()
    uid = db.add_sponsor_update(
        g.user["id"], nct, type=typ, version=version,
        title=title, summary=(request.form.get("summary") or "").strip(),
        source=(request.form.get("source") or "").strip(),
        due_at=_cal_form_dt("due_at", None) or "")
    # An amendment ships a new controlled-document version. Log it as PENDING; it
    # becomes the current version once IRB approves (see update_irb).
    if typ == "amendment" and version and nct:
        try:
            db.add_doc_version(
                g.user["id"], nct, doc_type="protocol", version=version,
                status="pending", update_id=uid,
                note="Introduced by " + title)
        except Exception:
            app.logger.exception("doc version create failed")
    flash("Update logged. Work the checklist so nothing slips.", "ok")
    return redirect(url_for("update_detail", update_id=uid))


@app.route("/app/updates/<int:update_id>/ack", methods=["POST"])
@login_required
def update_ack(update_id):
    if not _update_owned(update_id):
        abort(404)
    db.update_sponsor_update(update_id, ack_status="done", ack_at=db.now(),
                             event="acknowledged", note="Receipt acknowledged")
    flash("Acknowledged. The sponsor's record shows this site received it.", "ok")
    return redirect(url_for("update_detail", update_id=update_id))


@app.route("/app/updates/<int:update_id>/irb", methods=["POST"])
@login_required
def update_irb(update_id):
    if not _update_owned(update_id):
        abort(404)
    status = request.form.get("status") or ""
    if status not in ("submitted", "approved", "not_required"):
        flash("Unknown IRB status.", "error")
        return redirect(url_for("update_detail", update_id=update_id))
    fields = {"irb_status": status}
    when = _cal_form_dt("date", None) or db.now()[:10]
    if status == "submitted":
        fields["irb_submitted_at"] = when
        ev, note = "irb_submitted", f"Submitted to IRB {when}"
    elif status == "approved":
        fields["irb_approved_at"] = when
        if not db.get_sponsor_update(update_id)["irb_submitted_at"]:
            fields["irb_submitted_at"] = when
        # On approval the amendment's pending document versions become current
        # and supersede the prior ones - so the site always sees the in-effect
        # version. Fold the result into the IRB event note (a bare event with no
        # field change isn't logged).
        promoted = 0
        try:
            promoted = db.promote_doc_versions_for_update(
                g.user["id"], update_id, when)
        except Exception:
            app.logger.exception("doc version promote failed")
        ev = "irb_approved"
        note = f"IRB approved {when}" + (
            f" - {promoted} document version(s) now current" if promoted else "")
    else:
        ev, note = "irb_waived", "IRB review marked not required"
    db.update_sponsor_update(update_id, event=ev, note=note, **fields)
    flash("IRB status updated.", "ok")
    return redirect(url_for("update_detail", update_id=update_id))


@app.route("/app/updates/<int:update_id>/icf", methods=["POST"])
@login_required
def update_icf(update_id):
    if not _update_owned(update_id):
        abort(404)
    done = request.form.get("done") == "1"
    note = (request.form.get("icf_note") or "").strip()
    db.update_sponsor_update(
        update_id, icf_status=("done" if done else "pending"), icf_note=note,
        event="icf_updated" if done else "icf_reopened",
        note=note or ("Consent form updated" if done else "Reopened"))
    flash("Consent form step updated." if done else "Reopened consent step.", "ok")
    return redirect(url_for("update_detail", update_id=update_id))


@app.route("/app/updates/<int:update_id>/retrain", methods=["POST"])
@login_required
def update_retrain(update_id):
    if not _update_owned(update_id):
        abort(404)
    done = request.form.get("done") == "1"
    db.update_sponsor_update(
        update_id, retrain_status=("done" if done else "pending"),
        event="retrain_done" if done else "retrain_reopened",
        note="Staff retrained on the change" if done else "Reopened")
    flash("Staff training updated.", "ok")
    return redirect(url_for("update_detail", update_id=update_id))


@app.route("/app/updates/<int:update_id>/reconsent/book", methods=["POST"])
@login_required
def update_reconsent_book(update_id):
    u = _update_owned(update_id)
    if not u:
        abort(404)
    lead_ids = request.form.getlist("lead_ids", type=int)
    when = _cal_form_dt("date", "time")
    if not when:
        flash("Pick a date and time for the re-consent visits.", "error")
        return redirect(url_for("update_detail", update_id=update_id))
    if not lead_ids:
        flash("Select at least one participant to re-consent.", "error")
        return redirect(url_for("update_detail", update_id=update_id))
    # Only book participants who are genuinely re-consent candidates on this
    # trial and don't already have a re-consent visit for this amendment.
    valid = {c["id"] for c in db.reconsent_candidates(u["user_id"], u["nct"])}
    _, already, _ = _reconsent_counts(update_id)
    ver = u["version"] or u["title"] or "amendment"
    prep = ("Review the updated consent form before your visit\n"
            "Bring any questions about what changed")
    n = 0
    for lid in lead_ids:
        if lid not in valid or lid in already:
            continue
        db.add_visit(lid, when, kind="reconsent",
                     location=request.form.get("location", "").strip(),
                     note=f"Re-consent for {ver}",
                     title=f"Re-consent - {ver}",
                     duration_min=request.form.get("duration_min", type=int) or 20,
                     prep=prep, update_id=update_id)
        n += 1
    if n:
        db.update_sponsor_update(
            update_id, reconsent_status="in_progress",
            event="reconsent_booked",
            note=f"Booked {n} re-consent visit(s) for {when[:10]}")
        flash(f"Booked {n} re-consent visit{'s' if n != 1 else ''}. "
              "They're on the calendar and patient reminders are queued.", "ok")
    else:
        flash("Nothing to book - those participants already have a re-consent "
              "visit.", "warn")
    return redirect(url_for("update_detail", update_id=update_id))


@app.route("/app/updates/<int:update_id>/reconsent/complete", methods=["POST"])
@login_required
def update_reconsent_complete(update_id):
    if not _update_owned(update_id):
        abort(404)
    done = request.form.get("done") == "1"
    db.update_sponsor_update(
        update_id, reconsent_status=("complete" if done else "in_progress"),
        event="reconsent_complete" if done else "reconsent_reopened",
        note="All participants re-consented" if done else "Reopened re-consent")
    flash("Re-consent marked complete." if done else "Re-consent reopened.", "ok")
    return redirect(url_for("update_detail", update_id=update_id))


def _seed_demo_updates_if_demo():
    """Seed a couple of realistic sponsor updates (one live amendment mid-flight,
    one safety letter) so the Updates hub reads as a live workflow. Idempotent;
    demo-only (COMPLIANCE.md)."""
    if not g.user:
        return
    if not (_demo_mode_enabled() or _is_demo_account(g.user)
            or _site_demo_enabled()):
        return
    try:
        if db.list_sponsor_updates(g.user["id"]):
            return
    except Exception:
        return
    ncts = sorted(db.user_claimed_ncts(g.user["id"]))
    if not ncts:
        return
    today = dt.date.today()

    def _d(days):
        return (today + dt.timedelta(days=days)).strftime("%Y-%m-%d")

    # Land the amendment on the trial with the most enrolled/active participants
    # so the re-consent queue actually has people in it (that's the demo's point).
    nct = max(ncts, key=lambda n: len(db.reconsent_candidates(g.user["id"], n)))
    # A live amendment mid-flight: acknowledged + IRB submitted, ICF/re-consent/
    # retrain still open, due in ~10 days. This is the money demo.
    try:
        uid = db.add_sponsor_update(
            g.user["id"], nct, type="amendment", version="Protocol v4.0",
            title="Amendment 3 - new safety labs at every visit",
            summary=("Adds a fasting lipid panel and ECG at all treatment "
                     "visits, updates the risk section, and revises the "
                     "consent form. All currently-enrolled participants must "
                     "re-consent to the updated ICF before their next visit."),
            source="Sponsor - Clinical Ops", received_at=_d(-4), due_at=_d(10))
        db.update_sponsor_update(
            uid, ack_status="done", ack_at=_d(-4),
            irb_status="submitted", irb_submitted_at=_d(-2))
        # Document-version trail so the site sees exactly which Protocol/ICF is in
        # effect and its history. v4.0 is PENDING (this amendment, awaiting IRB);
        # v3.0 is current; earlier ones superseded.
        for doc in ("protocol", "icf"):
            db.add_doc_version(g.user["id"], nct, doc_type=doc, version="v1.0",
                               status="superseded", effective_at=_d(-430))
            db.add_doc_version(g.user["id"], nct, doc_type=doc, version="v2.0",
                               status="superseded", effective_at=_d(-250))
            db.add_doc_version(g.user["id"], nct, doc_type=doc, version="v3.0",
                               status="current", effective_at=_d(-95))
            db.add_doc_version(g.user["id"], nct, doc_type=doc, version="v4.0",
                               status="pending", update_id=uid,
                               note="Introduced by Amendment 3")
        db.add_doc_version(g.user["id"], nct, doc_type="ib", version="Edition 6",
                           status="current", effective_at=_d(-140))
    except Exception:
        app.logger.exception("demo update seeding failed")
    # A safety letter that only needs acknowledgement + a quick retrain.
    try:
        db.add_sponsor_update(
            g.user["id"], nct, type="safety",
            title="Urgent safety letter - updated dosing caution",
            summary=("New safety signal: hold dosing and call the medical "
                     "monitor if ALT/AST exceeds 3x ULN. File in the ISF and "
                     "brief the team."),
            source="Sponsor - Pharmacovigilance",
            received_at=_d(-1), due_at=_d(3))
    except Exception:
        app.logger.exception("demo safety letter seeding failed")

    # Every OTHER claimed trial gets at least one light sponsor update, so the
    # hub is never empty no matter which study is in scope (a common demo miss).
    _other = [
        ("bulletin", "Q3 enrollment newsletter",
         "Site ranking, upcoming monitoring visit windows, and a reminder to "
         "keep the delegation log current. No action beyond acknowledgement.",
         "Sponsor - Clinical Ops", 0),
        ("doc_request", "Updated CV + GCP certificate requested",
         "Annual refresh: please upload the current PI CV and GCP training "
         "certificate for the regulatory binder.",
         "CRO - Site Management", 7),
    ]
    for i, n in enumerate(x for x in ncts if x != nct):
        typ, ttl, summ, src, due = _other[i % len(_other)]
        try:
            db.add_sponsor_update(
                g.user["id"], n, type=typ, title=ttl, summary=summ,
                source=src, received_at=_d(-2), due_at=_d(due) if due else "")
        except Exception:
            app.logger.exception("demo per-trial update seeding failed")


# --------------------------------------------------------------------------- #
# Internal patient -> trial matching. The clinic's own (de-identified) patients
# surfaced as candidates for studies it runs (internal) or partner trials
# (external). Approving an internal match adds the person to the ATS as a
# likely-eligible applicant; approving an external one sends a secure referral
# (in the real product, only after the patient consents). This is the upstream
# "found + pre-screened" supply that drives enrollment velocity.
# --------------------------------------------------------------------------- #
def _match_initials(ref):
    letters = "".join(c for c in (ref or "") if c.isalpha())
    return (letters[:2].upper() or "?")


def _match_view(m):
    """Flatten a patient_matches dict into the fields the matching screens read."""
    v = _verdict_view({"verdict": m.get("verdict"), "score": m.get("score"),
                       "met": m.get("met"), "not_met": m.get("not_met"),
                       "unknown": m.get("unknown")})
    return {
        "id": m["id"], "ref": m.get("patient_ref") or "?",
        "initials": _match_initials(m.get("patient_ref")),
        "age": m.get("age"), "sex": m.get("sex"), "summary": m.get("summary"),
        "source_label": m.get("source_label"), "nct": m.get("nct"),
        "trial_title": m.get("trial_title") or m.get("nct"),
        "condition": m.get("condition"), "kind": m.get("kind"),
        "site_name": m.get("site_name"), "site_location": m.get("site_location"),
        "verdict": v, "met": m.get("met") or [], "unknown": m.get("unknown") or [],
        "not_met": m.get("not_met") or [], "rationale": m.get("rationale"),
        "status": m.get("status"), "lead_id": m.get("lead_id"),
    }


def _seed_demo_replies_if_demo(items):
    """Make the dashboard's Replies tab look real in demo mode by putting a few
    revealed candidates into a 'patient wrote last, waiting on you' state - the
    #1 daily action a coordinator does. Idempotent: converges to ~3 awaiting
    replies and then does nothing (each seeded thread stays patient-last, so it
    keeps counting and no new ones are added). Off once SITE_DEMO is off, before
    onboarding real sites."""
    if not g.user:
        return
    if not (_demo_mode_enabled() or _is_demo_account(g.user) or _site_demo_enabled()):
        return
    TARGET = 3
    openers = [
        "Hi {first}, thanks for applying - you're a strong fit. Any questions "
        "before we book your screening visit?",
        "Hi {first}, you've cleared our initial review. When works for a quick "
        "screening call this week?",
        "Hi {first}, welcome! I'm your study coordinator - happy to help with "
        "anything before your first visit.",
    ]
    questions = [
        "Thanks! Roughly how many visits are involved, and is parking available?",
        "Sounds good - mornings work best for me. Is Thursday possible?",
        "Appreciate it! Will taking part affect my regular care?",
    ]
    have, candidates = 0, []
    for it in items:
        l = it["lead"]
        if not l["revealed"] or l["status"] in db.LEAD_CLOSED or l["status"] == "enrolled":
            continue
        msgs = db.get_messages(l["id"])
        awaiting = bool(db.lead_unread_for_site(l["id"])) or (
            msgs and msgs[-1]["sender"] == "patient")
        if awaiting:
            have += 1
        else:
            candidates.append((l, msgs))
    i = 0
    for l, msgs in candidates:
        if have >= TARGET:
            break
        # Never stack a second patient reply on a thread that already has one -
        # a later system message (e.g. a visit reminder) can push the patient's
        # turn off the end and make it look "awaiting us" again on refresh.
        if any(m["sender"] == "patient" for m in msgs):
            continue
        first = (l["name"] or "there").split()[0]
        if not any(m["sender"] in ("site", "patient") for m in msgs):
            db.add_message(l["id"], "site", openers[i % len(openers)].format(first=first))
        db.add_message(l["id"], "patient", questions[i % len(questions)])
        have += 1
        i += 1


def _seed_demo_visits_if_demo(items):
    """Make the dashboard's Upcoming-visits rail look real in demo mode by
    booking a few near-future screening visits on revealed candidates that
    don't already have one. Idempotent: converges to ~3 upcoming visits and
    then does nothing, so it won't pile up on refresh. Off once SITE_DEMO is
    off (before onboarding real sites)."""
    if not g.user:
        return
    if not (_demo_mode_enabled() or _is_demo_account(g.user) or _site_demo_enabled()):
        return

    def _parse_ts(ts):
        try:
            return dt.datetime.strptime(str(ts)[:16], "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return None

    now = dt.datetime.now()
    horizon = now + dt.timedelta(days=14)
    have, candidates = 0, []
    for it in items:
        l = it["lead"]
        if not l["revealed"] or l["status"] in db.LEAD_CLOSED:
            continue
        has_future = False
        for v in db.get_visits(l["id"]):
            w = _parse_ts(v["visit_at"])
            if w and now <= w <= horizon:
                has_future = True
                break
        if has_future:
            have += 1
        else:
            candidates.append(l)
    slots = [(1, 10), (2, 14), (4, 9), (6, 11)]  # (days ahead, hour), varied
    i = 0
    for l in candidates:
        if have >= 3:
            break
        days, hour = slots[i % len(slots)]
        when = (now + dt.timedelta(days=days)).replace(
            hour=hour, minute=0, second=0, microsecond=0)
        try:
            db.add_visit(l["id"], when.strftime("%Y-%m-%d %H:%M"),
                         kind="screening", location=l["site"] or "Study site",
                         note="Bring a photo ID. Allow about 90 minutes.")
            have += 1
            i += 1
        except Exception:
            app.logger.exception("demo visit seeding failed")


def _seed_matches_if_demo():
    """Top up the demo match queue on demand. Fires for the demo account AND for
    any logged-in study-team account while the site is still a pre-launch demo
    (SITE_DEMO on) - matching is a newer surface than the rest of the demo
    seeding, so accounts seeded before it existed (they already have studies, so
    load_user won't re-seed them) still light up. Idempotent per patient_ref, so
    it never duplicates. SITE_DEMO MUST be OFF before onboarding real sites (see
    COMPLIANCE.md), which also turns this off."""
    if not g.user:
        return
    if _demo_mode_enabled() or _is_demo_account(g.user) or _site_demo_enabled():
        try:
            db.seed_demo_patient_matches(g.user["id"])
        except Exception:
            app.logger.exception("demo match seeding failed")


def _match_source(user_id, counts):
    """The internal patient panel the matches were surfaced from. Makes the
    "you connected your records; here's what the nightly scan found" story
    concrete. Patient-panel size is only shown in the demo (we don't know a
    real clinic's panel size)."""
    demo = (_demo_mode_enabled() or _is_demo_account(g.user)
            or _site_demo_enabled())
    prof = db.get_site_profile(user_id)
    name = ""
    if prof is not None:
        try:
            name = (prof["org_name"] or "").strip()
        except (IndexError, KeyError):
            name = ""
    if not name:
        name = "Riverside Family Health" if demo else "Your clinic"
    try:
        studies = len(db.list_study_claims(user_id))
    except Exception:
        studies = 0
    return {"name": name, "demo": demo, "patients": 1284 if demo else 0,
            "studies": studies, "surfaced": counts.get("total", 0),
            "new": counts.get("new", 0)}


@app.route("/app/matching")
@login_required
def matching_page():
    """Review queue: scan the patients the engine surfaced and, with one click,
    add an internal candidate to your study or send an external referral."""
    _seed_matches_if_demo()
    claims = db.list_study_claims(g.user["id"])
    matches = [_match_view(m) for m in db.list_patient_matches(g.user["id"])]
    counts = db.patient_match_counts(g.user["id"])
    source = _match_source(g.user["id"], counts)
    return render_template("matching.html", claims=claims, matches=matches,
                           counts=counts, source=source)


@app.route("/app/matching/<int:match_id>")
@login_required
def match_detail(match_id):
    m = db.get_patient_match(g.user["id"], match_id)
    if not m:
        abort(404)
    return render_template("match_detail.html", m=_match_view(m))


@app.route("/app/matching/<int:match_id>/approve", methods=["POST"])
@login_required
def approve_match(match_id):
    m = db.get_patient_match(g.user["id"], match_id)
    if not m:
        abort(404)
    if m["status"] != "new":
        flash("That match was already handled.", "error")
        return redirect(url_for("matching_page"))
    if m["kind"] == "external":
        db.set_patient_match_status(g.user["id"], match_id, "referred")
        flash("Secure referral sent to " + (m["site_name"] or "the trial site")
              + ". They confirm the screening visit with the patient.", "success")
        return redirect(url_for("matching_page"))
    # Internal: fold the person into our own study's applicant pipeline as a
    # likely-eligible, already-reviewed candidate (carries the AI read forward).
    elig = {"verdict": m.get("verdict"), "score": m.get("score"),
            "met": m.get("met"), "unknown": m.get("unknown"),
            "not_met": m.get("not_met"), "rationale": m.get("rationale")}
    token = db.create_lead({
        "nct": m["nct"], "title": m["trial_title"], "condition": m["condition"],
        "name": m["full_name"] or m["patient_ref"], "age": m["age"],
        "sex": m["sex"], "source": "emr", "consent": 1, "records_connected": 1,
        "record_summary": m["summary"], "eligibility": json.dumps(elig)})
    lead = db.get_lead_by_token(token)
    lead_id = lead["id"] if lead else None
    if lead_id:
        db.accept_candidate(lead_id, "Matched from clinic records and approved")
    db.set_patient_match_status(g.user["id"], match_id, "approved", lead_id=lead_id)
    if lead_id:
        flash("Added to your study as a likely-eligible applicant. Send the "
              "booking link to schedule their screening visit.", "success")
        return redirect(url_for("applicant_detail", lead_id=lead_id))
    flash("Added to your study.", "success")
    return redirect(url_for("matching_page"))


@app.route("/app/matching/<int:match_id>/dismiss", methods=["POST"])
@login_required
def dismiss_match(match_id):
    if not db.set_patient_match_status(g.user["id"], match_id, "dismissed"):
        abort(404)
    flash("Match dismissed. It won't show in your review queue.", "success")
    return redirect(url_for("matching_page"))


@app.route("/app/applicant/<int:lead_id>")
@login_required
def applicant_detail(lead_id):
    """Full applicant record in the new UI: AI pre-screen verdict + reasons,
    the message thread, tasks, documents and the activity timeline. Owner-scoped
    to the study team's claimed studies (PHI stays isolated by user)."""
    lead = db.get_lead(lead_id)
    ncts = set(db.user_claimed_ncts(g.user["id"]))
    # The demo account (which SITE_DEMO signs any visitor into) may open only
    # seeded demo applicants. A real person's application is visible to the
    # owner and to the site that claimed the study, never to a walk-up
    # visitor or a link-preview crawler that was handed the URL.
    demo_ok = _is_demo_account(g.user) and not _is_inbound_application(lead or {})
    if not lead or (lead["nct"] and lead["nct"] not in ncts
                    and not demo_ok
                    and not _is_owner()):
        abort(404)
    if _is_demo_account(g.user) and _is_inbound_application(lead):
        abort(404)
    it = _decode_lead(lead, db.latest_reconciliation(lead_id))
    view = _queue_item(it)
    view["initials"] = _initials(lead["name"] or view["code"])
    # The booking calendar we'd send with one click: study default, else the
    # coordinator's account calendar (configured once in Settings).
    default_schedule = (db.get_claim_schedule_url(g.user["id"], lead["nct"])
                        or db.get_site_calendar_url(g.user["id"]))
    # Concierge handoff: pre-draft the email to the study's coordinator so the
    # operator can forward a consented applicant in one click (fetches the
    # coordinator contact; cached). Owner-only - this is an internal tool, so
    # site users never see the forward button even on their own applicants.
    fwd = None
    if _is_owner():
        try:
            fwd = _forward_draft(lead, it)
        except Exception:
            app.logger.exception("forward draft failed")
            fwd = None
    notes = db.list_notes(lead_id)
    # The one-time portal password is popped from the session so it is rendered
    # exactly once and never survives a refresh.
    reveal = session.pop("portal_reveal", None)
    if reveal and reveal.get("lead_id") != lead_id:
        reveal = None
    portal = db.get_portal_by_lead(lead_id)
    portal_url = ""
    if portal and portal["status"] == "active":
        portal_url = url_for("portal", token=portal["token"], _external=True)
    live_attr = {}
    if _is_owner():
        live_attr = db.apply_attribution_for(lead["nct"], lead["created_at"])
    clinic_notify = db.lead_clinic_notify(lead)
    return render_template("applicant_detail.html", it=it, l=lead, view=view,
                           statuses=db.LEAD_STATUSES, labels=db.LEAD_LABELS,
                           screener_labels=SCREENER_LABELS,
                           screener_flags=SCREENER_FLAGS, notes=notes,
                           default_schedule=default_schedule, fwd=fwd,
                           portal=portal, portal_reveal=reveal,
                           portal_url=portal_url, live_attr=live_attr,
                           clinic_notify=clinic_notify)


@app.route("/app/applicant/<int:lead_id>/send-intro", methods=["POST"])
@login_required
def send_applicant_clinic_intro(lead_id):
    """Owner-only: email the applicant the clinic contact + their thread."""
    if not _is_owner():
        abort(404)
    lead = db.get_lead(lead_id)
    if not lead:
        abort(404)
    back = url_for("applicant_detail", lead_id=lead_id)
    if _notify_applicant_clinic_connect(lead):
        db.add_message(
            lead_id, "system",
            "We emailed the applicant their application thread so they can "
            "message the study team.")
        flash("Application link emailed to the applicant.", "success")
    else:
        flash("Couldn't email that applicant.", "error")
    return redirect(back)


@app.route("/app/dashboard")
@login_required
def recruitment_dashboard():
    """Legacy CTMS funnel page. Hidden from the nav on purpose - the product is
    the shared inbox, not a recruitment analytics dashboard. Old bookmarks and
    Bridget citations still hit this URL, so send them to the inbox."""
    return redirect(url_for("marketing_hub"))


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
    areas = db.web_areas(days=days)
    unmet = db.unmet_demand(days=days)
    trials = db.top_trials(days=days)
    devices = db.device_breakdown(days=days)
    return render_template("analytics.html", web=web, recent=recent, days=days,
                           series=series, areas=areas, unmet=unmet,
                           top_trials=trials, devices=devices,
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


def _team_context():
    """Shared workspace roster + pending invites, shaped for the team UI. Used by
    both the standalone Team page and the Team tab inside Settings so the roster
    shows inline without a second click."""
    members = db.list_org_members(g.user["id"])
    members = [{
        "user_id": m["user_id"], "name": m["name"], "email": m["email"],
        "role": m["role"], "custom_label": m["role_label"] or "",
        "role_label": (m["role_label"] or db.ORG_ROLE_LABELS.get(m["role"], m["role"])),
        "is_me": m["user_id"] == g.user["id"],
    } for m in members]
    members.sort(key=lambda m: not m["is_me"])  # "you" first
    invites = [{
        "token": i["token"], "email": i["email"],
        "role_label": (i["role_label"] or db.ORG_ROLE_LABELS.get(i["role"], i["role"])),
        "url": url_for("team_join", token=i["token"], _external=True),
    } for i in db.list_org_invites(g.user["id"])]
    return {"members": members, "invites": invites,
            "roles": db.ORG_ROLES, "role_labels": db.ORG_ROLE_LABELS}


def _team_return():
    """Return to wherever a team action was submitted from (Settings Team tab or
    the standalone Team page), so acting inline never bounces the user away."""
    ref = request.referrer or ""
    if "/app/site" in ref:
        return url_for("site_setup") + "#team"
    return url_for("team_page")


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
    claims = db.list_study_claims(g.user["id"])
    # Auto-routing: channel picker labels + existing rules (with display labels).
    route_channels = [{"key": "any", "label": "Any channel"}]
    for _key in db.ROUTING_CHANNELS:
        if _key == "any":
            continue
        route_channels.append({"key": _key, "label": _channel_meta(_key)[0]})
    _study_titles = {c["nct"]: (c["title"] or c["nct"]) for c in claims}
    # Per-study connected sources: which channels each trial recruits through.
    # Drives the inbox's per-trial source filters. Presented as a checkbox grid.
    connectable_channels = [{"key": k, "label": _channel_meta(k)[0],
                             "icon": _channel_icon(_channel_meta(k)[0])}
                            for k in db.CONNECTABLE_CHANNELS]
    study_sources = []
    for c in claims:
        study_sources.append({
            "nct": c["nct"], "title": c["title"] or c["nct"],
            "connected": set(db.get_claim_connected_sources(g.user["id"], c["nct"])),
        })
    connector_url = db.get_site_connector(g.user["id"])[0]
    routing_rules = []
    for r in db.list_routing_rules(g.user["id"]):
        clabel, ctone = ("Any channel", "neutral") if r["channel"] == "any" \
            else _channel_meta(r["channel"])
        routing_rules.append({
            "id": r["id"],
            "channel_label": clabel, "channel_tone": ctone,
            "study_label": _study_titles.get(r["nct"], r["nct"]) if r["nct"]
            else "Any study",
            "assignee_name": r["assignee_name"], "assignee_email": r["assignee_email"],
        })
    # Connected message channels (Gmail / Instagram / demo sources) so they can be
    # managed from Settings, not only the inbox modal. Same rows + connect/sync/
    # disconnect routes the inbox uses -- one source of truth, surfaced where a
    # coordinator would actually look for "connected accounts".
    marketing_sources = [dict(r) for r in db.list_marketing_sources(g.user["id"])]
    for _s in marketing_sources:
        _last = _s.get("last_successful_sync_at") or ""
        _s["sync_time_label"] = (
            _marketing_time_label(_last) if _last else "Not synced yet")
    return render_template(
        "site_setup.html",
        profile=profile,
        marketing_sources=marketing_sources,
        marketing_sources_count=len(marketing_sources),
        channel_labels=db.MARKETING_CHANNEL_LABELS,
        gmail_oauth_ready=_google_ready(),
        instagram_oauth_ready=_instagram_ready(),
        claims=claims,
        route_channels=route_channels,
        routing_rules=routing_rules,
        connectable_channels=connectable_channels,
        study_sources=study_sources,
        connector_url=connector_url,
        posted=db.list_site_posted_studies(user_id=g.user["id"]),
        redcap_on=cfg.connected,
        redcap_token_set=bool(cfg.api_token),
        redcap_field_map=redcap._row_get(profile, "redcap_field_map"),
        redcap_intake_instrument=cfg.intake_instrument,
        redcap_intake_enabled=cfg.intake_enabled,
        redcap_instruments=instruments,
        redcap_simulated=simulated,
        active_tab=active_tab,
        **_team_context())


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
        # Coordinator's own booking calendar - patients self-book onto it.
        cal = f.get("calendar_url", "").strip()
        if cal and not cal.startswith(("http://", "https://")):
            cal = "https://" + cal
        db.set_site_calendar_url(g.user["id"], cal)
        flash("Site profile saved.", "success")
        return redirect(url_for("site_setup"))
    return _render_site_setup()


@app.route("/app/site/routing/add", methods=["POST"])
@login_required
def routing_rule_add():
    """Create an auto-routing rule (channel/study -> teammate). Any inquiry that
    matches will be auto-assigned to that person's inbox. KPI: Tier-2 efficiency
    -> contacted -> screened (no unassigned pile). Manage-team gated."""
    if not db.can_manage_team(g.user["id"]):
        flash("Only full-access members can manage routing.", "error")
        return redirect(url_for("site_setup") + "#routing")
    f = request.form
    try:
        assignee_id = int(f.get("assignee_id") or 0)
    except (TypeError, ValueError):
        assignee_id = 0
    rid = db.add_routing_rule(g.user["id"], f.get("channel", "any"),
                              f.get("nct", ""), assignee_id)
    flash("Routing rule added." if rid else "Couldn't add that rule.",
          "success" if rid else "error")
    return redirect(url_for("site_setup") + "#routing")


@app.route("/app/site/routing/delete", methods=["POST"])
@login_required
def routing_rule_delete():
    if not db.can_manage_team(g.user["id"]):
        flash("Only full-access members can manage routing.", "error")
        return redirect(url_for("site_setup") + "#routing")
    try:
        rule_id = int(request.form.get("rule_id") or 0)
    except (TypeError, ValueError):
        rule_id = 0
    ok = db.delete_routing_rule(g.user["id"], rule_id)
    flash("Routing rule removed." if ok else "Rule not found.",
          "success" if ok else "error")
    return redirect(url_for("site_setup") + "#routing")


@app.route("/app/site/sources", methods=["POST"])
@login_required
def study_sources_save():
    """Save which channels a study recruits through. Empty = every source. Drives
    the inbox's per-trial source filters so each trial shows only the logos it
    actually gets leads from. KPI: Tier-2 efficiency - a coordinator scans the
    right ad channels for that trial, not a mixed pile."""
    nct = (request.form.get("nct") or "").strip()
    sources = request.form.getlist("sources")
    if db.set_claim_connected_sources(g.user["id"], nct, sources):
        flash("Connected sources updated.", "success")
    else:
        flash("Couldn't update sources.", "error")
    return redirect(url_for("site_setup") + "#routing")


@app.route("/app/site/connector", methods=["POST"])
@login_required
def site_connector_save():
    """Save the outbound reply connector so coordinators can answer IG/FB/Messenger
    leads from the app (the reply is POSTed to this webhook, which delivers it back
    to the channel). Secret is write-only; blank keeps the stored value."""
    if not db.can_manage_team(g.user["id"]):
        flash("Only full-access members can manage the connector.", "error")
        return redirect(url_for("site_setup") + "#routing")
    f = request.form
    secret_raw = f.get("connector_secret", "")
    secret = secret_raw.strip() if secret_raw.strip() else None
    db.set_site_connector(g.user["id"], f.get("connector_webhook_url", ""), secret)
    flash("Reply connector saved.", "success")
    return redirect(url_for("site_setup") + "#routing")


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
    if not db.accept_candidate(lead_id, note):
        flash("Couldn't accept that candidate.", "error")
        return redirect(_lead_action_return())
    # Unlock contact only. Booking links are a separate, explicit send so a
    # demo or misconfigured calendar cannot email a real applicant by accident.
    _notify_applicant_by_id(lead_id, "accepted")
    flash("Accepted - contact unlocked. You can message them from this page.",
          "success")
    return redirect(_lead_action_return())


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
    return redirect(_lead_action_return())


@app.route("/app/leads/<int:lead_id>/schedule", methods=["POST"])
@login_required
def schedule_lead(lead_id):
    _ensure_site_access_for_lead(lead_id)
    """Attach a booking link (Calendly/Acuity/Cal.com/etc.) to an accepted
    candidate so the patient can self-schedule their screening call. Can also
    save the link as the study's reusable default (set_default=1)."""
    back = _lead_action_return()

    def _norm_link(v):
        v = (v or "").strip()
        if v and not v.startswith(("http://", "https://")):
            v = "https://" + v
        return v

    url = _norm_link(request.form.get("schedule_url", ""))
    # The applicant page is action-only: it sends the booking calendar the team
    # already configured (per-study default, else the account calendar in
    # Settings) with one click. Booking-link *config* lives in Settings, never
    # re-typed per applicant. An explicit URL in the form still overrides.
    lead0 = db.get_lead(lead_id)
    if not url and lead0:
        nct = lead0["nct"] or ""
        url = (db.get_claim_schedule_url(g.user["id"], nct) if nct else "") \
            or db.get_site_calendar_url(g.user["id"])
    if not url:
        flash("Add your booking calendar in Settings first.", "error")
        return redirect(back)
    if _is_placeholder_schedule_url(url):
        flash("That booking link is a demo placeholder.", "error")
        return redirect(back)
    lead = db.set_lead_schedule(lead_id, url)
    if not lead:
        flash("Couldn't find that candidate.", "error")
        return redirect(back)
    # Optionally remember the booking link as the study's default (API/back-compat;
    # the applicant UI no longer sets defaults - Settings owns that).
    if request.form.get("set_default") and lead["nct"]:
        db.set_claim_schedule_url(g.user["id"], lead["nct"], url)
    # Optional per-call video link (only if explicitly provided).
    video = _norm_link(request.form.get("video_url", ""))
    if video and video != (lead["video_url"] or ""):
        db.set_lead_video(lead_id, video)
        if lead["revealed"]:
            db.add_message(lead_id, "site",
                           f"Video call link for your screening visit: {video}")
    flash("Booking link saved on the application. It was not emailed.",
          "success")
    return redirect(back)


def _lead_action_return():
    """Send lead actions back to wherever they were triggered (leads board or
    the message center), defaulting to the leads board."""
    return _safe_next(request.form.get("next", "")) or url_for("leads")


@app.route("/app/leads/<int:lead_id>/message", methods=["POST"])
@login_required
def message_lead(lead_id):
    """Study-team inbox action: send a message without leaving the board."""
    lead = db.get_lead(lead_id)
    back = _lead_action_return()
    if not lead:
        flash("Couldn't find that candidate.", "error")
        return redirect(back)
    if not (db.lead_belongs_to_user(lead_id, g.user["id"]) or _is_owner()):
        abort(403)
    # Sites stay blinded until accept. The operator can write first so a live
    # applicant can be reached without pretending a site already accepted them.
    if not lead["revealed"] and not _is_owner():
        flash("Accept the candidate first to message them.", "error")
        return redirect(back)
    body = request.form.get("body", "").strip()
    if body:
        db.add_message(lead_id, "site", body)
        _deliver_reply(lead, body)
        flash("Message sent.", "success")
    else:
        flash("Write a message first.", "error")
    return redirect(back)


def _copilot_act_deny(token):
    db.mark_copilot_action(token, "canceled")
    return jsonify({"ok": False,
                    "error": "That applicant isn't in your studies."}), 403


@app.route("/app/copilot/digest")
@login_required
def copilot_digest():
    """Bridget's start-of-day brief: only the things that need the coordinator
    today, composed from the same grounded read tools. Read-only."""
    try:
        data = copilot.digest.daily_digest(g.user["id"])
    except Exception:
        app.logger.exception("copilot_digest failed")
        return jsonify({"ok": False, "error": "Couldn't build your digest."}), 500
    return jsonify({"ok": True, **data})


@app.route("/app/copilot/act", methods=["POST"])
@login_required
def copilot_act():
    """Execute a copilot action the user just CONFIRMED in the rail. The target
    is read from the stored proposal (not the client), re-scoped to the team, and
    performed with the same helpers as the manual UI so behavior is identical.
    Confirming is idempotent - a token executes at most once."""
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    edited = data.get("text")
    row, err = copilot.actions.load_valid(g.user["id"], token)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    kind = row["kind"]
    try:
        payload = json.loads(row["payload"] or "{}")
    except (ValueError, TypeError):
        payload = {}

    try:
        if kind == "send_message":
            lead_id = row["lead_id"]
            if not db.lead_belongs_to_user(lead_id, g.user["id"]):
                return _copilot_act_deny(token)
            lead = db.get_lead(lead_id)
            if not lead or not lead["revealed"]:
                return jsonify({"ok": False,
                                "error": "Accept the applicant first to message them."}), 400
            if not db.lead_contactable(lead_id):
                return jsonify({"ok": False,
                                "error": "This applicant opted out of messages."}), 400
            text = ((edited if edited is not None else payload.get("text")) or "").strip()[:4000]
            if not text:
                return jsonify({"ok": False, "error": "The message is empty."}), 400
            db.add_message(lead_id, "site", text)
            _deliver_reply(lead, text)
            db.mark_copilot_action(token, "confirmed")
            lbl = payload.get("label") or "the applicant"
            return jsonify({"ok": True, "answer": f"Sent to {lbl}.",
                            "citations": [{"label": lbl, "url": payload.get("url", "")}]})

        if kind == "send_booking_link":
            lead_id = row["lead_id"]
            if not db.lead_belongs_to_user(lead_id, g.user["id"]):
                return _copilot_act_deny(token)
            lead = db.get_lead(lead_id)
            url = (payload.get("url")
                   or db.get_claim_schedule_url(g.user["id"], lead["nct"])
                   or db.get_site_calendar_url(g.user["id"]))
            if not url:
                return jsonify({"ok": False,
                                "error": "Set your booking calendar in Settings first."}), 400
            db.set_lead_schedule(lead_id, url)
            db.mark_copilot_action(token, "confirmed")
            lbl = payload.get("label") or "the applicant"
            return jsonify({"ok": True,
                            "answer": f"Booking link saved for {lbl}. It was not emailed.",
                            "citations": [{"label": lbl,
                                           "url": payload.get("url_ref", "")}]})

        if kind in ("bulk_booking_reminder", "reconsent_reminder"):
            ids = payload.get("lead_ids") or []
            text = ((edited if edited is not None else payload.get("text")) or "").strip()[:4000]
            if not text:
                return jsonify({"ok": False, "error": "The message is empty."}), 400
            sent = skipped = 0
            for lid in ids:
                if not db.lead_belongs_to_user(lid, g.user["id"]):
                    continue
                lead = db.get_lead(lid)
                if not lead or not lead["revealed"]:
                    continue
                if not db.lead_contactable(lid):     # honor opt-out
                    skipped += 1
                    continue
                db.add_message(lid, "site", text)
                _notify_applicant_message(lead, text)
                sent += 1
            db.mark_copilot_action(token, "confirmed")
            noun = ("re-consent reminders" if kind == "reconsent_reminder"
                    else "booking reminders")
            msg = f"Sent {noun} to {sent} applicant(s)."
            if skipped:
                msg += f" Skipped {skipped} who opted out."
            return jsonify({"ok": True, "answer": msg})

        if kind == "blast":
            # The recipients were frozen into the proposal, so confirming sends
            # to exactly the people the coordinator was shown - not to whatever
            # the filter would match now. Re-checked against the team's scope
            # and the opt-out rail on the way out, same as the manual composer.
            ids = payload.get("lead_ids") or []
            text = ((edited if edited is not None else payload.get("text")) or "").strip()[:4000]
            if not text:
                return jsonify({"ok": False, "error": "The message is empty."}), 400
            sent = skipped = 0
            for lid in ids:
                if not db.lead_belongs_to_user(lid, g.user["id"]):
                    continue
                lead = db.get_lead(lid)
                if not lead or not lead["revealed"]:
                    continue
                if not db.lead_contactable(lid):
                    skipped += 1
                    continue
                db.add_message(lid, "site", text)
                _notify_applicant_message(lead, text)
                sent += 1
            db.create_blast(g.user["id"], payload.get("nct", ""),
                            payload.get("mode", "everyone"),
                            payload.get("value", ""),
                            payload.get("label", ""), text, "", ids)
            db.mark_copilot_action(token, "confirmed")
            msg = f"Sent to {sent} applicant(s)."
            if skipped:
                msg += f" Skipped {skipped} who opted out."
            return jsonify({"ok": True, "answer": msg})

        if kind == "handoff_coverage":
            cover_id = payload.get("cover_user_id")
            away, err = db.start_away(g.user["id"], g.user["id"], cover_id)
            if err:
                return jsonify({"ok": False, "error": err}), 400
            db.mark_copilot_action(token, "confirmed")
            moved = away["moved_leads"] + away["moved_threads"]
            who = payload.get("cover_name") or "your teammate"
            return jsonify({
                "ok": True,
                "answer": (f"{who} is covering for you. {moved} open "
                           f"conversation{'' if moved == 1 else 's'} moved over, "
                           "and new ones will route to them until you're back."),
                "citations": [{"label": "Inbox", "url": url_for("marketing_hub")}],
            })

        return jsonify({"ok": False, "error": "Unknown action."}), 400
    except Exception:
        app.logger.exception("copilot_act failed")
        return jsonify({"ok": False,
                        "error": "That action couldn't be completed."}), 500


# --------------------------------------------------------------------------- #
# @mentions - pulling a teammate into a thread instead of CC-ing them
# --------------------------------------------------------------------------- #
def _mention_flash(people):
    """"Priya was notified." / "Priya and Dan were notified." - so the author
    sees who actually got pulled in, and notices when a handle didn't resolve."""
    names = [p["name"] for p in people]
    if len(names) == 1:
        who, verb = names[0], "was"
    else:
        who = ", ".join(names[:-1]) + " and " + names[-1]
        verb = "were"
    extra = ""
    covers = []
    for p in people:
        away = db.active_away_for(p["user_id"])
        if away:
            covers.append(db.display_name(away["cover_user_id"]))
    if covers:
        extra = f" {covers[0]} was notified too, as cover."
    return f"{who} {verb} notified.{extra}"


def _mention_notify(people, author_name, where, url):
    """Tell each mentioned teammate (and, if they're away, their cover) that they
    were pulled into a thread. Uses the same async notifier as every other
    outbound mail, so it is inert unless NOTIFY_LIVE is on."""
    for p in people:
        targets = [p["user_id"]]
        cover = db.active_away_for(p["user_id"])
        if cover:
            targets.append(cover["cover_user_id"])
        for uid in targets:
            row = db.get_user(uid)
            if not row or not row["email"]:
                continue
            subject = f"{author_name} mentioned you on {where}"
            body = (f"{author_name} pulled you into {where} in BridgeMD.\n\n"
                    f"Open it here: {url}\n")
            _notify_async(row["email"], subject, body)


def _render_mentions(text):
    """Show @handles as chips in a rendered note.

    Server-side so the highlight survives with JS off, and escaped here because
    the filter has to be marked safe to emit the span. Only handles that a
    teammate actually answers to are chipped, so an unresolved "@someone" reads
    as the plain text it is - which is the visible signal that nobody was
    notified."""
    raw = str(text or "")
    known = set()
    try:
        if g.user:
            for m in db.mentionable_members(g.user["id"]):
                known.update(m["handles"])
    except Exception:
        pass

    def _sub(match):
        handle = match.group(1)
        if handle.lower() not in known:
            return escape(match.group(0))
        return Markup('<span class="mention-chip">@%s</span>') % handle

    parts, last = [], 0
    for m in db.MENTION_RE.finditer(raw):
        parts.append(escape(raw[last:m.start()]))
        parts.append(_sub(m))
        last = m.end()
    parts.append(escape(raw[last:]))
    return Markup("").join(parts)


app.jinja_env.filters["mentions"] = _render_mentions


@app.route("/app/leads/<int:lead_id>/note-draft.json", methods=["POST"])
@login_required
def lead_note_draft(lead_id):
    """Bridget turns a thread into a team note, so a coordinator stops retyping
    "called, left voicemail, they asked about travel" from scratch."""
    _ensure_site_access_for_lead(lead_id)
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"ok": False, "error": "not_found"}), 404
    msgs = db.get_messages(lead_id) or []
    last_in = ""
    for m in msgs:
        if m["sender"] == "patient":
            last_in = m["body"] or ""
    return jsonify({
        "ok": True, "human_review_required": True,
        "draft": copilot.drafts.draft("note_summary", {
            "first": (lead["name"] or "This applicant").split()[0],
            "stage": (lead["status"] or "").replace("_", " "),
            "inbound": last_in,
            "messages": [{"inbound": m["sender"] == "patient"} for m in msgs],
            "waiting": bool(db.lead_unread_for_site(lead_id)),
        }),
    })


@app.route("/app/team/mentionable.json")
@login_required
def mentionable_members_json():
    """Roster for the @-picker. Names + the handle that actually resolves, so
    the picker can never insert something the parser will drop."""
    return jsonify({"ok": True, "members": db.mentionable_members(g.user["id"])})


@app.route("/app/mentions")
@login_required
def mentions_page():
    """Everywhere a teammate pulled you in. Opening this page is what clears the
    badge - the mention has been surfaced, so it has been delivered."""
    rows = db.list_mentions(g.user["id"])
    items = []
    for m in rows:
        if m["object_type"] == "lead":
            lead = db.get_lead(m["object_id"])
            if not lead or not db.lead_belongs_to_user(m["object_id"],
                                                       g.user["id"]):
                continue
            title = lead["name"] or lead["title"] or "Applicant"
            sub = lead["title"] or lead["nct"] or ""
            url = url_for("applicant_detail", lead_id=lead["id"])
        else:
            thread = db.get_marketing_thread(g.user["id"], m["object_id"])
            if not thread:
                continue
            title = (thread["contact_name"] or thread["contact_handle"]
                     or "Conversation")
            sub = thread["subject"] or ""
            url = _marketing_thread_url(thread["id"])
        items.append({
            "id": m["id"], "title": title, "sub": sub, "url": url,
            "excerpt": m["excerpt"], "unread": not m["read_at"],
            "author": (m["author_name"] or m["author_email"] or "A teammate"),
            "when": (m["created_at"] or "")[:16],
        })
    db.mark_mentions_read(g.user["id"])
    return render_template("mentions.html", items=items)


# --------------------------------------------------------------------------- #
# Away coverage
# --------------------------------------------------------------------------- #
@app.route("/app/away", methods=["POST"])
@login_required
def away_start():
    """Hand this user's whole open queue to a teammate until they're back."""
    cover_id = request.form.get("cover_user_id", type=int)
    until = (request.form.get("until") or "").strip()
    note = (request.form.get("note") or "").strip()
    back = _safe_next(request.form.get("next", "")) or url_for("marketing_hub")
    if not cover_id:
        flash("Pick a teammate to cover for you.", "error")
        return redirect(back)
    away, err = db.start_away(g.user["id"], g.user["id"], cover_id,
                              until=until, note=note)
    if err:
        flash(err, "error")
        return redirect(back)
    who = db.display_name(cover_id)
    moved = away["moved_leads"] + away["moved_threads"]
    flash(f"{who} is covering for you. {moved} open conversation"
          f"{'' if moved == 1 else 's'} moved over.", "ok")
    return redirect(back)


@app.route("/app/away/end", methods=["POST"])
@login_required
def away_end():
    """Come back early. Everything the cover still holds returns to you."""
    back = _safe_next(request.form.get("next", "")) or url_for("marketing_hub")
    away = db.active_away_for(g.user["id"])
    if not away:
        flash("You don't have coverage turned on.", "error")
        return redirect(back)
    n = db.end_away(away["id"])
    flash(f"Welcome back. {n} conversation{'' if n == 1 else 's'} returned "
          "to you.", "ok")
    return redirect(back)


@app.route("/app/away/seen", methods=["POST"])
@login_required
def away_seen():
    """Dismiss the "what happened while you were out" card."""
    away_id = request.form.get("away_id", type=int)
    if away_id:
        db.mark_away_seen(g.user["id"], away_id)
    return redirect(_safe_next(request.form.get("next", ""))
                    or url_for("marketing_hub"))


# --------------------------------------------------------------------------- #
# Blasts - message a filtered audience inside one study
# --------------------------------------------------------------------------- #
def _blast_args(form):
    """Pull the audience filter off a form or query string, unchanged in shape
    between the preview and the send so the two can never disagree."""
    mode = (form.get("mode") or "everyone").strip()
    if mode not in db.BLAST_MODES:
        mode = "everyone"
    raw = (form.get("ids") or "").strip()
    ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    return (form.get("nct") or "").strip(), mode, (form.get("value") or "").strip(), ids


@app.route("/app/leads/blast/preview.json")
@login_required
def blast_preview():
    """Who a send would actually reach, before it happens. The count here comes
    from the SAME resolver the send uses, so the number on the button is the
    number of people messaged."""
    nct, mode, value, ids = _blast_args(request.args)
    leads, err = db.blast_audience(g.user["id"], nct, mode, value, ids)
    if err:
        return jsonify({"ok": False, "error": err, "count": 0, "sample": []})
    sample = [{
        "name": (l["name"] or "Applicant"),
        "stage": (l["status"] or "").replace("_", " ").title(),
    } for l in leads[:12]]
    warn = ""
    recent = db.leads_blasted_since(g.user["id"], [l["id"] for l in leads])
    if recent:
        warn = (f"{recent} of these were already messaged in the last 24 hours.")
    return jsonify({
        "ok": True, "count": len(leads), "sample": sample,
        "more": max(0, len(leads) - len(sample)),
        "label": db.blast_audience_label(mode, value, len(leads),
                                         db.display_name(_int_or(value))),
        "warning": warn,
    })


def _int_or(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@app.route("/app/leads/blast/draft.json", methods=["POST"])
@login_required
def blast_draft():
    """Bridget drafts the blast body. Human reviews and sends, as always."""
    nct = (request.form.get("nct") or "").strip()
    study = next((c["title"] for c in db.list_study_claims(g.user["id"])
                  if c["nct"] == nct and c["title"]), "") or nct or "the study"
    return jsonify({
        "ok": True, "human_review_required": True,
        "draft": copilot.drafts.draft("blast", {
            "study": study,
            "audience_summary": (request.form.get("label") or "").strip(),
            "count": _int_or(request.form.get("count"), 0),
        }),
    })


# --------------------------------------------------------------------------- #
# Patient portal - one applicant following their own application
# --------------------------------------------------------------------------- #
# The session key is scoped to a single portal token, so signing in to one
# person's portal can never carry over to another. It is deliberately separate
# from the patient_users session (PATIENT_SESSION_KEY): this is not an account,
# it is access to one record.
PORTAL_SESSION_KEY = "portal_token"


def _portal_session_ok(token):
    return session.get(PORTAL_SESSION_KEY) == token


def portal_session_lead_id():
    """The applicant this browser is signed in to via the portal, or None.

    Used by shared endpoints (file downloads) that already serve the study team
    and patient-account holders and now need to recognise a portal visitor too.
    A session still on the temporary password does not count as signed in."""
    token = session.get(PORTAL_SESSION_KEY)
    if not token:
        return None
    row = db.get_portal_by_token(token)
    if not row or row["must_change"]:
        return None
    return row["lead_id"]


def _portal_lead(row):
    """The lead a portal row points at, or None if it went away."""
    return db.get_lead(row["lead_id"]) if row else None


# What a past visit is called on the patient's own page. Deliberately plain:
# "Attended" rather than "Completed", because the patient is the one who
# attended. A visit whose date has passed but which the team never marked up
# stays neutral - the app does not know whether it happened, so it must not say.
_PORTAL_VISIT_STATE = {
    "completed": ("Attended", "ok"),
    "missed": ("Missed", "warn"),
    "cancelled": ("Cancelled", "neutral"),
}


def _portal_visit_when(raw):
    """Split a stored timestamp into (day, month, time, year) for the date tile.

    Kept out of the template because an ISO string wraps mid-value in a narrow
    tile ("2026-08-" / "16"), which reads as broken. The year is returned only
    when it isn't the current one: nobody needs it for next week's appointment,
    but someone scrolling back through last year's visits does."""
    raw = (raw or "").strip()
    ts = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            ts = dt.datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if ts is None:
        return raw[:10], "", raw[11:16], ""
    has_time = " " in raw
    year = "" if ts.year == dt.date.today().year else str(ts.year)
    return str(ts.day), ts.strftime("%b"), (ts.strftime("%H:%M") if has_time else ""), year


def _portal_visit(v):
    """One appointment, shaped for the patient's page."""
    kind = (v["kind"] or "screening")
    status = (_row(v, "status") or "scheduled").strip() or "scheduled"
    label, tone = _PORTAL_VISIT_STATE.get(status, ("", "neutral"))
    prep = [line.strip() for line in (_row(v, "prep") or "").splitlines()
            if line.strip()]
    at = v["visit_at"] or ""
    day, month, time_of_day, year = _portal_visit_when(at)
    return {
        "id": v["id"],
        "at": at,
        # Split for the date tile so it never wraps mid-value the way an ISO
        # string does, and reads the way someone says a date out loud.
        "day": day,
        "month": month,
        "year": year,
        "time": time_of_day,
        "title": (_row(v, "title") or "").strip()
                 or _VISIT_KIND_LABELS.get(kind, kind.title()),
        "location": v["location"] or "",
        "note": v["note"] or "",
        "status": status,
        "state_label": label,
        "state_tone": tone,
        "duration": _row(v, "duration_min", 0) or 0,
        "prep": prep,
    }


def _row(row, key, default=""):
    """Read a column that may predate a migration on an old row."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _portal_view(lead):
    """Everything the portal shows, assembled from the same helpers the study
    team's own screens use - so the patient is never shown a second, drifting
    version of their status."""
    status = (lead["status"] or "prescreen").strip()
    stages = []
    reached = True
    for key in db.LEAD_PIPELINE:
        is_now = key == status
        stages.append({
            "key": key,
            "label": db.LEAD_LABELS.get(key, key.title()),
            "done": reached and not is_now,
            "current": is_now,
        })
        if is_now:
            reached = False
    closed = status in db.LEAD_CLOSED
    visits = [_portal_visit(v) for v in (db.get_visits(lead["id"]) or [])]
    # Upcoming means still going ahead: a cancelled visit next week is not the
    # next appointment, and a visit whose date has passed is history even if the
    # team hasn't marked it up yet.
    _now = db.now()
    upcoming = [v for v in visits
                if v["status"] == "scheduled" and v["at"] >= _now]
    past = [v for v in visits
            if v["status"] != "scheduled" or v["at"] < _now]
    past.reverse()   # most recent first, the way you'd look back through them
    tasks = db.list_tasks(lead["id"]) or []
    return {
        "lead": lead,
        "status": status,
        "status_label": db.LEAD_LABELS.get(status, status.title()),
        "blurb": db.LEAD_BLURB.get(status, ""),
        "stages": ([] if closed else stages),
        "closed": closed,
        "events": db.get_lead_events(lead["id"]),
        "messages": db.get_messages(lead["id"]),
        "visits": visits,
        "upcoming": upcoming,
        "past_visits": past,
        "next_visit": (upcoming[0] if upcoming else None),
        "tasks": [t for t in tasks if t["assigned_to"] == "patient"],
        "doc_requests": db.list_doc_requests(lead["id"]) or [],
        "files": db.list_attachments(lead["id"]) or [],
    }


@app.route("/portal/<token>", methods=["GET"])
def portal(token):
    """The portal itself, or the password wall in front of it.

    Nothing identifying is rendered before sign-in: an unauthenticated visitor
    sees only a generic prompt, never the person's name, their trial, or their
    status. A wrong or revoked link is indistinguishable from a right one until
    the password is correct."""
    row = db.get_portal_by_token(token)
    if not row:
        return render_template("portal_login.html", token=token,
                               gone=True), 410
    if not _portal_session_ok(token):
        return render_template("portal_login.html", token=token, gone=False)
    lead = _portal_lead(row)
    if not lead:
        session.pop(PORTAL_SESSION_KEY, None)
        return render_template("portal_login.html", token=token,
                               gone=True), 410
    if row["must_change"]:
        return render_template("portal_password.html", token=token,
                               first_time=True)
    db.portal_touch(token)
    db.mark_thread_read(lead["id"], "patient")
    return render_template("portal.html", token=token, **_portal_view(lead))


@app.route("/portal/<token>/login", methods=["POST"])
def portal_login(token):
    blocked = _guard_ip_rate_limit("portal_login", template_name=
                                   "portal_login.html", token=token, gone=False)
    if blocked:
        return blocked
    row, err = db.portal_check_password(
        token, request.form.get("password", ""), check_password_hash)
    if err:
        # Rendered inline rather than flashed: a toast that fades after a few
        # seconds is the wrong pattern for the door itself - someone who looks
        # away is left staring at a form with no idea why it didn't work.
        return render_template("portal_login.html", token=token, gone=False,
                               error=err), 401
    session[PORTAL_SESSION_KEY] = token
    return redirect(url_for("portal", token=token))


@app.route("/portal/<token>/password", methods=["POST"])
def portal_change_password(token):
    """Set a new password. Required on first sign-in, available any time after -
    the credential the recruiter read out loud should not stay valid."""
    if not _portal_session_ok(token):
        return redirect(url_for("portal", token=token))
    row = db.get_portal_by_token(token)
    if not row:
        abort(410)
    new = request.form.get("password", "")
    if new != request.form.get("confirm", ""):
        flash("Those passwords don't match.", "error")
        return redirect(url_for("portal", token=token))
    ok, err = db.portal_set_password(token, new, _hash_password)
    if not ok:
        flash(err, "error")
        return redirect(url_for("portal", token=token))
    flash("Password updated. This is now the only password for this link.",
          "success")
    return redirect(url_for("portal", token=token))


@app.route("/portal/<token>/message", methods=["POST"])
def portal_message(token):
    """Patient replies into the SAME thread the study team already works from,
    so an answer here lands in the recruiter's inbox rather than a side channel."""
    if not _portal_session_ok(token):
        return redirect(url_for("portal", token=token))
    row = db.get_portal_by_token(token)
    lead = _portal_lead(row)
    if not row or not lead or row["must_change"]:
        abort(410)
    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Write a message first.", "error")
        return redirect(url_for("portal", token=token) + "#messages")
    db.add_message(lead["id"], "patient", body[:4000])
    flash("Sent to your study team.", "success")
    return redirect(url_for("portal", token=token) + "#messages")


@app.route("/portal/<token>/task/<int:task_id>", methods=["POST"])
def portal_toggle_task(token, task_id):
    """Tick off one of the to-dos the study team assigned."""
    if not _portal_session_ok(token):
        return redirect(url_for("portal", token=token))
    row = db.get_portal_by_token(token)
    lead = _portal_lead(row)
    if not row or not lead or row["must_change"]:
        abort(410)
    task = db.get_task(task_id)
    # Scope the write to THIS applicant - a task id from another record must not
    # be reachable just because this portal session is valid.
    if not task or task["lead_id"] != lead["id"]:
        abort(404)
    db.set_task_status(task_id,
                       "open" if (task["status"] or "") == "done" else "done")
    return redirect(url_for("portal", token=token) + "#todos")


@app.route("/portal/<token>/signout", methods=["POST"])
def portal_signout(token):
    session.pop(PORTAL_SESSION_KEY, None)
    flash("Signed out.", "success")
    return redirect(url_for("portal", token=token))


def _portal_invite_text(link, password):
    """The message the applicant actually receives.

    Delivery has to go through the thread: it is the one channel the applicant
    is already reading, and a credential the coordinator merely copies to their
    own clipboard reaches nobody. The password is safe to leave in the history
    because it dies the moment it is used - the portal forces a rotation on
    first sign-in - so what remains in the thread is a spent token plus a link
    the applicant will need again later anyway."""
    return ("You can now follow your application on your own private page.\n\n"
            f"Open it here: {link}\n"
            f"Temporary password: {password}\n\n"
            "You'll be asked to pick your own password the first time you sign "
            "in, and this temporary one stops working straight after. Only you "
            "should use this link - please don't forward it.")


# --- Study-team side: hand a patient access ---------------------------------
@app.route("/app/leads/<int:lead_id>/portal", methods=["POST"])
@login_required
def issue_portal(lead_id):
    """Create or re-issue portal access for one applicant.

    Two deliveries, on purpose: the invite is posted into the applicant's own
    thread (the only channel they actually read), and the same credential is
    shown to the coordinator once so they can repeat it on a call if asked. Only
    the hash is stored, so it can never be shown again - only reissued."""
    _ensure_site_access_for_lead(lead_id)
    # Land back where the coordinator issued from (inbox panel or applicant
    # page). The one-time password only renders on that next view, so the
    # redirect target must be the page that shows portal_reveal.
    back = (_safe_next(request.form.get("next", ""))
            or (url_for("applicant_detail", lead_id=lead_id) + "#portal"))
    lead = db.get_lead(lead_id)
    if not lead:
        flash("Couldn't find that applicant.", "error")
        return redirect(url_for("leads"))
    if not lead["revealed"]:
        flash("Accept the applicant first, then you can give them portal "
              "access.", "error")
        return redirect(back)
    row, pw = db.issue_portal_access(lead_id, g.user["id"], _hash_password)
    link = url_for("portal", token=row["token"], _external=True)

    invite = _portal_invite_text(link, pw)
    db.add_message(lead_id, "site", invite)
    _notify_applicant_message(lead, invite)

    # Also carried in the session (not the URL, so it never lands in a browser
    # history entry or a server log) so the coordinator can read it back to
    # someone on the phone without reissuing.
    session["portal_reveal"] = {"lead_id": lead_id, "link": link, "password": pw}
    flash("Portal invite sent to the applicant's thread.", "success")
    return redirect(back)


@app.route("/app/leads/<int:lead_id>/portal/revoke", methods=["POST"])
@login_required
def revoke_portal(lead_id):
    _ensure_site_access_for_lead(lead_id)
    back = (_safe_next(request.form.get("next", ""))
            or (url_for("applicant_detail", lead_id=lead_id) + "#portal"))
    if db.revoke_portal_access(lead_id):
        flash("Portal access revoked. That link no longer works.", "success")
    else:
        flash("That applicant doesn't have portal access.", "error")
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


@app.route("/app/leads/<int:lead_id>/note", methods=["POST"])
@login_required
def add_lead_note(lead_id):
    """Add an internal, team-only note to a candidate (never sent to the patient)."""
    _ensure_site_access_for_lead(lead_id)
    back = _lead_action_return()
    lead = db.get_lead(lead_id)
    if not lead:
        flash("Couldn't find that candidate.", "error")
        return redirect(back)
    body = request.form.get("body", "").strip()
    if not body:
        flash("Write a note first.", "error")
        return redirect(back)
    author = (db.display_name(g.user["id"]) or "Team") if g.user else "Team"
    note_id = db.add_note(lead_id, body, author=author,
                          author_user_id=(g.user["id"] if g.user else None))
    # @mentions replace CC-ing a teammate: they get pulled into THIS thread
    # instead of onto an email chain, and ownership doesn't change hands.
    people = db.record_mentions(g.user["id"], "lead", lead_id, "lead_note",
                                note_id, body)
    if people:
        _mention_notify(people, author, f"{lead['name'] or 'an applicant'}",
                        url_for("applicant_detail", lead_id=lead_id,
                                _external=True))
        flash("Note added. " + _mention_flash(people), "success")
    else:
        flash("Note added.", "success")
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
    """Fan a message (and optional task/document) out to an audience inside ONE
    trial, delivered to each recipient's private thread - never a shared room
    (who else is enrolled is PHI).

    The audience used to be "every accepted applicant in the study" and nothing
    else. It is now resolved by db.blast_audience, which can narrow to a stage, a
    tag, an owner, quiet applicants, or a hand-picked selection - while applying
    the same non-negotiable rails it always did (verified claim, accepted, not
    closed, not opted out). Every path into this loop goes through that resolver,
    so the single-study rule cannot be routed around."""
    body = request.form.get("body", "").strip()
    task_title = request.form.get("task", "").strip()
    back = _safe_next(request.form.get("next", "")) or url_for("leads")
    scope_nct, mode, value, picked = _blast_args(request.form)
    saved = _save_upload(request.files.get("file"))
    if not (body or task_title or saved):
        flash("Add a message, a to-do, or a document to send.", "error")
        return redirect(back)
    leads, err = db.blast_audience(g.user["id"], scope_nct, mode, value, picked)
    if err:
        flash(err, "error")
        return redirect(back)
    if not leads:
        flash("Nobody matches that audience right now.", "error")
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
    label = db.blast_audience_label(mode, value, len(leads),
                                    db.display_name(_int_or(value)))
    # Recorded so the send is auditable and so the composer can warn about
    # double-messaging the same people tomorrow.
    db.create_blast(g.user["id"], scope_nct, mode, value, label, body,
                    task_title, [ld["id"] for ld in leads])
    flash(f"Blast sent to {len(leads)} applicant"
          f"{'s' if len(leads) != 1 else ''} ({label}).", "success")
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
    # A portal visitor is signed in to exactly one applicant, so they may open
    # that applicant's documents and no one else's.
    portal_ok = portal_session_lead_id() == lead["id"]
    if not (site_ok or patient_ok or portal_ok):
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


@app.route("/app/messages")
@login_required
def patient_inbox():
    """Legacy inbox URL. Messaging is now consolidated: 1:1 threads live on the
    applicant detail page, and the cross-applicant "needs reply" queue lives on
    the Dashboard. Kept as a redirect so old links/bookmarks don't 404."""
    lead_id = request.args.get("lead_id", type=int)
    if lead_id:
        return redirect(url_for("applicant_detail", lead_id=lead_id))
    return redirect(url_for("marketing_hub"))


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
    if url and _is_placeholder_schedule_url(url):
        flash("That booking link is a demo placeholder.", "error")
        return redirect(url_for("candidate_page", token=lead["site_token"]))
    lead = db.set_lead_schedule(lead["id"], url)
    if url:
        flash("Booking link saved. It was not emailed to the applicant.", "ok")
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
        msg = f"Application moved to \"{db.LEAD_LABELS.get(status, status)}\"."
        # Early withdrawal: if a completion rule prorates, queue the partial
        # stipend for the visits the participant actually completed.
        if status == "withdrawn":
            lead = db.get_lead(lead_id)
            done = _maybe_queue_completion_payment(lead, withdrawn=True) \
                if lead else None
            if done:
                msg += (f" {payments_mod.format_cents(done['amount_cents'], done['currency'])}"
                        " prorated stipend queued.")
        flash(msg, "success")
    else:
        flash("Couldn't update that application.", "error")
    return redirect(_lead_action_return())


@app.route("/googlea0e509518fe5134f.html")
def google_site_verification():
    """Google Search Console ownership check (HTML-file method). These files
    always contain exactly one line: 'google-site-verification: <filename>'."""
    return app.response_class(
        "google-site-verification: googlea0e509518fe5134f.html\n",
        mimetype="text/html")


@app.route("/robots.txt")
def robots():
    body = ("User-agent: *\n"
            "Disallow: /a/\n"
            "Disallow: /c/\n"
            "Allow: /\nSitemap: "
            + url_for("sitemap", _external=True) + "\n")
    return app.response_class(body, mimetype="text/plain")


@app.route("/favicon.ico")
def favicon_ico():
    # Crawlers (Google included) and browsers probe the ROOT /favicon.ico by
    # convention, not only the <link rel="icon"> tags in the head. We only had
    # the icons under /static, so the root path 404'd - which makes search
    # engines keep showing a stale/cached icon. Serve it here so the current
    # favicon is reliably discovered.
    return send_file(os.path.join(app.static_folder, "favicon.ico"),
                     mimetype="image/vnd.microsoft.icon")


@app.route("/apple-touch-icon.png")
@app.route("/apple-touch-icon-precomposed.png")
def apple_touch_icon_root():
    # iOS / link-preview crawlers probe these root paths too.
    return send_file(os.path.join(app.static_folder, "apple-touch-icon.png"),
                     mimetype="image/png")


def _sitemap_loc(endpoint, **kw):
    return _abs_url(endpoint, **kw) if PUBLIC_BASE_URL \
        else url_for(endpoint, _external=True, **kw)


def _week_lastmod():
    """Monday of the current week (UTC). Programmatic pages refresh continuously
    but not every single day, so a weekly stamp is an honest freshness signal
    that doesn't shout 'changed!' on every crawl (which trains Google to ignore
    lastmod). Study pages keep their real last-seen date instead."""
    now = time.time()
    monday = now - (time.gmtime(now).tm_wday * 86400)
    return time.strftime("%Y-%m-%d", time.gmtime(monday))


def _sitemap_xml(urls):
    items = "".join(
        f"<url><loc>{u}</loc><lastmod>{lm}</lastmod></url>" for u, lm in urls)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + items + "</urlset>")
    return app.response_class(xml, mimetype="application/xml")


@app.route("/sitemap.xml")
def sitemap():
    """Sitemap INDEX -> per-section child sitemaps, so Search Console reports
    indexing coverage separately for static, condition, city, and study pages
    (much easier to diagnose than one ~9k-URL blob)."""
    wk = _week_lastmod()
    children = [_sitemap_loc(e) for e in
                ("sitemap_static", "sitemap_conditions",
                 "sitemap_cities", "sitemap_studies")]
    items = "".join(
        f"<sitemap><loc>{c}</loc><lastmod>{wk}</lastmod></sitemap>"
        for c in children)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + items + "</sitemapindex>")
    return app.response_class(xml, mimetype="application/xml")


@app.route("/sitemap-static.xml")
def sitemap_static():
    wk = _week_lastmod()
    urls = [(_sitemap_loc(e), wk) for e in
            ("home", "find", "trials_index", "how_it_works", "for_sites")]
    urls += [(_sitemap_loc("for_sites_feature", slug=f["slug"]), wk)
             for f in sites_features.nav_items()]
    return _sitemap_xml(urls)


@app.route("/sitemap-conditions.xml")
def sitemap_conditions():
    wk = _week_lastmod()
    urls = [(_sitemap_loc("condition_page", slug=slugify(c)), wk)
            for c in SEO_CONDITIONS]
    return _sitemap_xml(urls)


@app.route("/sitemap-cities.xml")
def sitemap_cities():
    """Only condition/city pages with CONFIRMED local recruiting trials. A page
    with no local trials self-noindexes, so listing the full condition x city
    grid just feeds Google thousands of 'discovered - currently not indexed'
    URLs and drags down the whole surface. Populated as pages render + by the
    /seo/warm cron (below); empty until then, which is the correct default."""
    wk = _week_lastmod()
    urls = []
    try:
        for r in db.list_local_seo_city_pages():
            lm = (r["updated_at"] or "")[:10] or wk
            urls.append((_sitemap_loc("condition_city_page", slug=r["slug"],
                                      city_slug=r["city_slug"]), lm))
    except Exception:
        app.logger.exception("sitemap city list failed")
    return _sitemap_xml(urls)


@app.route("/sitemap-studies.xml")
def sitemap_studies():
    """Per-study pages, scoped to trials surfaced by our condition/city pages.
    These keep their real last-seen date (recruiting status is time-sensitive)."""
    today = time.strftime("%Y-%m-%d")
    urls = []
    try:
        for s in db.list_seo_studies(limit=5000):
            lm = ((s["updated_at"] or today)[:10]) or today
            urls.append((_sitemap_loc("study_page", nct=s["nct"]), lm))
    except Exception:
        app.logger.exception("sitemap study list failed")
    return _sitemap_xml(urls)


@app.route("/seo/warm")
def seo_warm():
    """Warm the condition/city SEO surface in batches so sitemap-cities.xml can
    list only pages with real local trials. Each pair hits CT.gov once (cached),
    so process a slice per call and let a cron page through with ?offset=.
    Keyed by ALERTS_CRON_KEY when set, else no-login/testing only.

    Params: ?limit=100 (pairs per call), ?offset=0. Returns next_offset until
    done so a caller can loop: /seo/warm?key=..&offset=0, then next_offset, ..."""
    key = os.environ.get("ALERTS_CRON_KEY", "").strip()
    if key:
        if request.args.get("key", "") != key:
            abort(403)
    elif not NO_LOGIN:
        abort(403)
    pairs = [(c, city) for c in SEO_CONDITIONS for city in SEO_CITIES]
    total = len(pairs)
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 500))
    except (TypeError, ValueError):
        limit = 100
    # Self-advancing mode (?cursor=1): the endpoint remembers where it left off in
    # a persisted cursor and processes the NEXT slice each call, wrapping to 0 when
    # the whole grid is done. This lets a stateless external scheduler (cron-job.org
    # etc.) walk the entire condition x city grid by pinging ONE url on a schedule -
    # no loop / offset bookkeeping needed on the caller side.
    use_cursor = request.args.get("cursor", "").strip().lower() in ("1", "true", "yes")
    if use_cursor:
        try:
            offset = max(0, int(db.get_kv("seo_warm_cursor", 0) or 0)) % max(1, total)
        except (TypeError, ValueError):
            offset = 0
    else:
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
    processed = local = 0
    for c, city in pairs[offset:offset + limit]:
        try:
            trials, is_local = _seo_trials(c, city=city)
            if is_local and trials:
                local += 1
        except Exception:
            app.logger.exception("seo warm failed for %s / %s", c, city)
        processed += 1
    next_raw = offset + processed
    done = next_raw >= total
    if use_cursor:
        # Wrap back to the start when we reach the end so the grid keeps refreshing.
        try:
            db.set_kv("seo_warm_cursor", 0 if done else next_raw)
        except Exception:
            app.logger.exception("seo warm cursor save failed")
    return jsonify({
        "processed": processed, "local_in_batch": local,
        "offset": offset, "next_offset": None if done else next_raw,
        "cursor": (0 if done else next_raw) if use_cursor else None,
        "wrapped": bool(use_cursor and done),
        "total": total, "done": done})


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


def _zippopotam_place(country, code):
    """Postal code -> "City, ST" from zippopotam.us, or "" when unknown."""
    try:
        req = urllib.request.Request(
            f"https://api.zippopotam.us/{country}/{urllib.parse.quote(code)}",
            headers={"User-Agent": "BridgeMD/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        p = (data.get("places") or [None])[0]
        if p:
            # Canadian FSAs come back as neighbourhood lists in parentheses;
            # keep the recognisable part ("Downtown Toronto").
            city = (p.get("place name") or "").split(" (")[0].strip()
            st = (p.get("state abbreviation") or p.get("state") or "").strip()
            return ", ".join(x for x in (city, st) if x)
    except Exception:
        pass
    return ""


def display_area(location):
    """A place a person recognises. A bare postal code ("33029") becomes the
    city it belongs to ("Pembroke Pines, FL"); a place with a trailing US ZIP
    loses the ZIP. Anything else is returned as typed. Study teams and the
    operator read this in every email, so it must read like a place, not a
    code."""
    loc = (location or "").strip()
    if not loc:
        return ""
    up = loc.upper()
    m = _CA_POSTAL.match(up)
    if m:
        return _zippopotam_place("CA", m.group(1)) or loc
    if _CA_FSA.match(up):
        return _zippopotam_place("CA", up) or loc
    if _US_ZIP.match(up):
        return _zippopotam_place("US", up) or loc
    return _re.sub(r"\s+\d{5}(?:-\d{4})?$", "", loc).strip(" ,")


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
    """A site the patient could actually enroll at now or very soon (recruiting,
    not-yet-recruiting, or unlabeled)."""
    return (s.get("status") or "").upper() in (
        "", "RECRUITING", "NOT_YET_RECRUITING")


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
    match = match or {}
    # Placeholder reads (e.g. an un-assessed condition search) carry no LLM
    # judgment to police, and their verdict ("unscored") is deliberately not one
    # of the three real bands - so skip normalization/gating, which would flatten
    # it back to "possible" and re-introduce the misleading "you might qualify".
    if match.get("skip_quality"):
        out = dict(match)
        out.setdefault("verdict", "unscored")
        out.setdefault("score", 0)
        for k in ("met", "not_met", "unknown"):
            out.setdefault(k, [])
        out["quality_score"] = 100
        out["quality_flags"] = []
        out["quality_ok"] = True
        return out
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
def _match_snippet(trial, terms, kind, window=85):
    """For a structured drug/condition search, find where the searched term
    literally appears in a trial and return a short highlighted excerpt plus a
    label for WHICH part of the record it came from. That label doubles as a
    role signal: a drug in the "Study treatment" is very different from one that
    only shows up under "Eligibility requirement" (you must already take it).

    Returns {'field', 'before', 'match', 'after'} (each escaped separately by the
    template) or None when the term isn't literally mentioned. `terms` are the
    raw query terms; `kind` is "drug" or "condition"."""
    terms = sorted({(t or "").strip().lower() for t in (terms or [])
                    if t and len(str(t).strip()) >= 3}, key=len, reverse=True)
    if not terms:
        return None

    def _first_hit(text):
        if not text:
            return None
        low = text.lower()
        best = None
        for t in terms:
            i = low.find(t)
            if i != -1 and (best is None or i < best[0]):
                best = (i, i + len(t))
        return best

    # Ordered (label, text) candidates. Drug searches lead with the treatment
    # arms (where the drug's role lives); condition searches lead with the
    # conditions the study is actually about.
    iv_field = []
    for iv in (trial.get("interventions") or []):
        nm = (iv.get("name") or "").strip()
        joined = ", ".join([nm] + [o for o in (iv.get("otherNames") or []) if o])
        if joined:
            iv_field.append(("Study treatment", joined))
    cond_txt = "; ".join(c for c in (trial.get("conditions") or []) if c)
    cond_field = [("Condition studied", cond_txt)] if cond_txt else []
    title_field = [("Study title", trial.get("title") or "")]
    crit_field = [("Eligibility requirement", trial.get("criteria") or "")]
    summ_field = [("What it's testing", trial.get("briefSummary") or "")]

    if kind == "drug":
        cands = iv_field + crit_field + title_field + summ_field + cond_field
    else:
        cands = cond_field + title_field + iv_field + crit_field + summ_field

    for label, text in cands:
        hit = _first_hit(text)
        if not hit:
            continue
        s, e = hit
        start, end = max(0, s - window), min(len(text), e + window)
        if start > 0:
            sp = text.find(" ", start, s)
            if sp != -1:
                start = sp + 1
        if end < len(text):
            sp = text.rfind(" ", e, end)
            if sp != -1:
                end = sp
        before = ("… " if start > 0 else "") + text[start:s]
        after = text[e:end] + (" …" if end < len(text) else "")
        # Collapse whitespace so criteria (often full of newlines) reads cleanly.
        before = " ".join(before.split())
        after = " ".join(after.split())
        return {"field": label, "before": before,
                "match": text[s:e], "after": after}
    return None


# Second registry (ISRCTN) toggle. On by default; set ISRCTN_ENABLED=0 to disable
# if it ever adds latency without value for your patient base.
ISRCTN_ENABLED = os.environ.get("ISRCTN_ENABLED", "1") != "0"


def run_search(note, condition, country, require_site, coords=None, radius=50,
               unit="km", interventional_only=True, intervention="", assess=True,
               evidence_terms=None, evidence_kind="", broad_term="",
               location_only=False):
    """Fetch -> hard-gate -> LLM-score, ranked by relevance then distance.
    `radius`/`unit` define the geographic limit. By default only interventional
    (treatment) trials are kept - a doctor refers for therapy, not to a registry.
    `intervention` searches by drug name (e.g. "semaglutide") instead of/along
    with a condition.

    `assess` controls whether we run the per-trial LLM eligibility read. Only set
    it when the searcher actually described their situation - a bare condition
    search has no patient facts to judge specific inclusion criteria against, so
    scoring it just manufactures misleading "not a fit" verdicts. When False,
    every match shows the neutral "you might qualify" read instead.

    `broad_term`, if given, adds a ClinicalTrials.gov full-text (query.term) pass
    for that phrase - used for vague/symptom searches so trials that mention the
    symptom in their eligibility criteria surface even when it isn't the trial's
    condition label. Results are unioned with the condition search.

    Returns (search_label, results)."""
    profile = mt.patient_profile(note)
    # Which condition term(s) to search. A clean typed condition is used as-is.
    # When the searcher only described a situation/symptom (no clean term), we ask
    # the LLM for SEVERAL candidate conditions, not one: "trouble sleeping"
    # legitimately spans insomnia, sleep apnea, restless legs, etc. Searching the
    # union (and letting the per-trial read judge fit) avoids overfitting to a
    # single guess.
    cond_terms = []
    if condition:
        cond_terms = [condition]
    elif not intervention and not location_only and mt.LLM_API_KEY:
        try:
            raw = mt.llm_chat(
                "You map a patient's described situation or symptom to the "
                "medical conditions a clinical-trial search should cover. The "
                "description may be vague or a symptom that fits several "
                "conditions - list ALL the distinct conditions worth searching, "
                "most likely first, not just one. Reply with 1-4 condition names, "
                "comma-separated, no other text.", note[:4000]).strip()
            cond_terms = [c.strip(" .").strip()
                          for c in raw.split(",") if c.strip(" .").strip()][:4]
        except Exception:
            cond_terms = []
    search_label = (cond_terms[0] if cond_terms else "") or intervention
    if not search_label and not broad_term and not location_only:
        return "", []

    geo = None
    if coords and radius:
        geo = f"distance({coords[0]},{coords[1]},{int(radius)}{unit})"

    def _union_fetch():
        """Merge condition search(es) + optional full-text pass, deduped by NCT."""
        out, seen = [], set()

        def _absorb(fetched):
            for t in fetched:
                nid = t.get("nctId")
                if nid and nid in seen:
                    continue
                if nid:
                    seen.add(nid)
                out.append(t)

        if intervention:
            _absorb(mt.fetch_trials("", max_n=200, geo=geo,
                                    intervention=intervention))
        elif len(cond_terms) > 1:
            per = max(60, 200 // len(cond_terms))
            for term in cond_terms:
                _absorb(mt.fetch_trials(term, max_n=per, geo=geo))
                if len(out) >= 200:
                    break
        elif search_label:
            _absorb(mt.fetch_trials(search_label, max_n=200, geo=geo))
        elif location_only and geo:
            # Location-only browse: every recruiting trial with a site in range.
            _absorb(mt.fetch_trials("", max_n=200, geo=geo))
        # Full-text pass (eligibility criteria etc.) for vague/symptom searches.
        if broad_term and len(out) < 200:
            _absorb(mt.fetch_trials("", max_n=200 - len(out), geo=geo,
                                    term=broad_term))
        # Second registry: ISRCTN (UK/international) for breadth. Deduped by NCT
        # against the CT.gov set above (ISRCTN records that cross-reference an NCT
        # collapse into the richer CT.gov record). ISRCTN sites have no lat/lon,
        # so these naturally fall out of geo-scoped ("near me") searches and only
        # surface on country-level / no-location searches - keeping local quality
        # high while still adding global completeness.
        if ISRCTN_ENABLED and len(out) < 200:
            isr_term = intervention or (cond_terms[0] if cond_terms else search_label)
            if isr_term:
                try:
                    _absorb(mt.fetch_isrctn(
                        condition="" if intervention else isr_term,
                        intervention=intervention,
                        limit=min(40, 200 - len(out))))
                except Exception:
                    app.logger.exception("ISRCTN fetch failed")
        return out[:200]

    try:
        trials = _union_fetch()
    except Exception:
        raise RuntimeError(
            "Couldn't reach ClinicalTrials.gov right now. Please try again "
            "in a moment.")

    # Only genuinely joinable studies: recruiting or opening soon (belt-and-
    # suspenders on top of the API filter), and interventional by default.
    _OPEN = ("RECRUITING", "NOT_YET_RECRUITING")
    trials = [t for t in trials
              if (t.get("overallStatus") or "RECRUITING").upper() in _OPEN]
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
    patient_tokens = set()
    try:
        for _term in (cond_terms or [search_label]):
            if _term:
                patient_tokens |= codes.condition_tokens(_term)
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
        recruiting = [s for s in t.get("locations", []) if _is_active_site(s)]
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

        evidence = (_match_snippet(t, evidence_terms, evidence_kind)
                    if evidence_terms else None)
        return {"trial": t, "match": m, "site": site,
                "site_str": _site_str(site),
                "site_maps_url": _maps_url(site),
                "site_dir_url": _maps_dir_url(site),
                "coordinator": _coordinator(t, site),
                "public_contacts": _public_contacts(t, site),
                "distance": dist, "unit": unit, "relevance": _rel(t),
                "evidence": evidence,
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

    def _neutral_match():
        # No patient detail was provided, so we genuinely can't judge fit either
        # way. Say that honestly instead of implying "you might qualify".
        return {"verdict": "unscored", "score": 0, "skip_quality": True,
                "rationale": ("Recruiting and matches your search"
                              + (" near you." if coords else ".")
                              + " We haven't checked your eligibility - the study"
                              " team confirms whether you qualify after you apply."),
                "met": [], "not_met": [], "unknown": [],
                "quality_score": 100, "quality_flags": [], "quality_ok": True}

    # Only spend the (expensive, and here meaningless) per-trial eligibility read
    # when we have real patient detail to assess. Otherwise everything falls to
    # the neutral "you might qualify" read so a plain condition search never
    # tells people they're "probably not a fit" based on info they never gave.
    llm_picks = picks[:MAX_MATCH] if assess else []
    light_picks = picks[MAX_MATCH:] if assess else picks

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

    # The rest of the matches - shown too, ranked below via rank_key. When we
    # never assessed (a bare condition search), everything is "unscored" so we
    # don't imply a fit we can't stand behind.
    for t, near in light_picks:
        results.append(_build_result(t, near,
                                     _light_match() if assess else _neutral_match()))

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
    out, seen = [], {}
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
        # Dedup by the person first: CT.gov commonly lists the same named
        # contact twice - once with only a phone, once with the email - which
        # would otherwise render as two cards for one person. Key a real name by
        # the name so those merge; only fall back to email/phone when the contact
        # is anonymous ("Study contact").
        digits = re.sub(r"\D", "", phone)
        if name.lower() not in ("", "study contact"):
            key = ("name", name.lower())
        elif email:
            key = ("email", email.lower())
        else:
            key = ("phone", digits)
        if key in seen:
            existing = seen[key]  # backfill anything the first copy was missing
            if not existing["email"] and email:
                existing["email"] = email
            if not existing["phone"] and phone:
                existing["phone"] = phone
            if not existing["role"] and role:
                existing["role"] = role
            return
        rec = {"name": name, "email": email, "phone": phone, "role": role}
        seen[key] = rec
        out.append(rec)

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


_MAX_CLINIC_NOTIFY = 8
_PI_ROLES = ("PRINCIPAL_INVESTIGATOR", "PRINCIPAL-INVESTIGATOR",
             "PRINCIPAL INVESTIGATOR")


def _contact_email(c):
    return ((c or {}).get("email") or "").strip()


def _contact_role(c):
    return ((c or {}).get("role") or "").upper().replace("-", "_").replace(" ", "_")


def _is_pi_contact(c):
    role = _contact_role(c)
    if role in _PI_ROLES:
        return True
    return role.endswith("_INVESTIGATOR") and "SUB" not in role


def clinic_emails_from_site(site, skip_emails=None, include_pi=True):
    """Emails listed on one CT.gov facility. Coordinators first, then PIs."""
    skip = {e.lower() for e in (skip_emails or []) if e}
    contacts = list((site or {}).get("contacts") or [])
    picked, seen = [], set()

    def _take(c):
        email = _contact_email(c)
        key = email.lower()
        if not email or key in skip or key in seen:
            return
        seen.add(key)
        picked.append({
            "email": email,
            "name": _clean_name(c.get("name")) if c.get("name") else "",
            "role": (c.get("role") or "").strip(),
            "facility": (site or {}).get("facility") or "",
            "city": (site or {}).get("city") or "",
            "source": "facility",
        })

    for c in contacts:
        if _is_pi_contact(c):
            continue
        _take(c)
    if include_pi:
        for c in contacts:
            if _is_pi_contact(c):
                _take(c)
    return picked


def _site_matches_label(site, label):
    label = (label or "").strip().lower()
    if not label:
        return False
    facility = ((site or {}).get("facility") or "").strip().lower()
    full = _site_str(site).lower()
    return (facility and facility in label) or (full and (full == label or full in label or label in full))


def sites_for_patient_area(trial, site_label="", lat=None, lon=None,
                           radius=None, unit="km"):
    """Recruiting facilities for this apply, nearest first.

    Uses the site string captured at search time, then other recruiting sites
    inside the patient's radius. Does not return every location on the trial.
    """
    locations = [s for s in (trial or {}).get("locations") or [] if _is_active_site(s)]
    if not locations:
        return []
    has_geo = lat is not None and lon is not None
    if has_geo:
        ranked = []
        for s in locations:
            if s.get("lat") is None or s.get("lon") is None:
                continue
            d = haversine(lat, lon, s["lat"], s["lon"], unit)
            if radius and d > radius:
                continue
            ranked.append({**s, "distance": d})
        ranked.sort(key=lambda x: x["distance"])
        if ranked:
            locations = ranked
    labeled = [s for s in locations if _site_matches_label(s, site_label)]
    if labeled:
        rest = [s for s in locations if s not in labeled]
        return labeled + rest
    return locations


def resolve_clinic_notify_recipients(lead, trial=None, *, claimed_email="",
                                     posted_email="", fallback="", lat=None,
                                     lon=None, radius=None, unit="km",
                                     max_clinics=_MAX_CLINIC_NOTIFY,
                                     lookup_fn=None):
    """Who gets the on-apply email: local clinic / PI addresses, not the sponsor.

    CT.gov facility emails first. If a nearby site only lists a PI name, look
    up the clinic's public recruitment email. Sponsor/central inboxes are
    skipped. If nothing local is found, use fallback.
    """
    def _one(email, facility="", source="", name="", city="", role=""):
        email = (email or "").strip()
        if not email:
            return None
        return {"email": email, "facility": facility or "", "source": source,
                "name": name or "", "city": city or "", "role": role or ""}

    site_label = ""
    try:
        site_label = (lead["site"] or "").strip()
    except (KeyError, IndexError, TypeError):
        site_label = ""

    clinic, seen = [], set()

    def _add(bucket, rec):
        if not rec:
            return
        key = rec["email"].lower()
        if key in seen:
            return
        seen.add(key)
        bucket.append(rec)

    sponsor = ""
    try:
        sponsor = (trial or {}).get("leadSponsor") or ""
    except (KeyError, IndexError, TypeError):
        sponsor = ""
    if lookup_fn is None and os.environ.get("CLINIC_LOOKUP", "1") != "0":
        lookup_fn = lambda site, sponsor=sponsor: clinic_lookup.lookup_site_emails(
            site, sponsor=sponsor)

    lookup_tries = 0
    for site in sites_for_patient_area(
            trial, site_label=site_label, lat=lat, lon=lon,
            radius=radius, unit=unit or "km"):
        recs = clinic_emails_from_site(site)
        if not recs and lookup_fn and lookup_tries < 3:
            lookup_tries += 1
            try:
                recs = lookup_fn(site, sponsor=sponsor) or []
            except TypeError:
                recs = lookup_fn(site) or []
            except Exception:
                app.logger.exception("clinic email lookup failed for %s",
                                     (site or {}).get("facility"))
                recs = []
        for rec in recs:
            _add(clinic, rec)
            if len(clinic) >= max_clinics:
                break
        if len(clinic) >= max_clinics:
            break

    # The listing's own study contact is the exact source for the trial. It
    # comes right after any nearby site inboxes, so the handoff reaches the
    # people the registry says to write to, not only a guessed clinic address.
    for c in central_contacts_for_patients(trial):
        if c.get("email"):
            _add(clinic, _one(c["email"], "Study contact", "central",
                              name=c.get("name", "")))

    extras = []
    # A claimed or posted contact rides along only when it is a real address.
    # The demo seed's fictional site claims studies too, and its placeholder
    # inbox swallowed real handoffs.
    if claimed_email and not db.is_placeholder_site_email(claimed_email):
        _add(extras, _one(claimed_email, site_label, "claimed"))
    if posted_email and not db.is_placeholder_site_email(posted_email):
        _add(extras, _one(posted_email, site_label, "posted"))

    out = clinic + extras
    if out:
        return out
    rec = _one(fallback, site_label, "fallback")
    return [rec] if rec else []


def _resolve_clinic_notify_recipients(lead, trial=None, lat=None, lon=None,
                                      radius=None, unit="km"):
    """Load every public email for this apply: CT.gov clinic + sponsor + fallback."""
    nct = ""
    try:
        nct = (lead["nct"] or "").strip()
    except (KeyError, IndexError, TypeError):
        nct = ""
    claimed = db.site_contact_for_nct(nct) if nct else ""
    posted_email = ""
    if nct:
        posted = db.get_site_posted_study_by_nct(nct)
        if posted:
            posted_email = (posted["contact_email"] or "").strip()
    if trial is None and nct:
        try:
            trial = _get_study(nct)
        except Exception:
            app.logger.exception("clinic notify: study fetch failed for %s", nct)
            trial = None
    return resolve_clinic_notify_recipients(
        lead, trial=trial, claimed_email=claimed, posted_email=posted_email,
        fallback=SITE_NOTIFY_EMAIL, lat=lat, lon=lon, radius=radius,
        unit=unit or "km")


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
    """Fetch a campaign and 404 unless it belongs to the current team (shared
    across the org)."""
    camp = db.get_campaign(cid)
    if not camp or camp["user_id"] not in set(db.org_member_ids(g.user["id"])):
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
    prof = db.get_site_profile(g.user["id"]) or {}
    try:
        org_name = (prof["org_name"] or "").strip()
    except (KeyError, TypeError):
        org_name = ""
    return render_template(
        "campaign_detail.html", camp=camp, placements=placements,
        shares=shares, perf=perf, pl_perf=pl_perf, org_name=org_name,
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


@app.route("/app/campaigns/<int:cid>/ad-image.svg")
@login_required
def campaign_ad_image(cid):
    """Render the campaign's ad card as an SVG (the ad maker's image + download).

    Copy comes from the saved creative, but `h`/`b` query params can override it
    so the preview updates live as the user types (before saving). `template`,
    `accent`, and `size` pick the layout. This is the swap seam: today it's a
    local SVG template; later it can proxy Figma's render API or an image model
    without touching this route's callers (see adrender.py)."""
    camp = _own_campaign_or_404(cid)
    prof = db.get_site_profile(g.user["id"]) or {}
    org = ""
    try:
        org = (prof["org_name"] or "").strip()
    except (KeyError, TypeError):
        org = ""
    headline = request.args.get("h")
    body = request.args.get("b")
    headline = headline if headline is not None else (camp["headline"] or "")
    body = body if body is not None else (camp["body"] or "")
    svg = adrender.render_ad_svg(
        headline[:200], body[:400], org=org[:60],
        template=request.args.get("template", "clean"),
        accent=request.args.get("accent", "ink"),
        size=request.args.get("size", "square"))
    resp = make_response(svg)
    resp.headers["Content-Type"] = "image/svg+xml; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store"
    if request.args.get("dl"):
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", (camp["name"] or "ad")).strip("-")
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="{safe or "ad"}-{request.args.get("size","square")}.svg"')
    return resp


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


@app.route("/app/onboarding")
@login_required
def onboarding():
    """Quick setup for a new site: (1) forward your recruitment mail to your
    catch-all intake address, (2) optionally add a study so triage can pre-screen,
    (3) optionally invite teammates. Then the shared inbox starts filling - no
    CTMS rollout, no verification wall (owned mail shows immediately). KPI:
    activation -> time-to-first-triaged-inquiry."""
    catchall = db.get_or_create_catchall_address(g.user["id"])
    studies = db.list_study_claims(g.user["id"])
    profile = db.get_site_profile(g.user["id"])
    members = db.list_org_members(g.user["id"])
    lead_webhook = url_for("inbound_lead_webhook", _external=True)
    return render_template(
        "onboarding.html",
        catchall_full=intake_mod.full_intake_address(catchall["address"]),
        lead_webhook=lead_webhook,
        studies=studies, profile=profile, members=members,
        can_manage_team=db.can_manage_team(g.user["id"]))


@app.route("/app/onboarding/site", methods=["POST"])
@login_required
def onboarding_site():
    """Save the workspace name/contact (marks onboarding 'started')."""
    f = request.form
    org = (f.get("org_name") or "").strip()
    if not org:
        flash("Add your site or clinic name.", "error")
        return redirect(url_for("onboarding"))
    db.upsert_site_profile(
        g.user["id"], org, (f.get("contact_name") or g.user["name"] or "").strip(),
        (f.get("contact_email") or g.user["email"] or "").strip(),
        (f.get("contact_phone") or "").strip())
    flash("Workspace saved.", "success")
    return redirect(url_for("onboarding"))


@app.route("/app/onboarding/study", methods=["POST"])
@login_required
def onboarding_study():
    """Add a study so triage can pre-screen against its criteria. Creates a
    site-owned study (safe: it's the team's own record, not a claim on a public
    trial's applicant pool), so there's no verification wait."""
    f = request.form
    title = (f.get("title") or "").strip()
    if not title:
        flash("Give the study a name.", "error")
        return redirect(url_for("onboarding"))
    db.create_site_posted_study(g.user["id"], {
        "title": title,
        "condition": (f.get("condition") or "").strip(),
        "eligibility": (f.get("eligibility") or "").strip(),
        "location": (f.get("location") or "").strip(),
    })
    flash("Study added - triage will pre-screen inquiries against it.", "success")
    return redirect(url_for("onboarding"))


@app.route("/app/onboarding/invite", methods=["POST"])
@login_required
def onboarding_invite():
    """Invite a teammate into the shared workspace during setup."""
    if not db.can_manage_team(g.user["id"]):
        flash("Only a coordinator or PI can invite teammates.", "error")
        return redirect(url_for("onboarding"))
    email = (request.form.get("email") or "").strip()
    if not email:
        flash("Enter a teammate's email.", "error")
        return redirect(url_for("onboarding"))
    role, label = _resolve_team_role(request.form)
    db.create_org_invite(g.user["id"], email, role, role_label=label)
    flash(f"Invite sent to {email}.", "success")
    return redirect(url_for("onboarding"))


@app.route("/app/team")
@login_required
def team_page():
    """The shared workspace roster: everyone on the trial with full visibility.
    Coordinators/PIs can invite teammates and set roles; students see the team."""
    return render_template("team.html", **_team_context())


def _resolve_team_role(form):
    """Map the role form (including a custom 'Other') to a base role + display
    label. A custom role NEVER grants new access - it inherits one of two tiers:
    'full' (approve + manage, like a coordinator) or 'standard' (day-to-day)."""
    role = (form.get("role") or "student").strip()
    label = ""
    if role == "other":
        label = (form.get("custom_role") or "").strip()[:40]
        access = (form.get("access") or "standard").strip()
        role = "coordinator" if access == "full" else "student"
        if not label:
            label = db.ORG_ROLE_LABELS.get(role, "Member")
    if role not in db.ORG_ROLES:
        role = "student"
    return role, label


@app.route("/app/team/invite", methods=["POST"])
@login_required
def team_invite():
    if not db.can_manage_team(g.user["id"]):
        flash("Only a coordinator or PI can invite teammates.", "error")
        return redirect(url_for("team_page"))
    email = request.form.get("email", "").strip()
    role, label = _resolve_team_role(request.form)
    token = db.create_org_invite(g.user["id"], email, role, role_label=label)
    link = url_for("team_join", token=token, _external=True)
    flash(f"Invite link ready - share it with your teammate: {link}", "ok")
    return redirect(_team_return())


@app.route("/app/team/role", methods=["POST"])
@login_required
def team_set_role():
    if not db.can_manage_team(g.user["id"]):
        abort(403)
    target = request.form.get("user_id", type=int)
    role, label = _resolve_team_role(request.form)
    if target and db.set_member_role(g.user["id"], target, role, role_label=label):
        flash("Role updated.", "ok")
    else:
        flash("Couldn't update that role.", "error")
    return redirect(_team_return())


@app.route("/app/team/remove", methods=["POST"])
@login_required
def team_remove():
    if not db.can_manage_team(g.user["id"]):
        abort(403)
    target = request.form.get("user_id", type=int)
    if target and db.remove_member(g.user["id"], target):
        flash("Teammate removed from this workspace.", "ok")
    else:
        flash("Couldn't remove that member (can't remove the last coordinator).",
              "error")
    return redirect(_team_return())


@app.route("/app/team/invite/revoke", methods=["POST"])
@login_required
def team_revoke_invite():
    if not db.can_manage_team(g.user["id"]):
        abort(403)
    db.revoke_org_invite(g.user["id"], request.form.get("token", "").strip())
    flash("Invite revoked.", "ok")
    return redirect(_team_return())


@app.route("/app/team/join/<token>")
@login_required
def team_join(token):
    """Accept an invite: the signed-in study-team user joins the inviter's org
    with the invited role (leaving their prior personal workspace)."""
    inv = db.get_org_invite(token)
    if not inv or (inv["accepted_at"] or "").strip():
        flash("That invite link is invalid or already used.", "error")
        return redirect(url_for("marketing_hub"))
    if db.accept_org_invite(g.user["id"], token):
        flash("You've joined the team - you now share this workspace.", "ok")
    else:
        flash("Couldn't join that team.", "error")
    return redirect(url_for("marketing_hub"))


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
        _gcal_sync_async(lead, visit, invite_url)
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
