"""Regression checks for Gmail OAuth and encrypted connection persistence.

Run: python test_gmail_oauth.py
"""
import base64
import json
import os
import tempfile
import urllib.parse
from email import message_from_bytes


_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ["NO_LOGIN"] = "0"
os.environ["SITE_DEMO"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["SECRET_KEY"] = "gmail-oauth-test-secret"
os.environ["GOOGLE_CLIENT_ID"] = "test-client.apps.googleusercontent.com"
os.environ["GOOGLE_CLIENT_SECRET"] = "test-client-secret"
os.environ["PUBLIC_BASE_URL"] = ""
os.environ["OAUTH_TOKEN_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(
    b"0123456789abcdef0123456789abcdef").decode("ascii")

import app as webapp  # noqa: E402
import db  # noqa: E402
import token_crypto  # noqa: E402


CSRF = "gmail-oauth-test-csrf"
GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"


def _client_for(user_id):
    client = webapp.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session[webapp.USER_SESSION_KEY] = user_id
        flask_session[webapp.CSRF_SESSION_KEY] = CSRF
    return client


def _oauth_state(response):
    location = response.headers["Location"]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    return location, query, query["state"][0]


def test_gmail_oauth_flow():
    with webapp.app.app_context():
        user_id = db.create_user(
            "owner.gmail@example.com", "disabled", "Gmail Owner", verified=True)
        teammate_id = db.create_user(
            "teammate.gmail@example.com", "disabled", "Gmail Teammate",
            verified=True)
        invite = db.create_org_invite(
            user_id, "teammate.gmail@example.com", "student")
        assert db.accept_org_invite(teammate_id, invite)
        outsider_id = db.create_user(
            "outsider.gmail@example.com", "disabled", "Gmail Outsider",
            verified=True)
    client = _client_for(user_id)

    page = client.get("/marketing-hub")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Connect Gmail" in html
    assert "Live connection" in html
    assert "Demo channels" in html

    started = client.get("/marketing-hub/connect/gmail")
    assert started.status_code == 302
    location, query, state = _oauth_state(started)
    assert location.startswith(webapp.GOOGLE_AUTH_URL)
    assert query["client_id"] == ["test-client.apps.googleusercontent.com"]
    assert query["redirect_uri"] == [
        "http://localhost/integrations/gmail/callback"]
    assert query["access_type"] == ["offline"]
    assert query["include_granted_scopes"] == ["true"]
    assert query["prompt"] == ["consent select_account"]
    assert {GMAIL_READ, GMAIL_SEND}.issubset(set(query["scope"][0].split()))

    originals = {
        "exchange": webapp._google_exchange_code,
        "userinfo": webapp._google_userinfo,
        "profile": webapp._gmail_profile,
        "refresh": webapp._google_refresh_access_token,
        "revoke": webapp._google_revoke_token,
        "sync": webapp._sync_gmail_connection,
    }
    revoked = []
    try:
        webapp._google_exchange_code = lambda code, endpoint: {
            "access_token": "access-secret-one",
            "refresh_token": "refresh-secret",
            "expires_in": 3600,
            "scope": f"openid email {GMAIL_READ} {GMAIL_SEND}",
        }
        webapp._google_userinfo = lambda token: {
            "sub": "google-account-123",
            "email": "connected@gmail.com",
            "email_verified": True,
        }
        webapp._gmail_profile = lambda token: {
            "emailAddress": "connected@gmail.com",
            "historyId": "987654321",
            "messagesTotal": 50,
            "threadsTotal": 20,
        }
        webapp._sync_gmail_connection = lambda *args, **kwargs: {
            "imported_messages": 0,
            "newest_thread_id": None,
        }

        callback = client.get(
            "/integrations/gmail/callback",
            query_string={"state": state, "code": "authorization-code"})
        assert callback.status_code == 302
        assert urllib.parse.urlparse(callback.headers["Location"]).path == \
            "/marketing-hub"

        with webapp.app.app_context():
            sources = db.list_marketing_sources(user_id)
            assert len(sources) == 1
            source = sources[0]
            assert source["identifier"] == "connected@gmail.com"
            assert source["connection_mode"] == "live"
            assert source["connection_status"] == "connected"
            assert "access_token_encrypted" not in source.keys()

            connection = db.get_marketing_connection(
                user_id, source["connection_id"])
            assert connection["provider"] == "gmail"
            assert connection["external_account_id"] == "google-account-123"
            assert connection["account_identifier"] == "connected@gmail.com"
            assert connection["gmail_history_id"] == "987654321"
            assert connection["gmail_watch_expires_at"] == ""
            assert connection["instagram_account_id"] == ""
            assert connection["last_successful_sync_at"] == ""
            assert connection["token_expires_at"].endswith("+00:00")
            assert connection["status"] == "connected"
            assert connection["last_error_code"] == ""
            assert connection["access_token_encrypted"] != "access-secret-one"
            assert connection["refresh_token_encrypted"] != "refresh-secret"
            assert token_crypto.decrypt_token(
                connection["access_token_encrypted"], webapp.app.secret_key
            ) == "access-secret-one"
            assert token_crypto.decrypt_token(
                connection["refresh_token_encrypted"], webapp.app.secret_key
            ) == "refresh-secret"
            assert {GMAIL_READ, GMAIL_SEND}.issubset(
                set(json.loads(connection["granted_scopes"])))
            source_id = source["id"]
            connection_id = connection["id"]
            assert db.get_marketing_connection(
                teammate_id, connection_id)["id"] == connection_id
            assert db.get_marketing_connection(outsider_id, connection_id) is None

        connected_page = client.get("/marketing-hub")
        connected_html = connected_page.get_data(as_text=True)
        assert connected_page.status_code == 200
        assert "Sync" in connected_html
        assert "Disconnect" in connected_html
        assert "Not synced yet" in connected_html
        assert "connected@gmail.com" in connected_html
        assert "access-secret-one" not in connected_html
        assert "refresh-secret" not in connected_html

        outsider = _client_for(outsider_id)
        blocked = outsider.post(
            f"/marketing-hub/connections/{connection_id}/disconnect",
            data={"_csrf_token": CSRF})
        assert blocked.status_code == 404

        # Google often omits refresh_token during a later authorization. The
        # reconnect must retain the existing encrypted offline credential.
        reconnect_start = client.get("/marketing-hub/connect/gmail")
        _, _, reconnect_state = _oauth_state(reconnect_start)
        webapp._google_exchange_code = lambda code, endpoint: {
            "access_token": "access-secret-two",
            "expires_in": 3600,
            "scope": f"openid email {GMAIL_READ} {GMAIL_SEND}",
        }
        reconnect = client.get(
            "/integrations/gmail/callback",
            query_string={"state": reconnect_state, "code": "new-code"})
        assert reconnect.status_code == 302
        with webapp.app.app_context():
            connection = db.get_marketing_connection(user_id, connection_id)
            assert token_crypto.decrypt_token(
                connection["access_token_encrypted"], webapp.app.secret_key
            ) == "access-secret-two"
            assert token_crypto.decrypt_token(
                connection["refresh_token_encrypted"], webapp.app.secret_key
            ) == "refresh-secret"
            db.get_db().execute(
                "UPDATE marketing_connections SET token_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", connection_id))
            db.get_db().commit()

        webapp._google_refresh_access_token = lambda token: {
            "access_token": "refreshed-access-secret",
            "expires_in": 7200,
            "scope": f"openid email {GMAIL_READ} {GMAIL_SEND}",
        }
        with webapp.app.app_context():
            usable = webapp._gmail_connection_access_token(user_id, source_id)
            assert usable == "refreshed-access-secret"
            connection = db.get_marketing_connection(user_id, connection_id)
            assert token_crypto.decrypt_token(
                connection["access_token_encrypted"], webapp.app.secret_key
            ) == "refreshed-access-secret"

            assert db.set_marketing_connection_error(
                user_id, connection_id, "temporary_provider_error",
                "The provider asked the account to reconnect.", "needs_reauth")
            try:
                webapp._gmail_connection_access_token(user_id, source_id)
            except RuntimeError:
                pass
            else:
                raise AssertionError("needs_reauth connection returned a token")
            assert db.mark_marketing_connection_synced(
                user_id, connection_id, gmail_history_id="987654999",
                gmail_watch_expires_at="2026-08-26T12:00:00+00:00")
            connection = db.get_marketing_connection(user_id, connection_id)
            source = db.get_marketing_source(user_id, source_id)
            assert connection["last_successful_sync_at"]
            assert connection["gmail_history_id"] == "987654999"
            assert connection["gmail_watch_expires_at"] == \
                "2026-08-26T12:00:00+00:00"
            assert connection["status"] == "connected"
            assert connection["last_error_code"] == ""
            assert source["status"] == "connected"

        webapp._google_revoke_token = lambda token: revoked.append(token) or True
        disconnected = client.post(
            f"/marketing-hub/connections/{connection_id}/disconnect",
            data={"_csrf_token": CSRF})
        assert disconnected.status_code == 302
        assert revoked == ["refresh-secret"]
        with webapp.app.app_context():
            connection = db.get_marketing_connection(user_id, connection_id)
            source = db.get_marketing_source(user_id, source_id)
            assert connection["status"] == "disconnected"
            assert connection["access_token_encrypted"] == ""
            assert connection["refresh_token_encrypted"] == ""
            assert source["status"] == "disconnected"
    finally:
        webapp._google_exchange_code = originals["exchange"]
        webapp._google_userinfo = originals["userinfo"]
        webapp._gmail_profile = originals["profile"]
        webapp._google_refresh_access_token = originals["refresh"]
        webapp._google_revoke_token = originals["revoke"]
        webapp._sync_gmail_connection = originals["sync"]

    print("PASS: Gmail OAuth, encrypted tokens, refresh, and disconnect")


