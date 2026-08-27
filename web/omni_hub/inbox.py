"""Read side of the inbox: conversations with their extracted values merged in,
view counts, and the filter/sort/search pipeline the page renders from."""
import datetime as dt

from . import filters
from . import models


def time_label(ts):
    d = None
    try:
        d = dt.datetime.strptime((ts or "")[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return ts or ""
    now = dt.datetime.now()
    delta = now - d
    secs = delta.total_seconds()
    if secs < 60:
        return "Just now"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400 and d.date() == now.date():
        return f"{int(secs // 3600)}h"
    if (now.date() - d.date()).days == 1:
        return "Yesterday"
    if delta.days < 7:
        return d.strftime("%a")
    # %-d is glibc-only (Windows raises ValueError); format the day ourselves.
    return f"{d.strftime('%b')} {d.day}"


def load(ws_id, include_hidden_sources=False):
    """Every conversation with its ``f`` map, awaiting_reply and time label."""
    convs = models.list_conversations(ws_id, include_hidden_sources)
    values = models.values_by_conversation(ws_id)
    for c in convs:
        c["f"] = values.get(c["id"], {})
        c["time_label"] = time_label(c.get("last_message_at"))
    return convs


def apply_view(convs, view):
    if not view:
        return list(convs)
    out = [c for c in convs if filters.evaluate(view.get("filter") or {}, c)]
    return filters.apply_sort(out, view.get("sort"))


def view_counts(views, convs):
    return {v["key"]: sum(1 for c in convs if filters.evaluate(v.get("filter") or {}, c))
            for v in views}


def flag_counts(rules, convs):
    out = {}
    for r in rules:
        out[r["key"]] = sum(1 for c in convs
                            if any((f.get("rule_key") if isinstance(f, dict) else f) == r["key"]
                                   for f in (c.get("flags") or [])))
    return out


def stage_counts(stages, convs):
    return {s["key"]: sum(1 for c in convs if c.get("stage") == s["key"]) for s in stages}


def narrow(convs, stage=None, status="open", source=None, assignee=None, q=None):
    out = convs
    if status and status != "all":
        out = [c for c in out if c.get("status") == status]
    if stage and stage != "all":
        out = [c for c in out if c.get("stage") == stage]
    if source:
        out = [c for c in out if c.get("source_key") == source or c.get("source_kind") == source]
    if assignee == "unassigned":
        out = [c for c in out if not c.get("assignee")]
    elif assignee:
        out = [c for c in out if c.get("assignee") == assignee]
    if q:
        out = filters.search(out, q)
    return out


def facts_line(conv, fields, limit=2):
    """Up to ``limit`` list-worthy fields as (label, text, known) tuples; never
    three unknowns, because a row full of "unknown" says nothing."""
    out = []
    unknown_shown = 0
    for f in fields:
        if not f.get("show_in_list"):
            continue
        rec = (conv.get("f") or {}).get(f["key"])
        val = rec.get("value") if rec else None
        if val in (None, "", []):
            if unknown_shown >= 1 or len(out) >= limit:
                continue
            unknown_shown += 1
            out.append((f["label"], "unknown", False))
        else:
            out.append((f["label"], display_value(f, val), True))
        if len(out) >= limit:
            break
    return out


def display_value(field, val):
    if val in (None, "", []):
        return "Unknown"
    t = field.get("type")
    if t == "bool":
        return "Yes" if val in (True, "true", "yes", 1) else "No"
    if t == "enum":
        return str(val).replace("_", " ").capitalize()
    if t == "date":
        try:
            d = dt.datetime.strptime(str(val)[:10], "%Y-%m-%d")
            return f"{d.strftime('%b')} {d.day}, {d.year}"
        except ValueError:
            return str(val)
    if t == "list":
        return ", ".join(str(x) for x in val) if isinstance(val, list) else str(val)
    if t == "number":
        try:
            n = float(val)
            return str(int(n)) if n.is_integer() else f"{n:g}"
        except (TypeError, ValueError):
            return str(val)
    return str(val)
