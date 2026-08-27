"""Data access for the om_* tables. Plain functions over db.get_db(); every
row comes back as a dict with JSON columns already decoded.

Conventions:
  * timestamps are ``YYYY-MM-DD HH:MM`` strings (db.now()), so they sort as text
  * ``value`` in om_field_values is a JSON-encoded scalar so bool/number/list
    round-trip; use field_value()/decode_value() rather than the raw column
"""
import datetime as dt
import json
import re
import secrets

import db


def now():
    return db.now()


def ts(minutes_ago=0):
    return (dt.datetime.now() - dt.timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%d %H:%M")


def _j(raw, default):
    try:
        v = json.loads(raw) if raw not in (None, "") else default
        return v if v is not None else default
    except Exception:
        return default


def _d(row):
    return dict(row) if row is not None else None


def _dws(row):
    ws = _d(row)
    if not ws:
        return None
    ws["spec"] = _j(ws.get("spec_json"), {})
    ws["draft"] = _j(ws.get("draft_json"), {})
    ws["builder"] = _j(ws.get("builder_json"), {})
    return ws


# --------------------------------------------------------------------------- #
# Workspaces
# --------------------------------------------------------------------------- #
def new_wid():
    return secrets.token_urlsafe(9)


def create_workspace(name, template_key, origin="builder", intro_prompt="",
                     spec=None, draft=None, state="intro", builder=None):
    con = db.get_db()
    t = now()
    wid = new_wid()
    owner = db.gen_token()
    con.execute(
        "INSERT INTO om_workspaces (wid, owner_token, name, template_key, origin, "
        "intro_prompt, spec_json, draft_json, builder_state, builder_json, "
        "spec_version, created_at, updated_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
        (wid, owner, name or "", template_key or "generic", origin,
         intro_prompt or "", json.dumps(spec or {}), json.dumps(draft or {}),
         state, json.dumps(builder or {}), t, t, t))
    con.commit()
    return get_workspace(wid)


def get_workspace(wid):
    con = db.get_db()
    return _dws(con.execute(
        "SELECT * FROM om_workspaces WHERE wid = ?", (wid,)).fetchone())


def get_workspace_by_id(ws_id):
    con = db.get_db()
    return _dws(con.execute(
        "SELECT * FROM om_workspaces WHERE id = ?", (ws_id,)).fetchone())


def get_workspace_by_owner(owner_token):
    if not owner_token:
        return None
    con = db.get_db()
    return _dws(con.execute(
        "SELECT * FROM om_workspaces WHERE owner_token = ? "
        "ORDER BY id DESC LIMIT 1", (owner_token,)).fetchone())


_WS_COLS = {"name", "template_key", "origin", "intro_prompt", "spec_json",
            "draft_json", "builder_state", "builder_json", "spec_version"}


def update_workspace(ws_id, **cols):
    sets, vals = [], []
    for k, v in cols.items():
        if k not in _WS_COLS:
            raise ValueError(f"unknown workspace column {k}")
        if k in ("spec_json", "draft_json", "builder_json") and not isinstance(v, str):
            v = json.dumps(v)
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return
    sets.append("updated_at = ?")
    vals.append(now())
    vals.append(ws_id)
    con = db.get_db()
    con.execute(f"UPDATE om_workspaces SET {', '.join(sets)} WHERE id = ?", vals)
    con.commit()


def save_spec(ws_id, spec):
    """Persist a built spec and bump its version. Returns the new version."""
    con = db.get_db()
    con.execute(
        "UPDATE om_workspaces SET spec_json = ?, spec_version = spec_version + 1, "
        "name = ?, updated_at = ? WHERE id = ?",
        (json.dumps(spec), (spec.get("business") or {}).get("name") or "",
         now(), ws_id))
    con.commit()
    row = con.execute("SELECT spec_version FROM om_workspaces WHERE id = ?",
                      (ws_id,)).fetchone()
    return row["spec_version"] if row else 0


def touch_workspace(ws_id, minutes=10):
    """Bump last_seen_at at most every ``minutes`` to avoid write churn."""
    con = db.get_db()
    row = con.execute("SELECT last_seen_at FROM om_workspaces WHERE id = ?",
                      (ws_id,)).fetchone()
    if not row:
        return
    last = db._parse_ts(row["last_seen_at"])
    if last and (dt.datetime.now() - last).total_seconds() < minutes * 60:
        return
    con.execute("UPDATE om_workspaces SET last_seen_at = ? WHERE id = ?",
                (now(), ws_id))
    con.commit()


def delete_workspace(ws_id):
    con = db.get_db()
    con.execute("DELETE FROM om_workspaces WHERE id = ?", (ws_id,))
    con.commit()


def sweep_stale(days=14):
    cutoff = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    con = db.get_db()
    cur = con.execute("DELETE FROM om_workspaces WHERE last_seen_at < ?", (cutoff,))
    con.commit()
    return cur.rowcount


def wipe_conversations(ws_id):
    """Reset: drop every conversation (messages, values, events cascade),
    keep the spec, sources, fields, views and rules."""
    con = db.get_db()
    con.execute("DELETE FROM om_conversations WHERE workspace_id = ?", (ws_id,))
    con.execute("DELETE FROM om_contacts WHERE workspace_id = ?", (ws_id,))
    con.execute("DELETE FROM om_actions WHERE workspace_id = ?", (ws_id,))
    con.execute("DELETE FROM om_events WHERE workspace_id = ?", (ws_id,))
    con.commit()


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
def upsert_source(ws_id, key, kind, mode="api", label="", status=None,
                  simulated=1, meta=None):
    con = db.get_db()
    row = con.execute(
        "SELECT * FROM om_sources WHERE workspace_id = ? AND key = ?",
        (ws_id, key)).fetchone()
    if row:
        con.execute(
            "UPDATE om_sources SET kind = ?, mode = ?, label = ?, simulated = ?, "
            "meta_json = COALESCE(?, meta_json) WHERE id = ?",
            (kind, mode, label or row["label"], simulated,
             json.dumps(meta) if meta is not None else None, row["id"]))
        if status:
            con.execute("UPDATE om_sources SET status = ?, connected_at = ? WHERE id = ?",
                        (status, now() if status == "connected" else row["connected_at"],
                         row["id"]))
        con.commit()
        return get_source(ws_id, key)
    con.execute(
        "INSERT INTO om_sources (workspace_id, key, kind, mode, label, status, "
        "simulated, connected_at, meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (ws_id, key, kind, mode, label, status or "disconnected", simulated,
         now() if status == "connected" else "", json.dumps(meta or {})))
    con.commit()
    return get_source(ws_id, key)


