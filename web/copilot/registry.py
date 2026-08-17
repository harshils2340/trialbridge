"""Copilot tool registry - the single source of truth for what Bridget can do.

Every capability (read or action) is declared once here with a name, a plain
description, its parameters, and whether it needs an open applicant. The rest of
the system is driven off this table:

  * the LLM planner turns the registry into the menu it chooses from (so adding a
    tool teaches the planner automatically - no prompt edits);
  * ``agent.answer`` dispatches reads straight through ``run`` and routes actions
    into the existing confirm/propose flow;
  * the deterministic keyword classifier stays as a fallback and MUST return
    names that exist here.

Reads return the standard payload ({summary, items, citations}). Actions carry a
``propose`` intent that ``agent._propose`` knows how to turn into a confirmable
proposal - the safe send/act flow is never bypassed.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import tools


@dataclass
class Tool:
    name: str
    kind: str                       # "read" | "action"
    desc: str                       # one line the planner reads
    params: Dict[str, str] = field(default_factory=dict)  # name -> description
    needs_lead: bool = False        # requires an open applicant
    run: Optional[Callable] = None  # reads: (user_id, lead_id, params) -> payload
    propose: str = ""               # actions: intent key for agent._propose
    trace: List[str] = field(default_factory=list)  # honest "what I did" steps


# --- Read tools -------------------------------------------------------------- #
_READS = [
    Tool("pending_decisions", "read",
         "Applicants waiting on an accept/decline decision, best match first.",
         trace=["Scanned your review queue across all studies"],
         run=lambda uid, lid, p: tools.pending_decisions(uid)),
    Tool("stuck_in_screening", "read",
         "Applicants stuck in screening (no booking link, or idle for days).",
         trace=["Checked your screening queue for stalled applicants"],
         run=lambda uid, lid, p: tools.stuck_in_screening(uid)),
    Tool("funnel_overview", "read",
         "Enrollment funnel: totals, conversion, and the biggest drop-off.",
         trace=["Pulled funnel metrics across all your studies"],
         run=lambda uid, lid, p: tools.funnel_overview(uid)),
    Tool("search_messages", "read",
         "Find applicants whose message thread mentions a word or phrase.",
         params={"term": "the word or phrase to search for"},
         trace=["Searched your applicant message history"],
         run=lambda uid, lid, p: tools.search_messages(uid, p.get("term", ""))),
    Tool("applicant_summary", "read",
         "A short brief on the applicant currently open.",
         needs_lead=True,
         trace=["Read this applicant's record and activity"],
         run=lambda uid, lid, p: tools.applicant_summary(uid, lid)),
    Tool("explain_verdict", "read",
         "Explain the open applicant's pre-screen eligibility verdict.",
         needs_lead=True,
         trace=["Reviewed this applicant's eligibility check"],
         run=lambda uid, lid, p: tools.explain_verdict(uid, lid)),
    Tool("visit_prep", "read",
         "Upcoming visits with prep checklists (today, tomorrow, or this week).",
         params={"when": "one of: today | tomorrow | week"},
         trace=["Read your study-visit schedule and prep checklists"],
         run=lambda uid, lid, p: tools.visit_prep(uid, p.get("when", "week"))),
    Tool("week_schedule", "read",
         "A summary of visits over the next 7 days.",
         trace=["Pulled your visits for the next 7 days"],
         run=lambda uid, lid, p: tools.week_schedule(uid)),
    Tool("visits_out_of_window", "read",
         "Visits that are overdue, out of protocol window, or closing soon.",
         trace=["Checked every visit against its protocol window"],
         run=lambda uid, lid, p: tools.visits_out_of_window(uid)),
    Tool("reconsent_due", "read",
         "Participants with an upcoming re-consent visit.",
         trace=["Scanned the schedule for upcoming re-consent visits"],
         run=lambda uid, lid, p: tools.reconsent_due(uid)),
    Tool("documents_overview", "read",
         "Study documents that need action: pending, returned, or due soon.",
         trace=["Checked your document vault for anything pending or due"],
         run=lambda uid, lid, p: tools.documents_overview(uid)),
    Tool("campaign_performance", "read",
         "Which recruitment channels produce enrolled patients, by cost.",
         trace=["Read campaign performance across your studies"],
         run=lambda uid, lid, p: tools.campaign_performance(uid)),
    Tool("record_matches", "read",
         "New pre-screened candidates surfaced from the clinic's own records.",
         trace=["Pulled new candidate matches from your records"],
         run=lambda uid, lid, p: tools.record_matches(uid)),
]

# --- Action tools (build a confirmable proposal; never auto-send) ------------ #
_ACTIONS = [
    Tool("send_message", "action",
         "Draft a follow-up message to the open applicant for you to review.",
         params={"intent": "check_in | booking | reschedule | thanks"},
         needs_lead=True, propose="send_message",
         trace=["Pulled this applicant's context", "Drafted a message for your review"]),
    Tool("send_booking", "action",
         "Send the study's booking link to the open applicant.",
         needs_lead=True, propose="send_booking",
         trace=["Checked this applicant's booking status", "Prepared a booking link"]),
    Tool("bulk_booking", "action",
         "Send a booking reminder to everyone stuck in screening.",
         propose="bulk_booking",
         trace=["Scanned your studies for applicants who haven't booked"]),
    Tool("reconsent_reminder", "action",
         "Send a neutral re-consent reminder to everyone due for re-consent.",
         propose="reconsent_reminder",
         trace=["Found participants with an upcoming re-consent visit"]),
]

REGISTRY: Dict[str, Tool] = {t.name: t for t in _READS + _ACTIONS}


def get(name):
    return REGISTRY.get(name)


def catalog_for_prompt():
    """The tool menu the LLM planner chooses from - name, params, and whether it
    needs an open applicant. Kept terse to stay cheap."""
    lines = []
    for t in REGISTRY.values():
        ps = ("; params: " + ", ".join(f"{k} ({v})" for k, v in t.params.items())
              if t.params else "")
        lead = " [needs an open applicant]" if t.needs_lead else ""
        lines.append(f"- {t.name}: {t.desc}{ps}{lead}")
    return "\n".join(lines)


def names():
    return list(REGISTRY.keys())
