"""Propose -> confirm for everything the agent does, mirroring copilot/actions:
the proposal is persisted with a TTL, re-validated at confirm time against
the stored row (never the client), and executed exactly once."""
from copy_sanitize import sanitize_copy

from . import models
from .ai import compose as compose_mod
from .ai import policy


def propose_reply(ws, spec, template, conv, intent, instruction=""):
    messages = models.list_messages(conv["id"])
    d = compose_mod.compose(spec, template, conv, messages, instruction, intent=intent)
    if d.get("blocked"):
        return None, d["blocked"]
    if not d.get("text"):
        return None, "Nothing to propose."
    a = models.create_action(ws["id"], "send_reply",
                             {"text": d["text"], "intent": intent, "category": d["category"],
                              "agent": True},
                             conversation_id=conv["id"], policy=d["policy"])
    models.add_event(ws["id"], "action_proposed", {"kind": "send_reply", "intent": intent},
                     conversation_id=conv["id"])
    return a, ""


def rule_proposer(ws, spec, template=None):
    """The callback rules.run_all uses when a rule wants to say something
    (auto_reject drafts a decline, auto_reply drafts an acknowledgement).
    Never sends: it creates a proposal under the approval policy."""
    from . import seeds  # local: seeds imports models, keep the graph acyclic
    template = template or seeds.get(ws.get("template_key"))

    def propose(intent, conv, rule):
        if models.list_pending_actions(ws["id"], conv["id"]):
            return
        messages = models.list_messages(conv["id"])
        d = compose_mod.compose(spec, template, conv, messages, intent=intent)
        if d.get("blocked") or not d.get("text"):
            return
        models.create_action(ws["id"], "send_reply",
                             {"text": d["text"], "intent": intent, "rule": rule["key"],
                              "category": d["category"], "agent": True},
                             conversation_id=conv["id"], policy=d["policy"])
        models.add_event(ws["id"], "action_proposed", {"kind": "send_reply", "intent": intent,
                                                       "rule": rule["key"]},
                         conversation_id=conv["id"])
    return propose


def load_valid(ws, token):
    a = models.get_action(token)
    if not a or a["workspace_id"] != ws["id"]:
        return None, "That proposal is not in this workspace."
    if a["status"] != "proposed":
        return None, "Already handled."
    if a["expires_at"] < models.now():
        models.mark_action(token, "expired")
        return None, "That proposal expired. Ask again."
    return a, ""


def cancel(ws, token):
    a, err = load_valid(ws, token)
    if not a:
        return err
    models.mark_action(token, "canceled")
    return ""


def confirm(ws, spec, token, edited_text=None):
    """Execute once. Returns (result dict, error)."""
    a, err = load_valid(ws, token)
    if not a:
        return None, err
    kind = a["kind"]
    payload = a["payload"]
    if kind == "send_reply":
        text = sanitize_copy((edited_text if edited_text is not None else payload.get("text")) or "").strip()
        if not text:
            return None, "The message is empty."
        conv = models.get_conversation(ws["id"], a["conversation_id"])
        if not conv:
            return None, "That conversation is gone."
        mid = models.add_message(ws["id"], conv["id"], "outbound", text,
                                 author=(spec.get("agent") or {}).get("name", "Omni"),
                                 delivery_status="saved", by_agent=1, action_token=token)
        models.update_conversation(ws["id"], conv["id"], unread=0)
        models.mark_action(token, "confirmed")
        models.add_event(ws["id"], "action_confirmed", {"kind": kind, "message_id": mid},
                         conversation_id=conv["id"])
        return {"message_id": mid, "answer": "Sent."}, ""
    if kind == "config_patch":
        from . import builder, seeds, spec as spec_mod  # local: avoids an import cycle
        base = payload.get("base_version")
        if base is not None and base != ws.get("spec_version"):
            models.mark_action(token, "canceled")
            return None, "Your setup changed since I proposed this. Ask again."
        template = seeds.get(ws.get("template_key"))
        new_spec, applied, refused = spec_mod.apply_patch(spec, payload.get("ops") or [], template)
        if not applied:
            models.mark_action(token, "canceled")
            return None, (refused[0]["reason"] if refused else "Nothing to apply.")
        added = [spec_mod.slug((o.get("field") or {}).get("key") or (o.get("field") or {}).get("label"))
                 for o in applied if o.get("op") == "add_field"]
        _, n = builder.rebuild(ws, new_spec, template, only_keys=added or None)
        models.mark_action(token, "confirmed")
        models.add_event(ws["id"], "spec_patched", {"ops": [o.get("op") for o in applied]})
        kinds = {o.get("op") for o in applied}
        answer = "Applied."
        if added:
            answer = f"Added {', '.join(added).replace('_', ' ')} and read {n} conversation{'s' if n != 1 else ''} for it."
        return {"answer": answer, "applied": applied, "kinds": sorted(kinds), "added_fields": added}, ""
    if kind == "set_stage":
        models.update_conversation(ws["id"], a["conversation_id"], stage=payload.get("stage"))
        models.mark_action(token, "confirmed")
        return {"answer": "Moved."}, ""
    if kind == "assign":
        models.update_conversation(ws["id"], a["conversation_id"], assignee=payload.get("assignee", ""))
        models.mark_action(token, "confirmed")
        return {"answer": "Assigned."}, ""
    return None, "I do not know how to carry that out."


def category_of(spec, conv, text):
    return policy.categorize(text, "", conv.get("f"))