def get_source(ws_id, key):
    con = db.get_db()
    s = _d(con.execute(
        "SELECT * FROM om_sources WHERE workspace_id = ? AND key = ?",
        (ws_id, key)).fetchone())
    if s:
        s["meta"] = _j(s.get("meta_json"), {})
    return s


def list_sources(ws_id):
    con = db.get_db()
    out = []
    for r in con.execute(
            "SELECT * FROM om_sources WHERE workspace_id = ? ORDER BY id",
            (ws_id,)).fetchall():
        s = _d(r)
        s["meta"] = _j(s.get("meta_json"), {})
        out.append(s)
    return out


def set_source_status(ws_id, key, status):
    con = db.get_db()
    con.execute(
        "UPDATE om_sources SET status = ?, connected_at = CASE WHEN ? = 'connected' "
        "THEN ? ELSE connected_at END WHERE workspace_id = ? AND key = ?",
        (status, status, now(), ws_id, key))
    con.commit()


def delete_source(ws_id, key):
    con = db.get_db()
    con.execute("DELETE FROM om_sources WHERE workspace_id = ? AND key = ?",
                (ws_id, key))
    con.commit()


# --------------------------------------------------------------------------- #
# Contacts, conversations, messages
# --------------------------------------------------------------------------- #
def upsert_contact(ws_id, name="", handle="", email="", phone=""):
    con = db.get_db()
    handle = (handle or email or phone or name or "").strip()
    row = con.execute(
        "SELECT * FROM om_contacts WHERE workspace_id = ? AND handle = ?",
        (ws_id, handle)).fetchone()
    if row:
        if name and not row["name"]:
            con.execute("UPDATE om_contacts SET name = ? WHERE id = ?", (name, row["id"]))
            con.commit()
        return _d(con.execute("SELECT * FROM om_contacts WHERE id = ?",
                              (row["id"],)).fetchone())
    con.execute(
        "INSERT INTO om_contacts (workspace_id, name, handle, email, phone, "
        "meta_json, created_at) VALUES (?,?,?,?,?,?,?)",
        (ws_id, name or "", handle, email or "", phone or "", "{}", now()))
    con.commit()
    return _d(con.execute(
        "SELECT * FROM om_contacts WHERE workspace_id = ? AND handle = ?",
        (ws_id, handle)).fetchone())