def test_gmail_oauth_rejects_bad_state():
    with webapp.app.app_context():
        user_id = db.create_user(
            "state.gmail@example.com", "disabled", "State Test", verified=True)
    client = _client_for(user_id)
    assert client.get("/marketing-hub/connect/gmail").status_code == 302
    response = client.get(
        "/integrations/gmail/callback",
        query_string={"state": "attacker-state", "code": "unused"})
    assert response.status_code == 302
    with webapp.app.app_context():
        assert db.list_marketing_sources(user_id) == []
    print("PASS: Gmail OAuth state validation")


def test_logged_out_connect_creates_private_workspace():
    client = webapp.app.test_client()
    started = client.get("/marketing-hub/connect/gmail")
    assert started.status_code == 302
    location, _, state = _oauth_state(started)
    assert location.startswith(webapp.GOOGLE_AUTH_URL)

    originals = {
        "exchange": webapp._google_exchange_code,
        "userinfo": webapp._google_userinfo,
        "profile": webapp._gmail_profile,
        "sync": webapp._sync_gmail_connection,
    }
    try:
        webapp._google_exchange_code = lambda code, endpoint: {
            "access_token": "new-workspace-access",
            "refresh_token": "new-workspace-refresh",
            "expires_in": 3600,
            "scope": f"openid email profile {GMAIL_READ} {GMAIL_SEND}",
        }
        webapp._google_userinfo = lambda token: {
            "sub": "new-workspace-google-sub",
            "email": "new.workspace@gmail.com",
            "email_verified": True,
            "name": "New Workspace Owner",
            "picture": "https://example.test/avatar.png",
        }
        webapp._gmail_profile = lambda token: {
            "emailAddress": "new.workspace@gmail.com",
            "historyId": "workspace-history-1",
        }
        webapp._sync_gmail_connection = lambda *args, **kwargs: {
            "imported_messages": 0,
            "newest_thread_id": None,
        }
        callback = client.get(
            "/integrations/gmail/callback",
            query_string={"state": state, "code": "workspace-code"})
        assert callback.status_code == 302
        with client.session_transaction() as flask_session:
            user_id = flask_session.get(webapp.USER_SESSION_KEY)
        assert user_id
        with webapp.app.app_context():
            user = db.get_user(user_id)
            sources = db.list_marketing_sources(user_id)
            assert user["email"] == "new.workspace@gmail.com"
            assert user["oauth_sub"] == "new-workspace-google-sub"
            assert len(sources) == 1
            assert sources[0]["identifier"] == "new.workspace@gmail.com"
            assert sources[0]["connection_status"] == "connected"
    finally:
        webapp._google_exchange_code = originals["exchange"]
        webapp._google_userinfo = originals["userinfo"]
        webapp._gmail_profile = originals["profile"]
        webapp._sync_gmail_connection = originals["sync"]
    print("PASS: logged-out Gmail connect creates a private workspace")


