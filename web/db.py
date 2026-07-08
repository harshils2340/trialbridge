"""SQLite storage for TrialBridge: users, referrals, and status history.

One connection per request via Flask's `g`. Schema is created on first run
(idempotent), so there is no separate migration step to run.
"""
import datetime as dt
import json
import os
import pathlib
import secrets
import sqlite3
import time

from flask import g

# DB_PATH is configurable so production can point at a persistent disk
# (e.g. a Render mounted disk). Defaults to a file next to this module.
DB_PATH = pathlib.Path(
    os.environ.get("DB_PATH")
    or (pathlib.Path(__file__).resolve().parent / "trialbridge.db"))

# Referral pipeline. "received" = the site confirmed it got the referral.
# "enrolled" is the commission-eligible terminal state.
STATUSES = ["referred", "received", "contacted", "screened", "enrolled",
            "screen_failed", "declined", "withdrawn"]
OPEN_STATUSES = {"referred", "received", "contacted", "screened"}
# Who can move a referral into each status (site = coordinator via token link).
SITE_STATUSES = ["received", "contacted", "screened", "enrolled", "screen_failed"]

# Patient "application" pipeline - the real clinical-trial funnel, Indeed-style.
# applied -> pre-screen (records/questions) -> likely eligible (site confirms)
# -> screening visit (forms + in-person checks) -> enrolled.
LEAD_PIPELINE = ["submitted", "prescreen", "eligible", "screening", "enrolled"]
LEAD_CLOSED = ["closed", "withdrawn"]
LEAD_STATUSES = LEAD_PIPELINE + LEAD_CLOSED
LEAD_LABELS = {
    "submitted": "Application submitted",
    "prescreen": "Pre-screening",
    "eligible": "Likely eligible",
    "screening": "Screening visit",
    "enrolled": "Enrolled",
    "closed": "Not a match / closed",
    "withdrawn": "Withdrawn",
}
# Plain one-liners shown to the patient under each stage.
LEAD_BLURB = {
    "submitted": "We've saved your interest and shared it with the study team.",
    "prescreen": "The study team is checking your basic details - and any records "
                 "you've connected - to see if you might fit.",
    "eligible": "Good news: the study team thinks you're likely eligible and wants "
                "to take the next step with you.",
    "screening": "Next is the screening visit - some forms and in-person checks to "
                 "confirm you qualify before you start.",
    "enrolled": "You've been enrolled in the study. Congratulations!",
    "closed": "This study isn't moving forward with your application right now - it's "
              "worth applying to other matching trials.",
    "withdrawn": "You withdrew this application.",
}
# Legacy status keys -> current pipeline (applied idempotently on startup).
_LEAD_STATUS_REMAP = {"reviewing": "prescreen", "contacted": "eligible"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    name          TEXT NOT NULL,
    specialty     TEXT DEFAULT '',
    institution   TEXT DEFAULT '',
    created_at    TEXT NOT NULL
);

-- Study-team account profile (organization + contact info).
CREATE TABLE IF NOT EXISTS site_profiles (
    user_id       INTEGER PRIMARY KEY,
    org_name      TEXT DEFAULT '',
    contact_name  TEXT DEFAULT '',
    contact_email TEXT DEFAULT '',
    contact_phone TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- A study team claims NCTs they manage. Leads are scoped by these claims.
CREATE TABLE IF NOT EXISTS study_claims (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    nct         TEXT NOT NULL,
    title       TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    UNIQUE (user_id, nct),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS referrals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    token           TEXT UNIQUE,
    nct             TEXT NOT NULL,
    title           TEXT NOT NULL,
    patient_label   TEXT DEFAULT '',
    patient_summary TEXT DEFAULT '',
    condition       TEXT DEFAULT '',
    country         TEXT DEFAULT '',
    site            TEXT DEFAULT '',
    coordinator     TEXT DEFAULT '',
    coordinator_email TEXT DEFAULT '',
    patient_name    TEXT DEFAULT '',
    patient_contact TEXT DEFAULT '',
    consent         INTEGER DEFAULT 0,
    consent_at      TEXT DEFAULT '',
    notified_at     TEXT DEFAULT '',
    verdict         TEXT DEFAULT '',
    score           INTEGER DEFAULT 0,
    rationale       TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'referred',
    commission_cents INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS referral_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    referral_id INTEGER NOT NULL,
    status      TEXT NOT NULL,
    note        TEXT DEFAULT '',
    actor       TEXT DEFAULT 'you',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (referral_id) REFERENCES referrals(id)
);

-- Patient-initiated interest ("apply to a trial"). This is the consumer funnel:
-- a normal person finds a trial and asks to be contacted. Consented leads are
-- what sponsors/sites pay for. No PHI beyond what the patient volunteers.
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT UNIQUE,
    applicant_token TEXT DEFAULT '',
    nct             TEXT DEFAULT '',
    title           TEXT DEFAULT '',
    condition       TEXT DEFAULT '',
    location        TEXT DEFAULT '',
    site            TEXT DEFAULT '',
    name            TEXT DEFAULT '',
    email           TEXT DEFAULT '',
    phone           TEXT DEFAULT '',
    age             TEXT DEFAULT '',
    sex             TEXT DEFAULT '',
    notes           TEXT DEFAULT '',
    consent         INTEGER DEFAULT 0,
    source          TEXT DEFAULT 'web',
    status          TEXT NOT NULL DEFAULT 'submitted',
    records_connected INTEGER DEFAULT 0,
    record_summary  TEXT DEFAULT '',
    screener        TEXT DEFAULT '',
    eligibility     TEXT DEFAULT '',
    decision        TEXT DEFAULT '',
    decision_reason TEXT DEFAULT '',
    decided_at      TEXT DEFAULT '',
    revealed        INTEGER DEFAULT 0,
    schedule_url    TEXT DEFAULT '',
    nudged_at       TEXT DEFAULT '',
    referred_by     TEXT DEFAULT '',
    invite_token    TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT DEFAULT ''
);

-- Physician "invite a patient" links (the trusted-messenger channel). A clinician
-- mints a link for a specific trial; a patient who opens it and applies is
-- attributed back so the clinician can see what happened (closes the refer loop).
CREATE TABLE IF NOT EXISTS invites (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    token          TEXT UNIQUE NOT NULL,
    clinician_id   INTEGER,
    clinician_name TEXT DEFAULT '',
    nct            TEXT DEFAULT '',
    title          TEXT DEFAULT '',
    condition      TEXT DEFAULT '',
    note           TEXT DEFAULT '',
    clicks         INTEGER DEFAULT 0,
    created_at     TEXT NOT NULL
);

-- Two-way messages between a patient and the study team for an application.
-- 'system' messages are automated nudges/reminders. Retention lives or dies on
-- this staying alive; every message also has an in-app surface so it works with
-- email off. Blinded model: the site may only message once the candidate is
-- revealed (accepted); the patient may message their own application anytime.
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id      INTEGER NOT NULL,
    sender       TEXT NOT NULL,          -- 'patient' | 'site' | 'system'
    body         TEXT NOT NULL,
    read_patient INTEGER DEFAULT 0,
    read_site    INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

-- Scheduled visits (screening, follow-up) a site books for a candidate. Drives
-- automatic reminders (see reminders.py) and the patient's "upcoming visit" view.
CREATE TABLE IF NOT EXISTS lead_visits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL,
    kind        TEXT DEFAULT 'screening',
    visit_at    TEXT NOT NULL,
    location    TEXT DEFAULT '',
    note        TEXT DEFAULT '',
    reminded_at TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

-- Status history for a patient application (drives the "My applications" timeline).
CREATE TABLE IF NOT EXISTS lead_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL,
    status      TEXT NOT NULL,
    note        TEXT DEFAULT '',
    actor       TEXT DEFAULT 'you',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