def create_conversation(ws_id, source_id, contact_id, subject, stage,
                        assignee="", status="open", unread=1, priority="normal",
                        seed_key=None, last_message_at=None, flags=None):
    con = db.get_db()
    t = now()
    cur = con.execute(
        "INSERT INTO om_conversations (workspace_id, source_id, contact_id, "
        "seed_key, subject, stage, assignee, status, unread, priority, flags_json, "
        "last_message_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ws_id, source_id, contact_id, seed_key, subject or "", stage or "",
         assignee or "", status, 1 if unread else 0, priority or "normal",
         json.dumps(flags or []), last_message_at or t, t, t))
    con.commit()
    return cur.lastrowid


def conversation_id_by_seed(ws_id, seed_key):
    con = db.get_db()
    row = con.execute(
        "SELECT id FROM om_conversations WHERE workspace_id = ? AND seed_key = ?",
        (ws_id, seed_key)).fetchone()
    return row["id"] if row else None


_CONV_COLS = {"subject", "stage", "assignee", "status", "unread", "priority",
              "flags_json", "last_message_at", "contact_id"}


def update_conversation(ws_id, conv_id, **cols):
    sets, vals = [], []
    for k, v in cols.items():
        if k == "flags":
            k, v = "flags_json", json.dumps(v)
        if k not in _CONV_COLS:
            raise ValueError(f"unknown conversation column {k}")
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return
    sets.append("updated_at = ?")
    vals.extend([now(), ws_id, conv_id])
    con = db.get_db()
    con.execute(f"UPDATE om_conversations SET {', '.join(sets)} "
                "WHERE workspace_id = ? AND id = ?", vals)
    con.commit()


_CONV_SELECT = """
SELECT c.*, s.key AS source_key, s.kind AS source_kind, s.label AS source_label,
       s.status AS source_status, s.mode AS source_mode,
       ct.name AS contact_name, ct.handle AS contact_handle,
       ct.email AS contact_email, ct.phone AS contact_phone,
       (SELECT body FROM om_messages m WHERE m.conversation_id = c.id
          ORDER BY m.sent_at DESC, m.id DESC LIMIT 1) AS last_body,
       (SELECT kind FROM om_messages m WHERE m.conversation_id = c.id
          AND m.kind IN ('inbound','outbound')
          ORDER BY m.sent_at DESC, m.id DESC LIMIT 1) AS last_kind,
       (SELECT COUNT(*) FROM om_messages m WHERE m.conversation_id = c.id) AS message_count
FROM om_conversations c
JOIN om_sources s ON s.id = c.source_id
LEFT JOIN om_contacts ct ON ct.id = c.contact_id
"""


def _dconv(row):
    c = _d(row)
    if not c:
        return None
    c["flags"] = _j(c.get("flags_json"), [])
    c["awaiting_reply"] = (c.get("status") == "open"
                           and c.get("last_kind") == "inbound")
    c["contact"] = {"name": c.get("contact_name") or "",
                    "handle": c.get("contact_handle") or "",
                    "email": c.get("contact_email") or "",
                    "phone": c.get("contact_phone") or ""}
    c["snippet"] = snippet_of(c.get("last_body"))
    return c


def snippet_of(body, limit=120):
    """One line of what the person actually said. Form-dump bodies ("Lead
    form: ... Name: ... Zip: 60563 Message: actual words") repeat what a row
    already shows as fields, so when a Message: part exists, prefer it over
    the boilerplate. Used by the inbox rows and the builder preview alike."""
    body = (body or "").strip().replace("\n", " ")
    m = re.search(r"\bmessage\s*:\s*(.+)", body, re.IGNORECASE)
    if m and len(m.group(1).strip()) >= 12:
        body = m.group(1).strip()
    return body[:limit] + ("..." if len(body) > limit else "")


