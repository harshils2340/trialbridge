"""Recruitment funnel analytics - the plan + proof a site/sponsor buys.

77% of sites run recruitment with no plan and no numbers. Everything here is
computed straight from data we already capture (`leads` + `lead_events`), so it
doubles as the operational view (where are we leaking?) and the sales artifact
(here's your live funnel and conversion).

No external deps; renders as simple bars in the template.
"""
import datetime as dt
import statistics

import db

_FMT = "%Y-%m-%d %H:%M"


def _recon_outcome(row):
    """Read the 'outcome' from a reconciliation record that may be a sqlite3.Row
    (no .get) or a plain dict. Returns '' when missing."""
    if not row:
        return ""
    try:
        return row["outcome"]
    except (KeyError, IndexError, TypeError):
        return ""


def _parse(ts):
    try:
        return dt.datetime.strptime(ts, _FMT)
    except (ValueError, TypeError):
        return None


def _stage_index(status, pipeline):
    return pipeline.index(status) if status in pipeline else -1


def _lead_stage_entries(lead, events, pipeline):
    """Earliest timestamp the lead entered each pipeline stage (by index).
    Returns {index: datetime}. Falls back to created_at for the first stage."""
    entries = {}
    for e in events:
        idx = _stage_index(e["status"], pipeline)
        if idx < 0:
            continue
        t = _parse(e["created_at"])
        if t and (idx not in entries or t < entries[idx]):
            entries[idx] = t
    created = _parse(lead["created_at"])
    if created and (0 not in entries or created < entries[0]):
        entries[0] = created
    return entries


def _max_reached(lead, events, pipeline):
    """Highest pipeline index this lead ever attained (current status + history)."""
    best = _stage_index(lead["status"], pipeline)
    for e in events:
        best = max(best, _stage_index(e["status"], pipeline))
    return best


def _median_days(deltas):
    if not deltas:
        return None
    return round(statistics.median(deltas), 1)


def funnel_stats(ncts=None):
    pipeline = db.LEAD_PIPELINE
    labels = db.LEAD_LABELS
    use_scope = bool(ncts)
    leads = db.list_leads() if not use_scope else _scoped_leads(ncts)
    recon = db.latest_reconciliation_for_leads([l["id"] for l in leads])

    reached = [0] * len(pipeline)          # leads that ever hit each stage
    transition_days = {i: [] for i in range(len(pipeline) - 1)}
    per_trial = {}
    closed = withdrawn = 0

    for lead in leads:
        events = db.get_lead_events(lead["id"])
        entries = _lead_stage_entries(lead, events, pipeline)
        mx = _max_reached(lead, events, pipeline)
        for i in range(len(pipeline)):
            if mx >= i:
                reached[i] += 1
        for i in range(len(pipeline) - 1):
            if i in entries and (i + 1) in entries:
                d = (entries[i + 1] - entries[i]).total_seconds() / 86400.0
                if d >= 0:
                    transition_days[i].append(d)
        if lead["status"] == "closed":
            closed += 1
        elif lead["status"] == "withdrawn":
            withdrawn += 1

        key = lead["nct"] or lead["title"] or "Unknown"
        pt = per_trial.setdefault(key, {"nct": lead["nct"], "title": lead["title"],
                                        "total": 0, "enrolled": 0})
        pt["total"] += 1
        if mx >= _stage_index("enrolled", pipeline):
            pt["enrolled"] += 1

    stages = []
    for i, key in enumerate(pipeline):
        prev = reached[i - 1] if i > 0 else reached[i]
        conv = (reached[i] / prev * 100.0) if i > 0 and prev else (100.0 if i == 0 else 0.0)
        stages.append({
            "key": key, "label": labels.get(key, key), "reached": reached[i],
            "conv_from_prev": round(conv, 0),
            "pct_of_top": round(reached[i] / reached[0] * 100.0, 0) if reached[0] else 0,
        })

    time_in_stage = []
    for i in range(len(pipeline) - 1):
        time_in_stage.append({
            "from": labels.get(pipeline[i], pipeline[i]),
            "to": labels.get(pipeline[i + 1], pipeline[i + 1]),
            "median_days": _median_days(transition_days[i]),
            "n": len(transition_days[i]),
        })

    # Biggest leak = the step with the lowest conversion (needs >=1 who reached it).
    dropoff = None
    for i in range(1, len(pipeline)):
        if reached[i - 1] >= 1:
            lost = reached[i - 1] - reached[i]
            pct = lost / reached[i - 1] * 100.0
            if dropoff is None or pct > dropoff["pct"]:
                dropoff = {"from": labels.get(pipeline[i - 1], pipeline[i - 1]),
                           "to": labels.get(pipeline[i], pipeline[i]),
                           "pct": round(pct, 0), "lost": lost}

    trials = sorted(per_trial.values(), key=lambda x: -x["total"])
    for t in trials:
        t["conv"] = round(t["enrolled"] / t["total"] * 100.0, 0) if t["total"] else 0

    top = reached[0] or 1
    verified_enrolled = sum(
        1 for l in leads
        if _recon_outcome(recon.get(l["id"])) == "enrolled_verified")
    source_breakdown = _source_breakdown(leads, recon, pipeline)
    return {
        "stages": stages,
        "overall_conv": round(reached[-1] / top * 100.0, 1),
        "time_in_stage": time_in_stage,
        "dropoff": dropoff,
        "per_trial": trials[:12],
        "totals": {
            "total": len(leads),
            "enrolled": reached[-1],
            "verified_enrolled": verified_enrolled,
            "unverified_enrolled": max(0, reached[-1] - verified_enrolled),
            "active": _count_active(leads),
            "closed": closed,
            "withdrawn": withdrawn,
        },
        "verification_rate": round(
            (verified_enrolled / reached[-1] * 100.0), 1) if reached[-1] else 0.0,
        "source_breakdown": source_breakdown,
        "engagement": _engagement(ncts if use_scope else None),
    }


