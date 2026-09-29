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
import datetime as dt
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


# A trial counts as news only if ClinicalTrials.gov first posted it this
# recently. Older trials that drift into the result window (CT.gov returns a
# few hundred matches and we read the newest 100) are recorded as seen.
NEW_WITHIN_DAYS = max(1, int(os.environ.get("ALERTS_NEW_WITHIN_DAYS", "60")))


def _match_trials(alert):
    """Current recruiting, interventional trials matching this alert, newest
    first. Returns [(nct, title, first_posted)]."""
    geo = None
    if alert["lat"] is not None and alert["lon"] is not None and alert["radius"]:
        unit = alert["unit"] or "km"
        geo = f"distance({alert['lat']},{alert['lon']},{int(alert['radius'])}{unit})"
    try:
        trials = mt.fetch_trials(alert["condition"] or "", max_n=100, geo=geo,
                                 intervention=alert["intervention"] or "",
                                 sort="StudyFirstPostDate:desc")
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
            out.append((nct, t.get("title", ""), t.get("firstPosted", "")))
    return out


def _match_ncts(alert):
    """[(nct, title)] for callers that only need the id and title."""
    return [(n, t) for (n, t, _p) in _match_trials(alert)]


def _recent(first_posted, days=None):
    """True if the trial was first posted within the last `days` days."""
    days = days or NEW_WITHIN_DAYS
    try:
        posted = dt.date.fromisoformat((first_posted or "")[:10])
    except ValueError:
        return False
    return (dt.date.today() - posted).days <= days


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
    return db.add_alert_matches(alert_id, _match_trials(alert), is_new=False)


def check_alert(alert):
    """Record trials this alert has not seen. Only a recently posted trial is
    marked new; the rest are recorded as seen. Returns the new count. Sending
    happens per person in send_digests, not here."""
    matches = _match_trials(alert)
    seen = db.alert_seen_ncts(alert["id"])
    unseen = [m for m in matches if m[0] not in seen]
    new = [m for m in unseen if _recent(m[2])]
    old = [m for m in unseen if not _recent(m[2])]
    if new:
        db.add_alert_matches(alert["id"], new, is_new=True)
    if old:
        db.add_alert_matches(alert["id"], old, is_new=False)
    db.mark_alert_checked(alert["id"])
    return len(new)


def _last_notified(alerts):
    """The most recent alert email sent to this person, on any alert."""
    best = ""
    for a in alerts:
        v = _alert_val(a, "last_notified_at", "") or ""
        if v > best:
            best = v
    return best


def send_digests(alerts):
    """At most one alert email per person per NOTIFY_MIN_DAYS, however many
    alerts they saved. Each trial is emailed once. Returns emails sent."""
    if not _notifier:
        return 0
    people = {}
    for a in alerts:
        email = (_alert_val(a, "email", "") or "").strip().lower()
        if email:
            people.setdefault(email, []).append(a)
    sent = 0
    for email, mine in people.items():
        # The weekly gate is per person: the latest email on any of their
        # alerts counts, so a second alert never means a second email.
        latest = {"last_notified_at": _last_notified(mine),
                  "notify_min_days": max(
                      int(_alert_val(a, "notify_min_days", 0) or 0)
                      for a in mine) or NOTIFY_MIN_DAYS}
        if not db.alert_notify_due(latest, min_days=NOTIFY_MIN_DAYS):
            continue
        already = db.emailed_ncts_for(email)
        picks, labels = [], []
        for a in mine:
            rows = [r for r in db.unemailed_alert_matches(a["id"])
                    if r["nct"] not in already]
            for p in _curate_for_email(a, rows):
                if p["nct"] not in {x["nct"] for x in picks}:
                    picks.append(p)
                    label = _alert_val(a, "label") or _alert_val(a, "condition") or ""
                    if label and label not in labels:
                        labels.append(label)
        if not picks:
            continue
        picks.sort(key=lambda x: (-x["score"], x["nct"]))
        picks = picks[:EMAIL_MAX_MATCHES]
        head = dict(mine[0])
        head["label"] = " and ".join(labels[:2]) or head.get("label") or ""
        if len(mine) > 1:
            head["location"] = ""
        try:
            ok = _notifier(head, picks)
        except Exception:
            ok = False
            if _app is not None:
                _app.logger.exception("alert notify failed")
        if ok is False:
            continue
        db.mark_alert_matches_emailed([a["id"] for a in mine],
                                      [p["nct"] for p in picks])
        for a in mine:
            db.mark_alert_notified(a["id"])
        sent += 1
    return sent


def check_all():
    """Check every active alert once, then send at most one email per person.
    Safe to call from a request or a cron. One failing alert (transient CT.gov
    or DB-lock hiccup) must not abort the whole sweep."""
    if _app is None:
        return 0
    total = 0
    with _app.app_context():
        alerts = db.list_notifiable_alerts()
        if not alerts:
            # Nobody has an account with alerts enabled -> nothing to sweep.
            return 0
        for alert in alerts:
            try:
                total += check_alert(alert)
            except Exception:
                _app.logger.exception(
                    "alert check failed for id=%s", _alert_val(alert, "id"))
        try:
            send_digests(db.list_notifiable_alerts())
        except Exception:
            _app.logger.exception("alert digests failed")
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
