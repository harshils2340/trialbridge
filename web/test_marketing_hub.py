"""Regression checks for the shared marketing inbox.

Run: python test_marketing_hub.py
"""
import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ["NO_LOGIN"] = "0"
os.environ["SITE_DEMO"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["SECRET_KEY"] = "marketing-hub-test-secret"

import app as webapp  # noqa: E402
import db  # noqa: E402


CSRF = "marketing-hub-test-csrf"


def _client_for(user_id):
    client = webapp.app.test_client()
    with client.session_transaction() as session:
        session[webapp.USER_SESSION_KEY] = user_id
        session[webapp.CSRF_SESSION_KEY] = CSRF
    return client


def _post(client, path, data):
    payload = {"_csrf_token": CSRF, **data}
    return client.post(path, data=payload, follow_redirects=False)


def _create_users():
    with webapp.app.app_context():
        owner_id = db.create_user(
            "owner@example.com", "disabled", "Avery Stone", verified=True)
        cover_id = db.create_user(
            "cover@example.com", "disabled", "Riley Chen", verified=True)
        invite = db.create_org_invite(owner_id, "cover@example.com", "student")
        assert db.accept_org_invite(cover_id, invite)
        outsider_id = db.create_user(
            "outsider@example.com", "disabled", "Outside User", verified=True)
    return owner_id, cover_id, outsider_id


def test_marketing_hub_flow():
    owner_id, cover_id, outsider_id = _create_users()
    client = _client_for(owner_id)

    page = client.get("/app/inbox")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Marketing inbox" in html
    assert "No accounts yet" in html

    added = _post(client, "/marketing-hub/sources", {
        "channel": "email",
        "label": "Main inbox",
        "identifier": "hello@example.com",
    })
    assert added.status_code == 302

    with webapp.app.app_context():
        sources = db.list_marketing_sources(owner_id)
        assert len(sources) == 1
        source_id = sources[0]["id"]

    created = _post(client, "/marketing-hub/threads", {
        "source_id": str(source_id),
        "contact_name": "Morgan Lee",
        "contact_handle": "morgan@example.com",
        "subject": "Campaign question",
        "body": "Can someone help me understand the next step?",
    })
    assert created.status_code == 302

    with webapp.app.app_context():
        threads = db.list_marketing_threads(owner_id)
        assert len(threads) == 1
        thread_id = threads[0]["id"]
        assert threads[0]["assigned_to"] == owner_id

    assert _post(client, f"/marketing-hub/threads/{thread_id}/reply", {
        "body": "Yes. I can help with that.",
    }).status_code == 302
    assert _post(client, f"/marketing-hub/threads/{thread_id}/note", {
        "body": "Follow up before Friday.",
    }).status_code == 302
    assert _post(client, f"/marketing-hub/threads/{thread_id}/assign", {
        "assignee_id": str(cover_id),
    }).status_code == 302

    with webapp.app.app_context():
        messages = db.list_marketing_messages(owner_id, thread_id)
        assert [message["kind"] for message in messages] == [
            "inbound", "outbound", "note"]
        assert messages[1]["delivery_status"] == "saved"
        assert db.get_marketing_thread(owner_id, thread_id)["assigned_to"] == cover_id

    coverage = _post(client, "/marketing-hub/coverage", {
        "primary_user_id": str(owner_id),
        "cover_user_id": str(cover_id),
        "vacation_mode": "1",
        "away_until": "2099-12-31",
        "note": "Handle new partnership messages first.",
    })
    assert coverage.status_code == 302

    with webapp.app.app_context():
        settings = db.get_marketing_handoff(owner_id)
        assert settings["vacation_mode"] == 1
        assert db.marketing_active_owner_id(settings) == cover_id

    routed = _post(client, "/marketing-hub/threads", {
        "source_id": str(source_id),
        "contact_name": "Taylor Brooks",
        "contact_handle": "taylor@example.com",
        "subject": "New inbound",
        "body": "Who is available today?",
    })
    assert routed.status_code == 302
    with webapp.app.app_context():
        newest = db.list_marketing_threads(owner_id)[0]
        assert newest["assigned_to"] == cover_id

    outsider = _client_for(outsider_id)
    blocked = _post(outsider, f"/marketing-hub/threads/{thread_id}/status", {
        "status": "resolved",
    })
    assert blocked.status_code == 404
    outsider_page = outsider.get(f"/app/inbox?thread={thread_id}")
    assert "Morgan Lee" not in outsider_page.get_data(as_text=True)

    print("PASS: persistent inbox, replies, notes, assignment, handoff, isolation")


def test_demo_reseed_removes_connection_before_source():
    with webapp.app.app_context():
        demo_id = db.create_user(
            "dejosama@fieveclinical.com", "disabled", "Demo Owner",
            verified=True)
        connected = db.connect_marketing_account(
            demo_id, provider="gmail", channel="email",
            external_account_id="discarded-demo-account",
            account_identifier="discarded@example.com",
            access_token_encrypted="encrypted-placeholder",
            granted_scopes=[], token_expires_at="2099-01-01T00:00:00+00:00")
        assert connected
        db.seed_demo_marketing_hub(demo_id)
        assert db.get_db().execute(
            "SELECT COUNT(*) FROM marketing_connections WHERE org_id = ?",
            (db.user_org_id(demo_id),)).fetchone()[0] == 0
        assert any(source["identifier"] == "recruit@fieveclinical.com"
                   for source in db.list_marketing_sources(demo_id))
    print("PASS: demo reseed preserves marketing connection FK ordering")


def main():
    try:
        test_marketing_hub_flow()
        test_demo_reseed_removes_connection_before_source()
        print("PASS: marketing hub tests")
    finally:
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass


if __name__ == "__main__":
    main()