CREATE TABLE IF NOT EXISTS search_stats (
    term_key    TEXT PRIMARY KEY,
    term        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    hits        INTEGER DEFAULT 0,
    last_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trend_cache (
    kind        TEXT PRIMARY KEY,
    terms       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Plain-English trial summaries (see web/summarize.py). Cached per study so we
-- only rewrite each description once.
CREATE TABLE IF NOT EXISTS trial_summaries (
    nct         TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Ranked patient-search results, keyed by a short id. Persisted (not in-process
-- memory) so a trial detail page opened on a DIFFERENT gunicorn worker than the
-- one that ran the search can still read it. Pruned by age/count on write.
CREATE TABLE IF NOT EXISTS search_cache (
    sid         TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL
);

-- Patient-mediated health records, connected ONCE per applicant (see records.py).
-- Stored against the applicant token so every future application auto-fills.
CREATE TABLE IF NOT EXISTS records_profiles (
    applicant_token TEXT PRIMARY KEY,
    provider        TEXT DEFAULT '',
    age             TEXT DEFAULT '',
    sex             TEXT DEFAULT '',
    data            TEXT NOT NULL,
    summary         TEXT DEFAULT '',
    connected_at    TEXT NOT NULL
);

-- Trial alerts (saved searches). A patient registers interests once; a
-- background job (see alerts.py) watches ClinicalTrials.gov and notifies them
-- when NEW matching recruiting trials appear -> push, not pull.
CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    applicant_token TEXT NOT NULL,
    label           TEXT DEFAULT '',
    condition       TEXT DEFAULT '',
    intervention    TEXT DEFAULT '',
    location        TEXT DEFAULT '',
    lat             REAL,
    lon             REAL,
    cc              TEXT DEFAULT '',
    radius          INTEGER DEFAULT 50,
    unit            TEXT DEFAULT 'km',
    email           TEXT DEFAULT '',
    active          INTEGER DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_checked_at TEXT DEFAULT ''
);

-- Trials an alert has already seen. Baseline (is_new=0) is seeded at creation so
-- we never spam the patient with trials that already existed; genuinely new
-- matches land with is_new=1 until surfaced.
CREATE TABLE IF NOT EXISTS alert_matches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id    INTEGER NOT NULL,
    nct         TEXT NOT NULL,
    title       TEXT DEFAULT '',
    is_new      INTEGER DEFAULT 1,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (alert_id) REFERENCES alerts(id)
);

CREATE INDEX IF NOT EXISTS idx_referrals_user ON referrals(user_id);
CREATE INDEX IF NOT EXISTS idx_events_ref ON referral_events(referral_id);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);
CREATE INDEX IF NOT EXISTS idx_lead_events_lead ON lead_events(lead_id);
CREATE INDEX IF NOT EXISTS idx_search_stats_kind ON search_stats(kind, hits);
CREATE INDEX IF NOT EXISTS idx_search_cache_created ON search_cache(created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_applicant ON alerts(applicant_token);
CREATE INDEX IF NOT EXISTS idx_alert_matches_alert ON alert_matches(alert_id);
CREATE INDEX IF NOT EXISTS idx_messages_lead ON messages(lead_id);
CREATE INDEX IF NOT EXISTS idx_visits_lead ON lead_visits(lead_id);
CREATE INDEX IF NOT EXISTS idx_invites_clinician ON invites(clinician_id);
CREATE INDEX IF NOT EXISTS idx_claims_user ON study_claims(user_id);
CREATE INDEX IF NOT EXISTS idx_claims_nct ON study_claims(nct);
"""


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def gen_token():
    return secrets.token_urlsafe(16)


# New columns added after the first release - applied idempotently so existing
# databases upgrade without a manual migration step.
_MIGRATIONS = {
    "referrals": {
        "token": "TEXT",
        "coordinator_email": "TEXT DEFAULT ''",
        "patient_name": "TEXT DEFAULT ''",
        "patient_contact": "TEXT DEFAULT ''",
        "consent": "INTEGER DEFAULT 0",
        "consent_at": "TEXT DEFAULT ''",
        "notified_at": "TEXT DEFAULT ''",
    },
    "referral_events": {"actor": "TEXT DEFAULT 'you'"},
    "users": {
        "ehr_connected": "INTEGER DEFAULT 0",
        "ehr_provider": "TEXT DEFAULT ''",
        "ehr_connected_at": "TEXT DEFAULT ''",
    },
    "leads": {
        "applicant_token": "TEXT DEFAULT ''",
        "updated_at": "TEXT DEFAULT ''",
        "records_connected": "INTEGER DEFAULT 0",
        "record_summary": "TEXT DEFAULT ''",
        "screener": "TEXT DEFAULT ''",
        "eligibility": "TEXT DEFAULT ''",
        "decision": "TEXT DEFAULT ''",
        "decision_reason": "TEXT DEFAULT ''",
        "decided_at": "TEXT DEFAULT ''",
        "revealed": "INTEGER DEFAULT 0",
        "schedule_url": "TEXT DEFAULT ''",
        "nudged_at": "TEXT DEFAULT ''",
        "referred_by": "TEXT DEFAULT ''",
        "invite_token": "TEXT DEFAULT ''",
    },
}


def _migrate(con):
    for table, cols in _MIGRATIONS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    # Backfill tokens for any pre-existing referrals.
    for row in con.execute("SELECT id FROM referrals WHERE token IS NULL").fetchall():
        con.execute("UPDATE referrals SET token = ? WHERE id = ?",
                    (gen_token(), row[0]))
    # Normalize legacy lead status and backfill updated_at + an initial event.
    con.execute("UPDATE leads SET status = 'submitted' WHERE status = 'new'")
    for old, new in _LEAD_STATUS_REMAP.items():
        con.execute("UPDATE leads SET status = ? WHERE status = ?", (new, old))
        con.execute("UPDATE lead_events SET status = ? WHERE status = ?", (new, old))
    con.execute("UPDATE leads SET updated_at = created_at "
                "WHERE updated_at IS NULL OR updated_at = ''")
    for row in con.execute(
            "SELECT id, status, created_at FROM leads WHERE id NOT IN "
            "(SELECT DISTINCT lead_id FROM lead_events)").fetchall():
        con.execute(
            "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
            "VALUES (?,?,?,?,?)",
            (row[0], row[1] or "submitted", "application submitted", "you",
             row[2]))
    # Index depends on a migrated column, so create it after the ALTERs above.
    con.execute("CREATE INDEX IF NOT EXISTS idx_leads_applicant "
                "ON leads(applicant_token)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_leads_invite "
                "ON leads(invite_token)")


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    _migrate(con)
    con.commit()
    con.close()


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def create_user(email, password_hash, name, specialty="", institution=""):
    db = get_db()
    cur = db.execute(
        "INSERT INTO users (email, password_hash, name, specialty, institution, "
        "created_at) VALUES (?,?,?,?,?,?)",
        (email.lower().strip(), password_hash, name.strip(), specialty.strip(),
         institution.strip(), now()))
    db.commit()
    return cur.lastrowid


def get_user_by_email(email):
    return get_db().execute(
        "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)).fetchone()


def get_user(user_id):
    return get_db().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def set_ehr_connection(user_id, connected, provider=""):
    db = get_db()
    db.execute(
        "UPDATE users SET ehr_connected = ?, ehr_provider = ?, "
        "ehr_connected_at = ? WHERE id = ?",
        (1 if connected else 0, provider if connected else "",
         now() if connected else "", user_id))
    db.commit()


def _norm_nct(nct):
    nct = (nct or "").strip().upper()
    if not nct:
        return ""
    if not nct.startswith("NCT"):
        nct = "NCT" + nct
    return nct


# --------------------------------------------------------------------------- #
# Study-team setup (profile + claimed studies)
# --------------------------------------------------------------------------- #
def get_site_profile(user_id):
    return get_db().execute(
        "SELECT * FROM site_profiles WHERE user_id = ?", (user_id,)).fetchone()


def upsert_site_profile(user_id, org_name, contact_name, contact_email, contact_phone):
    ts = now()
    db = get_db()
    db.execute(
        "INSERT INTO site_profiles (user_id, org_name, contact_name, contact_email, "
        "contact_phone, created_at, updated_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET org_name=excluded.org_name, "
        "contact_name=excluded.contact_name, contact_email=excluded.contact_email, "
        "contact_phone=excluded.contact_phone, updated_at=excluded.updated_at",
        (user_id, (org_name or "").strip(), (contact_name or "").strip(),
         (contact_email or "").strip(), (contact_phone or "").strip(), ts, ts))
    db.commit()


def list_study_claims(user_id):
    return get_db().execute(
        "SELECT * FROM study_claims WHERE user_id = ? ORDER BY created_at DESC, id DESC",
        (user_id,)).fetchall()


def user_claimed_ncts(user_id):
    rows = get_db().execute(
        "SELECT nct FROM study_claims WHERE user_id = ?", (user_id,)).fetchall()
    return {r["nct"] for r in rows if r["nct"]}


def lead_counts_for_ncts(ncts):
    ncts = sorted({x for x in (ncts or []) if x})
    if not ncts:
        return {}
    qs = ",".join("?" * len(ncts))
    rows = get_db().execute(
        f"SELECT status, COUNT(*) n FROM leads WHERE nct IN ({qs}) GROUP BY status",
        ncts).fetchall()
    return {r["status"]: r["n"] for r in rows}


def site_contact_for_nct(nct):
    """Notification email for a claimed study (profile contact first, then login)."""
    nct = _norm_nct(nct)
    if not nct:
        return ""
    row = get_db().execute(
        "SELECT p.contact_email, u.email FROM study_claims c "
        "JOIN users u ON u.id = c.user_id "
        "LEFT JOIN site_profiles p ON p.user_id = c.user_id "
        "WHERE c.nct = ? ORDER BY c.id DESC LIMIT 1", (nct,)).fetchone()
    if not row:
        return ""
    return (row["contact_email"] or row["email"] or "").strip()


def add_study_claim(user_id, nct, title=""):
    nct = _norm_nct(nct)
    if not nct:
        return False
    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO study_claims (user_id, nct, title, created_at) "
        "VALUES (?,?,?,?)", (user_id, nct, (title or "").strip(), now()))
    db.commit()
    return True


def remove_study_claim(user_id, nct):
    nct = _norm_nct(nct)
    if not nct:
        return False
    db = get_db()
    db.execute("DELETE FROM study_claims WHERE user_id = ? AND nct = ?",
               (user_id, nct))
    db.commit()
    return True


def lead_belongs_to_user(lead_id, user_id):
    lead = get_lead(lead_id)
    if not lead:
        return False
    claims = user_claimed_ncts(user_id)
    return bool(claims and lead["nct"] in claims)


def lead_token_belongs_to_user(token, user_id):
    lead = get_lead_by_token(token)
    if not lead:
        return False
    claims = user_claimed_ncts(user_id)
    return bool(claims and lead["nct"] in claims)


# --------------------------------------------------------------------------- #
# Referrals
# --------------------------------------------------------------------------- #
def create_referral(user_id, data):
    db = get_db()
    ts = now()
    token = gen_token()
    consent = 1 if data.get("consent") else 0
    cur = db.execute(
        """INSERT INTO referrals
           (user_id, token, nct, title, patient_label, patient_summary, condition,
            country, site, coordinator, coordinator_email, patient_name,
            patient_contact, consent, consent_at, verdict, score, rationale,
            status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, token, data.get("nct", ""), data.get("title", ""),
         data.get("patient_label", ""), data.get("patient_summary", ""),
         data.get("condition", ""), data.get("country", ""),
         data.get("site", ""), data.get("coordinator", ""),
         data.get("coordinator_email", ""), data.get("patient_name", ""),
         data.get("patient_contact", ""), consent, ts if consent else "",
         data.get("verdict", ""), int(data.get("score") or 0),
         data.get("rationale", ""), "referred", ts, ts))
    ref_id = cur.lastrowid
    db.execute(
        "INSERT INTO referral_events (referral_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)", (ref_id, "referred", "referral created", "you", ts))
    db.commit()
    return ref_id


