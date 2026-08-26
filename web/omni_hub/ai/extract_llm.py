"""The model rung of extraction: only runs for fields the canned and keyword
rungs left unknown, and only when a key exists. Returns per-field records
with a verbatim quote that is checked against the message before it counts."""
import datetime as dt
import json
import re

from . import llm

SYSTEM = """You extract structured fields from a customer's inbound messages for a business. You are given the
business type, the FIELDS to extract (key, type, options, hint), and the conversation's INBOUND messages with ids.
For each field return the value, a confidence from 0 to 1, the exact short quote from the messages that supports it,
and the id of the message the quote came from. Rules:
1. Only use what the customer wrote. If the messages do not say, return value null, confidence 0, quote "". Never guess.
2. Match enum values exactly from the options list. Dates as YYYY-MM-DD; resolve relative dates against TODAY and lower
   the confidence. Booleans as true or false. Numbers as numbers. Lists as JSON arrays of short strings.
3. The quote must be copied verbatim from a message and be at most 120 characters.
4. Never use em dashes.
Respond with ONLY a JSON object: {"fields": [{"key": "", "value": null, "confidence": 0, "quote": "", "message_id": 0}]}"""


def _norm_value(field, raw):
    t = field.get("type")
    if raw in (None, ""):
        return None
    if t == "enum":
        opts = field.get("options") or []
        s = str(raw).strip().lower().replace(" ", "_")
        return s if s in opts else next((o for o in opts if o.lower() == s), None)
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        return True if s in ("true", "yes", "y", "1") else False if s in ("false", "no", "n", "0") else None
    if t == "number":
        try:
            return float(str(raw).replace("$", "").replace(",", ""))
        except ValueError:
            return None
    if t == "date":
        s = str(raw).strip()
        return s[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", s) else None
    if t == "list":
        if isinstance(raw, list):
            return [str(x)[:60] for x in raw][:8]
        return [x.strip() for x in re.split(r",|;|/| and ", str(raw)) if x.strip()][:8]
    return str(raw)[:200]


def extract(spec, fields, messages):
    """{key: {value, confidence, quote, message_id}} or None."""
    if not llm.available() or not fields or not messages:
        return None
    msgs = messages[-8:]
    lines = []
    total = 0
    for m in msgs:
        body = (m.get("body") or "")[:1500]
        total += len(body)
        if total > 6000:
            break
        lines.append(f"[{m['id']}] (customer, {m.get('sent_at', '')}) {body}")
    fields_json = json.dumps([{"key": f["key"], "type": f["type"], "options": f.get("options") or [],
                               "hint": f.get("hint") or ""} for f in fields])
    user = (f"BUSINESS: {(spec.get('business') or {}).get('type', '')}, TODAY: {dt.date.today().isoformat()}\n"
            f"FIELDS (JSON): {fields_json}\nMESSAGES:\n" + "\n".join(lines) + "\nExtract now.")
    data = llm.chat_json(SYSTEM, user, max_tokens=120 + 60 * len(fields))
    if not data or not isinstance(data.get("fields"), list):
        return None
    by_id = {m["id"]: (m.get("body") or "").lower() for m in msgs}
    spec_fields = {f["key"]: f for f in fields}
    out = {}
    for rec in data["fields"]:
        if not isinstance(rec, dict) or rec.get("key") not in spec_fields:
            continue
        f = spec_fields[rec["key"]]
        value = _norm_value(f, rec.get("value"))
        if value in (None, "", []):
            continue
        try:
            conf = max(0.0, min(1.0, float(rec.get("confidence") or 0)))
        except (TypeError, ValueError):
            conf = 0.4
        quote = str(rec.get("quote") or "")[:120]
        mid = rec.get("message_id")
        body = by_id.get(mid, "")
        if quote and re.sub(r"\s+", " ", quote.lower()) not in re.sub(r"\s+", " ", body):
            conf = min(conf, 0.4)
            quote, mid = "", None
        out[f["key"]] = {"value": value, "confidence": conf, "quote": quote, "message_id": mid}
    return out
