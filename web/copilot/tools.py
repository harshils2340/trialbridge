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


def _stuck_leads(user_id, days=5):
    """Leads in screening that aren't moving: no booking link sent, or idle N+
    days. Returns [(lead_row, no_link, idle_days)], most-idle first."""
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
    return stuck


def stuck_lead_ids(user_id, days=5):
    """Lead ids stuck in screening that can actually be messaged (revealed and
    not opted out)."""
    return [l["id"] for (l, _n, _i) in _stuck_leads(user_id, days)
            if contactable(l)]


def stuck_in_screening(user_id, days=5, limit=8):
    """In screening but not moving - no booking link sent, or idle for N+ days.
    This is usually the biggest enrollment leak, so it gets its own tool."""
    stuck = _stuck_leads(user_id, days)
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
# Calendar / visit-prep tools - read the study-visit schedule so Bridget can
# answer "what do I need to prep?", "what's out of window?", "who needs
# re-consent?". Grounded in the same visit rows the Calendar page shows; every
# citation links back to /app/calendar so the human verifies and acts.
# --------------------------------------------------------------------------- #
_KIND_LABELS = {
    "screening": "Screening", "baseline": "Baseline", "treatment": "Treatment",
    "followup": "Follow-up", "reconsent": "Re-consent", "phone": "Phone check-in",
    "close": "Close-out",
}


def _visit_flag(row):
    """Mirror the Calendar page's status flag (overdue / out of window / closing)."""
    status = (_row_get(row, "status") or "scheduled")
    if status in ("completed", "cancelled"):
        return status
    if status == "missed":
        return "overdue"
    now = dt.datetime.now()
    va = _parse_ts(_row_get(row, "visit_at"))
    we = _parse_ts(_row_get(row, "window_end"))
    if va and va < now:
        return "overdue"
    if we and va and va > we:
        return "deviation"
    if we and va:
        days = (we.date() - now.date()).days
        if 0 <= days <= 3:
            return "closing"
    return "ok"


def _prep_items(row):
    return [p.strip() for p in (_row_get(row, "prep") or "").splitlines() if p.strip()]


def _visit_rows(user_id, back_days=14, ahead_days=45):
    """All visits across the user's studies in a window (past 2wks .. next 45d)."""
    if not user_ncts(user_id):
        return []
    today = dt.date.today()
    lo = (today - dt.timedelta(days=back_days)).strftime("%Y-%m-%d %H:%M")
    hi = (today + dt.timedelta(days=ahead_days)).strftime("%Y-%m-%d 23:59")
    try:
        return list(db.list_calendar_visits(user_id, lo, hi))
    except Exception:
        return []


def _visit_when(row):
    va = _parse_ts(_row_get(row, "visit_at"))
    if not va:
        return ""
    hour = va.hour % 12 or 12
    return f"{va.strftime('%a %b')} {va.day}, {hour}:{va.strftime('%M %p')}"


def _visit_item(row):
    return {
        "who": _row_get(row, "lead_name") or "Participant",
        "kind": _KIND_LABELS.get(_row_get(row, "kind"), "Visit"),
        "when": _visit_when(row),
        "study": _row_get(row, "trial_title") or _row_get(row, "nct") or "",
        "prep": _prep_items(row),
        "url": "/app/calendar",
    }


_FLAG_WORD = {"overdue": "past due", "deviation": "booked outside its window",
              "closing": "window closing soon"}


def _n(count, singular, plural=None):
    """'1 visit' / '3 visits' - plain-English counting, no '(s)'."""
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def visit_prep(user_id, when="week"):
    """Visits with prep to get ready, for today / tomorrow / the next 7 days."""
    today = dt.date.today()
    rows = _visit_rows(user_id)
    if when == "today":
        keep = [r for r in rows if (_parse_ts(_row_get(r, "visit_at")) or dt.datetime.max).date() == today]
        span = "today"
    elif when == "tomorrow":
        tom = today + dt.timedelta(days=1)
        keep = [r for r in rows if (_parse_ts(_row_get(r, "visit_at")) or dt.datetime.max).date() == tom]
        span = "tomorrow"
    else:
        end = today + dt.timedelta(days=7)
        keep = [r for r in rows
                if today <= (_parse_ts(_row_get(r, "visit_at")) or dt.datetime.max).date() <= end]
        span = "in the next 7 days"
    keep = [r for r in keep if _visit_flag(r) not in ("cancelled",)]
    keep.sort(key=lambda r: _row_get(r, "visit_at"))
    if not keep:
        return {"summary": f"You have no visits scheduled {span}.",
                "items": [], "citations": []}
    with_prep = [r for r in keep if _prep_items(r)]
    items = [_visit_item(r) for r in keep[:10]]
    lines = [f"You have {_n(len(keep), 'visit')} {span}."]
    if with_prep:
        verb = "has" if len(with_prep) == 1 else "have"
        lines.append(f"{_n(len(with_prep), 'visit')} {verb} a prep checklist to "
                     "send or complete beforehand.")
    return {
        "summary": " ".join(lines),
        "items": items,
        "citations": [{"label": "Calendar", "url": "/app/calendar"}],
    }


