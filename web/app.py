#!/usr/bin/env python3
"""TrialBridge - a doctor-facing wrapper on ClinicalTrials.gov.

Paste a de-identified patient note, get ranked recruiting trials with a plain
explanation of the fit, then refer a patient in one click and track that
referral through the pipeline (referred -> contacted -> screened -> enrolled).
TrialBridge never pays clinicians for referrals or enrollments - status is
tracked for follow-up only (anti-kickback / fee-splitting compliance).

Run:
    cd matcher
    export LLM_API_KEY="sk-..."          # OpenAI-compatible (optional; see below)
    .venv/bin/python web/app.py          # http://127.0.0.1:5000

Without an LLM key the app still fetches + gates trials (deterministic age/sex
screening) but skips the per-trial eligibility reasoning.
"""
import functools
import json
import math
import os
import pathlib
import secrets
import sys
import time
import datetime as dt
import urllib.parse
import urllib.request
from collections import OrderedDict

from flask import (Flask, abort, flash, g, jsonify, make_response, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

# Reuse the matching engine + referral helpers from the parent package.
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import match_trials as mt  # noqa: E402
import refer as rf  # noqa: E402

import alerts as alerts_mod  # noqa: E402
import analytics  # noqa: E402
import codes  # noqa: E402
import db  # noqa: E402
import fhir  # noqa: E402
import ingest  # noqa: E402
import mailer  # noqa: E402
import records as records_mod  # noqa: E402
import redcap  # noqa: E402
import reminders as reminders_mod  # noqa: E402
import summarize  # noqa: E402
import trends  # noqa: E402

app = Flask(__name__)
trends.configure(app)
# Short, plain-English card teaser (deterministic) available in every template.
app.jinja_env.globals["card_blurb"] = summarize.card_blurb

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

MAX_MATCH = int(os.environ.get("WEB_MAX_MATCH", "6"))  # LLM calls per search
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024    # 16 MB upload cap
app.teardown_appcontext(db.close_db)

# Create tables on import so the app is safe under any launcher (flask run, wsgi).
db.init_db()

# In no-login demo mode, seed a few realistic (clearly fake) candidates so the
# study-team review board shows an end-to-end picture. No-op once real leads exist.
if os.environ.get("NO_LOGIN", "1") == "1":
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


@app.errorhandler(500)
def server_error(_e):
    app.logger.exception("Unhandled error")
    return render_template("error.html", code=500,
                           msg="Something went wrong on our end. Please try again."), 500


@app.route("/healthz")
def healthz():
    """Lightweight process-level health probe for hosting checks."""
    return jsonify({"ok": True}), 200


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
# TEMP: no-login testing mode. When on, the clinician tool is open to everyone
# (falls back to a shared demo account) so there's zero barrier to trying it.
# Set NO_LOGIN=1 only for local demos; production should keep this off.
NO_LOGIN = os.environ.get("NO_LOGIN", "0") == "1"
_DEMO_EMAIL = "demo@trialbridge.local"
PATIENT_SESSION_KEY = "patient_user_id"
PATIENT_PENDING_KEY = "patient_pending_id"
PATIENT_PENDING_PURPOSE_KEY = "patient_pending_purpose"
PATIENT_NEXT_KEY = "patient_next"
RATE_LIMIT_WINDOW_SECONDS = max(
    1, int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "300")))
RATE_LIMIT_DEFAULT_MSG = (
    "Too many attempts from this network. Please wait a few minutes and try again.")
RATE_LIMIT_ROUTES = {
    "account_signup": max(1, int(os.environ.get("RATE_LIMIT_SIGNUP_MAX", "8"))),
    "account_login": max(1, int(os.environ.get("RATE_LIMIT_LOGIN_MAX", "10"))),
    "account_verify": max(1, int(os.environ.get("RATE_LIMIT_VERIFY_MAX", "10"))),
    "account_verify_resend": max(
        1, int(os.environ.get("RATE_LIMIT_VERIFY_RESEND_MAX", "5"))),
    "interest": max(1, int(os.environ.get("RATE_LIMIT_INTEREST_MAX", "12"))),
}


def _ensure_demo_user():
    u = db.get_user_by_email(_DEMO_EMAIL)
    if not u:
        pw = generate_password_hash("demo-no-login", method="pbkdf2:sha256")
        db.create_user(_DEMO_EMAIL, pw, "Demo Clinician", "", "")
        u = db.get_user_by_email(_DEMO_EMAIL)
    return u


