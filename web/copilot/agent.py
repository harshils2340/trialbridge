"""Copilot orchestrator.

Given a natural-language query (plus the page context - which applicant the user
is looking at), it picks ONE tool, runs it (scoped to the user), and returns a
grounded answer + citations. When ``LLM_API_KEY`` is set it rephrases the tool's
structured payload into natural language under a strict "answer only from this
JSON" system prompt; otherwise it uses the tool's deterministic summary. Either
way the citations come from the tool, never the model.
"""

import json
import re

import match_trials as mt

from . import actions, registry, tools

SYSTEM_PROMPT = (
    "You are BridgeMD Copilot, an assistant for a clinical-trial study team. "
    "You will be given a user question and a JSON payload of records retrieved "
    "from THIS user's own studies. Rules you must follow:\n"
    "1. Answer ONLY using facts in the JSON payload. If the answer isn't there, "
    "say you don't have that information. Never invent applicants, numbers, or "
    "eligibility.\n"
    "2. Never give medical advice and never decide eligibility - you assist, the "
    "coordinator decides.\n"
    "3. Refer to applicants by their code/label exactly as given. Do not guess "
    "names or contact details.\n"
    "4. Be concise: write a SINGLE short headline sentence stating the key number "
    "or takeaway. Do NOT list individual applicants, visits, or documents by name - "
    "the interface shows those to the user as a separate, scannable list beneath "
    "your reply, so enumerating them is redundant."
)


