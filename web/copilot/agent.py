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

from . import tools

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

# Suggested prompts shown in the empty rail.
STARTERS = [
    "Who's waiting on my decision?",
    "Who's stuck in screening?",
    "How's my enrollment funnel?",
]


def _classify(query):
    """Return (intent, params) from keyword rules. Deterministic + cheap; the
    LLM is only used later to phrase the grounded answer, not to route."""
    q = (query or "").lower().strip()

    m = re.search(r"(mention|about|said|talk\w*|contain\w*)\s+[\"']?([\w\- ]{2,40})",
                  q)
    if ("search" in q or "find" in q or "mention" in q) and m:
        return "search_messages", {"term": m.group(2).strip()}

    if any(w in q for w in ("stuck", "not booked", "haven't booked",
                            "hasn't booked", "no booking", "idle", "waiting to book")):
        return "stuck_in_screening", {}
    if any(w in q for w in ("waiting on", "pending", "decide", "decision",
                            "need to review", "who's waiting", "whos waiting",
                            "review queue", "to do", "todo")):
        return "pending_decisions", {}
    if any(w in q for w in ("funnel", "velocity", "conversion", "drop", "leak",
                            "enrollment rate", "how are we doing", "overview",
                            "metrics", "stats")):
        return "funnel_overview", {}
    if any(w in q for w in ("why", "verdict", "eligible", "ineligible",
                            "eligibility", "explain")):
        return "explain_verdict", {}
    if any(w in q for w in ("draft", "reply", "message", "write", "follow up",
                            "followup", "reminder", "reschedule", "thank")):
        intent = "check_in"
        if "book" in q or "remind" in q:
            intent = "booking"
        elif "reschedul" in q:
            intent = "reschedule"
        elif "thank" in q:
            intent = "thanks"
        return "draft_reply", {"intent": intent}
    if any(w in q for w in ("summar", "brief", "who is this", "tell me about",
                            "this applicant", "this candidate", "recap")):
        return "applicant_summary", {}
    return "help", {}


_NEEDS_LEAD = {"applicant_summary", "explain_verdict", "draft_reply"}


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

    if intent == "help":
        payload = _help_payload()
    elif intent == "pending_decisions":
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
    elif intent == "draft_reply":
        payload = tools.draft_reply(user_id, lead_id, params.get("intent"))
    else:
        payload = _help_payload()

    # Draft replies and the help text are returned verbatim (no LLM rephrasing -
    # a draft must be exactly the text we'll paste into the thread).
    text = payload.get("summary", "")
    if intent not in ("draft_reply", "help"):
        text = _ground_with_llm(query, payload) or text

    resp = {
        "answer": text,
        "citations": payload.get("citations", []),
        "suggestions": STARTERS if intent == "help" else [],
    }
    if payload.get("action"):
        resp["action"] = payload["action"]
    return resp
