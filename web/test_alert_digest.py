"""Weekly alert email: once a week per person, each trial once, easy to stop.

Run: python test_alert_digest.py
"""
import datetime as dt
import os
import tempfile
import time

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP
os.environ["NO_LOGIN"] = "0"
os.environ["SITE_DEMO"] = "0"
os.environ["NOTIFY_LIVE"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["SECRET_KEY"] = "alerts-test"
os.environ["CLINIC_LOOKUP"] = "0"

import app as webapp  # noqa: E402
import alerts  # noqa: E402
import db  # noqa: E402
import mailer  # noqa: E402

TODAY = dt.date.today()
RECENT = (TODAY - dt.timedelta(days=5)).isoformat()
OLD = "2019-02-01"
TRIALS = {
    "weight loss": [("NCT07000001", "Weight loss study of a new weight loss medicine", RECENT),
                    ("NCT03824652", "Weight loss program for adults", OLD)],
    "obesity": [("NCT07000001", "Weight loss study of a new weight loss medicine", RECENT),
                ("NCT07000002", "Obesity study comparing two obesity medicines", RECENT)],
}
_feed = {}


def _fake_match(alert):
    return list(_feed.get((alert["condition"] or "").lower(), []))


def _person(email):
    pid = db.create_patient_user(email, "", "Becki")
    db.set_patient_onboarding(pid, "Weight loss", email, True)
    return db.get_patient_user(pid)


def _alert(p, condition):
    aid = db.create_alert({"applicant_token": p["applicant_token"], "label": condition,
                           "condition": condition, "email": p["email"]})
    alerts.seed_baseline(aid)
    return aid


def test_one_email_a_week_and_no_repeats():
    sent = []
    alerts._match_trials = _fake_match
    alerts._notifier = lambda a, picks: sent.append((a["email"], [p["nct"] for p in picks])) or True
    with webapp.app.app_context():
        p = _person("becki@x.test")
        _feed.clear()
        a1 = _alert(p, "Weight loss")          # baseline: nothing yet
        a2 = _alert(p, "Obesity")
        _feed.update(TRIALS)
    alerts.check_all()
    # Two alerts, one email. The 2019 trial is not news.
    assert len(sent) == 1, sent
    assert sent[0][0] == "becki@x.test"
    assert "NCT03824652" not in sent[0][1] and "NCT07000001" in sent[0][1], sent
    # Same week: nothing more, however often the sweep runs.
    alerts.check_all()
    alerts.check_all()
    assert len(sent) == 1, sent
    # A week later with nothing newly posted: no email at all.
    with webapp.app.app_context():
        week_ago = (dt.datetime.now() - dt.timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        d = db.get_db()
        d.execute("UPDATE alerts SET last_notified_at = ?", (week_ago,))
        d.commit()
    alerts.check_all()
    assert len(sent) == 1, sent
    # A newly posted trial the next week goes out once, alone.
    _feed["weight loss"].append(("NCT07000009", "Weight loss study number nine", TODAY.isoformat()))
    alerts.check_all()
    assert len(sent) == 2 and sent[1][1] == ["NCT07000009"], sent
    print("PASS: one email per person per week, each trial once, old trials never 'new'")


def test_email_has_one_click_unsubscribe():
    subject, body = mailer.build_alert_message(
        {"label": "Weight loss", "condition": "", "intervention": "", "location": ""},
        [{"nct": "NCT07000001", "title": "A study"}], "https://bridgemd.health/alerts",
        unsubscribe_url="https://bridgemd.health/alerts/unsubscribe?e=a&t=b")
    assert "https://bridgemd.health/alerts/unsubscribe?e=a&t=b" in body
    assert "—" not in body and "—" not in subject
    captured = {}
    webapp._NOTIFIER.live = True
    webapp._NOTIFIER._send_email_fn = lambda to, s, b, headers=None: (
        captured.update(to=to, headers=headers) or (True, "ok"))
    try:
        assert webapp._notify_alert({"email": "becki@x.test", "label": "Weight loss",
                                     "condition": "", "intervention": "", "location": ""},
                                    [{"nct": "NCT07000001", "title": "A study"}])
    finally:
        webapp._NOTIFIER.live = False
    h = captured["headers"]
    assert h["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert h["List-Unsubscribe"].startswith("<https://") and "/alerts/unsubscribe?" in h["List-Unsubscribe"]
    print("PASS: alert email carries a one-click unsubscribe header and link")


def test_unsubscribe_route():
    c = webapp.app.test_client()
    with webapp.app.app_context():
        p = _person("stop@x.test")
        _alert(p, "Obesity")
        tok = webapp._unsubscribe_token("stop@x.test")
    # A link scanner opening the link must not unsubscribe.
    r = c.get(f"/alerts/unsubscribe?e=stop@x.test&t={tok}")
    assert r.status_code == 200 and b"Unsubscribe" in r.data
    with webapp.app.app_context():
        assert any(a["email"] == "stop@x.test" for a in db.list_notifiable_alerts())
    assert c.post("/alerts/unsubscribe", data={"e": "stop@x.test", "t": "bad"}).status_code == 400
    # The mail provider's one-click POST.
    r = c.post(f"/alerts/unsubscribe?e=stop@x.test&t={tok}",
               data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200
    with webapp.app.app_context():
        assert not any(a["email"] == "stop@x.test" for a in db.list_notifiable_alerts())
    print("PASS: unsubscribe needs a valid token, GET only confirms, POST turns alerts off")


def test_resend_repeats_the_same_code():
    sent = []
    orig_send, orig_cfg = webapp.mailer.send_email, webapp.mailer.smtp_configured
    webapp.mailer.send_email = lambda to, s, b, **kw: sent.append(s) or (True, "ok")
    webapp.mailer.smtp_configured = lambda: True
    try:
        with webapp.app.app_context():
            p = _person("code@x.test")
            for _ in range(3):
                webapp._issue_patient_code(p, "apply")
            codes = {s.split()[0] for s in sent}
            assert len(sent) == 3 and len(codes) == 1, sent
            code = codes.pop()
            assert sent[0] == f"{code} is your BridgeMD verification code"
            assert not db.verify_patient_code(p["id"], "apply", "000000", int(time.time()))
            assert db.verify_patient_code(p["id"], "apply", code, int(time.time()))
            # Spent once used.
            assert not db.verify_patient_code(p["id"], "apply", code, int(time.time()))
    finally:
        webapp.mailer.send_email, webapp.mailer.smtp_configured = orig_send, orig_cfg
    print("PASS: every resend shows the same code, and it works once")


def main():
    with webapp.app.app_context():
        db.init_db()
    for t in (test_one_email_a_week_and_no_repeats, test_email_has_one_click_unsubscribe,
              test_unsubscribe_route, test_resend_repeats_the_same_code):
        t()
    print("All 4 alert digest tests passed")


if __name__ == "__main__":
    main()
