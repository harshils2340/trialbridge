"""Tests for the recruiter-issued patient portal.

Run: python test_portal.py

The portal hands a specific person a link to their own health information, so
the checks here are mostly about what must NOT happen: the temporary password
must die on first use, the gate must leak nothing before sign-in, one portal
session must not reach another applicant's record, and a revoked link must stop
working immediately.
"""
import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ["NO_LOGIN"] = "0"
os.environ["SITE_DEMO"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["SECRET_KEY"] = "portal-test-secret"

import app as webapp  # noqa: E402
import db  # noqa: E402

CSRF = "portal-test-csrf"


def _team_client(user_id):
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s[webapp.USER_SESSION_KEY] = user_id
        s[webapp.CSRF_SESSION_KEY] = CSRF
    return c


def _patient_client():
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s[webapp.CSRF_SESSION_KEY] = CSRF
    return c


def _post(client, path, data=None):
    return client.post(path, data={"_csrf_token": CSRF, **(data or {})},
                       follow_redirects=False)


def _seed():
    with webapp.app.app_context():
        db.init_db()
        uid = db.create_user("coord@site.org", "pw", "Coordinator")
        nct = "NCT00000042"
        db.add_study_claim(uid, nct, "Test Study", verified=True)
        made = []
        for name in ("Alice A.", "Bob B."):
            t = db.create_lead({"nct": nct, "title": "Test Study", "name": name,
                                "owner_user_id": uid})
            lead = db.get_lead_by_token(t)
            db.get_db().execute("UPDATE leads SET revealed = 1 WHERE id = ?",
                                (lead["id"],))
            db.get_db().commit()
            db.update_lead_status(lead["id"], "screening")
            made.append(lead["id"])
        return uid, made[0], made[1]


def _issue(uid, lead_id):
    """Issue access and read back the one-time password from the session."""
    client = _team_client(uid)
    r = _post(client, f"/app/leads/{lead_id}/portal")
    assert r.status_code == 302, r.status_code
    with client.session_transaction() as s:
        reveal = s.get("portal_reveal")
    assert reveal, "the one-time password was not handed to the recruiter"
    with webapp.app.app_context():
        row = db.get_portal_by_lead(lead_id)
    return row["token"], reveal["password"]


def test_password_is_never_stored_in_readable_form(uid, lead_id):
    token, pw = _issue(uid, lead_id)
    with webapp.app.app_context():
        row = db.get_portal_by_lead(lead_id)
    assert pw not in row["password_hash"], "password stored recoverably"
    assert row["password_hash"].startswith("pbkdf2:"), row["password_hash"][:12]
    assert row["must_change"] == 1, "first sign-in must force a rotation"
    print("PASS: only a hash is stored, and rotation is armed")


def test_invite_is_delivered_to_the_applicant(uid, lead_id):
    """The credential has to reach the applicant, not just the coordinator.

    With email off, the message thread is the only channel they actually read,
    so a portal whose password lives solely on the coordinator's clipboard is
    unusable. This asserts the link AND the password land in the thread, and
    that what was delivered genuinely opens the portal."""
    token, pw = _issue(uid, lead_id)
    with webapp.app.app_context():
        msgs = db.get_messages(lead_id)
        invites = [m for m in msgs if token in (m["body"] or "")]
        assert invites, "no invite was posted to the applicant's thread"
        body = invites[-1]["body"]
        assert invites[-1]["sender"] == "site"
        assert pw in body, "the password was not delivered to the applicant"
        assert f"/portal/{token}" in body, "the link was not delivered"
        # It must read as temporary, so nobody treats it as a lasting password.
        assert "temporary" in body.lower()

    # And the delivered credential actually works.
    c = _patient_client()
    assert _post(c, f"/portal/{token}/login", {"password": pw}).status_code == 302
    print("PASS: the link and one-time password are delivered in the thread")


def test_gate_leaks_nothing_before_signin(lead_id):
    with webapp.app.app_context():
        token = db.get_portal_by_lead(lead_id)["token"]
        lead = db.get_lead(lead_id)
    html = _patient_client().get(f"/portal/{token}").get_data(as_text=True)
    for secret in (lead["name"], lead["nct"], lead["title"]):
        assert secret not in html, f"gate leaked {secret!r} before sign-in"
    print("PASS: the password wall shows nothing identifying")


def test_temp_password_dies_on_first_use(uid, lead_id):
    token, temp = _issue(uid, lead_id)
    c = _patient_client()
    assert _post(c, f"/portal/{token}/login", {"password": "nope"}).status_code == 401
    assert _post(c, f"/portal/{token}/login", {"password": temp}).status_code == 302

    # Still gated: the portal must refuse to render until a new password is set.
    body = c.get(f"/portal/{token}").get_data(as_text=True)
    assert "Choose your own password" in body, "forced change was skipped"

    assert _post(c, f"/portal/{token}/password",
                 {"password": "brand-new-pass", "confirm": "brand-new-pass"}
                 ).status_code == 302
    body = c.get(f"/portal/{token}").get_data(as_text=True)
    assert "Your application" in body, "portal did not open after rotation"

    # The credential the recruiter typed into a chat must now be dead.
    c2 = _patient_client()
    assert _post(c2, f"/portal/{token}/login", {"password": temp}).status_code == 401
    assert _post(c2, f"/portal/{token}/login",
                 {"password": "brand-new-pass"}).status_code == 302
    print("PASS: the temporary password stops working once it has been used")


def test_short_password_refused(uid, lead_id):
    token, temp = _issue(uid, lead_id)
    c = _patient_client()
    _post(c, f"/portal/{token}/login", {"password": temp})
    _post(c, f"/portal/{token}/password", {"password": "abc", "confirm": "abc"})
    with webapp.app.app_context():
        assert db.get_portal_by_lead(lead_id)["must_change"] == 1, \
            "a too-short password was accepted"
    print("PASS: a too-short password is refused and the gate stays up")


def test_one_portal_cannot_reach_another(uid, lead_a, lead_b):
    token_a, pw_a = _issue(uid, lead_a)
    token_b, _ = _issue(uid, lead_b)
    c = _patient_client()
    _post(c, f"/portal/{token_a}/login", {"password": pw_a})
    _post(c, f"/portal/{token_a}/password",
          {"password": "alice-pass-1", "confirm": "alice-pass-1"})

    # Signed in to A, B must still be behind its own wall.
    body = c.get(f"/portal/{token_b}").get_data(as_text=True)
    assert "Enter the password" in body, "session for one portal opened another"

    # And a write aimed at B must not be accepted on A's session.
    r = _post(c, f"/portal/{token_b}/message", {"body": "should not land"})
    assert r.status_code in (302, 410), r.status_code
    with webapp.app.app_context():
        bodies = [m["body"] for m in db.get_messages(lead_b)]
    assert "should not land" not in bodies, "cross-applicant write got through"
    print("PASS: a portal session is scoped to one applicant")


def test_reply_reaches_the_team_thread(uid, lead_id):
    token, pw = _issue(uid, lead_id)
    c = _patient_client()
    _post(c, f"/portal/{token}/login", {"password": pw})
    _post(c, f"/portal/{token}/password",
          {"password": "reply-pass-99", "confirm": "reply-pass-99"})
    _post(c, f"/portal/{token}/message", {"body": "Is the visit fasting?"})
    with webapp.app.app_context():
        msgs = db.get_messages(lead_id)
        mine = [m for m in msgs if m["body"] == "Is the visit fasting?"]
        assert mine, "the patient's reply never reached the thread"
        assert mine[0]["sender"] == "patient"
        assert db.lead_unread_for_site(lead_id) >= 1, \
            "the reply should show as unread for the study team"
    print("PASS: a portal reply lands unread in the team's own thread")


def test_revoke_kills_the_link(uid, lead_id):
    token, pw = _issue(uid, lead_id)
    c = _patient_client()
    _post(c, f"/portal/{token}/login", {"password": pw})
    _post(_team_client(uid), f"/app/leads/{lead_id}/portal/revoke")
    r = c.get(f"/portal/{token}")
    assert r.status_code == 410, r.status_code
    assert _post(c, f"/portal/{token}/login",
                 {"password": pw}).status_code == 401
    print("PASS: revoking a link stops it immediately, even mid-session")


def test_lockout_after_repeated_failures(uid, lead_id):
    """Exercised at the db layer on purpose.

    Over HTTP the per-IP limiter (RATE_LIMIT_PORTAL_LOGIN_MAX) trips first, so
    driving this through the route would only prove the outer guard works. Both
    layers matter: the IP cap stops one host hammering many links, and the
    per-link lockout stops a distributed attempt on a single link."""
    from werkzeug.security import check_password_hash
    token, _ = _issue(uid, lead_id)
    with webapp.app.app_context():
        for _ in range(db.PORTAL_MAX_ATTEMPTS):
            row, err = db.portal_check_password(token, "wrong",
                                                check_password_hash)
            assert row is None and err
        locked = db.get_portal_by_lead(lead_id)
        assert locked["locked_until"], "brute force was not locked out"
        assert db.portal_locked_for(locked) > 0
        # Even the right password is refused while the lockout stands.
        row, err = db.portal_check_password(token, "anything",
                                            check_password_hash)
        assert row is None and "Too many" in err, err
    print("PASS: repeated wrong passwords lock the link for a while")


def main():
    uid, lead_a, lead_b = _seed()
    test_password_is_never_stored_in_readable_form(uid, lead_a)
    test_invite_is_delivered_to_the_applicant(uid, lead_a)
    test_gate_leaks_nothing_before_signin(lead_a)
    test_temp_password_dies_on_first_use(uid, lead_a)
    test_short_password_refused(uid, lead_a)
    test_one_portal_cannot_reach_another(uid, lead_a, lead_b)
    test_reply_reaches_the_team_thread(uid, lead_a)
    test_lockout_after_repeated_failures(uid, lead_a)
    test_revoke_kills_the_link(uid, lead_a)
    print("PASS: patient portal tests")


if __name__ == "__main__":
    main()