def _display_items(items):
    """Normalize a tool's structured rows into {title, detail, study, url} so the
    UI can render a scannable list instead of a run-on sentence. Covers the shapes
    every read tool returns (applicants, visits, documents, campaigns, funnel
    stages, message hits); unknown keys are simply ignored."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        title = (it.get("code") or it.get("who") or it.get("name")
                 or it.get("title") or it.get("label") or it.get("stage") or "Item")
        bits = []
        if it.get("kind") and it.get("who"):
            bits.append(it["kind"])
        if it.get("when"):
            bits.append(it["when"])
        if it.get("issue"):
            bits.append(it["issue"])
        if it.get("why"):
            bits.append(it["why"])
        if it.get("verdict"):
            v = str(it["verdict"])
            if it.get("score") is not None:
                v += f" \u00b7 score {it['score']}"
            bits.append(v)
        elif it.get("score") is not None and it.get("matches") is None:
            bits.append(f"score {it['score']}")
        if it.get("status"):
            s = str(it["status"]).replace("_", " ")
            if it.get("version"):
                s += f" \u00b7 {it['version']}"
            bits.append(s)
        if it.get("channel"):
            c = str(it["channel"])
            if it.get("enrolled") is not None:
                c += f" \u00b7 {it['enrolled']} enrolled"
            cpe = it.get("cost_per_enrolled")
            if cpe is not None:
                try:
                    c += f" \u00b7 ${round(float(cpe)):,}/enrolled"
                except (TypeError, ValueError):
                    pass
            bits.append(c)
        if it.get("stage") and it.get("reached") is not None:
            st = f"{it['reached']} reached"
            if it.get("conv_from_prev") is not None:
                st += f" \u00b7 {it['conv_from_prev']}% from previous"
            bits.append(st)
        if it.get("matches") is not None:
            n = it["matches"]
            bits.append(f"{n} mention" + ("" if n == 1 else "s"))
        if it.get("snippet"):
            bits.append("\u201c" + str(it["snippet"]).strip() + "\u201d")
        if it.get("draft"):
            bits.append("Suggested reply: \u201c" + str(it["draft"]).strip() + "\u201d")
        elif it.get("blocked"):
            bits.append(str(it["blocked"]))
        out.append({
            "title": str(title),
            "detail": " \u00b7 ".join(b for b in bits if b),
            "study": str(it.get("study") or ""),
            "url": it.get("url"),
        })
    return out

# Action-first starters shown in the empty rail - phrased as work to do, not
# questions to ask (Bridget is an agent that acts, not a Q&A bot).
STARTERS = [
    "Draft replies to my inbox",
    "Send booking reminders to everyone stuck",
    "Find my biggest funnel leak",
]

# The trace ("what I actually did") now lives with each tool in registry.py.

# The planner picks a tool. When an LLM key is set it reads the registry menu and
# chooses; otherwise the deterministic keyword classifier below runs. Routing is
# never left to guesswork - an unknown tool name falls back to keywords.
PLANNER_SYSTEM = (
    "You are the router for a clinical-trial study-team assistant. Given the "
    "user's request and a menu of tools, choose the SINGLE best tool and extract "
    "its parameters. You only route; you never answer, invent data, or decide "
    "eligibility. Respond with ONLY a JSON object of the form "
    '{"tool": "<tool_name>", "params": {}}. Use the exact tool names given. If '
    'no tool fits, use {"tool": "help", "params": {}}.')


_BULK_HINTS = ("everyone", "all of them", "each of", "each one", "stuck",
               "hasn't booked", "haven't booked", "who hasn", "the ones",
               "all applicants", "everybody")


def _classify(query, has_lead=False):
    """Return (intent, params) from keyword rules. Deterministic + cheap; the
    LLM is only used later to phrase the grounded answer, not to route.

    Order matters: ACTION intents (that write) are matched before READ intents,
    so 'send a booking reminder to everyone stuck' is a bulk action, not the
    'who is stuck' read."""
    q = (query or "").lower().strip()

    m = re.search(r"(mention|about|said|talk\w*|contain\w*)\s+[\"']?([\w\- ]{2,40})",
                  q)
    if ("search" in q or "find" in q or "mention" in q) and m:
        return "search_messages", {"term": m.group(2).strip()}

    # --- Inbox / new replies (read + a suggested draft per applicant) ----------
    # This is inbox triage, not a single-applicant send, so it must beat the
    # generic 'draft/reply' branch below (which needs an applicant open).
    if any(w in q for w in ("inbox", "inboxes", "new message", "new messages",
                            "new reply", "new replies", "unanswered", "unread",
                            "who messaged", "who wrote", "who replied",
                            "needs a reply", "need a reply", "need replies",
                            "waiting on a reply", "waiting for a reply",
                            "reply to everyone", "replies to my", "reply to my",
                            "catch up on")):
        return "needs_reply", {}

    # --- Action intents (require an explicit send/booking/message verb) --------
    booking_word = any(w in q for w in ("book", "booking", "self-schedule",
                                        "calendly", "schedule link", "screening link"))
    if booking_word:
        if any(h in q for h in _BULK_HINTS):
            return "bulk_booking", {}
        return "send_booking", {}
    if any(w in q for w in ("draft", "reply", "replies", "message", "write",
                            "follow up", "followup", "nudge", "reschedule",
                            "thank", "respond")):
        # No applicant open -> they mean their inbox, not one person. Route to the
        # inbox reader (which drafts per applicant) instead of dead-ending on
        # "open an applicant first".
        if not has_lead:
            return "needs_reply", {}
        intent = "check_in"
        if "remind" in q:
            intent = "booking"
        elif "reschedul" in q:
            intent = "reschedule"
        elif "thank" in q:
            intent = "thanks"
        return "send_message", {"intent": intent}

    # Re-consent as an ACTION (send/remind) beats the read below.
    _reconsent = ("reconsent" in q or "re-consent" in q or "re consent" in q)
    if _reconsent and any(w in q for w in ("remind", "send", "nudge", "message",
                                           "notify", "reach out")):
        return "reconsent_reminder", {}

    # --- Calendar / visit-prep intents ----------------------------------------
    if (_reconsent
            or ("consent" in q and any(w in q for w in
                ("visit", "due", "upcoming", "who", "need", "soon")))):
        return "reconsent_due", {}
    if any(w in q for w in ("out of window", "outside the window", "outside their",
                            "deviation", "protocol window", "overdue", "past due",
                            "past-due", "missed visit", "behind on", "window clos")):
        return "visits_out_of_window", {}
    schedule_word = any(w in q for w in (
        "visit", "prep", "prepare", "get ready", "calendar", "schedule",
        "appointment", "this week", "coming up", "upcoming", "today", "tomorrow"))
    if schedule_word:
        when = "tomorrow" if "tomorrow" in q else ("today" if "today" in q else "week")
        if when in ("today", "tomorrow") or any(
                w in q for w in ("prep", "prepare", "get ready", "bring", "ready")):
            return "visit_prep", {"when": when}
        return "week_schedule", {}

    # --- Read intents ----------------------------------------------------------
    if any(w in q for w in ("document", "paperwork", "consent form", "icf",
                            "regulatory", "1572", "vault", "expir", "renew",
                            "signature", "pending doc")):
        return "documents_overview", {}
    if any(w in q for w in ("campaign", "channel", "ad ", " ads", "advert",
                            "spend", "cost per", "cost-per", "marketing",
                            "best source", "which source")):
        return "campaign_performance", {}
    if (("match" in q or "matches" in q)
            or ("record" in q and any(w in q for w in
                ("candidate", "match", "ehr", "new")))):
        return "record_matches", {}
    if any(w in q for w in ("stuck", "not booked", "no booking", "idle")):
        return "stuck_in_screening", {}
    if any(w in q for w in ("waiting on", "pending", "decide", "decision",
                            "need to review", "who's waiting", "whos waiting",
                            "review queue", "to do", "todo")):
        return "pending_decisions", {}
    if any(w in q for w in ("funnel", "velocity", "conversion", "drop", "leak",
                            "enrollment rate", "how are we doing", "overview",
                            "metrics", "stats")):
        return "funnel_overview", {}
    # "how many candidates / applicants do I have" -> funnel snapshot (has totals).
    if (any(w in q for w in ("how many", "count", "total", "number of")) and
            any(w in q for w in ("applicant", "candidate", "patient", "people",
                                 "enrolled", "trial", "study", "studies"))):
        return "funnel_overview", {}
    if any(w in q for w in ("why", "verdict", "eligible", "ineligible",
                            "eligibility", "explain")):
        return "explain_verdict", {}
    if any(w in q for w in ("summar", "brief", "who is this", "tell me about",
                            "this applicant", "this candidate", "recap")):
        return "applicant_summary", {}
    return "help", {}


_PROPOSAL_INTRO = {
    "send_message": "Here's a draft for {target} - review, edit if you like, then send:",
    "send_booking": "I'll send the booking link to {target}. Confirm to send:",
    "bulk_booking": "This will message {target}. Review and confirm:",
    "reconsent_reminder": "This will send a re-consent reminder to {target}. Review and confirm:",
}


def _help_payload():
    return {
        "summary": ("I work across your studies \u2014 inbox, review queue, "
                    "calendar, documents, and campaigns. Tell me what you want "
                    "to do, or pick one below."),
        "items": [], "citations": [],
    }


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text):
    """Pull the first JSON object out of a model response (handles code fences)."""
    m = _JSON_RE.search(text or "")
    return m.group(0) if m else "{}"


def _plan_llm(query, ctx):
    """LLM tool-calling router over the registry. Returns (tool, params) or None
    (on no key / bad output) so the caller falls back to keyword rules."""
    if not mt.LLM_API_KEY:
        return None
    try:
        user = (f"TOOLS:\n{registry.catalog_for_prompt()}\n\n"
                f"An applicant record is currently open: {bool(ctx.get('has_lead'))}\n"
                f"USER REQUEST: {query}\n\n"
                "Pick the single best tool and its params. JSON only.")
        data = json.loads(_extract_json(mt.llm_chat(PLANNER_SYSTEM, user)))
        name = (data.get("tool") or "").strip()
        if name != "help" and name not in registry.REGISTRY:
            return None
        params = data.get("params")
        return name, (params if isinstance(params, dict) else {})
    except Exception:
        return None


def _plan(query, ctx):
    """Choose a tool: LLM planner first (if configured), keyword rules otherwise."""
    return _plan_llm(query, ctx) or _classify(query, bool(ctx.get("has_lead")))


def _ground_with_llm(query, payload):
    """Rephrase the tool payload into natural language, strictly grounded.
    Returns None on any failure so callers fall back to the deterministic text."""
    if not mt.LLM_API_KEY:
        return None
    try:
        user = (f"USER QUESTION:\n{query}\n\nRETRIEVED RECORDS (JSON):\n"
                f"{json.dumps(payload.get('items') or payload.get('summary'), default=str)[:6000]}\n\n"
                f"Deterministic summary for reference: {payload.get('summary', '')}\n\n"
                "Write the answer now, following your rules.")
        out = mt.llm_chat(SYSTEM_PROMPT, user)
        return (out or "").strip() or None
    except Exception:
        return None


def answer(user_id, query, context=None):
    """Entry point. ``context`` may include {"lead_id": int} for the page the
    user is on. Returns {answer, citations, action?, suggestions}.

    Flow: plan a tool (LLM planner or keyword rules) -> dispatch through the
    registry. Reads return a grounded answer; actions return a confirmable
    proposal. The safe confirm/send path is never bypassed here."""
    context = context or {}
    lead_id = context.get("lead_id")
    intent, params = _plan(query, {"has_lead": bool(lead_id)})

    tool = registry.get(intent)
    if tool is None:                       # "help" or an unknown name
        p = _help_payload()
        return {"answer": p["summary"], "citations": [], "trace": [],
                "suggestions": STARTERS}

    if tool.needs_lead and not lead_id:
        return {
            "answer": "Open an applicant first, then ask again - that lets me "
                      "pull their record.",
            "citations": [], "suggestions": STARTERS,
        }

    # --- Action tools: build a confirmable proposal (never auto-send) ----------
    if tool.kind == "action":
        res = _propose(intent, user_id, lead_id, params)
        res.setdefault("trace", tool.trace)
        return res

    # --- Read tools: grounded answer ------------------------------------------
    payload = tool.run(user_id, lead_id, params)
    text = _ground_with_llm(query, payload) or payload.get("summary", "")
    return {
        "answer": text,
        "items": _display_items(payload.get("items")),
        "citations": payload.get("citations", []),
        "trace": tool.trace,
        "suggestions": [],
    }


def _propose(intent, user_id, lead_id, params):
    """Build a confirmable action proposal, or explain why it can't be done."""
    if intent == "send_message":
        prop = actions.build_message_proposal(user_id, lead_id, params.get("intent"))
    elif intent == "send_booking":
        prop = actions.build_booking_proposal(user_id, lead_id)
    elif intent == "reconsent_reminder":
        prop = actions.build_reconsent_reminder_proposal(user_id)
    else:  # bulk_booking
        prop = actions.build_bulk_reminder_proposal(user_id)

    if prop is None:
        return {"answer": "I can't find that applicant in your studies.",
                "citations": [], "suggestions": []}
    if prop.get("blocked"):
        return {"answer": prop["blocked"], "citations": [], "suggestions": []}

    intro = _PROPOSAL_INTRO.get(intent, "Confirm to continue:").format(
        target=prop.get("target", "this applicant"))
    return {"answer": intro, "citations": [], "proposal": prop}
