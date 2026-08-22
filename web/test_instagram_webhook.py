"""Regression checks for Instagram OAuth, webhooks, and lifecycle callbacks.

Run: python test_instagram_webhook.py
"""
import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
import urllib.parse


_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ["NO_LOGIN"] = "0"
os.environ["SITE_DEMO"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["SECRET_KEY"] = "instagram-webhook-test-secret"
os.environ["INSTAGRAM_APP_ID"] = "meta-app-id"
os.environ["INSTAGRAM_APP_SECRET"] = "meta-app-secret"
os.environ["INSTAGRAM_WEBHOOK_VERIFY_TOKEN"] = "meta-verify-token"
os.environ["PUBLIC_BASE_URL"] = ""

import app as webapp  # noqa: E402
import db  # noqa: E402
import token_crypto  # noqa: E402


CSRF = "instagram-test-csrf"


def _signature(payload):
    return "sha256=" + hmac.new(
        b"meta-app-secret", payload, hashlib.sha256).hexdigest()


def _client_for(user_id):
    client = webapp.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session[webapp.USER_SESSION_KEY] = user_id
        flask_session[webapp.CSRF_SESSION_KEY] = CSRF
    return client


def _signed_request(user_id, issued_at):
    payload = json.dumps({
        "algorithm": "HMAC-SHA256",
        "issued_at": issued_at,
        "user_id": user_id,
    }, separators=(",", ":")).encode("utf-8")
    encoded_payload = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(
        b"meta-app-secret", encoded_payload, hashlib.sha256).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=")
    return (encoded_signature + b"." + encoded_payload).decode("ascii")


def test_instagram_webhook():
    client = webapp.app.test_client()

    verified = client.get(
        "/integrations/instagram/webhook",
        query_string={
            "hub.mode": "subscribe",
            "hub.verify_token": "meta-verify-token",
            "hub.challenge": "38490217",
        })
    assert verified.status_code == 200
    assert verified.get_data(as_text=True) == "38490217"
    assert verified.mimetype == "text/plain"

    rejected = client.get(
        "/integrations/instagram/webhook",
        query_string={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "do-not-return",
        })
    assert rejected.status_code == 403
    assert "do-not-return" not in rejected.get_data(as_text=True)

    payload = json.dumps({
        "object": "instagram",
        "entry": [{
            "id": "ig-professional-123",
            "time": 1776543210,
            "messaging": [{"sender": {"id": "ig-user-456"}}],
        }],
    }, separators=(",", ":")).encode("utf-8")

    unsigned = client.post(
        "/integrations/instagram/webhook", data=payload,
        content_type="application/json")
    assert unsigned.status_code == 403

    headers = {"X-Hub-Signature-256": _signature(payload)}
    accepted = client.post(
        "/integrations/instagram/webhook", data=payload,
        content_type="application/json", headers=headers)
    assert accepted.status_code == 200
    assert accepted.get_json() == {"ok": True, "imported_messages": 0}

    duplicate = client.post(
        "/integrations/instagram/webhook", data=payload,
        content_type="application/json", headers=headers)
    assert duplicate.status_code == 200

    with webapp.app.app_context():
        rows = db.get_db().execute(
            "SELECT * FROM marketing_webhook_events").fetchall()
        assert len(rows) == 1
        assert rows[0]["provider"] == "instagram"
        assert rows[0]["account_external_id"] == "ig-professional-123"
        assert rows[0]["status"] == "ignored"
        assert json.loads(rows[0]["payload_json"])["object"] == "instagram"


def test_instagram_oauth_and_lifecycle_callbacks():
    with webapp.app.app_context():
        user_id = db.create_user(
            "instagram.owner@example.com", "disabled", "Instagram Owner",
            verified=True)
    client = _client_for(user_id)
    assert client.get("/integrations/instagram/deauthorize").status_code == 200
    deletion_info = client.get("/integrations/instagram/data-deletion")
    assert deletion_info.status_code == 200
    assert "Instagram data deletion" in deletion_info.get_data(as_text=True)

    started = client.get("/marketing-hub/connect/instagram")
    assert started.status_code == 302
    location = started.headers["Location"]
    parsed = urllib.parse.urlparse(location)
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "www.instagram.com"
    assert parsed.path == "/oauth/authorize"
    assert query["client_id"] == ["meta-app-id"]
    assert query["redirect_uri"] == [
        "http://localhost/integrations/instagram/callback"]
    assert query["response_type"] == ["code"]
    assert query["enable_fb_login"] == ["0"]
    assert query["force_authentication"] == ["1"]
    assert set(query["scope"][0].split(",")) == set(
        webapp.INSTAGRAM_OAUTH_SCOPES)
    state = query["state"][0]

    originals = {
        "exchange": webapp._instagram_exchange_code,
        "long_lived": webapp._instagram_exchange_long_lived_token,
        "profile": webapp._instagram_profile,
        "subscribe": webapp._instagram_subscribe_webhooks,
        "unsubscribe": webapp._instagram_unsubscribe_webhooks,
        "send_reply": webapp._instagram_send_reply,
    }
    subscriptions = []
    unsubscriptions = []
    try:
        webapp._instagram_exchange_code = lambda code: {
            "access_token": "short-instagram-token",
            "user_id": "ig-professional-123",
            "permissions": list(webapp.INSTAGRAM_OAUTH_SCOPES),
        }
        webapp._instagram_exchange_long_lived_token = lambda token: {
            "access_token": "long-instagram-token",
            "token_type": "bearer",
            "expires_in": 5184000,
        }
        webapp._instagram_profile = lambda token, account_id: {
            "user_id": "ig-professional-123",
            "username": "bridgemd_trials",
            "name": "BridgeMD Trials",
            "account_type": "BUSINESS",
        }
        webapp._instagram_subscribe_webhooks = lambda token, account_id: (
            subscriptions.append((token, account_id)) or True)
        webapp._instagram_unsubscribe_webhooks = lambda token, account_id: (
            unsubscriptions.append((token, account_id)) or True)
        webapp._instagram_send_reply = lambda token, account_id, recipient, body: (
            "ig-outbound-1")

        callback = client.get(
            "/integrations/instagram/callback",
            query_string={"state": state, "code": "instagram-auth-code"})
        assert callback.status_code == 302
        assert urllib.parse.urlparse(callback.headers["Location"]).path == \
            "/app/inbox"
        assert subscriptions == [
            ("long-instagram-token", "ig-professional-123")]

        with webapp.app.app_context():
            sources = db.list_marketing_sources(user_id)
            live_sources = [row for row in sources
                            if row["connection_mode"] == "live"]
            assert len(live_sources) == 1
            source = live_sources[0]
            assert source["channel"] == "instagram"
            assert source["identifier"] == "@bridgemd_trials"
            assert source["provider"] == "instagram"
            assert source["instagram_account_id"] == "ig-professional-123"
            assert source["connection_status"] == "connected"
            connection = db.get_marketing_connection(
                user_id, source["connection_id"])
            assert connection["external_account_id"] == "ig-professional-123"
            assert token_crypto.decrypt_token(
                connection["access_token_encrypted"], webapp.app.secret_key
            ) == "long-instagram-token"
            assert set(json.loads(connection["granted_scopes"])) == set(
                webapp.INSTAGRAM_OAUTH_SCOPES)
            source_id = source["id"]
            connection_id = connection["id"]

        inbound_payload = json.dumps({
            "object": "instagram",
            "entry": [{
                "id": "ig-professional-123",
                "time": int(time.time()),
                "messaging": [{
                    "sender": {"id": "ig-patient-789"},
                    "recipient": {"id": "ig-professional-123"},
                    "timestamp": int(time.time() * 1000),
                    "message": {
                        "mid": "ig-inbound-1",
                        "text": "Can I learn more about the study?",
                    },
                }],
            }],
        }, separators=(",", ":")).encode("utf-8")
        ingested = client.post(
            "/integrations/instagram/webhook", data=inbound_payload,
            content_type="application/json",
            headers={"X-Hub-Signature-256": _signature(inbound_payload)})
        assert ingested.status_code == 200
        assert ingested.get_json()["imported_messages"] == 1
        with webapp.app.app_context():
            thread = db.list_marketing_threads(
                user_id, channel="instagram")[0]
            assert thread["external_ref"] == "ig-patient-789"
            thread_id = thread["id"]

        delivered = client.post(
            f"/marketing-hub/threads/{thread_id}/reply",
            data={"_csrf_token": CSRF, "body": "Thanks for reaching out."})
        assert delivered.status_code == 302
        with webapp.app.app_context():
            messages = db.list_marketing_messages(user_id, thread_id)
            assert messages[-1]["external_ref"] == "ig-outbound-1"
            assert messages[-1]["delivery_status"] == "sent"
            before_count = len(messages)
            db.get_db().execute(
                "UPDATE marketing_messages SET created_at = ? "
                "WHERE thread_id = ? AND kind = 'inbound'",
                ("2020-01-01 00:00", thread_id))
            db.get_db().commit()
        stale_reply = client.post(
            f"/marketing-hub/threads/{thread_id}/reply",
            data={"_csrf_token": CSRF, "body": "This must not be sent."})
        assert stale_reply.status_code == 302
        with webapp.app.app_context():
            assert len(db.list_marketing_messages(
                user_id, thread_id)) == before_count

        page = client.get("/app/inbox")
        html = page.get_data(as_text=True)
        assert page.status_code == 200
        assert "Connect Instagram" in html
        assert "@bridgemd_trials" in html
        assert "Workspace accounts" in html
        assert "Live" in html

        invalid_start = client.get("/marketing-hub/connect/instagram")
        invalid_query = urllib.parse.parse_qs(
            urllib.parse.urlparse(invalid_start.headers["Location"]).query)
        invalid_callback = client.get(
            "/integrations/instagram/callback",
            query_string={
                "state": invalid_query["state"][0] + "-wrong",
                "code": "must-not-be-used",
            })
        assert invalid_callback.status_code == 302

        disconnected_by_user = client.post(
            f"/marketing-hub/connections/{connection_id}/disconnect",
            data={"_csrf_token": CSRF})
        assert disconnected_by_user.status_code == 302
        assert unsubscriptions == [
            ("long-instagram-token", "ig-professional-123")]
        with webapp.app.app_context():
            disconnected = db.get_marketing_connection(user_id, connection_id)
            assert disconnected["status"] == "disconnected"
            assert disconnected["access_token_encrypted"] == ""
            reauthorized = db.connect_marketing_account(
                user_id, provider="instagram", channel="instagram",
                external_account_id="ig-professional-123",
                account_identifier="@bridgemd_trials",
                access_token_encrypted=token_crypto.encrypt_token(
                    "reauthorized-token", webapp.app.secret_key),
                granted_scopes=webapp.INSTAGRAM_OAUTH_SCOPES,
                token_expires_at=webapp._oauth_token_expiry(5184000),
                instagram_account_id="ig-professional-123",
                label="Instagram DMs")
            assert reauthorized["connection_id"] == connection_id

        deauthorize = client.post(
            "/integrations/instagram/deauthorize",
            data={"signed_request": _signed_request(
                "ig-professional-123", 1776543210)})
        assert deauthorize.status_code == 200
        assert deauthorize.get_json() == {"success": True}
        with webapp.app.app_context():
            disconnected = db.get_marketing_connection(user_id, connection_id)
            assert disconnected["status"] == "disconnected"
            assert disconnected["access_token_encrypted"] == ""
            assert db.get_marketing_source(user_id, source_id) is not None

            reconnected = db.connect_marketing_account(
                user_id, provider="instagram", channel="instagram",
                external_account_id="ig-professional-123",
                account_identifier="@bridgemd_trials",
                access_token_encrypted=token_crypto.encrypt_token(
                    "replacement-token", webapp.app.secret_key),
                granted_scopes=webapp.INSTAGRAM_OAUTH_SCOPES,
                token_expires_at=webapp._oauth_token_expiry(5184000),
                instagram_account_id="ig-professional-123",
                label="Instagram DMs")
            assert reconnected["source_id"] == source_id
            thread_id = db.create_marketing_thread(
                user_id, source_id, "Instagram Contact", "ig-contact-456",
                "Instagram DM", "Please tell me about the study.")
            assert thread_id

        deletion_request = _signed_request("ig-professional-123", 1776543220)
        deletion = client.post(
            "/integrations/instagram/data-deletion",
            data={"signed_request": deletion_request})
        assert deletion.status_code == 200
        deletion_payload = deletion.get_json()
        assert len(deletion_payload["confirmation_code"]) == 32
        assert deletion_payload["url"].endswith(
            "/integrations/instagram/data-deletion/status/"
            + deletion_payload["confirmation_code"])

        retry = client.post(
            "/integrations/instagram/data-deletion",
            data={"signed_request": deletion_request})
        assert retry.status_code == 200
        assert retry.get_json() == deletion_payload

        with webapp.app.app_context():
            conn = db.get_db()
            assert conn.execute(
                "SELECT COUNT(*) FROM marketing_connections WHERE provider = "
                "'instagram'").fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM marketing_sources WHERE id = ?",
                (source_id,)).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM marketing_threads WHERE id = ?",
                (thread_id,)).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM marketing_webhook_events WHERE "
                "account_external_id = 'ig-professional-123'").fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM marketing_data_deletions").fetchone()[0] == 1

        status = client.get(urllib.parse.urlparse(deletion_payload["url"]).path)
        assert status.status_code == 200
        status_html = status.get_data(as_text=True)
        assert "Instagram data deletion completed" in status_html
        assert deletion_payload["confirmation_code"] in status_html
        assert "no-store" in status.headers["Cache-Control"]

        invalid_deletion = client.post(
            "/integrations/instagram/data-deletion",
            data={"signed_request": "invalid.payload"})
        assert invalid_deletion.status_code == 403
    finally:
        webapp._instagram_exchange_code = originals["exchange"]
        webapp._instagram_exchange_long_lived_token = originals["long_lived"]
        webapp._instagram_profile = originals["profile"]
        webapp._instagram_subscribe_webhooks = originals["subscribe"]
        webapp._instagram_unsubscribe_webhooks = originals["unsubscribe"]
        webapp._instagram_send_reply = originals["send_reply"]


def main():
    try:
        test_instagram_webhook()
        test_instagram_oauth_and_lifecycle_callbacks()
        print("PASS: Instagram OAuth, webhooks, and lifecycle callbacks")
    finally:
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass


if __name__ == "__main__":
    main()
