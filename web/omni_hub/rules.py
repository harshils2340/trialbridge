"""Rules: sentences the user typed, evaluated with the same filter AST as views.

flag           add a labeled flag to the conversation (idempotent by rule key)
set_priority   raise priority
set_stage      move to a stage (never out of a terminal stage)
auto_reject    move to a terminal stage, flag it, and propose a polite decline
auto_reply     propose (or, under auto_send policy, record) a reply

Rules never send anything by themselves; anything outbound becomes a proposal
that the approval policy decides how to treat.
"""
from . import filters
from . import inbox
from . import models


def _flag_present(conv, rule_key):
    return any((f.get("rule_key") if isinstance(f, dict) else f) == rule_key
               for f in (conv.get("flags") or []))


def match_counts(rules, convs):
    return {r["key"]: sum(1 for c in convs if filters.evaluate(r.get("when") or {}, c))
            for r in rules}


def run_all(ws, spec, convs=None, propose=None):
    """Apply every enabled rule to every conversation. ``propose`` is an
    optional callback (kind, conv, rule) used by auto_reject/auto_reply to
    create proposals; when absent those rules only flag and move."""
    ws_id = ws["id"]
    convs = convs if convs is not None else inbox.load(ws_id, include_hidden_sources=True)
    terminal = {s["key"] for s in spec.get("stages") or [] if s.get("terminal")}
    fired = {}
    for rule in spec.get("rules") or []:
        if not rule.get("enabled", True):
            continue
        then = rule.get("then") or {}
        typ = then.get("type")
        for c in convs:
            if not filters.evaluate(rule.get("when") or {}, c):
                continue
            changed = {}
            if typ in ("flag", "auto_reject") and not _flag_present(c, rule["key"]):
                label = then.get("label") or ("Auto rejected" if typ == "auto_reject" else rule["name"])
                c["flags"] = list(c.get("flags") or []) + [
                    {"rule_key": rule["key"], "label": label,
                     "tone": then.get("tone") or ("neutral" if typ == "auto_reject" else "warn")}]
                changed["flags"] = c["flags"]
            if typ == "set_priority":
                order = ["low", "normal", "high", "urgent"]
                want = then.get("value", "high")
                if order.index(want) > order.index(c.get("priority") or "normal"):
                    c["priority"] = want
                    changed["priority"] = want
            if typ in ("set_stage", "auto_reject"):
                stage = then.get("stage")
                if stage and c.get("stage") != stage and c.get("stage") not in terminal:
                    c["stage"] = stage
                    changed["stage"] = stage
                    if typ == "auto_reject" and propose:
                        propose("decline", c, rule)
            if typ == "auto_reply" and propose and c.get("awaiting_reply") \
                    and not _flag_present(c, rule["key"]):
                c["flags"] = list(c.get("flags") or []) + [
                    {"rule_key": rule["key"], "label": rule["name"], "tone": "info"}]
                changed["flags"] = c["flags"]
                propose(then.get("intent", "acknowledge"), c, rule)
            if changed:
                models.update_conversation(ws_id, c["id"], **changed)
                models.add_event(ws_id, "rule_fired", {"rule": rule["key"], **{
                    k: v for k, v in changed.items() if k != "flags"}}, conversation_id=c["id"])
                fired[rule["key"]] = fired.get(rule["key"], 0) + 1
    return fired
