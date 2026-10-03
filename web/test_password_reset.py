"""Forgot-password: a 6-digit emailed code plus a new password signs you in.

Run: python test_password_reset.py
"""
import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ["NO_LOGIN"] = "0"
os.environ["SITE_DEMO"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["SECRET_KEY"] = "password-reset-test"

import app as webapp  # noqa: E402
import db  # noqa: E402
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402


def test_reset():
    sent = []
    webapp.mailer.smtp_configured = lambda: True
    webapp.mailer.send_email = (
        lambda to, subj, body, **kw: sent.append((to, subj, body)) or (True, "ok"))
    with webapp.app.app_context():
        db.init_db()
        uid = db.create_user("owner@site.org", generate_password_hash("old-pass-1", method="pbkdf2:sha256"),
                             "Owner", verified=True)
    c = webapp.app.test_client()
    c.get("/login?next=/internal/feedback")
    with c.session_transaction() as s:
        csrf = s[webapp.CSRF_SESSION_KEY]
    assert "Forgot your password?" in c.get("/login").get_data(as_text=True)

    # An unknown address gets the same answer and no email.
    r = c.post("/login/forgot", data={"_csrf_token": csrf, "email": "nobody@x.org"})
    assert r.status_code == 302 and sent == []

    c.post("/login/forgot", data={"_csrf_token": csrf, "email": "Owner@site.org"})
    assert len(sent) == 1 and sent[0][0] == "owner@site.org"
    assert sent[0][1] == "Your BridgeMD password reset code"
    code = [w for w in sent[0][2].split() if w.isdigit() and len(w) == 6][0]

    def reset(code_, pw):
        return c.post("/login/reset", data={"_csrf_token": csrf, "code": code_,
                                            "password": pw})
    reset("000000" if code != "000000" else "111111", "new-pass-123")
    reset(code, "short")
    with c.session_transaction() as s:
        assert webapp.USER_SESSION_KEY not in s
    r = reset(code, "new-pass-123")
    assert r.status_code == 302 and r.headers["Location"].endswith("/internal/feedback")
    with c.session_transaction() as s:
        assert s[webapp.USER_SESSION_KEY] == uid
    with webapp.app.app_context():
        user = db.get_user(uid)
        assert check_password_hash(user["password_hash"], "new-pass-123")
        # The code is spent.
        assert not db.verify_user_code(uid, "reset", code, 0)


if __name__ == "__main__":
    test_reset()
    print("ok")