def list_conversations(ws_id, include_hidden_sources=False):
    con = db.get_db()
    sql = _CONV_SELECT + " WHERE c.workspace_id = ?"
    if not include_hidden_sources:
        sql += " AND s.status = 'connected'"
    sql += " ORDER BY c.unread DESC, c.last_message_at DESC, c.id DESC"
    return [_dconv(r) for r in con.execute(sql, (ws_id,)).fetchall()]


def get_conversation(ws_id, conv_id):
    con = db.get_db()
    return _dconv(con.execute(
        _CONV_SELECT + " WHERE c.workspace_id = ? AND c.id = ?",
        (ws_id, conv_id)).fetchone())


def add_message(ws_id, conv_id, kind, body, author="", delivery_status="",
                by_agent=0, action_token="", sent_at=None):
    con = db.get_db()
    t = sent_at or now()
    cur = con.execute(
        "INSERT INTO om_messages (workspace_id, conversation_id, kind, author, body, "
        "delivery_status, by_agent, action_token, sent_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ws_id, conv_id, kind, author or "", body or "", delivery_status or "",
         1 if by_agent else 0, action_token or "", t, now()))
    # A conversation's clock is its latest message, whichever direction.
    con.execute(
        "UPDATE om_conversations SET last_message_at = MAX(last_message_at, ?), "
        "updated_at = ? WHERE id = ?", (t, now(), conv_id))
    con.commit()
    return cur.lastrowid


def list_messages(conv_id):
    con = db.get_db()
    return [_d(r) for r in con.execute(
        "SELECT * FROM om_messages WHERE conversation_id = ? "
        "ORDER BY sent_at, id", (conv_id,)).fetchall()]


# --------------------------------------------------------------------------- #
# Spec projections: fields, views, rules
# --------------------------------------------------------------------------- #
def replace_fields(ws_id, fields):
    con = db.get_db()
    keep = [f["key"] for f in fields]
    for pos, f in enumerate(fields):
        con.execute(
            "INSERT INTO om_fields (workspace_id, key, label, type, options_json, hint, "
            "filterable, show_in_list, protected, position, origin) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(workspace_id, key) DO UPDATE SET label = excluded.label, "
            "type = excluded.type, options_json = excluded.options_json, "
            "hint = excluded.hint, filterable = excluded.filterable, "
            "show_in_list = excluded.show_in_list, protected = excluded.protected, "
            "position = excluded.position, origin = excluded.origin",
            (ws_id, f["key"], f.get("label") or f["key"], f.get("type") or "text",
             json.dumps(f.get("options") or []), f.get("hint") or "",
             1 if f.get("filterable", True) else 0,
             1 if f.get("show_in_list") else 0, 1 if f.get("protected") else 0,
             pos, f.get("origin") or "template"))
    if keep:
        q = ",".join("?" for _ in keep)
        con.execute(f"DELETE FROM om_field_values WHERE workspace_id = ? "
                    f"AND field_key NOT IN ({q})", [ws_id, *keep])
        con.execute(f"DELETE FROM om_fields WHERE workspace_id = ? AND key NOT IN ({q})",
                    [ws_id, *keep])
    else:
        con.execute("DELETE FROM om_field_values WHERE workspace_id = ?", (ws_id,))
        con.execute("DELETE FROM om_fields WHERE workspace_id = ?", (ws_id,))
    con.commit()


def list_fields(ws_id):
    con = db.get_db()
    out = []
    for r in con.execute("SELECT * FROM om_fields WHERE workspace_id = ? "
                         "ORDER BY position, id", (ws_id,)).fetchall():
        f = _d(r)
        f["options"] = _j(f.get("options_json"), [])
        out.append(f)
    return out