def get_referral_by_token(token):
    if not token:
        return None
    return get_db().execute(
        "SELECT * FROM referrals WHERE token = ?", (token,)).fetchone()


def mark_notified(ref_id, user_id, email=""):
    db = get_db()
    ref = get_referral(ref_id, user_id)
    if not ref:
        return False
    ts = now()
    db.execute("UPDATE referrals SET notified_at = ?, coordinator_email = ? "
               "WHERE id = ?",
               (ts, email or ref["coordinator_email"], ref_id))
    db.execute(
        "INSERT INTO referral_events (referral_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)",
        (ref_id, ref["status"], f"Referral sent to coordinator"
         f"{(' (' + email + ')') if email else ''}", "you", ts))
    db.commit()
    return True


def update_status_by_token(token, status, note="", actor="site"):
    db = get_db()
    ref = get_referral_by_token(token)
    if not ref or status not in STATUSES:
        return False
    ts = now()
    db.execute("UPDATE referrals SET status = ?, updated_at = ? WHERE id = ?",
               (status, ts, ref["id"]))
    db.execute(
        "INSERT INTO referral_events (referral_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)", (ref["id"], status, note, actor, ts))
    db.commit()
    return True


def list_referrals(user_id):
    return get_db().execute(
        "SELECT * FROM referrals WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
        (user_id,)).fetchall()


def get_referral(ref_id, user_id):
    return get_db().execute(
        "SELECT * FROM referrals WHERE id = ? AND user_id = ?",
        (ref_id, user_id)).fetchone()


def get_events(ref_id):
    return get_db().execute(
        "SELECT * FROM referral_events WHERE referral_id = ? ORDER BY id ASC",
        (ref_id,)).fetchall()


def update_status(ref_id, user_id, status, note=""):
    db = get_db()
    ref = get_referral(ref_id, user_id)
    if not ref:
        return False
    ts = now()
    db.execute("UPDATE referrals SET status = ?, updated_at = ? WHERE id = ?",
               (status, ts, ref_id))
    db.execute(
        "INSERT INTO referral_events (referral_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)", (ref_id, status, note, "you", ts))
    db.commit()
    return True


