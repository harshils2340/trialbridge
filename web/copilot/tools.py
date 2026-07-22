"""Copilot tools - the thin, well-scoped functions the assistant reasons over.

Each tool takes the acting ``user_id`` and returns a structured payload:

    {
      "summary": str,                 # one-line deterministic answer
      "items":   [ {...}, ... ],      # structured rows (used for grounding)
      "citations": [ {"label", "url"} ],
      "action":  {optional proposed write - never executed here},
    }

Authz lives HERE, not in the model: every tool filters to the user's claimed
studies. Names/contact never enter a payload unless the lead was revealed.
"""

import datetime as dt
import json

import db
import analytics


def user_ncts(user_id):
    return sorted(db.user_claimed_ncts(user_id) or [])


def _row_get(row, key, default=""):
    try:
        val = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if val is None else val


def code(lead):
    return f"Candidate #{int(lead['id']):04d}"


def label(lead):
    """De-identified by default; the real name only once the lead is revealed."""
    if _row_get(lead, "revealed") and _row_get(lead, "name"):
        return lead["name"]
    return code(lead)


def _url(lead):
    return f"/app/applicant/{int(lead['id'])}"


_VERDICT_LABEL = {
    "likely_eligible": "likely eligible",
    "possible": "possible",
    "needs_review": "needs review",
    "unlikely": "unlikely",
    "error": "needs review",
}


def verdict(lead):
    """Mirror the queue's eligibility read so the copilot agrees with the UI."""
    raw = _row_get(lead, "eligibility")
    try:
        elig = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        elig = {}
    v = (elig.get("verdict") or "").strip()
    met = len(elig.get("met") or [])
    not_met = len(elig.get("not_met") or [])
    unknown = len(elig.get("unknown") or [])
    if not v:
        if not_met:
            v = "unlikely" if not_met >= 2 else "possible"
        elif unknown:
            v = "possible"
        elif met:
            v = "likely_eligible"
        else:
            v = "needs_review"
    score = int(elig.get("score") or 0)
    if not score:
        base = {"likely_eligible": 90, "possible": 64, "needs_review": 52,
                "unlikely": 26}.get(v, 55)
        score = max(5, min(99, base - 7 * not_met + (4 if met else 0) - 2 * unknown))
    return {
        "verdict": v,
        "label": _VERDICT_LABEL.get(v, "needs review"),
        "score": score,
        "met": elig.get("met") or [],
        "not_met": elig.get("not_met") or [],
        "unknown": elig.get("unknown") or [],
    }


def own_lead(user_id, lead_id):
    """Fetch a lead ONLY if it belongs to one of the user's claimed studies."""
    if not lead_id:
        return None
    try:
        lead = db.get_lead(int(lead_id))
    except (ValueError, TypeError):
        return None
    if not lead:
        return None
    # Ownership is by claimed study. A lead with no NCT can't be attributed to
    # this user, so deny it rather than risk leaking across tenants.
    nct = _row_get(lead, "nct")
    if not nct or nct not in set(user_ncts(user_id)):
        return None
    return lead


