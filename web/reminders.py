"""The "Navigator" in software: proactive, scheduled outreach that keeps
applicants from going quiet - the #1 driver of trial retention.

Two jobs run on a background sweep (mirrors alerts.py; in prod point cron at
/reminders/run instead):

1. Visit reminders - for any booked visit happening within the next window
   (default 24h) that hasn't been reminded, drop a reminder into the thread and
   email the patient, then mark it reminded.
2. Quiet-applicant nudges - for any application still active in the funnel with
   no activity for QUIET_DAYS (and not nudged recently), post a gentle check-in
   and email them. Silence is what kills enrollment; this breaks it.

Everything has an in-app surface (a system message on the thread), so it works
with email off (NOTIFY_LIVE=0). Email is sent via the callbacks app.py provides.
"""
import datetime as dt
import os
import threading
import time

import db

_app = None
_on_visit = None
_on_nudge = None

INTERVAL = int(os.environ.get("REMINDERS_INTERVAL_SECONDS", str(3600)))
VISIT_WINDOW_H = int(os.environ.get("REMINDERS_VISIT_WINDOW_HOURS", "24"))
QUIET_DAYS = int(os.environ.get("REMINDERS_QUIET_DAYS", "3"))
_FMT = "%Y-%m-%d %H:%M"


def configure(app, on_visit=None, on_nudge=None):
    """on_visit(visit_row) and on_nudge(lead_row) send the (optional) emails."""
    global _app, _on_visit, _on_nudge
    _app = app
    _on_visit = on_visit
    _on_nudge = on_nudge
    if os.environ.get("REMINDERS_BACKGROUND", "1") == "1":
        threading.Thread(target=_loop, daemon=True).start()


def _parse(ts):
    try:
        return dt.datetime.strptime(ts, _FMT)
    except (ValueError, TypeError):
        return None


def check_visits():
    """Remind patients about visits happening within the window."""
    within = (dt.datetime.now() + dt.timedelta(hours=VISIT_WINDOW_H)).strftime(_FMT)
    n = 0
    for v in db.visits_due_for_reminder(within):
        when, loc = v["visit_at"], v["location"]
        msg = f"Reminder: your {v['kind']} visit is on {when}"
        msg += f" at {loc}." if loc else "."
        msg += " Showing up is the key step - reply here if anything's in the way."
        db.add_message(v["lead_id"], "system", msg)
        db.mark_visit_reminded(v["id"])
        if _on_visit:
            try:
                _on_visit(v)
            except Exception:
                if _app is not None:
                    _app.logger.exception("visit reminder email failed")
        n += 1
    return n


def check_nudges():
    """Re-engage applications that have gone quiet mid-funnel."""
    cutoff = dt.datetime.now() - dt.timedelta(days=QUIET_DAYS)
    n = 0
    for lead in db.list_active_stage_leads():
        last = _parse(db.last_activity_at(lead["id"], lead["created_at"]))
        if last is None or last > cutoff:
            continue                          # still recent -> leave alone
        nudged = _parse(lead["nudged_at"])
        if nudged is not None and nudged > cutoff:
            continue                          # already nudged recently
        db.add_message(
            lead["id"], "system",
            "Just checking in - your application is still active. Reply here with "
            "any questions, or let the team know you're still interested.")
        db.set_nudged(lead["id"])
        if _on_nudge:
            try:
                _on_nudge(lead)
            except Exception:
                if _app is not None:
                    _app.logger.exception("nudge email failed")
        n += 1
    return n


def run_all():
    if _app is None:
        return {"visits": 0, "nudges": 0}
    with _app.app_context():
        return {"visits": check_visits(), "nudges": check_nudges()}


def _loop():
    time.sleep(45)
    while True:
        try:
            run_all()
        except Exception:
            if _app is not None:
                _app.logger.exception("reminder sweep failed")
        time.sleep(INTERVAL)