def visits_out_of_window(user_id):
    """Visits that are overdue, booked outside the protocol window, or closing."""
    rows = _visit_rows(user_id)
    flagged = [(r, _visit_flag(r)) for r in rows]
    hits = [(r, f) for r, f in flagged if f in ("overdue", "deviation", "closing")]
    order = {"deviation": 0, "overdue": 1, "closing": 2}
    hits.sort(key=lambda t: (order.get(t[1], 9), _row_get(t[0], "visit_at")))
    if not hits:
        return {"summary": "Every visit is inside its protocol window - nothing "
                "overdue or out of window right now.", "items": [], "citations": []}
    items = []
    for r, f in hits[:10]:
        it = _visit_item(r)
        it["issue"] = _FLAG_WORD.get(f, f)
        items.append(it)
    verb = "needs" if len(hits) == 1 else "need"
    return {
        "summary": (f"{_n(len(hits), 'visit')} {verb} attention: overdue, out of "
                    "protocol window, or with a window closing in the next few days."),
        "items": items,
        "citations": [{"label": "Calendar", "url": "/app/calendar"}],
    }


def reconsent_due(user_id):
    """Upcoming re-consent visits (e.g. after a protocol/ICF amendment)."""
    today = dt.date.today()
    rows = _visit_rows(user_id)
    hits = [r for r in rows
            if _row_get(r, "kind") == "reconsent"
            and (_parse_ts(_row_get(r, "visit_at")) or dt.datetime.min).date() >= today]
    hits.sort(key=lambda r: _row_get(r, "visit_at"))
    if not hits:
        return {"summary": "No re-consent visits are on the calendar right now.",
                "items": [], "citations": []}
    verb = "has" if len(hits) == 1 else "have"
    return {
        "summary": (f"{_n(len(hits), 'participant')} {verb} a re-consent visit "
                    "coming up. Make sure the current ICF version is ready for "
                    "each one."),
        "items": [_visit_item(r) for r in hits[:10]],
        "citations": [{"label": "Calendar", "url": "/app/calendar"}],
    }


def contactable(lead):
    """A revealed lead who has not opted out of contact. The single gate every
    copilot send path checks before messaging anyone (compliance)."""
    if not _row_get(lead, "revealed"):
        return False
    return not int(_row_get(lead, "contact_opt_out", 0) or 0)


def reconsent_lead_ids(user_id):
    """Revealed, contactable participants with an upcoming re-consent visit."""
    today = dt.date.today()
    ids, seen = [], set()
    for r in _visit_rows(user_id):
        if _row_get(r, "kind") != "reconsent":
            continue
        va = _parse_ts(_row_get(r, "visit_at"))
        if not va or va.date() < today:
            continue
        lid = _row_get(r, "lead_id")
        if not lid or lid in seen:
            continue
        seen.add(lid)
        lead = own_lead(user_id, lid)
        if lead and contactable(lead):
            ids.append(int(lid))
    return ids


def week_schedule(user_id):
    """A plain summary of the next 7 days plus anything needing attention."""
    prep = visit_prep(user_id, "week")
    window = visits_out_of_window(user_id)
    n_attn = len(window["items"])
    summary = prep["summary"]
    if n_attn:
        summary += f" {n_attn} of them need attention (overdue or protocol window)."
    return {"summary": summary, "items": prep["items"],
            "citations": [{"label": "Calendar", "url": "/app/calendar"}]}


# --------------------------------------------------------------------------- #
# Paperwork tools - documents, campaigns, and record matches. These widen what
# Bridget is "connected to" beyond the applicant queue and the calendar, so the
# same grounded-answer contract covers the admin surfaces a coordinator lives in.
# --------------------------------------------------------------------------- #
_DOC_ACTIONABLE = ("pending", "in_review", "returned")