def _count_active(leads):
    return sum(1 for l in leads if l["status"] in db._ACTIVE_STAGES)


def _scoped_leads(ncts):
    ncts = sorted({x for x in (ncts or []) if x})
    if not ncts:
        return []
    qs = ",".join("?" * len(ncts))
    return db.get_db().execute(
        f"SELECT * FROM leads WHERE nct IN ({qs}) ORDER BY updated_at DESC, id DESC",
        ncts).fetchall()


def _engagement(ncts=None):
    if ncts:
        return db.engagement_for_ncts(ncts)
    d = db.get_db()
    msgs = d.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
    from_patient = d.execute(
        "SELECT COUNT(*) n FROM messages WHERE sender='patient'").fetchone()["n"]
    visits = d.execute("SELECT COUNT(*) n FROM lead_visits").fetchone()["n"]
    return {"messages": msgs, "patient_messages": from_patient, "visits": visits}


def _source_breakdown(leads, recon, pipeline):
    """Channel-level throughput/cohort quality by lead source."""
    def _bucket(src):
        s = (src or "web").strip().lower()
        return "physician" if s in {
            "referral", "invite", "physician", "emr", "doctor_referral"
        } else "patient"

    out = {}
    for lead in leads:
        src = _bucket(lead["source"])
        row = out.setdefault(src, {
            "source": src,
            "total": 0,
            "prescreen": 0,
            "eligible": 0,
            "screening": 0,
            "enrolled": 0,
            "verified_enrolled": 0,
        })
        row["total"] += 1
        mx = _stage_index(lead["status"], pipeline)
        row["prescreen"] += 1 if mx >= _stage_index("prescreen", pipeline) else 0
        row["eligible"] += 1 if mx >= _stage_index("eligible", pipeline) else 0
        row["screening"] += 1 if mx >= _stage_index("screening", pipeline) else 0
        row["enrolled"] += 1 if mx >= _stage_index("enrolled", pipeline) else 0
        if _recon_outcome(recon.get(lead["id"])) == "enrolled_verified":
            row["verified_enrolled"] += 1
    rows = list(out.values())
    # Keep the dashboard stable with exactly two platform sources.
    for src in ("physician", "patient"):
        out.setdefault(src, {
            "source": src,
            "total": 0,
            "prescreen": 0,
            "eligible": 0,
            "screening": 0,
            "enrolled": 0,
            "verified_enrolled": 0,
        })
    rows = list(out.values())
    for r in rows:
        r["enroll_conv"] = round((r["enrolled"] / r["total"] * 100.0), 1) \
            if r["total"] else 0.0
        r["verified_rate"] = round((r["verified_enrolled"] / r["enrolled"] * 100.0), 1) \
            if r["enrolled"] else 0.0
    rows.sort(key=lambda x: (-x["total"], x["source"]))
    return rows