# Seed the retention/engagement surfaces (messages, visits, a physician referral)
# on top of the demo leads so a fresh no-login demo shows the whole loop alive.
if NO_LOGIN:
    try:
        with app.test_request_context():
            _demo = _ensure_demo_user()
            db.seed_demo_engagement(_demo["id"] if _demo else None)
    except Exception:
        app.logger.exception("demo engagement seeding failed")


def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        if not g.user:
            return redirect(url_for("login", next=request.path))
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
    uid = session.get("user_id")
    g.user = db.get_user(uid) if uid else None
    if g.user is None and NO_LOGIN:
        g.user = _ensure_demo_user()
    pid = session.get(PATIENT_SESSION_KEY)
    g.patient_user = db.get_patient_user(pid) if pid else None


APPLICANT_COOKIE = "tb_app"
INVITE_COOKIE = "tb_invite"


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
# Delivery / notifications for the consumer loop. Fully wired but OFF by default
# so nothing is emailed during testing. Go-live is ~2 min: set these env vars.
#   NOTIFY_LIVE=1
#   SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_FROM
#   SITE_NOTIFY_EMAIL=coordinator@site          (where new candidates are sent)
#   PUBLIC_BASE_URL=https://yourdomain.com      (optional, for correct email links)
# While OFF, the loop still works end to end: the operator copies the secure
# /c/<token> link from the dashboard and hands it to the site by hand.
# --------------------------------------------------------------------------- #
NOTIFY_LIVE = os.environ.get("NOTIFY_LIVE", "0") == "1"
SITE_NOTIFY_EMAIL = os.environ.get("SITE_NOTIFY_EMAIL", "").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")


def _abs_url(endpoint, **kw):
    """Absolute URL for links placed inside emails."""
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL + url_for(endpoint, **kw)
    return url_for(endpoint, _external=True, **kw)


def notifications_ready():
    return NOTIFY_LIVE and mailer.smtp_configured()


def _notify(to_addr, subject, body):
    """Single switch for every consumer-loop email. No-op (never errors) unless
    go-live is on AND SMTP is configured AND there's a recipient."""
    if not (notifications_ready() and to_addr):
        return False
    ok, _ = mailer.send_email(to_addr, subject, body)
    return ok


def _notify_site_new_candidate(token):
    """Tell the study site a new de-identified candidate is waiting (with the
    secure review link). Recipient: the lead's site email, else SITE_NOTIFY_EMAIL."""
    lead = db.get_lead_by_token(token)
    if not lead:
        return False
    to_addr = db.site_contact_for_nct(lead["nct"]) or SITE_NOTIFY_EMAIL
    link = _abs_url("candidate_page", token=token)
    subject, body = mailer.build_candidate_message(lead, link)
    return _notify(to_addr, subject, body)


def _notify_applicant(token, kind):
    """Tell the applicant their status changed. kind in {accepted, declined,
    screening, enrolled}."""
    lead = db.get_lead_by_token(token)
    if not lead or not lead["email"]:
        return False
    link = _abs_url("applications")
    subject, body = mailer.build_applicant_message(lead, kind, link)
    return _notify(lead["email"], subject, body)


def _notify_applicant_by_id(lead_id, kind):
    lead = db.get_lead(lead_id)
    return _notify_applicant(lead["token"], kind) if lead else False


def _notify_applicant_schedule(lead):
    """Email the applicant their booking link so they can self-schedule."""
    if not lead or not lead["email"] or not lead["schedule_url"]:
        return False
    subject, body = mailer.build_schedule_message(
        lead, lead["schedule_url"], _abs_url("applications"))
    return _notify(lead["email"], subject, body)


def _notify_applicant_message(lead, body):
    if not lead or not lead["email"]:
        return False
    subject, msg = mailer.build_dm_message(
        lead, body, _abs_url("applications"), to="patient")
    return _notify(lead["email"], subject, msg)


def _notify_site_message(lead, body):
    if not lead:
        return False
    to_addr = db.site_contact_for_nct(lead["nct"]) or SITE_NOTIFY_EMAIL
    link = _abs_url("candidate_page", token=lead["token"])
    subject, msg = mailer.build_dm_message(lead, body, link, to="site")
    return _notify(to_addr, subject, msg)


def _notify_applicant_visit(lead, when, location):
    if not lead or not lead["email"]:
        return False
    subject, body = mailer.build_visit_message(
        lead, when, location, _abs_url("applications"))
    return _notify(lead["email"], subject, body)


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
    if not visit["email"]:
        return False
    subject, body = mailer.build_reminder_message(
        visit, visit["visit_at"], visit["location"], _abs_url("applications"))
    return _notify(visit["email"], subject, body)


