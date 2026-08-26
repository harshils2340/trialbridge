"""Field extraction: canned (seed) -> keyword/regex -> LLM, in that order.

Every rung writes the same record shape (value, confidence, quote, source
message, provenance, status) so the UI's provenance affordance is identical no
matter which rung produced the value. The demo is byte-identical with or
without a key because seeds carry canned values; the keyword rung is what
makes pasted or uploaded messages work with no key at all.
"""
import datetime as dt
import re

from . import models
from . import seeds

_ENUM_STOP = {"and", "or", "of", "the", "a", "an", "to", "in", "on", "for", "other", "unsure"}
_BOOL_NO = ("no ", "not ", "never ", "haven't", "havent", "have not", "don't", "dont",
            "do not", "without", "nobody", "none")


def _hint_words(field):
    words = re.findall(r"[a-z][a-z']+", (field.get("hint") or "").lower())
    label = re.findall(r"[a-z][a-z']+", (field.get("label") or "").lower())
    stop = {"the", "a", "an", "of", "or", "and", "if", "any", "whether", "they",
            "them", "their", "mention", "mentions", "imply", "implies", "against",
            "with", "for", "from", "in", "on", "to", "is", "are", "was", "were",
            "how", "what", "when", "where", "which", "who", "person", "someone",
            "message", "date", "resolve", "relative", "dates", "us", "if"}
    return [w for w in words + label if len(w) > 2 and w not in stop]


_DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "us"),
    (re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", re.I), "month"),
    (re.compile(r"\bon the (\d{1,2})(?:st|nd|rd|th)\b", re.I), "dayonly"),
]
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_RELATIVE = [
    (re.compile(r"\btoday\b|\bthis morning\b|\btonight\b", re.I), 0, 0.9),
    (re.compile(r"\byesterday\b", re.I), 1, 0.9),
    (re.compile(r"\b(\d+|two|three|four|five|six|seven|ten)\s+days?\s+ago\b", re.I), None, 0.8),
    (re.compile(r"\blast\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I), "weekday", 0.7),
    (re.compile(r"\b(\d+|a|two|three|four|five|six)\s+weeks?\s+ago\b", re.I), "weeks", 0.6),
    (re.compile(r"\blast\s+week\b", re.I), 7, 0.6),
    (re.compile(r"\blast\s+month\b", re.I), 30, 0.5),
    (re.compile(r"\b(\d+|a|two|three|four|five|six)\s+months?\s+ago\b", re.I), "months", 0.5),
]
_WORDNUM = {"a": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10}


def _num(tok):
    tok = tok.lower()
    return _WORDNUM.get(tok) or (int(tok) if tok.isdigit() else 1)


def extract_date(text, ref):
    """(iso_date, confidence, quote) or None. ref: message datetime."""
    for rx, kind in _DATE_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        try:
            if kind == "iso":
                d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return d.isoformat(), 0.95, m.group(0)
            if kind == "us":
                d = dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
                return d.isoformat(), 0.95, m.group(0)
            if kind == "month":
                mon = _MONTHS[m.group(1).lower()[:3]]
                day = int(m.group(2))
                year = int(m.group(3)) if m.group(3) else ref.year
                d = dt.date(year, mon, day)
                if not m.group(3) and d > ref.date():
                    d = dt.date(year - 1, mon, day)
                return d.isoformat(), 0.85 if m.group(3) else 0.75, m.group(0)
            if kind == "dayonly":
                day = int(m.group(1))
                d = dt.date(ref.year, ref.month, day)
                if d > ref.date():
                    prev = (ref.replace(day=1) - dt.timedelta(days=1))
                    d = dt.date(prev.year, prev.month, min(day, 28))
                return d.isoformat(), 0.6, m.group(0)
        except ValueError:
            continue
    for rx, kind, conf in _RELATIVE:
        m = rx.search(text)
        if not m:
            continue
        if kind == "weekday":
            days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            target = days.index(m.group(1).lower())
            back = (ref.weekday() - target) % 7 or 7
            d = ref.date() - dt.timedelta(days=back)
        elif kind == "weeks":
            d = ref.date() - dt.timedelta(weeks=_num(m.group(1)))
        elif kind == "months":
            d = ref.date() - dt.timedelta(days=30 * _num(m.group(1)))
        elif kind is None:
            d = ref.date() - dt.timedelta(days=_num(m.group(1)))
        else:
            d = ref.date() - dt.timedelta(days=kind)
        return d.isoformat(), conf, m.group(0)
    return None


def _quote_around(text, idx, span=60):
    start = max(0, idx - span)
    end = min(len(text), idx + span)
    q = text[start:end].strip()
    return q[:120]


def keyword_extract(field, messages, ref=None):
    """Best-effort extraction from inbound message bodies. Returns
    (value, confidence, quote, message_id) or None."""
    ref = ref or dt.datetime.now()
    typ = field.get("type")
    for m in reversed(messages):  # newest first
        text = m.get("body") or ""
        low = text.lower()
        if typ == "date":
            hit = extract_date(text, ref)
            if hit:
                return hit[0], hit[1], hit[2], m.get("id")
        elif typ == "enum":
            best = None
            for opt in field.get("options") or []:
                phrase = opt.replace("_", " ")
                words = [phrase] + [w for w in opt.split("_") if w not in _ENUM_STOP]
                for w in words:
                    if len(w) < 3:
                        continue
                    # Whole words only, with common endings: "slip" matches
                    # "slipped", "car" does not match "care".
                    mm = re.search(r"\b" + re.escape(w.lower()) + r"(?:\w{0,2}?(?:ed|ing|s))?\b", low)
                    if mm:
                        conf = 0.75 if w == phrase else 0.55
                        if not best or conf > best[1]:
                            best = (opt, conf, _quote_around(text, mm.start()), m.get("id"))
            if best:
                return best
        elif typ == "bool":
            for w in _hint_words(field):
                mm = re.search(r"\b" + re.escape(w) + r"\w*", low)
                if not mm:
                    continue
                i = mm.start()
                window = low[max(0, i - 40):i]
                neg = any(n in window for n in _BOOL_NO)
                return (not neg), 0.55, _quote_around(text, i), m.get("id")
        elif typ == "number":
            for w in _hint_words(field):
                mm = re.search(r"\b" + re.escape(w) + r"\w*", low)
                if not mm:
                    continue
                i = mm.start()
                nm = re.search(r"\$?\d[\d,]*(?:\.\d+)?", text[max(0, i - 30):i + 40])
                if nm:
                    try:
                        val = float(nm.group(0).replace("$", "").replace(",", ""))
                        return val, 0.5, _quote_around(text, i), m.get("id")
                    except ValueError:
                        pass
        elif typ == "text":
            key = field.get("key", "")
            if key == "state":
                sm = re.search(r"\b([A-Z]{2})\b(?=[\s,.]|$)", text)
                if sm and sm.group(1) in _STATES:
                    return sm.group(1), 0.7, _quote_around(text, sm.start()), m.get("id")
                for name, abbr in _STATE_NAMES.items():
                    i = low.find(name)
                    if i >= 0:
                        return abbr, 0.75, _quote_around(text, i), m.get("id")
            # No generic fallback for free text: a window of words around a hint
            # word is not a value, and a wrong value can trip a rule.
    return None


_STATES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
           "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
           "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
           "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"}
_STATE_NAMES = {"illinois": "IL", "indiana": "IN", "wisconsin": "WI", "michigan": "MI",
                "california": "CA", "nevada": "NV", "new york": "NY", "texas": "TX",
                "florida": "FL", "ohio": "OH", "iowa": "IA", "missouri": "MO",
                "new jersey": "NJ", "pennsylvania": "PA", "arizona": "AZ", "georgia": "GA"}


def extract_conversation(ws, spec, conv, only_keys=None, llm=None):
    """Fill in field values for one conversation. Existing found values are
    kept unless their key is in only_keys. Returns the number of writes."""
    ws_id = ws["id"]
    fields = [f for f in spec.get("fields") or [] if not f.get("protected")]
    if only_keys:
        fields = [f for f in fields if f["key"] in only_keys]
    if not fields:
        return 0
    existing = models.values_for_conversation(ws_id, conv["id"])
    messages = [m for m in models.list_messages(conv["id"]) if m["kind"] == "inbound"]
    seed = seeds.seed_conversation(ws.get("template_key"), conv.get("seed_key")) \
        if conv.get("seed_key") else None
    canned = {}
    if seed:
        canned.update(seed.get("fields") or {})
        canned.update(seed.get("extra_fields") or {})
        # A field added later by prompt rarely gets the exact seed key
        # ("police_report_was_filed" vs "police_report"); when every word of a
        # canned key appears in the new key, reuse the canned value.
        for f in fields:
            if f["key"] in canned:
                continue
            fk = set(f["key"].split("_"))
            for ck, rec in list(canned.items()):
                if ck != f["key"] and set(ck.split("_")) <= fk:
                    canned[f["key"]] = rec
                    break
    ref = None
    if messages:
        try:
            ref = dt.datetime.strptime(messages[-1]["sent_at"][:16], "%Y-%m-%d %H:%M")
        except ValueError:
            ref = None
    writes = 0
    pending_llm = []
    for f in fields:
        key = f["key"]
        cur = existing.get(key)
        if cur and cur.get("status") == "found" and not (only_keys and key in only_keys):
            continue
        rec = canned.get(key)
        if rec and rec[0] not in (None, "", []):
            value, conf, quote = rec
            mid = next((m["id"] for m in messages if quote and quote.lower() in (m["body"] or "").lower()), None)
            models.upsert_field_value(ws_id, conv["id"], key, value, conf, quote, mid,
                                      provenance="canned", status="found")
            writes += 1
            continue
        hit = keyword_extract(f, messages, ref)
        if hit and hit[0] not in (None, ""):
            value, conf, quote, mid = hit
            models.upsert_field_value(ws_id, conv["id"], key, value, conf, quote, mid,
                                      provenance="keyword", status="found")
            writes += 1
            continue
        pending_llm.append(f)
        if not cur:
            models.upsert_field_value(ws_id, conv["id"], key, None, 0, "", None,
                                      provenance="keyword", status="unknown")
            writes += 1
    if pending_llm and llm and messages:
        try:
            got = llm(spec, pending_llm, messages) or {}
        except Exception:
            got = {}
        for f in pending_llm:
            r = got.get(f["key"])
            if not r or r.get("value") in (None, "", []):
                continue
            models.upsert_field_value(ws_id, conv["id"], f["key"], r["value"],
                                      r.get("confidence", 0.5), r.get("quote", ""),
                                      r.get("message_id"), provenance="llm", status="found")
            writes += 1
    return writes


def backfill(ws, spec, only_keys=None, llm=None, conv_ids=None):
    convs = models.list_conversations(ws["id"], include_hidden_sources=True)
    if conv_ids:
        convs = [c for c in convs if c["id"] in set(conv_ids)]
    total = 0
    for c in convs:
        total += extract_conversation(ws, spec, c, only_keys=only_keys, llm=llm)
    return total
