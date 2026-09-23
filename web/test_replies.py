"""Replies to study-team letters: read, matched, acted on.

Run: python test_replies.py
"""
import base64
import hashlib
import hmac
import json
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
os.environ["SECRET_KEY"] = "replies-test"
os.environ["CLINIC_LOOKUP"] = "0"

import app as webapp  # noqa: E402
import db  # noqa: E402
import replies  # noqa: E402

# The real out-of-office that started this, verbatim.
OOO = """Hi,

I am on leave until 12 October 2026, and will attend to your email upon my return.

If the matter cannot wait until my return, please contact the Linear doctors at rps@linear.org.au.

Or, if the matter is medically urgent, you could also contact the on-call Linear doctor on 6382 5116.
"""


def _lead():
    token = db.create_lead({
        "applicant_token": "t-nathan", "nct": "NCT07526519",
        "title": "A Phase 1 Study in Healthy Volunteers", "name": "Nathan Cappa",
        "email": "nathan@x.test", "phone": "", "consent": 1, "source": "web",
        "location": "Perth, WA", "condition": "Healthy Volunteer",
        "site": "Linear Early Phase Ltd, Perth, Western Australia, Australia",
        "dob": "1995-02-03"})
    lead = db.get_lead_by_token(token)
    db.record_clinic_notify(lead["id"], [
        {"email": "pschrader@linear.org.au", "facility": "Study contact",
         "source": "central",
         "subject": "Nathan in Western Australia applied to your healthy volunteer study"}])
    return db.get_lead(lead["id"])


def test_reads_the_out_of_office():
    assert replies.is_auto_reply("Automatic reply: Nathan in Western Australia applied")
    assert replies.is_auto_reply("Re: hello", {"Auto-Submitted": "auto-replied"})
    assert not replies.is_auto_reply("Re: Nathan in Western Australia applied", {})
    assert replies.subject_core("Automatic reply: RE: Nathan applied") == "Nathan applied"
    assert replies.alternate_contacts(OOO, exclude=["pschrader@linear.org.au"]) == \
        ["rps@linear.org.au"]
    assert replies.leave_until(OOO) == "12 October 2026"
    assert "6382 5116" in replies.extract_phones(OOO)
    # Never a legal desk, never the sender, never us.
    assert replies.alternate_contacts(
        "contact legal@corp.test or hello@bridgemd.health or me@corp.test",
        exclude=["me@corp.test"]) == []
    print("PASS: the out-of-office is read for the alternate contact and the date")


def test_auto_reply_resends_the_application_and_tells_the_operator():
    sent = []

    def send(to, subject, body, reply_to=None):
        sent.append({"to": to, "subject": subject, "body": body, "reply_to": reply_to})
        return True

    with webapp.app.app_context():
        lead = _lead()
        msg = {"id": "rcv-1", "from": "Peter Schrader <pschrader@linear.org.au>",
               "to": ["harshil@reply.bridgemd.health"],
               "subject": "Automatic reply: Nathan in Western Australia applied to "
                          "your healthy volunteer study",
               "text": OOO, "headers": {"Auto-Submitted": "auto-replied"}}
        res = replies.handle_received(msg, send, "hello@bridgemd.health", reach=14)
        assert res["kind"] == "auto_reply" and res["lead_id"] == lead["id"], res
        assert res["forwarded_to"] == ["rps@linear.org.au"], res
        # The application went to the person the reply named, as the full letter.
        letter = [m for m in sent if m["to"] == "rps@linear.org.au"]
        assert len(letter) == 1
        assert letter[0]["subject"] == "Nathan in Western Australia applied to your healthy volunteer study"
        assert "nathan@x.test" in letter[0]["body"] and "1995-02-03" in letter[0]["body"]
        assert "away until 12 October 2026" in letter[0]["body"]
        assert "pschrader@linear.org.au" in letter[0]["body"]
        # The operator got a copy that says what happened, replying to the clinic.
        copy = [m for m in sent if m["to"] == "hello@bridgemd.health"]
        assert len(copy) == 1 and copy[0]["reply_to"] == "pschrader@linear.org.au"
        assert "re-sent to rps@linear.org.au" in copy[0]["body"]
        assert copy[0]["subject"].startswith("Auto-reply from Peter Schrader")
        # It is on the record: the lead's handoff list and timeline.
        again = db.get_lead(lead["id"])
        assert any(r["email"] == "rps@linear.org.au" and r["source"] == "auto_reply"
                   for r in db.lead_clinic_notify(again))
        events = [e["note"] for e in db.get_db().execute(
            "SELECT note FROM lead_events WHERE lead_id = ? ORDER BY id", (lead["id"],))]
        assert any("re-sent to rps@linear.org.au" in n for n in events), events
        # A redelivered webhook does nothing the second time.
        n = len(sent)
        assert replies.handle_received(msg, send, "hello@bridgemd.health").get("duplicate")
        assert len(sent) == n
    print("PASS: auto-reply re-sends the application to the named contact and tells the operator")