def _nudge_applicant(lead):
    if not lead["email"]:
        return False
    subject, body = mailer.build_nudge_message(lead, _abs_url("applications"))
    return _notify(lead["email"], subject, body)


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
        rec = db.get_records_profile(token) if token else None
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
            or path.startswith("/app/site")):
        pov = "study"
    elif path.startswith("/app") or path.startswith("/referral"):
        pov = "clinician"
    else:
        pov = "patient"
    return {"user": g.user, "llm_on": bool(mt.LLM_API_KEY),
            "applications_count": apps_n, "pov_demo": NO_LOGIN, "pov": pov,
            "records_profile": rec, "records_provider": records_mod.provider_label(),
            "alerts_new_count": alerts_new, "messages_unread": msgs_unread,
            "site_unread": site_unread, "patient_user": g.patient_user}


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
    if request.method == "POST":
        f = request.form
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
            session.clear()
            session["user_id"] = uid
            return redirect(url_for("dashboard"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        user = db.get_user_by_email(email)
        if user and check_password_hash(user["password_hash"], pw):
            session.clear()
            session["user_id"] = user["id"]
            nxt = request.args.get("next")
            return redirect(nxt if nxt and nxt.startswith("/") else url_for("dashboard"))
        flash("Wrong email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
    flash(RATE_LIMIT_DEFAULT_MSG, "error")
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
    action = "sign-up verification" if purpose == "signup" else "login verification"
    subject = f"Your TrialBridge {action} code"
    body = "\n".join([
        f"Hi {patient['full_name'] or 'there'},",
        "",
        f"Your TrialBridge {action} code is:",
        "",
        f"  {code}",
        "",
        "It expires in 10 minutes.",
        "",
        "If you didn't request this, ignore this email.",
    ])
    ok, msg = mailer.send_email(patient["email"], subject, body)
    return ok, msg


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
            return render_template("patient_signup.html")
        if len(pw) < 8:
            flash("Use at least 8 characters for your password.", "error")
            return render_template("patient_signup.html")
        if db.get_patient_by_email(email):
            flash("An account with that email already exists. Try signing in.", "error")
            return redirect(url_for("patient_login"))
        pid = db.create_patient_user(
            email, generate_password_hash(pw, method="pbkdf2:sha256"), full_name)
        patient = db.get_patient_user(pid)
        ok, msg = _issue_patient_code(patient, "signup")
        if not ok:
            flash(msg, "error")
            return render_template("patient_signup.html")
        session[PATIENT_PENDING_KEY] = pid
        session[PATIENT_PENDING_PURPOSE_KEY] = "signup"
        flash("Check your email for a 6-digit verification code.", "success")
        return redirect(url_for("patient_verify"))
    return render_template("patient_signup.html")


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
            fresh = db.get_patient_user(patient["id"])
            if not fresh["onboarding_done"]:
                return redirect(url_for("patient_onboarding"))
            nxt = session.pop(PATIENT_NEXT_KEY, "")
            return redirect(nxt if nxt.startswith("/") else url_for("home"))
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
        if not patient or not check_password_hash(patient["password_hash"], pw):
            flash("Wrong email or password.", "error")
            return render_template("patient_login.html")
        if not patient["verified"]:
            session[PATIENT_PENDING_KEY] = patient["id"]
            session[PATIENT_PENDING_PURPOSE_KEY] = "signup"
            flash("Verify your email to finish account setup.", "error")
            return redirect(url_for("patient_verify"))
        ok, msg = _issue_patient_code(patient, "login")
        if not ok:
            flash(msg, "error")
            return render_template("patient_login.html")
        session[PATIENT_PENDING_KEY] = patient["id"]
        session[PATIENT_PENDING_PURPOSE_KEY] = "login"
        flash("Enter the 6-digit code we sent to your email.", "success")
        return redirect(url_for("patient_verify"))
    return render_template("patient_login.html")


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
        notify_email = f.get("notify_email", "").strip() or g.patient_user["email"]
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
    return render_template(
        "dashboard.html", referrals=referrals[:8], counts=counts,
        total=len(referrals), enrolled=enrolled, statuses=db.STATUSES)


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
SEED_CITIES = [
    "Toronto, ON", "Vancouver, BC", "Montreal, QC", "Calgary, AB",
    "New York, NY", "Los Angeles, CA", "Chicago, IL", "Houston, TX",
    "Miami, FL", "Boston, MA",
]
# The GLP-1 / peptide trend: people search by drug name, not condition. These
# power intervention-based landing pages and are queried via CT.gov query.intr.
SEED_DRUGS = [
    "Semaglutide", "Tirzepatide", "Retatrutide", "Survodutide",
    "Orforglipron", "CagriSema", "Liraglutide", "Dulaglutide",
]
_COND_BY_SLUG = {slugify(c): c for c in SEED_CONDITIONS}
_CITY_BY_SLUG = {slugify(c): c for c in SEED_CITIES}


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


def build_patient_note(condition, age="", sex="", about=""):
    """Turn a patient's self-reported details into a note the matcher can read."""
    lines = []
    if str(age).strip():
        lines.append(f"AGE: {str(age).strip()}")
    if sex in ("male", "female"):
        lines.append(f"SEX: {sex}")
    lines.append(f"Condition: {condition}.")
    if about.strip():
        lines.append(about.strip())
    return "\n".join(lines)


@app.route("/")
def home():
    return render_template("landing.html", vertical=VERTICAL,
                           conditions=trending_conditions(8),
                           drugs=trending_drugs(6), slugify=slugify,
                           condition_value=request.args.get("condition", ""),
                           location_value=request.args.get("location", ""))


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


@app.route("/find", methods=["GET", "POST"])
def find():
    """Public, no-login patient search. The search form lives on the homepage;
    this endpoint handles the POST and renders patient-friendly results. A GET
    just bounces back to the homepage (carrying any prefill)."""
    if request.method == "GET":
        args = {k: request.args[k] for k in ("condition", "location")
                if request.args.get(k)}
        return redirect(url_for("home", **args))

    condition = request.form.get("condition", "").strip()
    intervention = request.form.get("intervention", "").strip()
    location = request.form.get("location", "").strip()
    age = request.form.get("age", "").strip()
    sex = request.form.get("sex", "").strip()
    about = request.form.get("about", "").strip()
    try:
        radius = int(request.form.get("radius", "50") or 0)
    except ValueError:
        radius = 50

    label = condition or intervention
    if not label:
        flash("Tell us the condition or treatment you're looking for.", "error")
        return redirect(url_for("home", location=location))
    if not location:
        flash("Enter your city or postal code so we only show trials near you.",
              "error")
        return redirect(url_for("home", condition=condition))

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

    note = build_patient_note(label, age, sex, about)
    try:
        detected, results = run_search(note, condition, "", False, coords,
                                       radius, unit, interventional_only=True,
                                       intervention=intervention)
    except RuntimeError as e:
        flash(str(e), "error")
        return redirect(url_for("home", condition=condition, location=location))
    except Exception:
        app.logger.exception("public find failed")
        flash("Search failed unexpectedly. Please try again.", "error")
        return redirect(url_for("home", condition=condition, location=location))

    # Record the search so "trending" reflects real site traffic. Drug-name
    # queries go through the intervention field; everything else is a condition.
    try:
        if intervention:
            db.log_search_term(intervention, "drug")
        if condition:
            db.log_search_term(condition, "condition")
    except Exception:
        app.logger.exception("search stat logging failed")

    ctx = {"condition": label, "location": location, "unit": unit,
           "q_condition": condition, "q_intervention": intervention,
           "q_age": age, "q_sex": sex, "q_about": about, "q_radius": radius,
           "q_lat": lat_in, "q_lon": lon_in, "q_cc": cc_in}
    search_id = _cache_search(results, ctx)
    applied = db.applied_ncts(get_applicant_token())
    return render_template("patient_results.html", results=results,
                           search_id=search_id, applied=applied, **ctx)


@app.route("/trial/<search_id>/<nct>")
def trial_detail(search_id, nct):
    """Airbnb-style listing page for one trial: full info on the left, an apply
    panel on the right. Reads from the cached search so the eligibility breakdown
    matches what the patient saw in the results list."""
    r, ctx = _get_cached_trial(search_id, nct)
    if not r:
        flash("That trial result expired - please run your search again.", "error")
        return redirect(url_for("find"))
    applied = db.applied_ncts(get_applicant_token())
    summary = summarize.plain(r["trial"])
    return render_template("trial_detail.html", r=r, search_id=search_id,
                           applied=applied, summary=summary, **ctx)


@app.route("/interest", methods=["POST"])
def interest():
    """Patient asks to be contacted about a specific trial -> consented lead."""
    blocked = _guard_ip_rate_limit("interest")
    if blocked:
        return blocked
    if not g.patient_user:
        flash("Sign in or create an account to apply.", "error")
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

    # Short screener: only quick gate questions that records can't reliably
    # answer. Everything clinical is filled from the record / search match.
    screener = {}
    for q in ("travel", "other_trial", "pregnancy", "consent_capable"):
        v = f.get(q, "").strip()
        if v:
            screener[q] = v

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
        "records_connected": records_connected, "record_summary": record_summary,
        "referred_by": referred_by, "invite_token": invite_token,
    })
    # Go-live hook: tell the site a new blinded candidate is waiting. No-op while
    # NOTIFY_LIVE is off, so nothing is emailed during testing.
    _notify_site_new_candidate(token)
    return render_template("thanks.html", title=f.get("title", ""),
                           nct=f.get("nct", ""))


@app.route("/how-it-works")
def how_it_works():
    return render_template("how.html", pipeline=db.LEAD_PIPELINE,
                           labels=db.LEAD_LABELS, blurb=db.LEAD_BLURB)


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
        apps.append({
            "lead": ld,
            "events": db.get_lead_events(ld["id"]),
            "messages": db.get_messages(ld["id"]),
            "visits": db.get_visits(ld["id"]),
        })
        db.mark_thread_read(ld["id"], "patient")
    return render_template("applications.html", apps=apps,
                           pipeline=db.LEAD_PIPELINE, labels=db.LEAD_LABELS,
                           blurb=db.LEAD_BLURB, closed=db.LEAD_CLOSED)


@app.route("/applications/<token>/message", methods=["POST"])
def application_message(token):
    """Patient sends a message to the study team about their own application."""
    if not g.patient_user:
        flash("Sign in to message the study team.", "error")
        return redirect(url_for("patient_login", next=url_for("applications")))
    lead = db.get_lead_by_token(token)
    if not lead or lead["applicant_token"] != get_applicant_token():
        abort(403)
    body = request.form.get("body", "").strip()
    if body:
        db.add_message(lead["id"], "patient", body)
        _notify_site_message(lead, body)
        flash("Message sent to the study team.", "success")
    return redirect(url_for("applications") + f"#app-{lead['id']}")


@app.route("/applications/withdraw/<token>", methods=["POST"])
def withdraw_application(token):
    if not g.patient_user:
        flash("Sign in to manage your applications.", "error")
        return redirect(url_for("patient_login", next=url_for("applications")))
    if db.withdraw_lead(token, get_applicant_token()):
        flash("Application withdrawn.", "success")
    else:
        flash("Couldn't withdraw that application.", "error")
    return redirect(url_for("applications"))


@app.route("/records/connect", methods=["POST"])
def records_connect():
    """Patient authorizes their health records ONCE. We pull a de-identified
    profile and store it against their applicant token, so every application
    (past pending + future) auto-fills from it. Sandbox by default; a real
    aggregator (Metriport/1upHealth) slots in behind the same call."""
    if not g.patient_user:
        flash("Sign in to connect health records.", "error")
        return redirect(url_for("patient_login", next=_safe_next(request.referrer) or url_for("applications")))
    applicant = g.patient_user["applicant_token"]
    try:
        prof = records_mod.connect()
    except fhir.FhirError as e:
        flash(str(e), "error")
        return redirect(request.referrer or url_for("applications"))
    except Exception:
        app.logger.exception("records connect failed")
        flash("Couldn't connect your records right now. Please try again.",
              "error")
        return redirect(request.referrer or url_for("applications"))

    db.set_records_profile(applicant, prof)
    n = db.attach_records_to_open_leads(applicant, records_mod.summary_text(prof))
    msg = f"Health records connected via {prof.get('provider')}. "
    msg += (f"Auto-filled {n} pending application(s) - "
            if n else "New applications will auto-fill from your history - ")
    msg += "you won't have to re-enter your medical details."
    flash(msg, "success")
    return redirect(request.referrer or url_for("applications"))


@app.route("/records/disconnect", methods=["POST"])
def records_disconnect():
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
        items.append({"alert": a, "matches": db.get_alert_matches(a["id"])})
    # Viewing clears the "new" badge in the nav.
    if token:
        db.clear_new_flags(token)
    return render_template("alerts.html", items=items)


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
    email = f.get("email", "").strip()
    if not (condition or intervention):
        flash("Tell us a condition or treatment to watch for.", "error")
        return redirect(request.referrer or url_for("alerts"))
    if not email:
        flash("Add an email so we can notify you about new trials.", "error")
        return redirect(request.referrer or url_for("alerts"))

    applicant = g.patient_user["applicant_token"]

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    alert_id = db.create_alert({
        "applicant_token": applicant,
        "label": f.get("label", "").strip(),
        "condition": condition, "intervention": intervention,
        "location": f.get("location", "").strip(),
        "lat": _f(f.get("lat")), "lon": _f(f.get("lon")),
        "cc": f.get("cc", "").strip(), "radius": f.get("radius", "50").strip() or 50,
        "unit": f.get("unit", "km").strip() or "km", "email": email,
    })
    # Baseline current matches so the patient isn't spammed with the backlog.
    try:
        alerts_mod.seed_baseline(alert_id)
    except Exception:
        app.logger.exception("alert baseline failed")
    flash("Alert saved. We'll email you when a new matching trial opens - no "
          "need to keep searching.", "success")
    return redirect(url_for("alerts"))


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
    return jsonify(reminders_mod.run_all())


@app.route("/applications/connect-records/<token>", methods=["POST"])
def connect_records(token):
    # Back-compat: the old per-application button now triggers the connect-once
    # flow (records apply to every application, not just one).
    return records_connect()


@app.route("/trials/<slug>")
def condition_page(slug):
    condition = _COND_BY_SLUG.get(slug) or _titleize(slug)
    if not condition:
        abort(404)
    try:
        db.log_search_term(condition, "condition")
    except Exception:
        pass
    trials = []
    try:
        raw = mt.fetch_trials(condition, max_n=40)
        raw = [t for t in raw
               if (t.get("overallStatus") or "RECRUITING").upper() == "RECRUITING"
               and (t.get("studyType") or "").upper() != "OBSERVATIONAL"]
        trials = raw[:12]
    except Exception:
        app.logger.exception("condition page fetch failed")
    return render_template("condition.html", condition=condition, city=None,
                           trials=trials, cities=SEED_CITIES,
                           conditions=SEED_CONDITIONS, slugify=slugify)


@app.route("/trials/<slug>/<city_slug>")
def condition_city_page(slug, city_slug):
    condition = _COND_BY_SLUG.get(slug)
    city = _CITY_BY_SLUG.get(city_slug)
    if not condition or not city:
        abort(404)
    return render_template("condition.html", condition=condition, city=city,
                           trials=[], cities=SEED_CITIES,
                           conditions=SEED_CONDITIONS, slugify=slugify)


SCREENER_LABELS = {
    "travel": "Can travel to the study site",
    "other_trial": "Currently in another trial",
    "pregnancy": "Pregnant / planning pregnancy",
    "consent_capable": "Can give own consent",
}
# Answers that are a yellow flag for the study team to look at.
SCREENER_FLAGS = {"other_trial": "yes", "pregnancy": "yes", "consent_capable": "no"}


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
    flags = [SCREENER_LABELS.get(k, k) for k, bad in SCREENER_FLAGS.items()
             if scr.get(k) == bad]
    return {
        "lead": row,
        "events": db.get_lead_events(row["id"]),
        "elig": elig,
        "screener": scr,
        "flags": flags,
        "code": candidate_code(row),
        "age_band": age_band(row["age"]),
        "unread": db.lead_unread_for_site(row["id"]),
        "recon": recon or db.latest_reconciliation(row["id"]),
    }


@app.route("/app/leads")
@login_required
def leads():
    rows = db.list_leads_for_user(g.user["id"])
    recon = db.latest_reconciliation_for_leads([r["id"] for r in rows])
    claims = db.list_study_claims(g.user["id"])
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
                           recon_labels=db.RECON_LABELS,
                           recon_outcomes=db.RECON_OUTCOMES,
                           redcap_on=redcap.configured(), claims=claims)