# --------------------------------------------------------------------------- #
# Patient leads (consumer funnel)
# --------------------------------------------------------------------------- #
def create_lead(data):
    db = get_db()
    token = gen_token()
    ts = now()
    cur = db.execute(
        """INSERT INTO leads
           (token, applicant_token, nct, title, condition, location, site, name,
            email, phone, age, sex, notes, consent, source, status, screener,
            eligibility, records_connected, record_summary, referred_by,
            invite_token, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (token, data.get("applicant_token", ""), data.get("nct", ""),
         data.get("title", ""), data.get("condition", ""),
         data.get("location", ""), data.get("site", ""), data.get("name", ""),
         data.get("email", ""), data.get("phone", ""), data.get("age", ""),
         data.get("sex", ""), data.get("notes", ""),
         1 if data.get("consent") else 0, data.get("source", "web"),
         "prescreen", data.get("screener", ""), data.get("eligibility", ""),
         1 if data.get("records_connected") else 0,
         data.get("record_summary", ""), data.get("referred_by", ""),
         data.get("invite_token", ""), ts, ts))
    lead_id = cur.lastrowid
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)",
        (lead_id, "submitted", "application received", "you", ts))
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)",
        (lead_id, "prescreen", "ready for study-team review", "you", ts))
    db.commit()
    return token


def accept_candidate(lead_id, note=""):
    """Study team accepts a blinded candidate: mark likely eligible and reveal
    contact + record (mutual consent - the patient already opted in on apply)."""
    lead = get_lead(lead_id)
    if not lead:
        return False
    db = get_db()
    ts = now()
    db.execute("UPDATE leads SET status = 'eligible', decision = 'accepted', "
               "decided_at = ?, revealed = 1, updated_at = ? WHERE id = ?",
               (ts, ts, lead_id))
    msg = "accepted - likely eligible" + (f": {note}" if note else "")
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)", (lead_id, "eligible", msg, "you", ts))
    db.commit()
    return True


def decline_candidate(lead_id, reason=""):
    lead = get_lead(lead_id)
    if not lead:
        return False
    db = get_db()
    ts = now()
    db.execute("UPDATE leads SET status = 'closed', decision = 'declined', "
               "decision_reason = ?, decided_at = ?, updated_at = ? WHERE id = ?",
               (reason, ts, ts, lead_id))
    msg = "not a match" + (f": {reason}" if reason else "")
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)", (lead_id, "closed", msg, "you", ts))
    db.commit()
    return True


def accept_candidate_by_token(token, note=""):
    lead = get_lead_by_token(token)
    return accept_candidate(lead["id"], note) if lead else False


def decline_candidate_by_token(token, reason=""):
    lead = get_lead_by_token(token)
    return decline_candidate(lead["id"], reason) if lead else False


def advance_by_token(token, status, note=""):
    """Coordinator moves an already-accepted candidate forward (screening,
    enrolled) via their secure link."""
    lead = get_lead_by_token(token)
    if not lead:
        return False
    return update_lead_status(lead["id"], status, note, actor="site")


def list_leads():
    return get_db().execute(
        "SELECT * FROM leads ORDER BY updated_at DESC, id DESC").fetchall()


def list_leads_for_user(user_id):
    claims = sorted(user_claimed_ncts(user_id))
    if not claims:
        return []
    qs = ",".join("?" * len(claims))
    return get_db().execute(
        f"SELECT * FROM leads WHERE nct IN ({qs}) "
        "ORDER BY updated_at DESC, id DESC", claims).fetchall()


def list_leads_by_applicant(applicant_token):
    if not applicant_token:
        return []
    return get_db().execute(
        "SELECT * FROM leads WHERE applicant_token = ? "
        "ORDER BY updated_at DESC, id DESC", (applicant_token,)).fetchall()


def applied_ncts(applicant_token):
    """NCT ids this visitor has active (non-withdrawn) applications for."""
    if not applicant_token:
        return set()
    rows = get_db().execute(
        "SELECT nct FROM leads WHERE applicant_token = ? AND status != 'withdrawn'",
        (applicant_token,)).fetchall()
    return {r["nct"] for r in rows if r["nct"]}


def count_applications(applicant_token):
    if not applicant_token:
        return 0
    r = get_db().execute(
        "SELECT COUNT(*) n FROM leads WHERE applicant_token = ? "
        "AND status != 'withdrawn'", (applicant_token,)).fetchone()
    return r["n"] if r else 0


def get_lead(lead_id):
    return get_db().execute(
        "SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()


def get_lead_by_token(token):
    if not token:
        return None
    return get_db().execute(
        "SELECT * FROM leads WHERE token = ?", (token,)).fetchone()


def get_lead_events(lead_id):
    return get_db().execute(
        "SELECT * FROM lead_events WHERE lead_id = ? ORDER BY id ASC",
        (lead_id,)).fetchall()


def update_lead_status(lead_id, status, note="", actor="you"):
    if status not in LEAD_STATUSES:
        return False
    db = get_db()
    if not get_lead(lead_id):
        return False
    ts = now()
    db.execute("UPDATE leads SET status = ?, updated_at = ? WHERE id = ?",
               (status, ts, lead_id))
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)", (lead_id, status, note, actor, ts))
    db.commit()
    return True


def set_lead_schedule(lead_id, url, actor="site"):
    """Attach the study team's booking link to an accepted candidate so the
    patient can self-schedule their screening call. Moves the app to 'screening'
    and logs it on the timeline. Returns the lead row (or None)."""
    lead = get_lead(lead_id)
    if not lead:
        return None
    db = get_db()
    ts = now()
    db.execute("UPDATE leads SET schedule_url = ?, updated_at = ? WHERE id = ?",
               (url, ts, lead_id))
    if url and lead["status"] in ("eligible", "prescreen", "submitted"):
        db.execute("UPDATE leads SET status = 'screening' WHERE id = ?", (lead_id,))
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)",
        (lead_id, "screening" if url else lead["status"],
         "sent booking link" if url else "removed booking link", actor, ts))
    db.commit()
    return get_lead(lead_id)


# --------------------------------------------------------------------------- #
# Messaging (two-way patient <-> study team, + system nudges)
# --------------------------------------------------------------------------- #
def add_message(lead_id, sender, body):
    """Append a message to an application thread. sender in
    {'patient','site','system'}. The sender's own side is marked read."""
    body = (body or "").strip()
    if not body or sender not in ("patient", "site", "system"):
        return None
    db = get_db()
    db.execute(
        "INSERT INTO messages (lead_id, sender, body, read_patient, read_site, "
        "created_at) VALUES (?,?,?,?,?,?)",
        (lead_id, sender, body, 1 if sender == "patient" else 0,
         1 if sender == "site" else 0, now()))
    db.execute("UPDATE leads SET updated_at = ? WHERE id = ?", (now(), lead_id))
    db.commit()
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def get_messages(lead_id):
    return get_db().execute(
        "SELECT * FROM messages WHERE lead_id = ? ORDER BY id ASC",
        (lead_id,)).fetchall()


def mark_thread_read(lead_id, side):
    """side in {'patient','site'} - clears that reader's unread flags."""
    col = "read_patient" if side == "patient" else "read_site"
    db = get_db()
    db.execute(f"UPDATE messages SET {col} = 1 WHERE lead_id = ?", (lead_id,))
    db.commit()


def unread_for_patient(applicant_token):
    """Messages the patient hasn't read (from site/system) across their apps."""
    if not applicant_token:
        return 0
    r = get_db().execute(
        "SELECT COUNT(*) n FROM messages m JOIN leads l ON l.id = m.lead_id "
        "WHERE l.applicant_token = ? AND m.sender != 'patient' "
        "AND m.read_patient = 0", (applicant_token,)).fetchone()
    return r["n"] if r else 0


def unread_for_site(user_id=None):
    """Patient messages the study team hasn't read (badge on the board)."""
    if user_id:
        claims = sorted(user_claimed_ncts(user_id))
        if not claims:
            return 0
        qs = ",".join("?" * len(claims))
        q = ("SELECT COUNT(*) n FROM messages m JOIN leads l ON l.id = m.lead_id "
             f"WHERE m.sender = 'patient' AND m.read_site = 0 AND l.nct IN ({qs})")
        r = get_db().execute(q, claims).fetchone()
        return r["n"] if r else 0
    r = get_db().execute(
        "SELECT COUNT(*) n FROM messages WHERE sender = 'patient' "
        "AND read_site = 0").fetchone()
    return r["n"] if r else 0


def lead_unread_for_site(lead_id):
    r = get_db().execute(
        "SELECT COUNT(*) n FROM messages WHERE lead_id = ? AND sender = 'patient' "
        "AND read_site = 0", (lead_id,)).fetchone()
    return r["n"] if r else 0


def engagement_for_ncts(ncts):
    """Message/visit counts scoped to a list/set of NCT ids."""
    ncts = sorted({x for x in (ncts or []) if x})
    if not ncts:
        return {"messages": 0, "patient_messages": 0, "visits": 0}
    qs = ",".join("?" * len(ncts))
    db = get_db()
    msgs = db.execute(
        "SELECT COUNT(*) n FROM messages m JOIN leads l ON l.id = m.lead_id "
        f"WHERE l.nct IN ({qs})", ncts).fetchone()["n"]
    from_patient = db.execute(
        "SELECT COUNT(*) n FROM messages m JOIN leads l ON l.id = m.lead_id "
        f"WHERE l.nct IN ({qs}) AND m.sender='patient'", ncts).fetchone()["n"]
    visits = db.execute(
        "SELECT COUNT(*) n FROM lead_visits v JOIN leads l ON l.id = v.lead_id "
        f"WHERE l.nct IN ({qs})", ncts).fetchone()["n"]
    return {"messages": msgs, "patient_messages": from_patient, "visits": visits}


# --------------------------------------------------------------------------- #
# Visits (site books a screening/follow-up; drives reminders)
# --------------------------------------------------------------------------- #
def add_visit(lead_id, visit_at, kind="screening", location="", note=""):
    db = get_db()
    db.execute(
        "INSERT INTO lead_visits (lead_id, kind, visit_at, location, note, "
        "created_at) VALUES (?,?,?,?,?,?)",
        (lead_id, kind or "screening", visit_at, location, note, now()))
    db.commit()
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def get_visits(lead_id):
    return get_db().execute(
        "SELECT * FROM lead_visits WHERE lead_id = ? ORDER BY visit_at ASC",
        (lead_id,)).fetchall()


def upcoming_visits_for_applicant(applicant_token):
    """Future visits across a patient's applications, joined with trial title."""
    if not applicant_token:
        return []
    return get_db().execute(
        "SELECT v.*, l.title, l.nct, l.token AS lead_token FROM lead_visits v "
        "JOIN leads l ON l.id = v.lead_id WHERE l.applicant_token = ? "
        "AND v.visit_at >= ? ORDER BY v.visit_at ASC",
        (applicant_token, now())).fetchall()


def visits_due_for_reminder(within_iso):
    """Future visits happening before `within_iso` that haven't been reminded."""
    return get_db().execute(
        "SELECT v.*, l.email, l.name, l.title, l.nct, l.applicant_token "
        "FROM lead_visits v JOIN leads l ON l.id = v.lead_id "
        "WHERE v.reminded_at = '' AND v.visit_at >= ? AND v.visit_at <= ? "
        "ORDER BY v.visit_at ASC", (now(), within_iso)).fetchall()


def mark_visit_reminded(visit_id):
    db = get_db()
    db.execute("UPDATE lead_visits SET reminded_at = ? WHERE id = ?",
               (now(), visit_id))
    db.commit()


# --------------------------------------------------------------------------- #
# Quiet-applicant nudges (retention: re-engage people who've gone silent)
# --------------------------------------------------------------------------- #
_ACTIVE_STAGES = ("submitted", "prescreen", "eligible", "screening")


def list_active_stage_leads():
    """Leads still working through the funnel (not closed/withdrawn/enrolled)."""
    qs = ",".join("?" * len(_ACTIVE_STAGES))
    return get_db().execute(
        f"SELECT * FROM leads WHERE status IN ({qs}) ORDER BY id",
        _ACTIVE_STAGES).fetchall()


def last_activity_at(lead_id, created_at):
    """Most recent timestamp across created/events/messages for a lead."""
    db = get_db()
    e = db.execute("SELECT MAX(created_at) t FROM lead_events WHERE lead_id = ?",
                   (lead_id,)).fetchone()["t"]
    m = db.execute("SELECT MAX(created_at) t FROM messages WHERE lead_id = ?",
                   (lead_id,)).fetchone()["t"]
    return max(x for x in (created_at, e, m) if x)


def set_nudged(lead_id):
    db = get_db()
    db.execute("UPDATE leads SET nudged_at = ? WHERE id = ?", (now(), lead_id))
    db.commit()


def withdraw_lead(token, applicant_token):
    """Patient withdraws their own application (must own the applicant token)."""
    lead = get_lead_by_token(token)
    if not lead or lead["applicant_token"] != applicant_token:
        return False
    return update_lead_status(lead["id"], "withdrawn",
                              "withdrawn by applicant", actor="you")


def connect_records(token, applicant_token, summary):
    """Patient connects (prototype) health records to speed up pre-screening.
    Attaches a de-identified summary and moves the application into pre-screen."""
    lead = get_lead_by_token(token)
    if not lead or lead["applicant_token"] != applicant_token:
        return False
    db = get_db()
    ts = now()
    db.execute("UPDATE leads SET records_connected = 1, record_summary = ?, "
               "updated_at = ? WHERE id = ?", (summary, ts, lead["id"]))
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)",
        (lead["id"], lead["status"], "health records connected (prototype)",
         "you", ts))
    db.commit()
    if lead["status"] == "submitted":
        update_lead_status(lead["id"], "prescreen",
                           "auto-advanced after records connected", actor="you")
    return True


