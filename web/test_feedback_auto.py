"""Automatic feedback ask: 14 days after someone's newest application, once.

Run: python test_feedback_auto.py
"""
import datetime as dt
import os
import tempfile

os.environ["DB_PATH"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["NO_LOGIN"] = "0"
os.environ["SITE_DEMO"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["FEEDBACK_AUTO"] = "0"
os.environ["SECRET_KEY"] = "feedback-auto-test"

import app as webapp  # noqa: E402
import db  # noqa: E402
from test_feedback_ask import _live_lead  # noqa: E402


def test_auto_ask():
    sent = []

    def send(e):
        row = db.ensure_feedback(e["email"], name=e["name"], lead_id=e["leads"][-1]["id"])
        db.mark_feedback_asked(row["id"])
        sent.append(e["email"])
        return True, ""

    with webapp.app.app_context():
        db.init_db()
        _live_lead("Old Only", "old@gmail.com", "NCT07000011", "Asthma", 20)
        # First application 30 days ago, but a second one 3 days ago: wait.
        _live_lead("Two Apps", "two@gmail.com", "NCT07000012", "Obesity", 30)
        _live_lead("Two Apps", "two@gmail.com", "NCT07000013", "Obesity", 3)
        _live_lead("Too New", "new@gmail.com", "NCT07000014", "Migraine", 10)
        assert webapp.feedback_auto_run(send_fn=send, sleep=0) == 1
        assert sent == ["old@gmail.com"], sent
        # Running again sends nothing: each person is asked once.
        assert webapp.feedback_auto_run(send_fn=send, sleep=0) == 0
        # Two weeks after the newest application, the second person is due.
        con = db.get_db()
        old = (dt.datetime.now() - dt.timedelta(days=15)).strftime("%Y-%m-%d %H:%M")
        con.execute("UPDATE leads SET created_at = ? WHERE email = 'two@gmail.com'", (old,))
        con.execute("UPDATE web_events SET ts = ? WHERE visitor = 'v-two@gmail.com'", (old,))
        con.commit()
        assert webapp.feedback_auto_run(send_fn=send, sleep=0) == 1
        assert sent == ["old@gmail.com", "two@gmail.com"], sent
        # The manual page still uses its own 7-day rule from the first apply.
        due, _ = webapp._feedback_audience()
        assert "new@gmail.com" in [e["email"] for e in due]
    print("PASS: asked 14 days after the newest application, once per person")


if __name__ == "__main__":
    test_auto_ask()
    print("All feedback auto tests passed")