@app.route("/app/dashboard")
@login_required
def recruitment_dashboard():
    """The recruitment plan + proof: live funnel, conversion, time-in-stage, and
    where candidates drop off - built from the data the pipeline already logs."""
    claims = _site_claims()
    return render_template(
        "recruitment.html", stats=analytics.funnel_stats(claims),
        labels=db.LEAD_LABELS, claims=db.list_study_claims(g.user["id"]))


@app.route("/app/site", methods=["GET", "POST"])
@login_required
def site_setup():
    """Study-team onboarding: account profile + claim your active NCTs."""
    if request.method == "POST":
        f = request.form
        db.upsert_site_profile(
            g.user["id"], f.get("org_name", ""), f.get("contact_name", ""),
            f.get("contact_email", ""), f.get("contact_phone", ""))
        flash("Site profile saved.", "success")
        return redirect(url_for("site_setup"))
    return render_template(
        "site_setup.html",
        profile=db.get_site_profile(g.user["id"]),
        claims=db.list_study_claims(g.user["id"]))


@app.route("/app/site/claim", methods=["POST"])
@login_required
def add_study_claim():
    nct = request.form.get("nct", "").strip().upper()
    title = request.form.get("title", "").strip()
    if not nct:
        flash("Add an NCT number to claim a study.", "error")
    elif db.add_study_claim(g.user["id"], nct, title):
        flash("Study claimed. New applicants for that NCT now route to your board.",
              "success")
    else:
        flash("Couldn't claim that study. Check the NCT and try again.", "error")
    return redirect(url_for("site_setup"))