# --------------------------------------------------------------------------- #
# Physician invite links ("your doctor referred you") + attribution
# --------------------------------------------------------------------------- #
def create_invite(clinician_id, clinician_name, nct, title, condition, note=""):
    db = get_db()
    token = gen_token()
    db.execute(
        "INSERT INTO invites (token, clinician_id, clinician_name, nct, title, "
        "condition, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (token, clinician_id, clinician_name, nct, title, condition, note, now()))
    db.commit()
    return token


def get_invite(token):
    return get_db().execute("SELECT * FROM invites WHERE token = ?",
                            (token,)).fetchone()


def bump_invite_clicks(token):
    db = get_db()
    db.execute("UPDATE invites SET clicks = clicks + 1 WHERE token = ?", (token,))
    db.commit()


def list_invites(clinician_id):
    """A clinician's invites, each with how many patients applied + enrolled -
    this is the clinician closing their own loop (they hear what happened)."""
    rows = get_db().execute(
        "SELECT * FROM invites WHERE clinician_id = ? ORDER BY id DESC",
        (clinician_id,)).fetchall()
    out = []
    for r in rows:
        leads = get_db().execute(
            "SELECT status FROM leads WHERE invite_token = ?", (r["token"],)
        ).fetchall()
        applied = len(leads)
        enrolled = sum(1 for l in leads if l["status"] == "enrolled")
        active = sum(1 for l in leads if l["status"] in _ACTIVE_STAGES)
        out.append({"invite": r, "applied": applied, "enrolled": enrolled,
                    "active": active})
    return out


def lead_counts():
    rows = get_db().execute(
        "SELECT status, COUNT(*) n FROM leads GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


# --------------------------------------------------------------------------- #
# Demo data - realistic (but clearly fake) candidates for the study-team board.
# --------------------------------------------------------------------------- #
def _demo_record(cond, age, sex):
    who = " ".join(x for x in [age and f"{age}yo", sex] if x) or "adult"
    return (
        f"DEMO de-identified record for a {who} patient.\n"
        f"- Active problems: {cond}\n"
        f"- Medications: metformin 1000mg BID; atorvastatin 20mg\n"
        f"- Recent labs: BMI 34.2, HbA1c 7.8%, eGFR 88\n"
        f"- No prior investigational-drug participation on file.\n"
        f"Sample data to demonstrate records-based pre-screening.")


def _demo_lead_specs():
    """A small, staged set covering the whole funnel: several awaiting review,
    plus accepted / declined / screening / enrolled so the board looks real."""
    scr_ok = {"travel": "yes", "other_trial": "no",
              "pregnancy": "no", "consent_capable": "yes"}
    return [
        # ---- Awaiting review (prescreen, no decision) ----------------------
        {"days": 1, "status": "prescreen", "records": 1,
         "nct": "NCT05869903",
         "title": "Once-Weekly Semaglutide in Adults With Obesity",
         "condition": "Obesity", "location": "Toronto, ON",
         "site": "Toronto Metabolic Research Centre",
         "name": "Priya N.", "email": "priya.n@example.com",
         "phone": "+1 416 555 0148", "age": "44", "sex": "female",
         "screener": scr_ok,
         "elig": {"met": ["Age within 18-75", "BMI \u2265 30",
                          "No diabetes required for this arm"],
                  "unknown": ["Weight stable for 3 months (confirm at visit)",
                              "No thyroid cancer history (confirm)"],
                  "not_met": [],
                  "rationale": "Meets the core obesity criteria; two items to "
                               "confirm at the screening visit."}},
        {"days": 2, "status": "prescreen", "records": 0,
         "nct": "NCT06034262",
         "title": "Tirzepatide vs Placebo for Type 2 Diabetes and Weight",
         "condition": "Type 2 Diabetes", "location": "Mississauga, ON",
         "site": "Trillium Clinical Trials",
         "name": "Marcus L.", "email": "marcus.l@example.com",
         "phone": "+1 905 555 0193", "age": "58", "sex": "male",
         "screener": scr_ok,
         "elig": {"met": ["Type 2 diabetes diagnosis", "Age within range"],
                  "unknown": ["HbA1c 7-10% (not on file)",
                              "Stable metformin \u2265 3 months",
                              "eGFR \u2265 45 (not on file)"],
                  "not_met": [],
                  "rationale": "Likely fit but no records connected, so several "
                               "labs need to be checked."}},
        {"days": 3, "status": "prescreen", "records": 0,
         "nct": "NCT05929066",
         "title": "Investigational GLP-1/GIP Co-agonist for Weight Management",
         "condition": "Obesity", "location": "Hamilton, ON",
         "site": "Hamilton Health Research",
         "name": "Dana K.", "email": "dana.k@example.com",
         "phone": "+1 289 555 0170", "age": "36", "sex": "female",
         "screener": {"travel": "yes", "other_trial": "yes",
                      "pregnancy": "no", "consent_capable": "yes"},
         "elig": {"met": ["Age within range", "BMI \u2265 30"],
                  "unknown": ["Willing to stop current weight meds"],
                  "not_met": ["Currently enrolled in another interventional "
                              "trial (washout may be required)"],
                  "rationale": "Screener flags an active trial - confirm washout "
                               "period before proceeding."}},
        {"days": 4, "status": "prescreen", "records": 1,
         "nct": "NCT05442919",
         "title": "Resmetirom for Nonalcoholic Steatohepatitis (NASH)",
         "condition": "NASH (MASH)", "location": "Ottawa, ON",
         "site": "Ottawa Liver Institute",
         "name": "Robert P.", "email": "robert.p@example.com",
         "phone": "+1 613 555 0125", "age": "61", "sex": "male",
         "screener": scr_ok,
         "elig": {"met": ["Biopsy-confirmed NASH on record", "Age within range",
                          "F2-F3 fibrosis"],
                  "unknown": ["No decompensated cirrhosis (confirm imaging)"],
                  "not_met": [],
                  "rationale": "Strong fit from the connected record; one imaging "
                               "item to confirm."}},
        # ---- Reviewed: accepted -> likely eligible (contact revealed) ------
        {"days": 5, "status": "eligible", "decision": "accepted", "revealed": 1,
         "records": 1, "nct": "NCT05869903",
         "title": "Once-Weekly Semaglutide in Adults With Obesity",
         "condition": "Obesity", "location": "London, ON",
         "site": "Western Metabolic Clinic",
         "name": "Sarah M.", "email": "sarah.m@example.com",
         "phone": "+1 519 555 0132", "age": "52", "sex": "female",
         "screener": scr_ok, "accepted_days": 2,
         "elig": {"met": ["Age within range", "BMI \u2265 30", "No exclusionary "
                          "conditions on record"],
                  "unknown": [], "not_met": [],
                  "rationale": "Clear fit - moved to likely eligible."}},
        # ---- Reviewed: declined (no reveal) --------------------------------
        {"days": 6, "status": "closed", "decision": "declined", "revealed": 0,
         "records": 0, "nct": "NCT06034262",
         "title": "Tirzepatide vs Placebo for Type 2 Diabetes and Weight",
         "condition": "Type 2 Diabetes", "location": "Kitchener, ON",
         "site": "Grand River Trials",
         "name": "Emily R.", "email": "emily.r@example.com",
         "phone": "+1 226 555 0188", "age": "29", "sex": "female",
         "screener": {"travel": "yes", "other_trial": "no",
                      "pregnancy": "yes", "consent_capable": "yes"},
         "reason": "Screener flag: pregnancy - excluded per protocol",
         "declined_days": 3,
         "elig": {"met": ["Type 2 diabetes diagnosis"],
                  "unknown": ["HbA1c on file"],
                  "not_met": ["Pregnancy is an exclusion criterion"],
                  "rationale": "Not eligible due to protocol exclusion."}},
        # ---- Reviewed: at screening visit ----------------------------------
        {"days": 7, "status": "screening", "decision": "accepted", "revealed": 1,
         "records": 1, "nct": "NCT05869903",
         "title": "Once-Weekly Semaglutide in Adults With Obesity",
         "condition": "Obesity", "location": "Toronto, ON",
         "site": "Toronto Metabolic Research Centre",
         "name": "James T.", "email": "james.t@example.com",
         "phone": "+1 416 555 0161", "age": "47", "sex": "male",
         "screener": scr_ok, "accepted_days": 4, "screening_days": 1,
         "elig": {"met": ["Age within range", "BMI \u2265 30"],
                  "unknown": [], "not_met": [],
                  "rationale": "Invited to screening visit."}},
        # ---- Reviewed: enrolled --------------------------------------------
        {"days": 9, "status": "enrolled", "decision": "accepted", "revealed": 1,
         "records": 1, "nct": "NCT06034262",
         "title": "Tirzepatide vs Placebo for Type 2 Diabetes and Weight",
         "condition": "Type 2 Diabetes", "location": "Mississauga, ON",
         "site": "Trillium Clinical Trials",
         "name": "Linda C.", "email": "linda.c@example.com",
         "phone": "+1 905 555 0117", "age": "55", "sex": "female",
         "screener": scr_ok, "accepted_days": 6, "screening_days": 3,
         "enrolled_days": 1,
         "elig": {"met": ["Type 2 diabetes diagnosis", "Age within range",
                          "HbA1c in range"],
                  "unknown": [], "not_met": [],
                  "rationale": "Completed screening and enrolled."}},
    ]


def seed_demo_leads():
    """Populate clearly-labelled DEMO candidates so the study-team review board
    shows a full end-to-end picture before any real applicants arrive.
    No-op if any leads already exist (so real data is never mixed with demo)."""
    con = sqlite3.connect(DB_PATH)
    try:
        if con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]:
            return
        base = dt.datetime.now()

        def at(days_ago):
            return (base - dt.timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M")

        for s in _demo_lead_specs():
            created = at(s["days"])
            decided = at(s.get("accepted_days") or s.get("declined_days") or 0) \
                if (s.get("accepted_days") or s.get("declined_days")) else ""
            rec = _demo_record(s["condition"], s["age"], s["sex"]) \
                if s.get("records") else ""
            cur = con.execute(
                """INSERT INTO leads
                   (token, applicant_token, nct, title, condition, location, site,
                    name, email, phone, age, sex, notes, consent, source, status,
                    records_connected, record_summary, screener, eligibility,
                    decision, decision_reason, decided_at, revealed,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (gen_token(), "demo-" + gen_token(), s["nct"], s["title"],
                 s["condition"], s["location"], s["site"], s["name"], s["email"],
                 s["phone"], s["age"], s["sex"], "", 1, "demo", s["status"],
                 s.get("records", 0), rec, json.dumps(s["screener"]),
                 json.dumps(s["elig"]), s.get("decision", ""),
                 s.get("reason", ""), decided, s.get("revealed", 0),
                 created, decided or created))
            lid = cur.lastrowid
            # Build a plausible event trail for the timeline.
            evs = [("submitted", "application received", "you", s["days"]),
                   ("prescreen", "ready for study-team review", "you", s["days"])]
            if s.get("accepted_days"):
                evs.append(("eligible", "accepted - likely eligible", "you",
                            s["accepted_days"]))
            if s.get("declined_days"):
                evs.append(("closed", "not a match: " + s.get("reason", ""),
                            "you", s["declined_days"]))
            if s.get("screening_days"):
                evs.append(("screening", "invited to screening visit", "site",
                            s["screening_days"]))
            if s.get("enrolled_days"):
                evs.append(("enrolled", "enrolled in study", "site",
                            s["enrolled_days"]))
            for status, note, actor, days in evs:
                con.execute(
                    "INSERT INTO lead_events (lead_id, status, note, actor, "
                    "created_at) VALUES (?,?,?,?,?)",
                    (lid, status, note, actor, at(days)))
        con.commit()
    finally:
        con.close()


def seed_demo_engagement(clinician_id):
    """Populate the retention/engagement surfaces (messages, visits, one physician
    referral) on top of the demo leads so a fresh demo shows the whole loop alive.
    Idempotent: no-op once any message exists."""
    db = get_db()
    base = dt.datetime.now()

    def ts(days=0, hours=0):
        return (base + dt.timedelta(days=days, hours=hours)).strftime("%Y-%m-%d %H:%M")

    # Always ensure the demo study-team setup exists (claimed NCTs + profile),
    # even if message seeding already ran in a previous session.
    if clinician_id:
        study_rows = db.execute(
            "SELECT DISTINCT nct, title FROM leads WHERE nct != '' ORDER BY nct"
        ).fetchall()
        for s in study_rows[:3]:
            db.execute(
                "INSERT OR IGNORE INTO study_claims (user_id, nct, title, created_at) "
                "VALUES (?,?,?,?)",
                (clinician_id, s["nct"], s["title"] or "", ts(days=-10)))
        db.execute(
            "INSERT OR IGNORE INTO site_profiles (user_id, org_name, contact_name, "
            "contact_email, contact_phone, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (clinician_id, "Demo Research Site", "Demo Coordinator",
             "coordinator@demo-site.example", "+1 416 555 0199", ts(days=-10),
             ts(days=-1)))
    if db.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]:
        db.commit()
        return

    # Threads on the accepted/revealed candidates so neither side looks empty.
    revealed = db.execute(
        "SELECT * FROM leads WHERE revealed = 1 ORDER BY id").fetchall()
    for ld in revealed:
        first = ld["name"].split()[0] if ld["name"] else "there"
        db.execute("INSERT INTO messages (lead_id, sender, body, read_patient, "
                   "read_site, created_at) VALUES (?,?,?,?,?,?)",
                   (ld["id"], "site",
                    f"Hi {first}, thanks for applying - we've reviewed your details "
                    "and would love to take the next step. Any questions so far?",
                    1, 1, ts(hours=-30)))
        db.execute("INSERT INTO messages (lead_id, sender, body, read_patient, "
                   "read_site, created_at) VALUES (?,?,?,?,?,?)",
                   (ld["id"], "patient",
                    "Thank you! Yes - roughly how many visits are involved, and is "
                    "parking available?", 1, 0, ts(hours=-26)))

    # A visit on the screening/enrolled candidates (drives reminders + patient view).
    for ld in revealed:
        if ld["status"] in ("screening", "enrolled"):
            when = ts(days=2, hours=3) if ld["status"] == "screening" else ts(days=-2)
            db.execute("INSERT INTO lead_visits (lead_id, kind, visit_at, location, "
                       "note, reminded_at, created_at) VALUES (?,?,?,?,?,?,?)",
                       (ld["id"], "screening", when, ld["site"] or "Study site",
                        "Please bring a photo ID. Allow about 90 minutes.",
                        "", ts(hours=-20)))
            db.execute("INSERT INTO messages (lead_id, sender, body, read_patient, "
                       "read_site, created_at) VALUES (?,?,?,?,?,?)",
                       (ld["id"], "system",
                        f"Your screening visit is booked for {when} at "
                        f"{ld['site'] or 'the study site'}. We'll remind you beforehand.",
                        1, 1, ts(hours=-20)))

    # One physician referral with attribution, so the invite view + funnel show the
    # trusted-messenger channel producing a real applicant.
    if clinician_id:
        tok = gen_token()
        db.execute(
            "INSERT INTO invites (token, clinician_id, clinician_name, nct, title, "
            "condition, note, clicks, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (tok, clinician_id, "Demo Clinician", "NCT05869903",
             "Once-Weekly Semaglutide in Adults With Obesity", "Obesity",
             "I think this could be a good fit for you - worth a look.", 3,
             ts(days=-6)))
        target = db.execute(
            "SELECT id FROM leads WHERE nct = 'NCT05869903' AND status = 'eligible' "
            "ORDER BY id LIMIT 1").fetchone()
        if target:
            db.execute("UPDATE leads SET referred_by = 'Demo Clinician', "
                       "invite_token = ?, source = 'referral' WHERE id = ?",
                       (tok, target["id"]))
    db.commit()


# --------------------------------------------------------------------------- #
# Search traffic - powers the live "trending" chips on the landing page.
# --------------------------------------------------------------------------- #
def log_search_term(term, kind):
    """Record one search/visit for a term so trending reflects real traffic.
    kind is 'condition' or 'drug'. Safe to call on every request (best-effort)."""
    term = (term or "").strip()
    if not term or kind not in ("condition", "drug"):
        return
    key = kind + ":" + " ".join(term.lower().split())
    db = get_db()
    ts = now()
    db.execute(
        "INSERT INTO search_stats (term_key, term, kind, hits, last_at) "
        "VALUES (?,?,?,1,?) "
        "ON CONFLICT(term_key) DO UPDATE SET hits = hits + 1, "
        "term = excluded.term, last_at = excluded.last_at",
        (key, term, kind, ts))
    db.commit()


def top_terms(kind, limit=8):
    """Most-searched terms for a kind, busiest first."""
    rows = get_db().execute(
        "SELECT term FROM search_stats WHERE kind = ? "
        "ORDER BY hits DESC, last_at DESC LIMIT ?", (kind, limit)).fetchall()
    return [r["term"] for r in rows]


# --------------------------------------------------------------------------- #
# Search result cache - shared across gunicorn workers (see web/app.py). Kept in
# SQLite (not process memory) so a trial detail request served by a different
# worker than the one that ran the search can still find the ranked results.
# --------------------------------------------------------------------------- #
SEARCH_CACHE_TTL = 24 * 3600   # results are good for a day
SEARCH_CACHE_MAX = 500         # hard cap on rows kept


def save_search(sid, payload):
    """Persist a search result blob (JSON string) under a short id, then prune
    anything older than the TTL and trim back to SEARCH_CACHE_MAX rows."""
    con = get_db()
    ts = time.time()
    con.execute(
        "INSERT OR REPLACE INTO search_cache (sid, payload, created_at) "
        "VALUES (?,?,?)", (sid, payload, ts))
    con.execute("DELETE FROM search_cache WHERE created_at < ?",
                (ts - SEARCH_CACHE_TTL,))
    con.execute(
        "DELETE FROM search_cache WHERE sid NOT IN "
        "(SELECT sid FROM search_cache ORDER BY created_at DESC LIMIT ?)",
        (SEARCH_CACHE_MAX,))
    con.commit()


def get_search(sid):
    """Return the stored payload (JSON string) for a search id, or None."""
    row = get_db().execute(
        "SELECT payload FROM search_cache WHERE sid = ?", (sid,)).fetchone()
    return row["payload"] if row else None


# --------------------------------------------------------------------------- #
# Trending cache - externally-sourced trending terms (see web/trends.py).
# --------------------------------------------------------------------------- #
def get_trend_cache(kind):
    """Return (terms:list, updated_at:str|None) for a trending kind."""
    row = get_db().execute(
        "SELECT terms, updated_at FROM trend_cache WHERE kind = ?",
        (kind,)).fetchone()
    if not row:
        return [], None
    try:
        terms = json.loads(row["terms"])
    except (ValueError, TypeError):
        terms = []
    return terms, row["updated_at"]


def set_trend_cache(kind, terms):
    db = get_db()
    db.execute(
        "INSERT INTO trend_cache (kind, terms, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(kind) DO UPDATE SET terms = excluded.terms, "
        "updated_at = excluded.updated_at",
        (kind, json.dumps(list(terms)), now()))
    db.commit()


# --------------------------------------------------------------------------- #
# Plain-English trial summaries (see web/summarize.py)
# --------------------------------------------------------------------------- #
def get_trial_summary(nct):
    if not nct:
        return None
    row = get_db().execute(
        "SELECT data FROM trial_summaries WHERE nct = ?", (nct,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["data"])
    except (ValueError, TypeError):
        return None


def set_trial_summary(nct, data):
    if not nct:
        return
    db = get_db()
    db.execute(
        "INSERT INTO trial_summaries (nct, data, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(nct) DO UPDATE SET data = excluded.data, "
        "updated_at = excluded.updated_at",
        (nct, json.dumps(data), now()))
    db.commit()


# --------------------------------------------------------------------------- #
# Patient-mediated records profile (connected once, see records.py)
# --------------------------------------------------------------------------- #
def get_records_profile(applicant_token):
    if not applicant_token:
        return None
    row = get_db().execute(
        "SELECT * FROM records_profiles WHERE applicant_token = ?",
        (applicant_token,)).fetchone()
    if not row:
        return None
    try:
        prof = json.loads(row["data"])
    except (ValueError, TypeError):
        prof = {}
    prof.setdefault("provider", row["provider"])
    prof.setdefault("summary", row["summary"])
    prof["age"] = row["age"] or prof.get("age")
    prof["sex"] = row["sex"] or prof.get("sex")
    return prof


def set_records_profile(applicant_token, prof):
    if not applicant_token or not prof:
        return
    db = get_db()
    age = "" if prof.get("age") in (None, "") else str(prof.get("age"))
    db.execute(
        "INSERT INTO records_profiles "
        "(applicant_token, provider, age, sex, data, summary, connected_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(applicant_token) DO UPDATE SET provider = excluded.provider, "
        "age = excluded.age, sex = excluded.sex, data = excluded.data, "
        "summary = excluded.summary, connected_at = excluded.connected_at",
        (applicant_token, prof.get("provider", ""), age, prof.get("sex", ""),
         json.dumps(prof), prof.get("summary", ""), now()))
    db.commit()


def clear_records_profile(applicant_token):
    if not applicant_token:
        return
    db = get_db()
    db.execute("DELETE FROM records_profiles WHERE applicant_token = ?",
               (applicant_token,))
    db.commit()


# --------------------------------------------------------------------------- #
# Trial alerts (saved searches, see alerts.py)
# --------------------------------------------------------------------------- #
def create_alert(data):
    db = get_db()
    cur = db.execute(
        """INSERT INTO alerts
           (applicant_token, label, condition, intervention, location, lat, lon,
            cc, radius, unit, email, active, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)""",
        (data.get("applicant_token", ""), data.get("label", ""),
         data.get("condition", ""), data.get("intervention", ""),
         data.get("location", ""), data.get("lat"), data.get("lon"),
         data.get("cc", ""), int(data.get("radius") or 50),
         data.get("unit", "km"), data.get("email", ""), now()))
    db.commit()
    return cur.lastrowid


def list_alerts(applicant_token):
    if not applicant_token:
        return []
    return get_db().execute(
        "SELECT * FROM alerts WHERE applicant_token = ? ORDER BY id DESC",
        (applicant_token,)).fetchall()


def list_active_alerts():
    return get_db().execute(
        "SELECT * FROM alerts WHERE active = 1 ORDER BY id").fetchall()


def get_alert(alert_id):
    return get_db().execute(
        "SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()


def delete_alert(alert_id, applicant_token):
    a = get_alert(alert_id)
    if not a or a["applicant_token"] != applicant_token:
        return False
    db = get_db()
    db.execute("DELETE FROM alert_matches WHERE alert_id = ?", (alert_id,))
    db.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    db.commit()
    return True


def alert_seen_ncts(alert_id):
    rows = get_db().execute(
        "SELECT nct FROM alert_matches WHERE alert_id = ?", (alert_id,)).fetchall()
    return {r["nct"] for r in rows}


def add_alert_matches(alert_id, matches, is_new):
    """matches: iterable of (nct, title). Skips NCTs already recorded."""
    db = get_db()
    seen = alert_seen_ncts(alert_id)
    ts = now()
    added = 0
    for nct, title in matches:
        if not nct or nct in seen:
            continue
        db.execute(
            "INSERT INTO alert_matches (alert_id, nct, title, is_new, created_at) "
            "VALUES (?,?,?,?,?)",
            (alert_id, nct, title or "", 1 if is_new else 0, ts))
        seen.add(nct)
        added += 1
    db.commit()
    return added


def mark_alert_checked(alert_id):
    db = get_db()
    db.execute("UPDATE alerts SET last_checked_at = ? WHERE id = ?",
               (now(), alert_id))
    db.commit()


def get_alert_matches(alert_id, limit=20):
    return get_db().execute(
        "SELECT * FROM alert_matches WHERE alert_id = ? "
        "ORDER BY is_new DESC, id DESC LIMIT ?", (alert_id, limit)).fetchall()


def new_matches_count(applicant_token):
    if not applicant_token:
        return 0
    r = get_db().execute(
        "SELECT COUNT(*) n FROM alert_matches m JOIN alerts a ON a.id = m.alert_id "
        "WHERE a.applicant_token = ? AND m.is_new = 1",
        (applicant_token,)).fetchone()
    return r["n"] if r else 0


def clear_new_flags(applicant_token):
    """Called when the patient views their alerts, so the nav badge resets."""
    if not applicant_token:
        return
    db = get_db()
    db.execute(
        "UPDATE alert_matches SET is_new = 0 WHERE alert_id IN "
        "(SELECT id FROM alerts WHERE applicant_token = ?)", (applicant_token,))
    db.commit()


def attach_records_to_open_leads(applicant_token, summary):
    """When a patient connects records, backfill their pending (not-yet-decided)
    applications so the study team sees the record on those too."""
    if not applicant_token:
        return 0
    db = get_db()
    ts = now()
    rows = db.execute(
        "SELECT id FROM leads WHERE applicant_token = ? AND decision = '' "
        "AND records_connected = 0", (applicant_token,)).fetchall()
    for r in rows:
        db.execute("UPDATE leads SET records_connected = 1, record_summary = ?, "
                   "updated_at = ? WHERE id = ?", (summary, ts, r["id"]))
        db.execute(
            "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
            "VALUES (?,?,?,?,?)",
            (r["id"], "prescreen", "health records connected", "you", ts))
    db.commit()
    return len(rows)


def status_counts(user_id):
    rows = get_db().execute(
        "SELECT status, COUNT(*) n FROM referrals WHERE user_id = ? GROUP BY status",
        (user_id,)).fetchall()
    return {r["status"]: r["n"] for r in rows}


def enrolled_count(user_id):
    """Number of this user's referrals that reached the 'enrolled' state. Pure
    outcome tracking - TrialBridge never pays clinicians per referral/enrollment
    (anti-kickback / fee-splitting), so there is no money attached."""
    row = get_db().execute(
        "SELECT COUNT(*) AS n FROM referrals WHERE user_id = ? AND status = ?",
        (user_id, "enrolled")).fetchone()
    return row["n"] if row else 0