def replace_views(ws_id, views):
    con = db.get_db()
    keep = [v["key"] for v in views]
    for pos, v in enumerate(views):
        con.execute(
            "INSERT INTO om_views (workspace_id, key, name, filter_json, sort_json, "
            "position, pinned, origin) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(workspace_id, key) DO UPDATE SET name = excluded.name, "
            "filter_json = excluded.filter_json, sort_json = excluded.sort_json, "
            "position = excluded.position, pinned = excluded.pinned, origin = excluded.origin",
            (ws_id, v["key"], v.get("name") or v["key"], json.dumps(v.get("filter") or {}),
             json.dumps(v.get("sort") or {}), pos, 1 if v.get("pinned") else 0,
             v.get("origin") or "template"))
    if keep:
        q = ",".join("?" for _ in keep)
        con.execute(f"DELETE FROM om_views WHERE workspace_id = ? AND key NOT IN ({q})",
                    [ws_id, *keep])
    else:
        con.execute("DELETE FROM om_views WHERE workspace_id = ?", (ws_id,))
    con.commit()


def list_views(ws_id):
    con = db.get_db()
    out = []
    for r in con.execute("SELECT * FROM om_views WHERE workspace_id = ? "
                         "ORDER BY position, id", (ws_id,)).fetchall():
        v = _d(r)
        v["filter"] = _j(v.get("filter_json"), {})
        v["sort"] = _j(v.get("sort_json"), {})
        out.append(v)
    return out


def replace_rules(ws_id, rules):
    con = db.get_db()
    keep = [r["key"] for r in rules]
    for r in rules:
        con.execute(
            "INSERT INTO om_rules (workspace_id, key, name, text, trigger, condition_json, "
            "action_json, enabled, origin) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(workspace_id, key) DO UPDATE SET name = excluded.name, "
            "text = excluded.text, trigger = excluded.trigger, "
            "condition_json = excluded.condition_json, action_json = excluded.action_json, "
            "enabled = excluded.enabled, origin = excluded.origin",
            (ws_id, r["key"], r.get("name") or r["key"], r.get("text") or "",
             r.get("trigger") or "on_extract", json.dumps(r.get("when") or {}),
             json.dumps(r.get("then") or {}), 1 if r.get("enabled", True) else 0,
             r.get("origin") or "template"))
    if keep:
        q = ",".join("?" for _ in keep)
        con.execute(f"DELETE FROM om_rules WHERE workspace_id = ? AND key NOT IN ({q})",
                    [ws_id, *keep])
    else:
        con.execute("DELETE FROM om_rules WHERE workspace_id = ?", (ws_id,))
    con.commit()


def list_rules(ws_id):
    con = db.get_db()
    out = []
    for r in con.execute("SELECT * FROM om_rules WHERE workspace_id = ? ORDER BY id",
                         (ws_id,)).fetchall():
        d = _d(r)
        d["when"] = _j(d.get("condition_json"), {})
        d["then"] = _j(d.get("action_json"), {})
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Field values
# --------------------------------------------------------------------------- #
def upsert_field_value(ws_id, conv_id, key, value, confidence=0.0, quote="",
                       source_message_id=None, provenance="canned", status=None):
    if status is None:
        status = "unknown" if value in (None, "", []) else "found"
    con = db.get_db()
    con.execute(
        "INSERT INTO om_field_values (workspace_id, conversation_id, field_key, value, "
        "confidence, quote, source_message_id, provenance, status, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(workspace_id, conversation_id, field_key) DO UPDATE SET "
        "value = excluded.value, confidence = excluded.confidence, quote = excluded.quote, "
        "source_message_id = excluded.source_message_id, provenance = excluded.provenance, "
        "status = excluded.status, updated_at = excluded.updated_at",
        (ws_id, conv_id, key, json.dumps(value), float(confidence or 0), quote or "",
         source_message_id, provenance, status, now()))
    con.commit()


def values_by_conversation(ws_id):
    """{conversation_id: {field_key: record}} for a whole workspace."""
    con = db.get_db()
    out = {}
    for r in con.execute(
            "SELECT * FROM om_field_values WHERE workspace_id = ?", (ws_id,)).fetchall():
        d = _d(r)
        d["value"] = _j(d.get("value"), None)
        out.setdefault(d["conversation_id"], {})[d["field_key"]] = d
    return out


def values_for_conversation(ws_id, conv_id):
    con = db.get_db()
    out = {}
    for r in con.execute(
            "SELECT * FROM om_field_values WHERE workspace_id = ? AND conversation_id = ?",
            (ws_id, conv_id)).fetchall():
        d = _d(r)
        d["value"] = _j(d.get("value"), None)
        out[d["field_key"]] = d
    return out