@app.route("/app/site/claim/remove", methods=["POST"])
@login_required
def remove_study_claim():
    nct = request.form.get("nct", "").strip().upper()
    if nct:
        db.remove_study_claim(g.user["id"], nct)
        flash("Study unclaimed.", "success")
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


@app.route("/app/leads/<int:lead_id>/message", methods=["POST"])
@login_required
def message_lead(lead_id):
    """Study-team inbox action: send a message without leaving the board."""
    _ensure_site_access_for_lead(lead_id)
    lead = db.get_lead(lead_id)
    if not lead:
        flash("Couldn't find that candidate.", "error")
        return redirect(url_for("leads"))
    if not lead["revealed"]:
        flash("Accept the candidate first to message them.", "error")
        return redirect(url_for("leads"))
    body = request.form.get("body", "").strip()
    if body:
        db.add_message(lead_id, "site", body)
        _notify_applicant_message(lead, body)
        flash("Message sent.", "success")
    else:
        flash("Write a message first.", "error")
    return redirect(url_for("leads"))


@app.route("/c/<token>/schedule", methods=["POST"])
def candidate_schedule(token):
    url = request.form.get("schedule_url", "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    lead = db.get_lead_by_token(token)
    if not lead:
        abort(404)
    lead = db.set_lead_schedule(lead["id"], url)
    if url:
        _notify_applicant_schedule(lead)
        flash("Booking link sent to the applicant.", "ok")
    else:
        flash("Booking link removed.", "ok")
    return redirect(url_for("candidate_page", token=token))


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
    ok, msg = redcap.push_candidate(lead)
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


@app.route("/robots.txt")
def robots():
    body = ("User-agent: *\nAllow: /\nSitemap: "
            + url_for("sitemap", _external=True) + "\n")
    return app.response_class(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    urls = [url_for("home", _external=True), url_for("find", _external=True)]
    for c in SEED_CONDITIONS:
        urls.append(url_for("condition_page", slug=slugify(c), _external=True))
        for city in SEED_CITIES:
            urls.append(url_for("condition_city_page", slug=slugify(c),
                                city_slug=slugify(city), _external=True))
    items = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
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
            headers={"User-Agent": "TrialBridge/1.0"})
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
            headers={"User-Agent": "TrialBridge/1.0 (clinical trial finder)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        if data:
            cc = (data[0].get("address", {}).get("country_code") or "").upper()
            return float(data[0]["lat"]), float(data[0]["lon"]), cc
    except Exception:
        pass
    return None


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

    r = _nominatim(p)
    if r:
        return r
    if _CA_POSTAL.match(up):
        return _nominatim(f"{up[:3]} {up[3:]}, Canada")
    return None


def _nominatim_reverse(lat, lon):
    """(lat, lon) -> (label, country_code). Best-effort human-readable place."""
    try:
        q = urllib.parse.urlencode({"lat": lat, "lon": lon, "format": "json",
                                    "zoom": "12", "addressdetails": "1"})
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/reverse?{q}",
            headers={"User-Agent": "TrialBridge/1.0 (clinical trial finder)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        a = data.get("address", {})
        city = (a.get("city") or a.get("town") or a.get("village")
                or a.get("municipality") or a.get("county") or "")
        parts = [p for p in (city, a.get("state"), a.get("country")) if p]
        label = ", ".join(parts) or data.get("display_name", "")
        return label, (a.get("country_code") or "").upper()
    except Exception:
        return "", ""


def units_for(country_code):
    """US uses miles; everyone else (incl. Canada) uses kilometres."""
    return "mi" if (country_code or "").upper() == "US" else "km"


@app.route("/geo/reverse")
@login_required
def geo_reverse():
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"ok": False}), 400
    label, cc = _nominatim_reverse(lat, lon)
    return jsonify({"ok": bool(label), "label": label, "cc": cc,
                    "unit": units_for(cc)})


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

    # Pick which trials to screen. With a location, keep only trials with a
    # RECRUITING site inside the radius, screen the closest first - so we never
    # surface something across the country or a site that isn't enrolling.
    picks = []  # list of (trial, sites_nearby)
    if coords:
        within = []
        for t in gated:
            near = nearby_sites(t, coords[0], coords[1], unit, radius)
            if near:                              # has a reachable recruiting site
                within.append((t, near))
        # Nearest first, but let strong concept relevance win close ties so a
        # slightly-farther on-topic trial isn't buried by an off-topic closer one.
        within.sort(key=lambda x: (round(x[1][0]["distance"], 0), -_rel(x[0])))
        picks = within[:MAX_MATCH]
    else:
        gated.sort(key=lambda t: -_rel(t))        # most on-topic into LLM budget
        picks = [(t, []) for t in gated[:MAX_MATCH]]

    results = []
    for t, near in picks:
        if mt.LLM_API_KEY:
            try:
                m = mt.llm_match(note, t)
            except Exception as e:
                m = {"verdict": "error", "score": 0, "rationale": str(e)[:120],
                     "met": [], "not_met": [], "unknown": []}
        else:
            m = {"verdict": "possible", "score": 0,
                 "rationale": "Add an LLM key for eligibility reasoning.",
                 "met": [], "not_met": [], "unknown": []}
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

        results.append({"trial": t, "match": m, "site": site,
                        "site_str": _site_str(site),
                        "coordinator": _coordinator(t, site),
                        "distance": dist, "unit": unit, "relevance": _rel(t),
                        "nearby": near[:8], "nearby_total": len(near),
                        "other_count": len(others), "other_regions": other_regions[:5]})

    def rank_key(r):
        t, m = r["trial"], r["match"]
        obs = (t.get("studyType") or "").upper() == "OBSERVATIONAL"
        # 1st relevance (verdict), 2nd distance, then quality tiebreakers.
        dist_sort = r["distance"] if r["distance"] is not None else 1e9
        local = bool(mt.sites_in_country(t, country)) if country else True
        return (mt.VERDICT_RANK.get(m.get("verdict"), 3), round(dist_sort, 1),
                0 if local else 1, 0 if not obs else 1,
                -int(r.get("relevance") or 0),
                len(m.get("not_met") or []), len(m.get("unknown") or []),
                -int(m.get("score") or 0))

    results.sort(key=rank_key)
    return search_label, results


def _site_str(site):
    s = site or {}
    return ", ".join(p for p in (s.get("facility"), s.get("city"),
                                 s.get("state"), s.get("country")) if p)


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
        "Content-Disposition": "attachment; filename=trialbridge_referrals.csv"})


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
@app.route("/c/<token>")
def candidate_page(token):
    lead = db.get_lead_by_token(token)
    if not lead:
        abort(404)
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
        _notify_applicant(token, "accepted")
        flash("Accepted. The applicant's contact details are now unlocked below "
              "so you can invite them to a screening visit.", "ok")
    else:
        flash("Couldn't accept this candidate.", "error")
    return redirect(url_for("candidate_page", token=token))


