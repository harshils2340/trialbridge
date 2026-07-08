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
    created_at      TEXT NOT NULL,
    updated_at      TEXT DEFAULT ''
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

CREATE INDEX IF NOT EXISTS idx_referrals_user ON referrals(user_id);
CREATE INDEX IF NOT EXISTS idx_events_ref ON referral_events(referral_id);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);
CREATE INDEX IF NOT EXISTS idx_lead_events_lead ON lead_events(lead_id);
CREATE INDEX IF NOT EXISTS idx_search_stats_kind ON search_stats(kind, hits);
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
            eligibility, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (token, data.get("applicant_token", ""), data.get("nct", ""),
         data.get("title", ""), data.get("condition", ""),
         data.get("location", ""), data.get("site", ""), data.get("name", ""),
         data.get("email", ""), data.get("phone", ""), data.get("age", ""),
         data.get("sex", ""), data.get("notes", ""),
         1 if data.get("consent") else 0, data.get("source", "web"),
         "prescreen", data.get("screener", ""), data.get("eligibility", ""),
         ts, ts))
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


def lead_counts():
    rows = get_db().execute(
        "SELECT status, COUNT(*) n FROM leads GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


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
