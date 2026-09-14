"""Accept unlocks contact. It does not email a booking link.

Run: python test_accept_unlock.py
"""
import os
import tempfile

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP
os.environ["NO_LOGIN"] = "0"
os.environ["SITE_DEMO"] = "0"
os.environ["NOTIFY_LIVE"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["SECRET_KEY"] = "accept-unlock-test"
os.environ["OWNER_EMAIL"] = "owner@bridgemd.test"

import app as webapp  # noqa: E402
import db  # noqa: E402
import mailer  # noqa: E402

CSRF = "accept-unlock-csrf"
DEMO_CAL = "https://calendly.com/bridgemd-demo/screening"
_SEED_N = 0


def _client(user_id):
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s[webapp.USER_SESSION_KEY] = user_id
        s[webapp.CSRF_SESSION_KEY] = CSRF
    return c


def _post(client, path, data=None):
    return client.post(path, data={"_csrf_token": CSRF, **(data or {})},
                       follow_redirects=False)


def _get_or_create(email, name):
    row = db.get_user_by_email(email)
    return row["id"] if row else db.create_user(email, "pw", name)


def _seed():
    global _SEED_N
    _SEED_N += 1
    with webapp.app.app_context():
        db.init_db()
        site_id = _get_or_create(f"site{_SEED_N}@clinic.test", "Site Coord")
        owner_id = _get_or_create("owner@bridgemd.test", "Owner")
        nct = "NCT07219966"
        db.add_study_claim(site_id, nct, "Brenipatide AUD", verified=True)
        db.set_site_calendar_url(site_id, DEMO_CAL)
        db.set_claim_schedule_url(site_id, nct, DEMO_CAL)
        token = db.create_lead({
            "nct": nct, "title": "Brenipatide AUD", "name": "George Test",
            "email": "patient@test.local", "consent": True, "source": "web",
            "owner_user_id": site_id,
        })
        lead = db.get_lead_by_token(token)
        return site_id, owner_id, lead["id"]


def test_apply_confirmation_includes_clinic():
    lead = {"name": "George Cole", "nct": "NCT07219966",
            "title": "Brenipatide AUD"}
    clinics = [{"facility": "Northwind Clinical",
                "email": "info@northwindclinical.com"}]
    _subj, body = mailer.build_apply_confirmation(
        lead, "https://bridgemd.health/a/tok", clinic_contacts=clinics)
    assert "info@northwindclinical.com" in body
    assert "/a/tok" in body
    _subj2, body2 = mailer.build_clinic_connect_message(
        lead, "https://bridgemd.health/a/tok", clinic_contacts=clinics)
    assert "ignore" in body2.lower()
    assert "info@northwindclinical.com" in body2
    print("PASS: applicant emails include the clinic and thread link")


def test_placeholder_url_is_detected():
    assert webapp._is_placeholder_schedule_url(DEMO_CAL)
    assert not webapp._is_placeholder_schedule_url(
        "https://calendly.com/real-clinic/screening")
    print("PASS: demo Calendly is treated as a placeholder")


def test_accept_does_not_email_booking_link():
    site_id, _owner_id, lead_id = _seed()
    sent = {"schedule": 0, "accepted": 0}
    orig_sched = webapp._notify_applicant_schedule
    orig_status = webapp._notify_applicant_by_id

    def _sched(lead):
        sent["schedule"] += 1
        return orig_sched(lead)

    def _status(lid, kind):
        sent["accepted"] += 1 if kind == "accepted" else 0
        return orig_status(lid, kind)

    webapp._notify_applicant_schedule = _sched
    webapp._notify_applicant_by_id = _status
    try:
        r = _post(_client(site_id), f"/app/leads/{lead_id}/accept")
        assert r.status_code in (302, 303), r.status_code
    finally:
        webapp._notify_applicant_schedule = orig_sched
        webapp._notify_applicant_by_id = orig_status

    with webapp.app.app_context():
        lead = db.get_lead(lead_id)
    assert lead["revealed"], "accept should unlock contact"
    assert not (lead["schedule_url"] or "").strip(), lead["schedule_url"]
    assert sent["schedule"] == 0, sent
    assert sent["accepted"] == 1, sent
    print("PASS: accept unlocks contact and does not send a booking email")


def test_guest_can_message_site_from_secret_link():
    _site_id, _owner_id, lead_id = _seed()
    with webapp.app.app_context():
        lead = db.get_lead(lead_id)
        token = lead["token"]
        url = webapp._applicant_thread_url(lead)
    assert f"/a/{token}" in url, url
    client = webapp.app.test_client()
    with client.session_transaction() as s:
        s[webapp.CSRF_SESSION_KEY] = CSRF
    r = client.get(f"/a/{token}")
    assert r.status_code == 200, r.status_code
    html = r.get_data(as_text=True)
    assert "Messages with the study team" in html
    assert "Write a message" in html
    r = _post(client, f"/a/{token}/message",
              data={"body": "Can someone from the clinic call me?"})
    assert r.status_code in (302, 303), r.status_code
    with webapp.app.app_context():
        msgs = db.get_messages(lead_id)
    assert any("clinic call me" in (m["body"] or "") for m in msgs), msgs
    print("PASS: guest applicant can message the site from /a/<token>")


def test_owner_can_message_before_accept():
    _site_id, owner_id, lead_id = _seed()
    sent = []
    orig = webapp._deliver_reply

    def _deliver(lead, body, connector=None):
        sent.append(body)
        return True

    webapp._deliver_reply = _deliver
    try:
        r = _post(_client(owner_id), f"/app/leads/{lead_id}/message",
                  data={"body": "Hi George, this is BridgeMD following up."})
        assert r.status_code in (302, 303), r.status_code
    finally:
        webapp._deliver_reply = orig

    with webapp.app.app_context():
        msgs = db.get_messages(lead_id)
    assert any("following up" in (m["body"] or "") for m in msgs), msgs
    assert sent and "following up" in sent[0]
    print("PASS: owner can message an applicant without accepting first")


if __name__ == "__main__":
    test_apply_confirmation_includes_clinic()
    test_placeholder_url_is_detected()
    test_accept_does_not_email_booking_link()
    test_guest_can_message_site_from_secret_link()
    test_owner_can_message_before_accept()