def _gmail_message(message_id, body, *, html_body=False, internal_date,
                   unread=True):
    mime_type = "text/html" if html_body else "text/plain"
    encoded = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
    return {
        "id": message_id,
        "threadId": "gmail-thread-1",
        "internalDate": internal_date,
        "labelIds": ["INBOX"] + (["UNREAD"] if unread else []),
        "snippet": body,
        "payload": {
            "mimeType": mime_type,
            "headers": [
                {"name": "From", "value": "Jamie Sender <jamie@example.com>"},
                {"name": "To", "value": "connected.sync@gmail.com"},
                {"name": "Subject", "value": "Campaign question"},
                {"name": "Message-ID", "value": f"<{message_id}@example.test>"},
                {"name": "Content-Type", "value": f"{mime_type}; charset=utf-8"},
            ],
            "body": {"data": encoded},
        },
    }


def test_gmail_reply_context_preserves_blank_subject():
    inbound = _gmail_message(
        "blank-subject-inbound", "An email without a subject.",
        internal_date="1787140800000")
    inbound_headers = {
        header["name"]: header for header in inbound["payload"]["headers"]}
    inbound_headers["Subject"]["value"] = ""

    prior_outbound = _gmail_message(
        "bad-fallback-outbound", "A previous fallback reply.",
        internal_date="1787144400000", unread=False)
    outbound_headers = {
        header["name"]: header for header in prior_outbound["payload"]["headers"]}
    prior_outbound["labelIds"] = ["SENT"]
    outbound_headers["From"]["value"] = "connected.sync@gmail.com"
    outbound_headers["To"]["value"] = "jamie@example.com"
    outbound_headers["Subject"]["value"] = "New email"

    raw_thread = {
        "id": "blank-subject-thread",
        "messages": [inbound, prior_outbound],
    }
    context = webapp._gmail_reply_context(
        raw_thread, "connected.sync@gmail.com", "jamie@example.com")
    assert context["recipient"] == "jamie@example.com"
    assert context["subject_header_present"] is True
    assert context["subject"] == ""
    assert context["in_reply_to"] == \
        "<blank-subject-inbound@example.test>"
    assert context["references"] == \
        "<blank-subject-inbound@example.test>"
    parsed = webapp._gmail_parse_thread(
        {"id": "blank-subject-thread", "messages": [inbound]},
        "connected.sync@gmail.com")
    assert parsed["subject"] == "(no subject)"
    print("PASS: blank Gmail subjects remain blank in provider replies")


