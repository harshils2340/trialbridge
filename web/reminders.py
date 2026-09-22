"""Proactive, scheduled outreach that keeps applications from stalling.

Two jobs run on a background sweep (mirrors alerts.py; in prod point cron at
/reminders/run instead):

1. Visit reminders - for any booked visit happening within the next window
   (default 24h) that hasn't been reminded, drop a reminder into the thread and
   email the patient, then mark it reminded.
2. Quiet-application check-ins - for any application still active in the funnel
   with no activity (from the patient OR the study team) for QUIET_DAYS, email
   the study team asking for a status update. The patient never sees this: they
   can't do anything with a "just checking in" message, and if the team is
   already handling it off-thread, this never fires in the first place, since
   any message the team sends on the thread counts as activity and resets the
   clock.

Visit reminders are patient-facing and keep an in-app surface (a system message
on the thread) so they work with email off (NOTIFY_LIVE=0). Check-ins are
sent only via the callback app.py provides.
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
# Stop checking in after this many unanswered study-team emails so a stalled
# lead never gets emailed about forever.
MAX_NUDGES = int(os.environ.get("REMINDERS_MAX_NUDGES", "3"))
_FMT = "%Y-%m-%d %H:%M"

# SINGLE SWITCH for the cron VISIT REMINDER emails. While off, the in-app
# system message still posts on the thread - only the email is suppressed.
# Turn back on by flipping this to True, or set env REMINDER_EMAILS=1.
# Study-team check-ins aren't gated by this: they're an operational email to
# the clinic, not a patient-facing notification, so they follow the same
# always-on NOTIFY_LIVE switch every other clinic email uses.
SEND_REMINDER_EMAILS = False


def _reminder_emails_enabled():
    override = os.environ.get("REMINDER_EMAILS")
    if override == "1":
        return True
    if override == "0":
        return False
    return SEND_REMINDER_EMAILS


def configure(app, on_visit=None, on_nudge=None):
    """on_visit(visit_row) emails the patient a visit reminder (optional, gated
    by SEND_REMINDER_EMAILS). on_nudge(lead_row, prior) emails the study team a
    status check on a quiet application; prior is how many times we've already
    asked."""
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
        if _on_visit and _reminder_emails_enabled():
            try:
                _on_visit(v)
            except Exception:
                if _app is not None:
                    _app.logger.exception("visit reminder email failed")
        n += 1
    return n


def check_clinic_checkins():
    """Ask the study team for a status update on applications that have gone
    quiet mid-funnel, instead of messaging the applicant. "Quiet" already means
    neither the patient nor the study team has touched the thread for
    QUIET_DAYS (last_activity_at covers messages from either side), so this
    never fires while the team is actively in touch with the applicant, even
    off-thread activity they've logged by replying here at all resets it."""
    cutoff = dt.datetime.now() - dt.timedelta(days=QUIET_DAYS)
    n = 0
    for lead in db.list_active_stage_leads():
        last = _parse(db.last_activity_at(lead["id"], lead["created_at"]))
        if last is None or last > cutoff:
            continue                          # still recent -> leave alone
        nudged = _parse(lead["nudged_at"])
        if nudged is not None and nudged > cutoff:
            continue                          # already checked in recently
        prior = lead["nudge_count"] or 0
        if prior >= MAX_NUDGES:
            continue                          # stop; the team has heard enough
        db.set_nudged(lead["id"])
        if _on_nudge:
            try:
                _on_nudge(lead, prior)
            except Exception:
                if _app is not None:
                    _app.logger.exception("clinic check-in email failed")
        n += 1
    return n


def run_all():
    if _app is None:
        return {"visits": 0, "clinic_checkins": 0}
    with _app.app_context():
        return {"visits": check_visits(), "clinic_checkins": check_clinic_checkins()}


def _loop():
    time.sleep(45)
    while True:
        try:
            run_all()
        except Exception:
            if _app is not None:
                _app.logger.exception("reminder sweep failed")
        time.sleep(INTERVAL)