def _parse_ts(raw):
    try:
        return dt.datetime.strptime(str(raw)[:16], "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None


def _days_since(raw):
    t = _parse_ts(raw)
    if not t:
        return None
    return max(0, (dt.datetime.now() - t).days)


# --------------------------------------------------------------------------- #
# Read tools
# --------------------------------------------------------------------------- #
def pending_decisions(user_id, limit=8):
    """Applicants awaiting an accept/decline decision, best-scoring first."""
    leads = db.list_leads_for_user(user_id)
    pend = [l for l in leads
            if _row_get(l, "status") in ("submitted", "prescreen", "eligible")
            and not str(_row_get(l, "decision")).strip()]
    scored = sorted(pend, key=lambda l: -verdict(l)["score"])
    items = []
    for l in scored[:limit]:
        v = verdict(l)
        items.append({"code": label(l), "verdict": v["label"], "score": v["score"],
                      "study": _row_get(l, "nct"), "url": _url(l)})
    if not items:
        return {"summary": "Nothing is waiting on a decision right now - the "
                "queue is clear.", "items": [], "citations": []}
    top = items[0]
    return {
        "summary": (f"{len(pend)} applicant(s) are waiting on your decision. "
                    f"Highest match: {top['code']} ({top['verdict']}, "
                    f"score {top['score']})."),
        "items": items,
        "citations": [{"label": i["code"], "url": i["url"]} for i in items],
    }


def stuck_in_screening(user_id, days=5, limit=8):
    """In screening but not moving - no booking link sent, or idle for N+ days.
    This is usually the biggest enrollment leak, so it gets its own tool."""
    leads = db.list_leads_for_user(user_id)
    stuck = []
    for l in leads:
        if _row_get(l, "status") != "screening":
            continue
        no_link = not str(_row_get(l, "schedule_url")).strip()
        idle = _days_since(_row_get(l, "updated_at") or _row_get(l, "created_at"))
        if no_link or (idle is not None and idle >= days):
            stuck.append((l, no_link, idle))
    stuck.sort(key=lambda t: -(t[2] or 0))
    items = []
    for l, no_link, idle in stuck[:limit]:
        why = "no booking link sent yet" if no_link else f"idle {idle}d"
        items.append({"code": label(l), "why": why, "idle_days": idle,
                      "study": _row_get(l, "nct"), "url": _url(l)})
    if not items:
        return {"summary": "No one is stuck in screening - everyone in that "
                "stage has a booking link and recent activity.", "items": [],
                "citations": []}
    return {
        "summary": (f"{len(stuck)} applicant(s) are stuck in screening. Sending a "
                    "booking link (or a nudge) is the fastest way to move "
                    "screened -> enrolled."),
        "items": items,
        "citations": [{"label": i["code"], "url": i["url"]} for i in items],
    }


def applicant_summary(user_id, lead_id):
    """A de-identified 3-line brief on one applicant."""
    lead = own_lead(user_id, lead_id)
    if not lead:
        return {"summary": "I can't find that applicant in your studies.",
                "items": [], "citations": []}
    v = verdict(lead)
    unread = db.lead_unread_for_site(lead["id"])
    booked = bool(str(_row_get(lead, "schedule_url")).strip())
    video = bool(str(_row_get(lead, "video_url")).strip())
    idle = _days_since(_row_get(lead, "updated_at"))
    bits = [
        f"{label(lead)} - {_row_get(lead, 'status') or 'new'} stage.",
        f"Pre-screen: {v['label']} (score {v['score']}).",
    ]
    if unread:
        bits.append(f"{unread} unread message(s) from them.")
    bits.append("Booking link sent." if booked else "No booking link sent yet.")
    if video:
        bits.append("Video call link is set.")
    if idle is not None:
        bits.append(f"Last activity {idle}d ago.")
    return {
        "summary": " ".join(bits),
        "items": [{
            "code": label(lead), "stage": _row_get(lead, "status"),
            "verdict": v["label"], "score": v["score"], "unread": unread,
            "booked": booked, "video": video, "idle_days": idle,
            "url": _url(lead),
        }],
        "citations": [{"label": label(lead), "url": _url(lead)}],
    }


def explain_verdict(user_id, lead_id):
    """Explain the pre-screen verdict against the trial criteria (assist only -
    it never makes the eligibility decision itself)."""
    lead = own_lead(user_id, lead_id)
    if not lead:
        return {"summary": "I can't find that applicant in your studies.",
                "items": [], "citations": []}
    v = verdict(lead)
    parts = [f"{label(lead)} reads as {v['label']} (score {v['score']})."]
    if v["not_met"]:
        parts.append("Criteria not met: " + "; ".join(str(x) for x in v["not_met"][:5]) + ".")
    if v["unknown"]:
        parts.append("Still unknown: " + "; ".join(str(x) for x in v["unknown"][:5]) + ".")
    if v["met"]:
        parts.append(f"{len(v['met'])} criteria met.")
    parts.append("This is an assist - you make the final eligibility call.")
    return {
        "summary": " ".join(parts),
        "items": [{"code": label(lead), "verdict": v["label"], "score": v["score"],
                   "met": v["met"], "not_met": v["not_met"], "unknown": v["unknown"],
                   "url": _url(lead)}],
        "citations": [{"label": label(lead), "url": _url(lead)}],
    }


def funnel_overview(user_id):
    """Enrollment velocity read off the dashboard: totals + biggest leak."""
    ncts = user_ncts(user_id)
    if not ncts:
        return {"summary": "No studies claimed yet, so there's no funnel to read.",
                "items": [], "citations": []}
    stats = analytics.funnel_stats(ncts)
    totals = stats.get("totals", {})
    drop = stats.get("dropoff")
    parts = [
        f"{totals.get('total', 0)} applicant(s), {totals.get('enrolled', 0)} enrolled "
        f"({stats.get('overall_conv', 0)}% overall).",
    ]
    if drop:
        parts.append(f"Biggest leak: {drop['from']} -> {drop['to']} "
                     f"({drop['pct']}% drop, {drop['lost']} lost).")
    items = [{"stage": s["label"], "reached": s["reached"],
              "conv_from_prev": s["conv_from_prev"]} for s in stats.get("stages", [])]
    return {
        "summary": " ".join(parts),
        "items": items,
        "citations": [{"label": "Recruitment dashboard", "url": "/app/dashboard"}],
    }


def search_messages(user_id, term, limit=8):
    """Find applicants whose message thread mentions a term. Returns codes +
    match counts; the message body is only shown for revealed leads."""
    term = (term or "").strip()
    if len(term) < 2:
        return {"summary": "Give me a word or phrase to search for in messages.",
                "items": [], "citations": []}
    leads = db.list_leads_for_user(user_id)
    low = term.lower()
    items = []
    for l in leads:
        msgs = db.get_messages(l["id"])
        hits = [m for m in msgs if low in (_row_get(m, "body")).lower()]
        if not hits:
            continue
        snippet = ""
        if _row_get(l, "revealed"):
            snippet = (_row_get(hits[0], "body"))[:120]
        items.append({"code": label(l), "matches": len(hits), "snippet": snippet,
                      "url": _url(l)})
        if len(items) >= limit:
            break
    if not items:
        return {"summary": f"No message threads mention \u201c{term}\u201d.",
                "items": [], "citations": []}
    return {
        "summary": f"{len(items)} applicant(s) mention \u201c{term}\u201d in messages.",
        "items": items,
        "citations": [{"label": i["code"], "url": i["url"]} for i in items],
    }


# --------------------------------------------------------------------------- #
# Draft tool (proposes text; never sends - a human confirms in the thread)
# --------------------------------------------------------------------------- #
_DRAFT_TEMPLATES = {
    "check_in": "Hi {name}, just checking in on your interest in the study - "
                "happy to answer any questions you have.",
    "booking": "Hi {name}, when you have a moment please book your screening "
               "visit using the link we sent. Let me know if none of the times "
               "work and we'll find another.",
    "reschedule": "Hi {name}, are you still available for your screening visit? "
                  "If not, we can easily reschedule - just reply here.",
    "thanks": "Thanks so much, {name}. We have what we need for now and will "
              "follow up with next steps shortly.",
}


def draft_reply(user_id, lead_id, intent="check_in"):
    lead = own_lead(user_id, lead_id)
    if not lead:
        return {"summary": "Open an applicant first and I'll draft a reply for them.",
                "items": [], "citations": []}
    name = lead["name"].split(" ")[0] if _row_get(lead, "revealed") and _row_get(lead, "name") else "there"
    key = "check_in"
    for k in _DRAFT_TEMPLATES:
        if k in (intent or ""):
            key = k
            break
    text = _DRAFT_TEMPLATES[key].format(name=name)
    return {
        "summary": text,
        "items": [{"code": label(lead), "url": _url(lead)}],
        "citations": [{"label": label(lead), "url": _url(lead)}],
        "action": {"kind": "draft_message", "lead_id": int(lead["id"]),
                   "text": text, "url": _url(lead)},
    }
