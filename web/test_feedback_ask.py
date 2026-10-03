"""The founder's feedback ask: who gets it, what it says, what the page saves.

Run: python test_feedback_ask.py
"""
import datetime as dt
import os
import tempfile
import time

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ["NO_LOGIN"] = "0"
os.environ["SITE_DEMO"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["SECRET_KEY"] = "feedback-ask-test"

import app as webapp  # noqa: E402
import db  # noqa: E402
import mailer  # noqa: E402


def _live_lead(name, email, nct, condition, days_ago):
    """A browser apply from `days_ago` days back, with its apply event."""
    tok = db.create_lead({"nct": nct, "title": f"{condition} study",
                          "condition": condition, "name": name,
                          "email": email, "source": "web",
                          "applicant_token": f"tok-{email}"})
    ts = (dt.datetime.now() - dt.timedelta(days=days_ago)).strftime(
        "%Y-%m-%d %H:%M")
    con = db.get_db()
    con.execute("UPDATE leads SET created_at = ? WHERE token = ?", (ts, tok))
    con.execute(
        "INSERT INTO web_events (ts, visitor, name, path, source, medium, "
        "campaign, referrer, detail) VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, f"v-{email}", "apply", "/interest", "", "", "", "",
         f'{{"nct":"{nct}"}}'))
    con.commit()
    return db.get_lead_by_token(tok)


def _owner_client():
    uid = db.create_user(webapp.OWNER_EMAIL, "pw", "Owner")
    client = webapp.app.test_client()
    with client.session_transaction() as s:
        s[webapp.USER_SESSION_KEY] = uid
    return client


