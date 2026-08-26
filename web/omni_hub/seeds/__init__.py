"""Template registry + seeding.

A template is one Python module exporting TEMPLATE: the spec, the interview
question bank, keyword signals for detection, reply ladders, guardrails, sample
inputs for the paste box, and 14 to 20 seed conversations with canned
extraction values and drafts, so the demo is identical with or without an LLM.
"""
import datetime as dt
import importlib

from copy_sanitize import sanitize_copy

from .. import models

TEMPLATE_KEYS = [
    "clinical_trial_site", "pi_law_firm", "group_therapy", "dental_medspa",
    "specialty_referrals", "home_health", "nurse_staffing", "bootcamp_admissions",
    "legal_aid", "property_leasing", "generic",
]

_CACHE = {}


def get(key):
    key = key if key in TEMPLATE_KEYS else "generic"
    if key not in _CACHE:
        try:
            mod = importlib.import_module(f".{key}", __name__)
            _CACHE[key] = mod.TEMPLATE
        except ModuleNotFoundError:
            if key == "generic":
                raise
            return get("generic")
    return _CACHE[key]


def available():
    """Templates that actually exist on disk, in display order."""
    out = []
    for k in TEMPLATE_KEYS:
        try:
            t = get(k)
        except ModuleNotFoundError:
            continue
        if t.get("key") == k:
            out.append(t)
    return out


def list_templates():
    return [{"key": t["key"], "name": t["name"], "tagline": t.get("tagline", ""),
             "example_prompt": t.get("example_prompt", "")}
            for t in available() if t["key"] != "generic"]


def detect(prompt):
    """Keyword-scored template detection. Returns (key, score)."""
    low = (prompt or "").lower()
    best, best_score = "generic", 0
    for t in available():
        if t["key"] == "generic":
            continue
        score = 0
        for phrase, weight in t.get("signals") or []:
            if phrase in low:
                score += weight
        if score > best_score:
            best, best_score = t["key"], score
    if best_score < 3:
        return "generic", best_score
    return best, best_score


def seed_conversation(template_key, seed_key):
    for c in get(template_key).get("conversations") or []:
        if c["key"] == seed_key:
            return c
    return None


def _ts(minutes_ago):
    return (dt.datetime.now() - dt.timedelta(minutes=int(minutes_ago))).strftime(
        "%Y-%m-%d %H:%M")


def _find_message_id(message_ids_bodies, quote):
    if not quote:
        return None
    q = quote.lower()
    for mid, body in message_ids_bodies:
        if q in (body or "").lower():
            return mid
    return None


def seed_workspace(ws, template, spec, source_keys=None):
    """Create sources and seed conversations for the given connectors.
    Idempotent: a conversation is keyed by (workspace, seed_key)."""
    ws_id = ws["id"]
    wanted = []
    for c in spec.get("connectors") or []:
        if source_keys is None:
            if c.get("auto_connect", True):
                wanted.append(c)
        elif c["key"] in source_keys:
            wanted.append(c)
    field_keys = {f["key"] for f in spec.get("fields") or []}
    seeded = 0
    for c in wanted:
        src = models.upsert_source(ws_id, c["key"], c["kind"], mode=c.get("mode", "api"),
                                   label=c.get("label", ""), status="connected")
        for sc in template.get("conversations") or []:
            if sc["source"] != c["kind"] and sc["source"] != c["key"]:
                continue
            if models.conversation_id_by_seed(ws_id, sc["key"]):
                continue
            contact = models.upsert_contact(ws_id, sc["contact"].get("name", ""),
                                            sc["contact"].get("handle", ""),
                                            sc["contact"].get("email", ""),
                                            sc["contact"].get("phone", ""))
            msgs = sc["messages"]
            last_at = _ts(min(m["ago"] for m in msgs))
            conv_id = models.create_conversation(
                ws_id, src["id"], contact["id"], sanitize_copy(sc["subject"]),
                sc.get("stage") or (spec["stages"][0]["key"] if spec.get("stages") else ""),
                assignee=sc.get("assignee", ""), status=sc.get("status", "open"),
                unread=sc.get("unread", True), priority=sc.get("priority", "normal"),
                seed_key=sc["key"], last_message_at=last_at)
            ids = []
            for m in msgs:
                mid = models.add_message(
                    ws_id, conv_id, m["kind"], sanitize_copy(m["body"]),
                    author=m.get("author") or (sc["contact"].get("name") if m["kind"] == "inbound" else ""),
                    delivery_status="saved" if m["kind"] == "outbound" else "",
                    sent_at=_ts(m["ago"]))
                if m["kind"] == "inbound":
                    ids.append((mid, m["body"]))
            canned = dict(sc.get("fields") or {})
            canned.update({k: val for k, val in (sc.get("extra_fields") or {}).items()
                           if k in field_keys})
            for key in field_keys:
                rec = canned.get(key)
                if rec:
                    value, conf, quote = rec
                    models.upsert_field_value(
                        ws_id, conv_id, key, value, conf, sanitize_copy(quote or ""),
                        _find_message_id(ids, quote), provenance="canned",
                        status="unknown" if value in (None, "", []) else "found")
                else:
                    models.upsert_field_value(ws_id, conv_id, key, None, 0, "",
                                              None, provenance="canned", status="unknown")
            seeded += 1
    if seeded:
        models.add_event(ws_id, "seeded", {"count": seeded,
                                            "sources": [c["key"] for c in wanted]})
    return seeded