@app.route("/c/<token>/decline", methods=["POST"])
def candidate_decline(token):
    if db.decline_candidate_by_token(token, request.form.get("reason", "").strip()):
        _notify_applicant(token, "declined")
        flash("Marked as not a match. No contact details were revealed.", "ok")
    else:
        flash("Couldn't update this candidate.", "error")
    return redirect(url_for("candidate_page", token=token))


@app.route("/c/<token>/message", methods=["POST"])
def candidate_message(token):
    """Study team messages the applicant. Only after acceptance (blinded model)."""
    lead = db.get_lead_by_token(token)
    if not lead:
        abort(404)
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
    lead = db.get_lead_by_token(token)
    if not lead:
        abort(404)
    if not lead["revealed"]:
        flash("Accept the candidate first to book a visit.", "error")
        return redirect(url_for("candidate_page", token=token))
    raw = request.form.get("visit_at", "").strip()
    kind = request.form.get("kind", "screening").strip() or "screening"
    location = request.form.get("location", "").strip()
    note = request.form.get("note", "").strip()
    if raw:
        when = raw.replace("T", " ")            # datetime-local -> our format
        db.add_visit(lead["id"], when, kind, location, note)
        db.update_lead_status(lead["id"], "screening",
                              f"{kind} visit booked for {when}", actor="site")
        sysmsg = f"Your {kind} visit is booked for {when}"
        sysmsg += f" at {location}." if location else "."
        sysmsg += " We'll remind you beforehand."
        db.add_message(lead["id"], "system", sysmsg)
        _notify_applicant_visit(lead, when, location)
        flash("Visit booked and shared with the applicant.", "ok")
    else:
        flash("Pick a date and time for the visit.", "error")
    return redirect(url_for("candidate_page", token=token))


@app.route("/c/<token>/status", methods=["POST"])
def candidate_advance(token):
    status = request.form.get("status", "").strip()
    note = request.form.get("note", "").strip()
    if status in ("screening", "enrolled", "closed") and \
            db.advance_by_token(token, status, note):
        if status in ("screening", "enrolled"):
            _notify_applicant(token, status)
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
    print(f"TrialBridge on http://127.0.0.1:{port}  (LLM: "
          f"{'on' if mt.LLM_API_KEY else 'OFF - set LLM_API_KEY'})")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True,
            use_reloader=False)