def test_human_reply_is_forwarded_and_logged_only():
    sent = []
    with webapp.app.app_context():
        lead = _lead()
        msg = {"id": "rcv-2", "from": "pschrader@linear.org.au",
               "subject": "RE: Nathan in Western Australia applied to your healthy volunteer study",
               "text": "Thanks, we will call Nathan tomorrow. Peter", "headers": {}}
        res = replies.handle_received(
            msg, lambda to, s, b, reply_to=None: sent.append((to, s, reply_to)) or True,
            "hello@bridgemd.health")
        assert res["kind"] == "reply" and res["forwarded_to"] == []
        assert sent == [("hello@bridgemd.health",
                         "pschrader@linear.org.au replied: Nathan in Western Australia applied to your healthy volunteer study",
                         "pschrader@linear.org.au")]
        events = [e["note"] for e in db.get_db().execute(
            "SELECT note FROM lead_events WHERE lead_id = ? ORDER BY id", (lead["id"],))]
        assert any("study team replied" in n for n in events), events
    print("PASS: a human reply is forwarded to the operator and logged on the lead")


def test_webhook_is_signature_checked():
    secret = "whsec_" + base64.b64encode(b"k" * 24).decode()
    body = json.dumps({"type": "email.received", "data": {"email_id": "x"}}).encode()
    ts = str(int(time.time()))
    sig = base64.b64encode(hmac.new(base64.b64decode(secret[6:]),
                                    f"m1.{ts}.".encode() + body,
                                    hashlib.sha256).digest()).decode()
    good = {"svix-id": "m1", "svix-timestamp": ts, "svix-signature": "v1," + sig}
    assert webapp._svix_ok(secret, good, body)
    assert not webapp._svix_ok(secret, dict(good, **{"svix-signature": "v1,nope"}), body)
    assert not webapp._svix_ok(secret, good, body + b" ")
    client = webapp.app.test_client()
    os.environ.pop("RESEND_WEBHOOK_SECRET", None)
    assert client.post("/hooks/resend", data=body, headers=good).status_code == 403
    os.environ["RESEND_WEBHOOK_SECRET"] = secret
    assert client.post("/hooks/resend", data=body,
                       headers=dict(good, **{"svix-signature": "v1,nope"})).status_code == 403
    r = client.post("/hooks/resend", data=json.dumps({"type": "email.sent"}).encode(),
                    headers=good)
    # Wrong signature for this body, so still refused: the check covers the body.
    assert r.status_code == 403
    print("PASS: the webhook refuses unsigned or tampered posts")


def main():
    with webapp.app.app_context():
        db.init_db()
    for t in (test_reads_the_out_of_office,
              test_auto_reply_resends_the_application_and_tells_the_operator,
              test_human_reply_is_forwarded_and_logged_only,
              test_webhook_is_signature_checked):
        t()
    print("All 4 reply tests passed")


if __name__ == "__main__":
    main()