# --------------------------------------------------------------------------- #
# Actions (propose -> confirm), builder turns, events
# --------------------------------------------------------------------------- #
def create_action(ws_id, kind, payload, conversation_id=None, policy="review",
                  ttl_minutes=30):
    con = db.get_db()
    token = db.gen_token()
    exp = (dt.datetime.now() + dt.timedelta(minutes=ttl_minutes)).strftime(
        "%Y-%m-%d %H:%M")
    con.execute(
        "INSERT INTO om_actions (token, workspace_id, conversation_id, kind, payload_json, "
        "policy, status, created_at, expires_at) VALUES (?,?,?,?,?,?,'proposed',?,?)",
        (token, ws_id, conversation_id, kind, json.dumps(payload or {}), policy,
         now(), exp))
    con.commit()
    return get_action(token)


def get_action(token):
    con = db.get_db()
    a = _d(con.execute("SELECT * FROM om_actions WHERE token = ?", (token,)).fetchone())
    if a:
        a["payload"] = _j(a.get("payload_json"), {})
    return a


def mark_action(token, status):
    con = db.get_db()
    con.execute("UPDATE om_actions SET status = ?, confirmed_at = ? WHERE token = ?",
                (status, now() if status == "confirmed" else "", token))
    con.commit()


def list_pending_actions(ws_id, conversation_id=None):
    con = db.get_db()
    sql = "SELECT * FROM om_actions WHERE workspace_id = ? AND status = 'proposed'"
    args = [ws_id]
    if conversation_id is not None:
        sql += " AND conversation_id = ?"
        args.append(conversation_id)
    sql += " ORDER BY id"
    out = []
    for r in con.execute(sql, args).fetchall():
        a = _d(r)
        a["payload"] = _j(a.get("payload_json"), {})
        out.append(a)
    return out


def add_turn(ws_id, role, kind, body="", payload=None):
    con = db.get_db()
    row = con.execute("SELECT COALESCE(MAX(turn_no), 0) AS n FROM om_builder_turns "
                      "WHERE workspace_id = ?", (ws_id,)).fetchone()
    n = (row["n"] if row else 0) + 1
    con.execute(
        "INSERT INTO om_builder_turns (workspace_id, turn_no, role, kind, body, "
        "payload_json, created_at) VALUES (?,?,?,?,?,?,?)",
        (ws_id, n, role, kind, body or "", json.dumps(payload or {}), now()))
    con.commit()
    return n


def list_turns(ws_id):
    con = db.get_db()
    out = []
    for r in con.execute("SELECT * FROM om_builder_turns WHERE workspace_id = ? "
                         "ORDER BY turn_no", (ws_id,)).fetchall():
        t = _d(r)
        t["payload"] = _j(t.get("payload_json"), {})
        out.append(t)
    return out


def add_event(ws_id, kind, detail=None, conversation_id=None):
    con = db.get_db()
    con.execute(
        "INSERT INTO om_events (workspace_id, conversation_id, kind, detail_json, "
        "created_at) VALUES (?,?,?,?,?)",
        (ws_id, conversation_id, kind, json.dumps(detail or {}), now()))
    con.commit()


def list_events(ws_id, conversation_id=None, limit=50):
    con = db.get_db()
    sql = "SELECT * FROM om_events WHERE workspace_id = ?"
    args = [ws_id]
    if conversation_id is not None:
        sql += " AND conversation_id = ?"
        args.append(conversation_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in con.execute(sql, args).fetchall():
        e = _d(r)
        e["detail"] = _j(e.get("detail_json"), {})
        out.append(e)
    return out


def counts(ws_id):
    con = db.get_db()
    row = con.execute(
        "SELECT (SELECT COUNT(*) FROM om_conversations WHERE workspace_id = ?) AS conversations, "
        "(SELECT COUNT(*) FROM om_sources WHERE workspace_id = ? AND status = 'connected') AS sources, "
        "(SELECT COUNT(*) FROM om_fields WHERE workspace_id = ?) AS fields, "
        "(SELECT COUNT(*) FROM om_views WHERE workspace_id = ?) AS views, "
        "(SELECT COUNT(*) FROM om_rules WHERE workspace_id = ?) AS rules",
        (ws_id, ws_id, ws_id, ws_id, ws_id)).fetchone()
    return _d(row)
