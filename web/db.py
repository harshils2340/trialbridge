"""SQLite storage for TrialBridge: users, referrals, and status history.

One connection per request via Flask's `g`. Schema is created on first run
(idempotent), so there is no separate migration step to run.
"""
import datetime as dt
import pathlib
import secrets
import sqlite3

from flask import g

DB_PATH = pathlib.Path(__file__).resolve().parent / "trialbridge.db"

# Referral pipeline. "received" = the site confirmed it got the referral.
# "enrolled" is the commission-eligible terminal state.
STATUSES = ["referred", "received", "contacted", "screened", "enrolled",
            "screen_failed", "declined", "withdrawn"]
OPEN_STATUSES = {"referred", "received", "contacted", "screened"}
# Who can move a referral into each status (site = coordinator via token link).
SITE_STATUSES = ["received", "contacted", "screened", "enrolled", "screen_failed"]

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
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token         TEXT UNIQUE,
    nct           TEXT DEFAULT '',
    title         TEXT DEFAULT '',
    condition     TEXT DEFAULT '',
    location      TEXT DEFAULT '',
    site          TEXT DEFAULT '',
    name          TEXT DEFAULT '',
    email         TEXT DEFAULT '',
    phone         TEXT DEFAULT '',
    age           TEXT DEFAULT '',
    sex           TEXT DEFAULT '',
    notes         TEXT DEFAULT '',
    consent       INTEGER DEFAULT 0,
    source        TEXT DEFAULT 'web',
    status        TEXT NOT NULL DEFAULT 'new',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_referrals_user ON referrals(user_id);
CREATE INDEX IF NOT EXISTS idx_events_ref ON referral_events(referral_id);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);
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


# New columns added after the first release — applied idempotently so existing
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


def set_commission(ref_id, user_id, cents):
    db = get_db()
    if not get_referral(ref_id, user_id):
        return False
    db.execute("UPDATE referrals SET commission_cents = ? WHERE id = ?",
               (int(cents), ref_id))
    db.commit()
    return True


# --------------------------------------------------------------------------- #
# Patient leads (consumer funnel)
# --------------------------------------------------------------------------- #
def create_lead(data):
    db = get_db()
    token = gen_token()
    db.execute(
        """INSERT INTO leads
           (token, nct, title, condition, location, site, name, email, phone,
            age, sex, notes, consent, source, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (token, data.get("nct", ""), data.get("title", ""),
         data.get("condition", ""), data.get("location", ""),
         data.get("site", ""), data.get("name", ""), data.get("email", ""),
         data.get("phone", ""), data.get("age", ""), data.get("sex", ""),
         data.get("notes", ""), 1 if data.get("consent") else 0,
         data.get("source", "web"), "new", now()))
    db.commit()
    return token


def list_leads():
    return get_db().execute(
        "SELECT * FROM leads ORDER BY created_at DESC, id DESC").fetchall()


def lead_counts():
    rows = get_db().execute(
        "SELECT status, COUNT(*) n FROM leads GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def status_counts(user_id):
    rows = get_db().execute(
        "SELECT status, COUNT(*) n FROM referrals WHERE user_id = ? GROUP BY status",
        (user_id,)).fetchall()
    return {r["status"]: r["n"] for r in rows}


def commission_summary(user_id):
    """Return (enrolled_count, earned_cents, pipeline_cents) for the user."""
    rows = get_db().execute(
        "SELECT status, commission_cents FROM referrals WHERE user_id = ?",
        (user_id,)).fetchall()
    enrolled = earned = pipeline = 0
    for r in rows:
        if r["status"] == "enrolled":
            enrolled += 1
            earned += r["commission_cents"] or 0
        elif r["status"] in OPEN_STATUSES:
            pipeline += r["commission_cents"] or 0
    return enrolled, earned, pipeline
