"""Filter expressions evaluated in Python over conversation dicts.

The AST is {"all": [...]} / {"any": [...]} of {"field", "op", "value"}. Field
refs are built-ins (stage, status, source, assignee, unread, priority, flags,
awaiting_reply, last_message_at, subject, contact_name) or extracted values as
"f.<key>". A missing value is empty, never an error, so a view can be written
before the field it filters on has been extracted for anyone.
"""
import datetime as dt


def _get(conv, field):
    if field.startswith("f."):
        rec = (conv.get("f") or {}).get(field[2:])
        if not rec:
            return None, "unknown"
        return rec.get("value"), rec.get("status") or ("found" if rec.get("value") not in (None, "", []) else "unknown")
    if field == "source":
        return conv.get("source_kind"), "found"
    if field == "text":
        return f"{conv.get('subject') or ''} {conv.get('last_body') or conv.get('snippet') or ''}", "found"
    if field == "contact_name":
        return (conv.get("contact") or {}).get("name") or conv.get("contact_name"), "found"
    if field == "unread":
        return bool(conv.get("unread")), "found"
    if field == "awaiting_reply":
        return bool(conv.get("awaiting_reply")), "found"
    v = conv.get(field)
    return v, "found"


def _parse_date(val):
    if val is None:
        return None
    if isinstance(val, (dt.date, dt.datetime)):
        return val if isinstance(val, dt.datetime) else dt.datetime.combine(val, dt.time())
    s = str(val).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(s[:16] if fmt == "%Y-%m-%d %H:%M" else s, fmt)
        except ValueError:
            continue
    return None


def _num(val):
    try:
        if isinstance(val, bool):
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def _norm(val):
    if isinstance(val, str):
        return val.strip().lower()
    return val


def _eq(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return _truthy(a) == _truthy(b)
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return na == nb
    return _norm(a) == _norm(b)


def _truthy(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("true", "yes", "y", "1"):
        return True
    if s in ("false", "no", "n", "0"):
        return False
    return None


def evaluate(expr, conv, today=None):
    if not expr:
        return True
    if "all" in expr:
        return all(evaluate(c, conv, today) for c in expr["all"])
    if "any" in expr:
        return any(evaluate(c, conv, today) for c in expr["any"])
    field = expr.get("field", "")
    op = expr.get("op", "eq")
    want = expr.get("value")
    have, status = _get(conv, field)
    today = today or dt.datetime.now()

    if op == "is_unknown":
        return status == "unknown" or have in (None, "", [])
    if op == "is_empty":
        return have in (None, "", [])
    if op == "not_empty":
        return have not in (None, "", [])
    if op == "has_flag":
        return any(_norm(f.get("label") if isinstance(f, dict) else f) == _norm(want)
                   or _norm(f.get("rule_key") if isinstance(f, dict) else f) == _norm(want)
                   for f in (conv.get("flags") or []))
    if have in (None, "", []) and op not in ("neq", "not_in"):
        return False
    if op == "eq":
        if isinstance(have, list):
            return any(_eq(h, want) for h in have)
        return _eq(have, want)
    if op == "neq":
        if have in (None, "", []):
            return True
        if isinstance(have, list):
            return not any(_eq(h, want) for h in have)
        return not _eq(have, want)
    if op == "in":
        wants = want if isinstance(want, list) else [want]
        if isinstance(have, list):
            return any(_eq(h, w) for h in have for w in wants)
        return any(_eq(have, w) for w in wants)
    if op == "not_in":
        wants = want if isinstance(want, list) else [want]
        if have in (None, "", []):
            return True
        if isinstance(have, list):
            return not any(_eq(h, w) for h in have for w in wants)
        return not any(_eq(have, w) for w in wants)
    if op == "contains":
        hay = " ".join(str(h) for h in have) if isinstance(have, list) else str(have)
        return str(want or "").lower() in hay.lower()
    if op in ("gt", "gte", "lt", "lte"):
        a, b = _num(have), _num(want)
        if a is None or b is None:
            da, db_ = _parse_date(have), _parse_date(want)
            if da is None or db_ is None:
                return False
            a, b = da.timestamp(), db_.timestamp()
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    if op == "within_days":
        d = _parse_date(have)
        n = _num(want)
        if d is None or n is None:
            return False
        return abs((d - today).total_seconds()) <= n * 86400
    if op == "older_than_days":
        d = _parse_date(have)
        n = _num(want)
        if d is None or n is None:
            return False
        return (today - d).total_seconds() > n * 86400
    return False


def sort_key(sort):
    by = (sort or {}).get("by") or "last_message_at"
    desc = (sort or {}).get("dir", "desc") != "asc"

    def key(conv):
        v, _ = _get(conv, by)
        if v is None or v == "":
            # Unknowns sink to the bottom regardless of direction.
            return (1, 0)
        if isinstance(v, bool):
            v = int(v)
        n = _num(v)
        if n is not None:
            return (0, -n if desc else n)
        d = _parse_date(v)
        if d is not None:
            t = d.timestamp()
            return (0, -t if desc else t)
        s = str(v).lower()
        return (0, s)

    return key


def apply_sort(convs, sort):
    by = (sort or {}).get("by") or "last_message_at"
    desc = (sort or {}).get("dir", "desc") != "asc"
    keyed = sort_key(sort)
    try:
        out = sorted(convs, key=keyed)
    except TypeError:
        out = list(convs)
    if desc and all(isinstance(keyed(c)[1], str) for c in out):
        out.reverse()
    if by == "last_message_at":
        # Unread first, then the sort; the inbox reads top-down like mail.
        out.sort(key=lambda c: 0 if c.get("unread") else 1)
    return out


def search(convs, q):
    q = (q or "").strip().lower()
    if not q:
        return convs
    out = []
    for c in convs:
        hay = [c.get("subject") or "", (c.get("contact") or {}).get("name") or "",
               (c.get("contact") or {}).get("handle") or "", c.get("snippet") or "",
               c.get("last_body") or ""]
        for rec in (c.get("f") or {}).values():
            v = rec.get("value")
            if v not in (None, ""):
                hay.append(str(v))
        if q in " ".join(hay).lower():
            out.append(c)
    return out
