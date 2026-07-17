"""Trial alerts: turn searching (pull) into notifications (push).

A patient registers what they're interested in once. A background job watches
ClinicalTrials.gov and, when a NEW recruiting trial matches, records it and
notifies them. We baseline every alert at creation (recording what already
exists as "seen") so the patient is only ever interrupted for genuinely new
trials, never spammed with the existing backlog.

The check is deliberately light (a live CT.gov query + recruiting/interventional
filter) - no per-trial LLM - so it scales across many alerts. In production the
loop can be replaced by cron hitting /alerts/run; the daemon here means it works
with zero extra infrastructure too.
"""
import os
import re
import threading
import time

import match_trials as mt
import db

_app = None
_notifier = None
INTERVAL = int(os.environ.get("ALERTS_INTERVAL_SECONDS", str(6 * 3600)))
NOTIFY_MIN_DAYS = max(1, int(os.environ.get("ALERTS_NOTIFY_MIN_DAYS", "7")))
EMAIL_MAX_MATCHES = max(1, int(os.environ.get("ALERTS_EMAIL_MAX_MATCHES", "2")))


def configure(app, notifier=None):
    """Give the module the Flask app (for DB access off-request) and an optional
    notifier(alert, new_matches) callback used when new trials appear."""
    global _app, _notifier
    _app = app
    _notifier = notifier
    if os.environ.get("ALERTS_BACKGROUND", "1") == "1":
        threading.Thread(target=_loop, daemon=True).start()


def _match_ncts(alert):
    """Current recruiting, interventional trials matching this alert.
    Returns [(nct, title)]."""
    geo = None
    if alert["lat"] is not None and alert["lon"] is not None and alert["radius"]:
        unit = alert["unit"] or "km"
        geo = f"distance({alert['lat']},{alert['lon']},{int(alert['radius'])}{unit})"
    try:
        trials = mt.fetch_trials(alert["condition"] or "", max_n=100, geo=geo,
                                 intervention=alert["intervention"] or "")
    except Exception:
        return []
    out = []
    for t in trials:
        if (t.get("overallStatus") or "RECRUITING").upper() != "RECRUITING":
            continue
        if (t.get("studyType") or "").upper() == "OBSERVATIONAL":
            continue
        nct = t.get("nctId")
        if nct:
            out.append((nct, t.get("title", "")))
    return out


def _tokens(*parts):
    txt = " ".join((p or "") for p in parts).strip().lower()
    return {t for t in re.split(r"[^a-z0-9]+", txt) if len(t) >= 3}


def _quality_score(alert, match):
    """Simple relevance score so alert emails only include high-signal matches."""
    title = (match.get("title") or "").strip()
    if not title:
        return 0
    q = _tokens(_alert_val(alert, "label"), _alert_val(alert, "condition"),
                _alert_val(alert, "intervention"))
    t = _tokens(title)
    overlap = len(q & t)
    score = overlap * 12
    if len(title) >= 24:
        score += 10
    if len(title) > 170:
        score -= 8
    return score


# A "strong fit" must share at least one real condition/treatment keyword with
# the trial title (overlap*12), not merely be a long title (length bonus = 10).
# Setting the bar above the length-only bonus forces a genuine relevance signal,
# which is what keeps the alert list tight instead of a loose CT.gov dump.
STRONG_FIT_MIN_SCORE = 12


def _alert_val(alert, key, default=None):
    """Read a field from an alert row/dict (sqlite Row has no .get)."""
    try:
        return alert[key]
    except (KeyError, IndexError, TypeError):
        return default


def _strong_only(alert):
    """Whether this alert should only ever surface high-signal matches.
    Defaults to True so alerts stay quiet and trustworthy unless the patient
    explicitly opts into looser (all) matches."""
    v = _alert_val(alert, "strong_only", 1)
    return True if v is None else bool(v)


