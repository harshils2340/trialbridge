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
    assert "Inbox" in html
    assert 'aria-label="Manage connected accounts"' in html
    assert "Connect Gmail" in html
    assert 'id="studySwitch"' in html
    assert "mh-intake-nav" not in html

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

    rendered = client.get("/app/inbox").get_data(as_text=True)
    assert "is-row-channel" in rendered
    assert 'aria-label="Email"' in rendered

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

        db.add_study_claim(
            owner_id, "NCT00000001", "Inbox Study", verified=True)
        existing_token = db.create_lead({
            "applicant_token": "existing-person",
            "nct": "NCT00000001",
            "title": "Inbox Study",
            "name": "Morgan Lee",
            "email": "morgan@example.com",
            "consent": 1,
            "owner_user_id": owner_id,
        })
        existing_lead_id = db.get_lead_by_token(existing_token)["id"]
        db.get_db().execute(
            "UPDATE marketing_threads SET nct = ?, study_label = ? WHERE id = ?",
            ("NCT00000001", "Inbox Study", thread_id))
        db.get_db().commit()

    no_csrf = client.post(
        f"/marketing-hub/threads/{thread_id}/applicant",
        data={"consent": "1"})
    assert no_csrf.status_code == 302
    with webapp.app.app_context():
        assert db.get_marketing_thread(owner_id, thread_id)["linked_lead_id"] is None

    denied_link = _post(
        client, f"/marketing-hub/threads/{thread_id}/applicant", {})
    assert denied_link.status_code == 302
    with webapp.app.app_context():
        assert db.get_marketing_thread(owner_id, thread_id)["linked_lead_id"] is None

    linked = _post(
        client, f"/marketing-hub/threads/{thread_id}/applicant",
        {"consent": "1", "age": "42"})
    assert linked.status_code == 302
    with webapp.app.app_context():
        linked_thread = db.get_marketing_thread(owner_id, thread_id)
        assert linked_thread["linked_lead_id"]
        linked_lead_id = linked_thread["linked_lead_id"]
        assert linked_lead_id != existing_lead_id
        assert db.get_lead(linked_lead_id)["consent"] == 1

    assert _post(
        client, f"/marketing-hub/threads/{thread_id}/stage",
        {"stage": "screening"}).status_code == 302
    with webapp.app.app_context():
        assert db.get_marketing_thread(
            owner_id, thread_id)["pipeline_stage"] == "screening"
        assert db.get_lead(linked_lead_id)["status"] == "screening"
        assert db.list_tasks(linked_lead_id)

    # Real/private workspaces cannot invoke the synthetic records provider.
    assert _post(
        client, f"/marketing-hub/threads/{thread_id}/records",
        {"authorization_confirmed": "1"}).status_code == 302
    with webapp.app.app_context():
        assert db.get_lead(linked_lead_id)["records_connected"] == 0

    outsider = _client_for(outsider_id)
    assert _post(
        outsider, f"/marketing-hub/threads/{thread_id}/stage",
        {"stage": "enrolled"}).status_code == 404
    assert _post(
        outsider, f"/marketing-hub/threads/{thread_id}/records",
        {"authorization_confirmed": "1"}).status_code == 404
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


def test_demo_records_and_checklist():
    with webapp.app.app_context():
        demo_id = db.get_user_by_email("dejosama@fieveclinical.com")["id"]
        linked = next(
            row for row in db.list_marketing_threads(demo_id, status="all")
            if row["linked_lead_id"])
        thread_id = linked["id"]
        lead_id = linked["linked_lead_id"]
    client = _client_for(demo_id)
    original_connect = webapp.records_mod.connect
    try:
        webapp.records_mod.connect = lambda *_args, **_kwargs: {
            "provider": "SMART Health IT (sandbox)",
            "age": 34,
            "sex": "female",
            "conditions": ["Migraine"],
            "meds": ["Topiramate"],
            "labs": ["Blood pressure 120/80"],
            "summary": "Synthetic record: migraine and topiramate.",
            "sync_status": "connected",
            "source_status": "sandbox_ready",
            "external_patient_id": "demo-patient",
            "external_query_id": "",
            "last_sync_at": db.now(),
            "last_sync_error": "",
            "completeness_score": 100,
        }
        pulled = _post(
            client, f"/marketing-hub/threads/{thread_id}/records",
            {"authorization_confirmed": "1"})
        assert pulled.status_code == 302
    finally:
        webapp.records_mod.connect = original_connect
    with webapp.app.app_context():
        lead = db.get_lead(lead_id)
        assert lead["records_connected"] == 1
        assert lead["records_authorized_at"]
        assert db.get_records_profile(lead["applicant_token"])["provider"] == \
            "SMART Health IT (sandbox)"

    assert _post(
        client, f"/marketing-hub/threads/{thread_id}/stage",
        {"stage": "screening"}).status_code == 302
    with webapp.app.app_context():
        task = db.list_tasks(lead_id)[0]
    assert _post(
        client,
        f"/marketing-hub/threads/{thread_id}/checklist/{task['id']}",
        {"done": "1"}).status_code == 302
    with webapp.app.app_context():
        assert next(
            row for row in db.list_tasks(lead_id)
            if row["id"] == task["id"])["status"] == "done"
    print("PASS: demo-only records and checklist workflow")


def main():
    try:
        test_marketing_hub_flow()
        test_demo_reseed_removes_connection_before_source()
        test_demo_records_and_checklist()
        print("PASS: marketing hub tests")
    finally:
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass


if __name__ == "__main__":
    main()
