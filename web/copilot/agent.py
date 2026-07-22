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

from . import actions, tools

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
    "4. Be concise and practical - a couple of sentences, plain language."
)

# Action-first starters shown in the empty rail - phrased as work to do, not
# questions to ask (Bridget is an agent that acts, not a Q&A bot).
STARTERS = [
    "Triage my review queue",
    "Send booking reminders to everyone stuck",
    "Find my biggest funnel leak",
]

# Honest, action-phrased trace of what Bridget actually did to answer - shown as
# check-marked steps in the rail (like an agent narrating its tool use).
_TRACE = {
    "pending_decisions": ["Scanned your review queue across all studies"],
    "stuck_in_screening": ["Checked your screening queue for stalled applicants"],
    "funnel_overview": ["Pulled funnel metrics across all your studies"],
    "search_messages": ["Searched your applicant message history"],
    "applicant_summary": ["Read this applicant's record and activity"],
    "explain_verdict": ["Reviewed this applicant's eligibility check"],
    "send_message": ["Pulled this applicant's context", "Drafted a message for your review"],
    "send_booking": ["Checked this applicant's booking status", "Prepared a booking link"],
    "bulk_booking": ["Scanned your studies for applicants who haven't booked"],
}


_BULK_HINTS = ("everyone", "all of them", "each of", "each one", "stuck",
               "hasn't booked", "haven't booked", "who hasn", "the ones",
               "all applicants", "everybody")


def _classify(query):
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

    # --- Action intents (require an explicit send/booking/message verb) --------
    booking_word = any(w in q for w in ("book", "booking", "self-schedule",
                                        "calendly", "schedule link", "screening link"))
    if booking_word:
        if any(h in q for h in _BULK_HINTS):
            return "bulk_booking", {}
        return "send_booking", {}
    if any(w in q for w in ("draft", "reply", "message", "write", "follow up",
                            "followup", "nudge", "reschedule", "thank")):
        intent = "check_in"
        if "remind" in q:
            intent = "booking"
        elif "reschedul" in q:
            intent = "reschedule"
        elif "thank" in q:
            intent = "thanks"
        return "send_message", {"intent": intent}

    # --- Read intents ----------------------------------------------------------
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


_NEEDS_LEAD = {"applicant_summary", "explain_verdict", "send_message",
               "send_booking"}
_PROPOSAL_INTRO = {
    "send_message": "Here's a draft for {target} - review, edit if you like, then send:",
    "send_booking": "I'll send the booking link to {target}. Confirm to send:",
    "bulk_booking": "This will message {target}. Review and confirm:",
}


def _help_payload():
    return {
        "summary": ("I can help with your studies. Try: \u201cwho's waiting on my "
                    "decision?\u201d, \u201cwho's stuck in screening?\u201d, "
                    "\u201chow's my funnel?\u201d, open an applicant and ask "
                    "\u201csummarize this applicant\u201d or \u201cdraft a "
                    "follow-up\u201d."),
        "items": [], "citations": [],
    }


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
    user is on. Returns {answer, citations, action?, suggestions}."""
    context = context or {}
    intent, params = _classify(query)

    lead_id = context.get("lead_id")
    if intent in _NEEDS_LEAD and not lead_id:
        return {
            "answer": "Open an applicant first, then ask again - that lets me "
                      "pull their record.",
            "citations": [], "suggestions": STARTERS,
        }

    # --- Action intents: build a confirmable proposal (never auto-send) --------
    if intent in ("send_message", "send_booking", "bulk_booking"):
        res = _propose(intent, user_id, lead_id, params)
        res.setdefault("trace", _TRACE.get(intent, []))
        return res

    # --- Read intents: grounded answer -----------------------------------------
    if intent == "pending_decisions":
        payload = tools.pending_decisions(user_id)
    elif intent == "stuck_in_screening":
        payload = tools.stuck_in_screening(user_id)
    elif intent == "funnel_overview":
        payload = tools.funnel_overview(user_id)
    elif intent == "search_messages":
        payload = tools.search_messages(user_id, params.get("term", ""))
    elif intent == "applicant_summary":
        payload = tools.applicant_summary(user_id, lead_id)
    elif intent == "explain_verdict":
        payload = tools.explain_verdict(user_id, lead_id)
    else:
        payload = _help_payload()

    text = payload.get("summary", "")
    if intent != "help":
        text = _ground_with_llm(query, payload) or text
    return {
        "answer": text,
        "citations": payload.get("citations", []),
        "trace": _TRACE.get(intent, []),
        "suggestions": STARTERS if intent == "help" else [],
    }


def _propose(intent, user_id, lead_id, params):
    """Build a confirmable action proposal, or explain why it can't be done."""
    if intent == "send_message":
        prop = actions.build_message_proposal(user_id, lead_id, params.get("intent"))
    elif intent == "send_booking":
        prop = actions.build_booking_proposal(user_id, lead_id)
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
