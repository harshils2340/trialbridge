"""Proactive daily digest - the "agent works for you" surface.

Instead of only answering when asked, Bridget can compose a start-of-day brief:
it runs the same grounded read tools and keeps only the sections that need the
coordinator today. This is a pure function over the tools, so it has no side
effects and can be:

  * served on demand (``GET /app/copilot/digest``), or
  * pushed on a schedule by a cron/worker calling ``run_for_all`` (see the
    ``__main__`` entrypoint) once real email/SMS delivery is configured.

Nothing here sends anything - it only summarizes what already exists, scoped to
the user's own studies (authz lives in the tools).
"""

from . import tools

# (section title, tool). Order = the order a coordinator should work them.
_SECTIONS = [
    ("Waiting on your decision", tools.pending_decisions),
    ("Stuck in screening", tools.stuck_in_screening),
    ("Visits needing attention", tools.visits_out_of_window),
    ("Documents due", tools.documents_overview),
    ("Re-consent coming up", tools.reconsent_due),
    ("New record matches", tools.record_matches),
]


def daily_digest(user_id, per_section=5):
    """Return {headline, total, sections[]} - only sections with something to do."""
    sections = []
    for title, fn in _SECTIONS:
        try:
            payload = fn(user_id)
        except Exception:
            continue
        items = payload.get("items") or []
        if not items:
            continue
        sections.append({
            "title": title,
            "count": len(items),
            "summary": payload.get("summary", ""),
            "items": items[:per_section],
            "citations": payload.get("citations", []),
        })
    total = sum(s["count"] for s in sections)
    if total:
        headline = f"{tools._n(total, 'thing')} need you across your studies today."
    else:
        headline = "You're all caught up - nothing needs you across your studies."
    return {"headline": headline, "total": total, "sections": sections}


def run_for_all():
    """Cron entrypoint: build a digest for every study-team user. Returns a list
    of (user_id, digest) so a caller can deliver it. Delivery is intentionally
    NOT done here - wire it to your transactional email/SMS provider (under a BAA)
    where opt-out is honored, then call this from a scheduler."""
    import db
    out = []
    try:
        users = db.list_study_team_users()
    except Exception:
        users = []
    for u in users:
        uid = u["id"] if hasattr(u, "keys") else u
        out.append((uid, daily_digest(uid)))
    return out


if __name__ == "__main__":  # pragma: no cover - manual/cron use
    import json
    import db
    db.init_db()
    print(json.dumps(run_for_all(), default=str, indent=2))