def _curate_for_email(alert, rows):
    """Pick a small, useful set of matches for an email digest."""
    picks = []
    for r in rows:
        row = dict(r)
        nct = (row.get("nct") or "").strip()
        title = (row.get("title") or "").strip()
        if not nct or not title:
            continue
        m = {"nct": nct, "title": title, "score": _quality_score(alert, row)}
        picks.append(m)
    picks.sort(key=lambda x: (-x["score"], x["nct"]))
    if _strong_only(alert):
        # Require a real relevance signal; otherwise skip the email entirely.
        picks = [x for x in picks if x["score"] >= STRONG_FIT_MIN_SCORE]
    return picks[:EMAIL_MAX_MATCHES]


def preview_matches(alert, limit=6):
    """What this alert WOULD email today: the current recruiting matches that
    pass the same quality bar the digest uses. Powers the 'what you'll get'
    preview on the alerts page so the patient can trust it before relying on
    it. Live CT.gov call - call from a request, not the tight loop."""
    rows = [{"nct": n, "title": t} for (n, t) in _match_ncts(alert)]
    picks = []
    for row in rows:
        picks.append({"nct": row["nct"], "title": row["title"],
                      "score": _quality_score(alert, row)})
    picks.sort(key=lambda x: (-x["score"], x["nct"]))
    if _strong_only(alert):
        picks = [x for x in picks if x["score"] >= STRONG_FIT_MIN_SCORE]
    for p in picks:
        p["strong"] = p["score"] >= STRONG_FIT_MIN_SCORE
    return picks[:limit]


def curate_for_display(alert, rows, limit=12):
    """Curate the STORED matches for the on-page alert list using the same
    relevance/quality bar (and Strong-fit-only toggle) the email digest uses.
    Without this the page shows the raw CT.gov dump - lots of loosely-related
    studies - which reads nothing like the tight search results the patient
    trusts. Returns dicts: {nct, title, score, is_new, strong}."""
    picks = []
    for r in rows:
        row = dict(r)
        nct = (row.get("nct") or "").strip()
        title = (row.get("title") or "").strip()
        if not nct or not title:
            continue
        score = _quality_score(alert, row)
        picks.append({"nct": nct, "title": title, "score": score,
                      "is_new": bool(row.get("is_new")),
                      "strong": score >= STRONG_FIT_MIN_SCORE})
    # New trials first (so the "New" banner still fires), then by relevance.
    picks.sort(key=lambda x: (not x["is_new"], -x["score"], x["nct"]))
    if _strong_only(alert):
        picks = [x for x in picks if x["score"] >= STRONG_FIT_MIN_SCORE]
    return picks[:limit]


def seed_baseline(alert_id):
    """Record what already matches as seen (is_new=0), so only future trials
    trigger a notification. Call once, right after creating the alert."""
    alert = db.get_alert(alert_id)
    if not alert:
        return 0
    return db.add_alert_matches(alert_id, _match_ncts(alert), is_new=False)


def check_alert(alert):
    """Find trials not seen before -> record as new + notify. Returns new count."""
    matches = _match_ncts(alert)
    seen = db.alert_seen_ncts(alert["id"])
    new = [(n, t) for (n, t) in matches if n not in seen]
    if new:
        db.add_alert_matches(alert["id"], new, is_new=True)
    if _notifier and db.alert_notify_due(alert, min_days=NOTIFY_MIN_DAYS):
        pending = db.get_new_alert_matches(alert["id"], limit=150)
        curated = _curate_for_email(alert, pending)
        if curated:
            try:
                _notifier(alert, curated)
                db.mark_alert_notified(alert["id"])
            except Exception:
                if _app is not None:
                    _app.logger.exception("alert notify failed")
    db.mark_alert_checked(alert["id"])
    return len(new)


def check_all():
    """Check every active alert once. Safe to call from a request or a cron."""
    if _app is None:
        return 0
    total = 0
    with _app.app_context():
        for alert in db.list_active_alerts():
            total += check_alert(alert)
    return total


def _loop():
    time.sleep(30)  # let startup finish before the first sweep
    while True:
        try:
            check_all()
        except Exception:
            if _app is not None:
                _app.logger.exception("alert sweep failed")
        time.sleep(INTERVAL)
