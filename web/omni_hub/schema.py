"""Omni tables. Separate from db.SCHEMA on purpose: zero edits to db.py, and
the whole feature can move to its own service by copying this directory.

Every table cascades from om_workspaces, so deleting a workspace (or sweeping
stale demo workspaces) removes everything it owned. ``db.get_db`` turns
foreign keys on, which is what makes the cascade real.
"""
import sqlite3

import db

OMNI_SCHEMA = """
CREATE TABLE IF NOT EXISTS om_workspaces (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wid           TEXT UNIQUE NOT NULL,
    owner_token   TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    template_key  TEXT NOT NULL DEFAULT 'generic',
    origin        TEXT NOT NULL DEFAULT 'builder',
    intro_prompt  TEXT NOT NULL DEFAULT '',
    spec_json     TEXT NOT NULL DEFAULT '{}',
    draft_json    TEXT NOT NULL DEFAULT '{}',
    builder_state TEXT NOT NULL DEFAULT 'intro',
    builder_json  TEXT NOT NULL DEFAULT '{}',
    spec_version  INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_om_ws_seen ON om_workspaces(last_seen_at);

CREATE TABLE IF NOT EXISTS om_sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id  INTEGER NOT NULL REFERENCES om_workspaces(id) ON DELETE CASCADE,
    key           TEXT NOT NULL,
    kind          TEXT NOT NULL,
    mode          TEXT NOT NULL DEFAULT 'api',
    label         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'disconnected',
    simulated     INTEGER NOT NULL DEFAULT 1,
    connected_at  TEXT NOT NULL DEFAULT '',
    meta_json     TEXT NOT NULL DEFAULT '{}',
    UNIQUE(workspace_id, key)
);

CREATE TABLE IF NOT EXISTS om_contacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id  INTEGER NOT NULL REFERENCES om_workspaces(id) ON DELETE CASCADE,
    name          TEXT NOT NULL DEFAULT '',
    handle        TEXT NOT NULL DEFAULT '',
    email         TEXT NOT NULL DEFAULT '',
    phone         TEXT NOT NULL DEFAULT '',
    meta_json     TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    UNIQUE(workspace_id, handle)
);

CREATE TABLE IF NOT EXISTS om_conversations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id    INTEGER NOT NULL REFERENCES om_workspaces(id) ON DELETE CASCADE,
    source_id       INTEGER NOT NULL REFERENCES om_sources(id) ON DELETE CASCADE,
    contact_id      INTEGER REFERENCES om_contacts(id) ON DELETE SET NULL,
    seed_key        TEXT,
    subject         TEXT NOT NULL DEFAULT '',
    stage           TEXT NOT NULL DEFAULT '',
    assignee        TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'open',
    unread          INTEGER NOT NULL DEFAULT 1,
    priority        TEXT NOT NULL DEFAULT 'normal',
    flags_json      TEXT NOT NULL DEFAULT '[]',
    last_message_at TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(workspace_id, seed_key)
);
CREATE INDEX IF NOT EXISTS idx_om_conv_ws ON om_conversations(workspace_id, status, last_message_at);

CREATE TABLE IF NOT EXISTS om_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id    INTEGER NOT NULL REFERENCES om_workspaces(id) ON DELETE CASCADE,
    conversation_id INTEGER NOT NULL REFERENCES om_conversations(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    author          TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    delivery_status TEXT NOT NULL DEFAULT '',
    by_agent        INTEGER NOT NULL DEFAULT 0,
    action_token    TEXT NOT NULL DEFAULT '',
    sent_at         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_om_msg_conv ON om_messages(conversation_id, sent_at);

CREATE TABLE IF NOT EXISTS om_fields (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id  INTEGER NOT NULL REFERENCES om_workspaces(id) ON DELETE CASCADE,
    key           TEXT NOT NULL,
    label         TEXT NOT NULL,
    type          TEXT NOT NULL DEFAULT 'text',
    options_json  TEXT NOT NULL DEFAULT '[]',
    hint          TEXT NOT NULL DEFAULT '',
    filterable    INTEGER NOT NULL DEFAULT 1,
    show_in_list  INTEGER NOT NULL DEFAULT 0,
    protected     INTEGER NOT NULL DEFAULT 0,
    position      INTEGER NOT NULL DEFAULT 0,
    origin        TEXT NOT NULL DEFAULT 'template',
    UNIQUE(workspace_id, key)
);

CREATE TABLE IF NOT EXISTS om_field_values (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id      INTEGER NOT NULL REFERENCES om_workspaces(id) ON DELETE CASCADE,
    conversation_id   INTEGER NOT NULL REFERENCES om_conversations(id) ON DELETE CASCADE,
    field_key         TEXT NOT NULL,
    value             TEXT NOT NULL DEFAULT 'null',
    confidence        REAL NOT NULL DEFAULT 0,
    quote             TEXT NOT NULL DEFAULT '',
    source_message_id INTEGER REFERENCES om_messages(id) ON DELETE SET NULL,
    provenance        TEXT NOT NULL DEFAULT 'canned',
    status            TEXT NOT NULL DEFAULT 'unknown',
    updated_at        TEXT NOT NULL,
    UNIQUE(workspace_id, conversation_id, field_key)
);
CREATE INDEX IF NOT EXISTS idx_om_fv_conv ON om_field_values(conversation_id);

CREATE TABLE IF NOT EXISTS om_views (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id  INTEGER NOT NULL REFERENCES om_workspaces(id) ON DELETE CASCADE,
    key           TEXT NOT NULL,
    name          TEXT NOT NULL,
    filter_json   TEXT NOT NULL DEFAULT '{}',
    sort_json     TEXT NOT NULL DEFAULT '{}',
    position      INTEGER NOT NULL DEFAULT 0,
    pinned        INTEGER NOT NULL DEFAULT 0,
    origin        TEXT NOT NULL DEFAULT 'template',
    UNIQUE(workspace_id, key)
);

CREATE TABLE IF NOT EXISTS om_rules (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id   INTEGER NOT NULL REFERENCES om_workspaces(id) ON DELETE CASCADE,
    key            TEXT NOT NULL,
    name           TEXT NOT NULL,
    text           TEXT NOT NULL DEFAULT '',
    trigger        TEXT NOT NULL DEFAULT 'on_extract',
    condition_json TEXT NOT NULL DEFAULT '{}',
    action_json    TEXT NOT NULL DEFAULT '{}',
    enabled        INTEGER NOT NULL DEFAULT 1,
    origin         TEXT NOT NULL DEFAULT 'template',
    UNIQUE(workspace_id, key)
);

CREATE TABLE IF NOT EXISTS om_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT UNIQUE NOT NULL,
    workspace_id    INTEGER NOT NULL REFERENCES om_workspaces(id) ON DELETE CASCADE,
    conversation_id INTEGER REFERENCES om_conversations(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    policy          TEXT NOT NULL DEFAULT 'review',
    status          TEXT NOT NULL DEFAULT 'proposed',
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    confirmed_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_om_actions_token ON om_actions(token);

CREATE TABLE IF NOT EXISTS om_builder_turns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id  INTEGER NOT NULL REFERENCES om_workspaces(id) ON DELETE CASCADE,
    turn_no       INTEGER NOT NULL,
    role          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    body          TEXT NOT NULL DEFAULT '',
    payload_json  TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_om_turns_ws ON om_builder_turns(workspace_id, turn_no);

CREATE TABLE IF NOT EXISTS om_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id    INTEGER NOT NULL REFERENCES om_workspaces(id) ON DELETE CASCADE,
    conversation_id INTEGER,
    kind            TEXT NOT NULL,
    detail_json     TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_om_events_ws ON om_events(workspace_id, id);
"""

# Idempotent column additions, same shape as db._migrate: {table: [(col, ddl)]}.
_MIGRATIONS = {}


def init_schema():
    con = sqlite3.connect(db.DB_PATH)
    try:
        con.executescript(OMNI_SCHEMA)
        for table, cols in _MIGRATIONS.items():
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            for col, ddl in cols:
                if col not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        con.commit()
    finally:
        con.close()
