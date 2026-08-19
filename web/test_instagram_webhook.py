"""Regression checks for Meta webhook verification and signed deliveries.

Run: python test_instagram_webhook.py
"""
import hashlib
import hmac
import json
import os
import tempfile


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

import app as webapp  # noqa: E402
import db  # noqa: E402


def _signature(payload):
    return "sha256=" + hmac.new(
        b"meta-app-secret", payload, hashlib.sha256).hexdigest()


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
    assert accepted.get_json() == {"ok": True}

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
        assert rows[0]["status"] == "pending"
        assert json.loads(rows[0]["payload_json"])["object"] == "instagram"


def main():
    try:
        test_instagram_webhook()
        print("PASS: Instagram webhook verification, signing, and idempotency")
    finally:
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass


if __name__ == "__main__":
    main()