def documents_overview(user_id, limit=8):
    """Study documents that need action: pending review, returned, or due soon.
    Grounded in the same document vault the Documents page shows."""
    try:
        docs = list(db.list_documents(user_id))
    except Exception:
        docs = []
    if not docs:
        return {"summary": "No study documents are on file yet.",
                "items": [], "citations": []}
    today = dt.date.today()
    actionable = []
    for d in docs:
        status = _row_get(d, "status") or "pending"
        due = _parse_ts(_row_get(d, "due_at"))
        due_days = (due.date() - today).days if due else None
        overdue = due_days is not None and due_days < 0
        soon = due_days is not None and 0 <= due_days <= 7
        if status in _DOC_ACTIONABLE or overdue or soon:
            actionable.append((d, status, due_days, overdue))
    # Overdue first, then soonest due, then pending.
    actionable.sort(key=lambda t: (not t[3], t[2] if t[2] is not None else 999))
    if not actionable:
        return {"summary": f"All {len(docs)} documents are current - nothing "
                "pending, returned, or due this week.", "items": [],
                "citations": [{"label": "Documents", "url": "/app/documents"}]}
    items = []
    for d, status, due_days, overdue in actionable[:limit]:
        if overdue:
            why = f"overdue by {_n(abs(due_days), 'day')}"
        elif due_days is not None:
            why = "due today" if due_days == 0 else f"due in {_n(due_days, 'day')}"
        else:
            why = status.replace("_", " ")
        items.append({
            "title": _row_get(d, "title") or _row_get(d, "doc_type") or "Document",
            "status": status, "version": _row_get(d, "version"),
            "party": _row_get(d, "party_name") or _row_get(d, "party"),
            "study": _row_get(d, "nct"), "why": why, "url": "/app/documents"})
    n_over = sum(1 for _d, _s, _dd, ov in actionable if ov)
    lead = f"{_n(len(actionable), 'document')} need attention"
    if n_over:
        lead += f", {n_over} overdue"
    return {
        "summary": lead + ". Clearing these keeps sponsor and IRB paperwork "
                   "current so visits aren't held up.",
        "items": items,
        "citations": [{"label": "Documents", "url": "/app/documents"}],
    }


def campaign_performance(user_id, limit=6):
    """Which recruitment channels actually produce enrolled patients, by
    cost-per-enrolled. Read off the real attributed pipeline."""
    try:
        perf = db.campaign_performance(user_id, ncts=user_ncts(user_id))
    except Exception:
        perf = []
    live = [c for c in perf if c.get("applicants")]
    if not live:
        return {"summary": "No campaigns have produced attributed applicants yet.",
                "items": [], "citations": [{"label": "Campaigns",
                                            "url": "/app/campaigns"}]}
    # Best channel = lowest cost per enrolled (campaigns with enrollments first).
    def _key(c):
        cpe = c.get("cost_per_enrolled")
        return (cpe is None, cpe if cpe is not None else 0)
    ranked = sorted(live, key=_key)
    items = []
    for c in ranked[:limit]:
        items.append({
            "name": c.get("name"), "channel": c.get("channel"),
            "applicants": c.get("applicants", 0), "enrolled": c.get("enrolled", 0),
            "spend_usd": c.get("spend_usd", 0),
            "cost_per_enrolled": c.get("cost_per_enrolled"),
            "cost_per_applicant": c.get("cost_per_applicant"),
            "url": "/app/campaigns"})
    best = next((c for c in ranked if c.get("enrolled")), None)
    total_app = sum(c.get("applicants", 0) for c in live)
    total_enr = sum(c.get("enrolled", 0) for c in live)
    parts = [f"{_n(len(live), 'campaign')} running: {total_app} applicants, "
             f"{total_enr} enrolled."]
    if best and best.get("cost_per_enrolled") is not None:
        parts.append(f"Best value is {best['name']} at "
                     f"${best['cost_per_enrolled']:,.0f} per enrolled - lean spend there.")
    return {
        "summary": " ".join(parts),
        "items": items,
        "citations": [{"label": "Campaigns", "url": "/app/campaigns"}],
    }


def record_matches(user_id, limit=8):
    """New pre-screened candidates surfaced from the clinic's own records."""
    try:
        counts = db.patient_match_counts(user_id)
        new = db.list_patient_matches(user_id, status="new")
    except Exception:
        counts, new = {"new": 0}, []
    if not new:
        return {"summary": "No new candidate matches from your records right now.",
                "items": [], "citations": [{"label": "Matches",
                                            "url": "/app/matches"}]}
    items = []
    for m in new[:limit]:
        items.append({
            "code": _row_get(m, "display_name") or _row_get(m, "label")
                    or f"Record #{_row_get(m, 'id')}",
            "score": _row_get(m, "score", 0),
            "study": _row_get(m, "title") or _row_get(m, "nct"),
            "url": "/app/matches"})
    return {
        "summary": (f"{_n(int(counts.get('new', len(new))), 'new candidate')} "
                    "matched from your records and ready to review. Reviewing and "
                    "reaching out is the top of your funnel."),
        "items": items,
        "citations": [{"label": "Matches", "url": "/app/matches"}],
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


def message_draft(user_id, lead_id, intent="check_in"):
    """Return a proposed follow-up message for one applicant (de-identified name
    only when revealed), or None if the lead isn't in the user's team. Used to
    seed a confirmable send_message action."""
    lead = own_lead(user_id, lead_id)
    if not lead:
        return None
    revealed = bool(_row_get(lead, "revealed"))
    name = (lead["name"].split(" ")[0]
            if revealed and _row_get(lead, "name") else "there")
    key = "check_in"
    for k in _DRAFT_TEMPLATES:
        if k in (intent or ""):
            key = k
            break
    return {"text": _DRAFT_TEMPLATES[key].format(name=name),
            "label": label(lead), "url": _url(lead), "revealed": revealed,
            "opted_out": bool(int(_row_get(lead, "contact_opt_out", 0) or 0))}