def test_everything():
    with webapp.app.app_context():
        db.init_db()
        _live_lead("RACHEL Yates", "rachel@gmail.com", "NCT06631287",
                   "Long COVID", 40)
        _live_lead("Rachel Yates", "Rachel@gmail.com", "NCT07000001",
                   "Asthma", 30)
        _live_lead("Willism. Lee", "sam@gmail.com", "NCT07000002",
                   "Type 2 diabetes", 12)
        _live_lead("New Person", "new@gmail.com", "NCT07000003",
                   "Migraine", 2)
        _live_lead("Owner", webapp.OWNER_EMAIL, "NCT07000004", "Migraine", 20)
        db.create_lead({"nct": "NCT00000009", "name": "Seed", "source": "web",
                        "email": "seed@example.com",
                        "applicant_token": "seeded-volume-1"})

        # One entry per person, recent and team addresses left out with a reason.
        send, skipped = webapp._feedback_audience()
        emails = [e["email"] for e in send]
        assert emails == ["sam@gmail.com", "rachel@gmail.com"], emails
        rachel = send[1]
        assert len(rachel["leads"]) == 2
        why = {e["email"]: e["why"] for e in skipped}
        assert why == {"new@gmail.com": "Applied less than 7 days ago",
                       webapp.OWNER_EMAIL: "A BridgeMD team address"}, why

        # The email: first name, both studies, five star links, no dashes.
        subject, text, html_body = mailer.build_feedback_request(
            rachel["leads"], "https://bridgemd.health/feedback/abc")
        assert subject == "How did your trial application go?"
        assert "Hi Rachel," in text
        assert "a long COVID study and an asthma study" in text, text
        for n in range(1, 6):
            assert f"/feedback/abc?stars={n}" in html_body
        assert "linkedin.com/in/harshils23" in html_body
        for s in (subject, text, html_body):
            assert "—" not in s and "–" not in s
        assert "max-width" not in html_body

        # Nothing goes out while sending is off.
        client = _owner_client()
        page = client.get("/internal/feedback").get_data(as_text=True)
        assert "2 people will get the feedback email" in page
        assert "rachel@gmail.com" in page and "Left out: 2" in page
        r = client.get("/internal/feedback/preview?email=sam@gmail.com")
        assert r.status_code == 200 and "Hi Willism," in r.get_data(as_text=True)
        assert db.list_feedback() == []

        # The owner corrects the typo'd name; preview and send both use it.
        with client.session_transaction() as s:
            csrf = s[webapp.CSRF_SESSION_KEY]
        client.post("/internal/feedback/greeting", data={
            "_csrf_token": csrf, "email": "sam@gmail.com", "first_name": "William"})
        assert 'value="William"' in client.get("/internal/feedback").get_data(as_text=True)
        r = client.get("/internal/feedback/preview?email=sam@gmail.com")
        assert "Hi William," in r.get_data(as_text=True)
        assert client.post("/internal/feedback/greeting", data={
            "_csrf_token": csrf, "email": "stranger@gmail.com",
            "first_name": "X"}).status_code == 404

        sent = []
        # Turn sending on for this flow only: the boot backfills key off the
        # same switch, so mark them done first or they mail real clinics.
        webapp._clinic_backfill_started = True
        webapp._connect_backfill_started = True
        webapp._founder_backfill_started = True
        webapp.notifications_ready = lambda: True
        webapp.mailer.send_email = (
            lambda to, subj, body, **kw: sent.append((to, subj, kw)) or (True, "ok"))

        # A stale count sends nothing.
        with client.session_transaction() as s:
            csrf = s[webapp.CSRF_SESSION_KEY]
        client.post("/internal/feedback/send",
                    data={"count": "5", "_csrf_token": csrf})
        time.sleep(0.2)
        assert sent == []

        # The test copy goes to the owner and records into the owner's row.
        client.post("/internal/feedback/test", data={"_csrf_token": csrf})
        assert [s[0] for s in sent] == [webapp.OWNER_EMAIL]
        assert sent[0][1].startswith("[Test] ")
        assert db.feedback_by_email("rachel@gmail.com") is None

        sent.clear()
        client.post("/internal/feedback/send",
                    data={"count": "2", "_csrf_token": csrf})
        for _ in range(50):
            if len(sent) == 2 and db.feedback_by_email("sam@gmail.com") and \
                    db.feedback_by_email("sam@gmail.com")["asked_at"]:
                break
            time.sleep(0.1)
        assert sorted(s[0] for s in sent) == ["rachel@gmail.com", "sam@gmail.com"]
        assert all(s[2].get("html_body") for s in sent)
        to_sam = next(s for s in sent if s[0] == "sam@gmail.com")
        assert "Hi William," in to_sam[2]["html_body"]
        send, skipped = webapp._feedback_audience()
        assert send == [], send
        assert any(e["why"].startswith("Already asked") for e in skipped)

        # The page: a GET never writes, the tapped star saves, the form saves.
        row = db.feedback_by_email("rachel@gmail.com")
        tok = row["token"]
        visitor = webapp.app.test_client()
        page = visitor.get(f"/feedback/{tok}?stars=4").get_data(as_text=True)
        assert "Thanks, Rachel. Your rating is saved." in page
        assert db.feedback_by_token(tok)["stars"] == 0
        with visitor.session_transaction() as s:
            vcsrf = s[webapp.CSRF_SESSION_KEY]
        r = visitor.post(f"/feedback/{tok}", data={"stars": "4"},
                         headers={"X-Requested-With": "XMLHttpRequest",
                                  "X-CSRF-Token": vcsrf})
        assert r.get_json() == {"ok": True}
        assert db.feedback_by_token(tok)["stars"] == 4
        r = visitor.post(f"/feedback/{tok}?stars=4", data={
            "_csrf_token": vcsrf, "stars": "5", "outcome": "enrolled",
            "comment": "Found my trial in a day.", "quote_ok": "1"})
        assert "Thank you, Rachel" in r.get_data(as_text=True)
        row = db.feedback_by_token(tok)
        assert (row["stars"], row["outcome"], row["quote_ok"]) == (5, "enrolled", 1)
        assert row["comment"] == "Found my trial in a day."

        # Preview and unknown links save nothing.
        assert visitor.get("/feedback/nope").status_code == 404
        visitor.post("/feedback/preview", data={"_csrf_token": vcsrf, "stars": "1"})
        page = client.get("/internal/feedback").get_data(as_text=True)
        assert "Found my trial in a day." in page
        assert ">1<" in page.split("Enrolled")[1][:200]


if __name__ == "__main__":
    test_everything()
    print("ok")