def test_gmail_full_and_incremental_sync():
    with webapp.app.app_context():
        user_id = db.create_user(
            "sync.owner@example.com", "disabled", "Sync Owner", verified=True)
        db.add_study_claim(
            user_id, "NCT01234567", "Scoped Gmail regression study")
        stored = db.connect_marketing_account(
            user_id, provider="gmail", channel="email",
            external_account_id="sync-google-account",
            account_identifier="connected.sync@gmail.com",
            access_token_encrypted=token_crypto.encrypt_token(
                "sync-access-token", webapp.app.secret_key),
            refresh_token_encrypted=token_crypto.encrypt_token(
                "sync-refresh-token", webapp.app.secret_key),
            granted_scopes=[GMAIL_READ, GMAIL_SEND],
            token_expires_at="2099-01-01T00:00:00+00:00",
            gmail_history_id="100", label="Live Gmail")
        source_id = stored["source_id"]
        connection_id = stored["connection_id"]

    first = _gmail_message(
        "gmail-message-1", "Hello, I have a question about your campaign.",
        internal_date="1787140800000")
    second = _gmail_message(
        "gmail-message-2",
        "<p>Following up on this request.</p>"
        "<div class=\"gmail_quote\">Quoted old content</div>",
        html_body=True, internal_date="1787144400000")
    third = _gmail_message(
        "gmail-message-3", "One more detail for the team.",
        internal_date="1787148000000")
    state = {"phase": "full"}
    original_api_get = webapp._gmail_api_get
    original_api_post = webapp._gmail_api_post
    sent_payloads = []

    def fake_api_get(token, resource, params=None):
        assert token == "sync-access-token"
        phase = state["phase"]
        if resource == "threads":
            return {"threads": [{"id": "gmail-thread-1"}]}
        if resource == "history":
            if phase == "stale":
                raise webapp._GmailAPIError(404, "not_found", "History expired")
            message_id = "gmail-message-3" if phase == "route" \
                else "gmail-message-2"
            history_id = "202" if phase == "route" else "201"
            return {
                "history": [{"messagesAdded": [{"message": {
                    "id": message_id, "threadId": "gmail-thread-1"}}]}],
                "historyId": history_id,
            }
        if resource == "threads/gmail-thread-1":
            messages = [first]
            if phase in ("incremental", "stale", "route"):
                messages.append(second)
            if phase == "route":
                messages.append(third)
            return {"id": "gmail-thread-1", "messages": messages}
        if resource == "profile":
            return {"emailAddress": "connected.sync@gmail.com",
                    "historyId": "202" if phase == "stale" else "200"}
        raise AssertionError(f"Unexpected Gmail resource: {resource}")

    def fake_api_post(token, resource, payload):
        assert token == "sync-access-token"
        assert resource == "messages/send"
        sent_payloads.append(payload)
        return {
            "id": "gmail-sent-message-1",
            "threadId": "gmail-thread-1",
            "labelIds": ["SENT"],
        }

    try:
        webapp._gmail_api_get = fake_api_get
        webapp._gmail_api_post = fake_api_post
        with webapp.app.app_context():
            initial = webapp._sync_gmail_connection(
                user_id, connection_id, force_full=True)
            assert initial["mode"] == "full"
            assert initial["imported_messages"] == 1
            assert initial["imported_threads"] == 1
            thread_id = initial["newest_thread_id"]
            thread = db.get_marketing_thread(user_id, thread_id)
            messages = db.list_marketing_messages(user_id, thread_id)
            connection = db.get_marketing_connection(user_id, connection_id)
            assert thread["external_ref"] == "gmail-thread-1"
            assert thread["contact_handle"] == "jamie@example.com"
            assert thread["unread"] == 1
            assert len(messages) == 1
            assert messages[0]["external_ref"] == "gmail-message-1"
            assert messages[0]["body"].startswith("Hello, I have a question")
            assert connection["gmail_history_id"] == "200"
            assert connection["last_successful_sync_at"]

            db.get_db().execute(
                "UPDATE marketing_threads SET subject = ? WHERE id = ?",
                ("Stale local subject", thread_id))
            db.get_db().commit()
            duplicate = webapp._sync_gmail_connection(
                user_id, connection_id, force_full=True)
            assert duplicate["imported_messages"] == 0
            assert len(db.list_marketing_messages(user_id, thread_id)) == 1
            assert db.get_marketing_thread(
                user_id, thread_id)["subject"] == "Campaign question"

            state["phase"] = "incremental"
            incremental = webapp._sync_gmail_connection(user_id, connection_id)
            assert incremental["mode"] == "incremental"
            assert incremental["imported_messages"] == 1
            messages = db.list_marketing_messages(user_id, thread_id)
            assert len(messages) == 2
            assert messages[1]["body"] == "Following up on this request."
            assert "Quoted old content" not in messages[1]["body"]
            assert db.get_marketing_connection(
                user_id, connection_id)["gmail_history_id"] == "201"

            state["phase"] = "stale"
            stale = webapp._sync_gmail_connection(user_id, connection_id)
            assert stale["mode"] == "full"
            assert stale["imported_messages"] == 0
            assert db.get_marketing_connection(
                user_id, connection_id)["gmail_history_id"] == "202"

        state["phase"] = "route"
        client = _client_for(user_id)
        synced = client.post(
            f"/marketing-hub/connections/{connection_id}/sync",
            headers={"X-CSRF-Token": CSRF, "Accept": "application/json",
                     "X-Requested-With": "XMLHttpRequest"})
        assert synced.status_code == 200
        payload = synced.get_json()
        assert payload["ok"] is True
        assert payload["imported_messages"] == 1
        assert payload["newest_thread_id"] == thread_id
        page = client.get("/marketing-hub")
        page_html = page.get_data(as_text=True)
        assert "data-gmail-sync" in page_html
        assert "connected.sync@gmail.com" in page_html
        assert "sync-access-token" not in page_html

        with client.session_transaction() as flask_session:
            flask_session["active_nct"] = "NCT01234567"
        scoped_page = client.get("/marketing-hub")
        scoped_html = scoped_page.get_data(as_text=True)
        assert scoped_page.status_code == 200
        assert "Send with Gmail" in scoped_html
        assert "Campaign question" in scoped_html

        delivered = client.post(
            f"/marketing-hub/threads/{thread_id}/reply",
            data={
                "_csrf_token": CSRF,
                "body": "Thanks, Jamie. The team will follow up today.",
            }, follow_redirects=False)
        assert delivered.status_code == 302
        assert len(sent_payloads) == 1
        send_payload = sent_payloads[0]
        assert send_payload["threadId"] == "gmail-thread-1"
        encoded = send_payload["raw"]
        raw_message = base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4))
        mime_message = message_from_bytes(raw_message)
        assert mime_message["To"] == "jamie@example.com"
        assert mime_message["From"] == "connected.sync@gmail.com"
        assert mime_message["Subject"] == "Campaign question"
        assert mime_message["In-Reply-To"] == \
            "<gmail-message-3@example.test>"
        assert "<gmail-message-3@example.test>" in \
            mime_message["References"]
        assert mime_message.get_payload(decode=True).decode("utf-8").strip() == \
            "Thanks, Jamie. The team will follow up today."

        with webapp.app.app_context():
            messages = db.list_marketing_messages(user_id, thread_id)
            assert messages[-1]["kind"] == "outbound"
            assert messages[-1]["delivery_status"] == "sent"
            assert messages[-1]["external_ref"] == "gmail-sent-message-1"
            message_count = len(messages)

        delivered_page = client.get(f"/marketing-hub?thread={thread_id}")
        delivered_html = delivered_page.get_data(as_text=True)
        assert "Sent via Gmail" in delivered_html
        assert "Send with Gmail" in delivered_html

        def reject_api_post(token, resource, payload):
            raise webapp._GmailAPIError(
                400, "invalid_argument", "The reply was rejected")

        webapp._gmail_api_post = reject_api_post
        rejected = client.post(
            f"/marketing-hub/threads/{thread_id}/reply",
            data={"_csrf_token": CSRF, "body": "Do not save this locally."},
            follow_redirects=False)
        assert rejected.status_code == 302
        with webapp.app.app_context():
            messages = db.list_marketing_messages(user_id, thread_id)
            assert len(messages) == message_count
            assert all(message["body"] != "Do not save this locally."
                       for message in messages)
    finally:
        webapp._gmail_api_get = original_api_get
        webapp._gmail_api_post = original_api_post
    print("PASS: Gmail sync, threaded delivery, rejection, and idempotency")


def main():
    try:
        test_gmail_oauth_flow()
        test_gmail_oauth_rejects_bad_state()
        test_logged_out_connect_creates_private_workspace()
        test_gmail_reply_context_preserves_blank_subject()
        test_gmail_full_and_incremental_sync()
        print("PASS: Gmail OAuth tests")
    finally:
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass


if __name__ == "__main__":
    main()
