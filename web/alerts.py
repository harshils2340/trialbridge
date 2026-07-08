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
import threading
import time

import match_trials as mt
import db

_app = None
_notifier = None
INTERVAL = int(os.environ.get("ALERTS_INTERVAL_SECONDS", str(6 * 3600)))


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
        if _notifier:
            try:
                _notifier(alert, new)
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
