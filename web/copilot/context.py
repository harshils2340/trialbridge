"""Workspace context for Bridget, study scope, page, open applicant.

The rest of the app scopes via ``session.active_nct``; copilot reads the same
session here so "this trial" and blasts match what the switcher shows.
"""

from flask import session

import db


def build(user, data=None):
    """Return context dict for ``agent.answer`` from the logged-in user."""
    data = data or {}
    ctx = {"user_id": user["id"]}

    lead_id = data.get("lead_id")
    if lead_id is not None:
        try:
            ctx["lead_id"] = int(lead_id)
        except (TypeError, ValueError):
            pass

    studies = db.list_team_studies(user["id"])
    ctx["studies"] = [
        {"nct": s["nct"], "title": (s["title"] or s["nct"])}
        for s in studies
    ]
    valid = {s["nct"] for s in studies}

    if "active_nct" not in session:
        active_nct = studies[0]["nct"] if len(studies) == 1 else ""
    else:
        active_nct = session.get("active_nct") or ""
        if active_nct and active_nct not in valid:
            active_nct = studies[0]["nct"] if len(studies) == 1 else ""

    ctx["active_nct"] = active_nct or ""
    if active_nct:
        ctx["scope_label"] = next(
            (s["title"] or s["nct"] for s in studies if s["nct"] == active_nct),
            active_nct,
        )
    else:
        ctx["scope_label"] = "All studies"

    ctx["has_lead"] = bool(ctx.get("lead_id"))
    ctx["page"] = (data.get("page") or "").strip()
    return ctx
