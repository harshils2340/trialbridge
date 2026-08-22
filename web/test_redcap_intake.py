"""REDCap intake/screening integration test (offline, simulated mode).

Verifies the connected-REDCap intake flow end to end without hitting a real
REDCap server:

- per-site REDCap config resolution + the IRB/consent live-form gate
- record pre-fill mapping (name/email/condition/nct)
- site saves a REDCap connection; token is write-only (blank keeps the secret)
- patient reaches the simulated screening form and submits it
- lead moves to a screening-complete state
- the webhook receiver marks a matching lead complete

Run:
  /Users/harshils/GraphMD/matcher/.venv/bin/python test_redcap_intake.py
"""
from __future__ import annotations

import os
import secrets
import tempfile
import time


_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ.setdefault("SECRET_KEY", "redcap-test-secret")
os.environ.setdefault("NO_LOGIN", "0")
os.environ.setdefault("ALERTS_BACKGROUND", "0")
os.environ.setdefault("REMINDERS_BACKGROUND", "0")

from werkzeug.security import generate_password_hash  # noqa: E402

import app as webapp  # noqa: E402
import db  # noqa: E402
import redcap  # noqa: E402


def _pass(name: str, detail: str = ""):
    print(f"PASS: {name}" + (f" ({detail})" if detail else ""))


def _fail(name: str, detail: str = ""):
    raise AssertionError(f"{name} failed" + (f": {detail}" if detail else ""))


def _csrf(client):
    with client.session_transaction() as sess:
        tok = sess.get("_csrf_token")
        if tok:
            return tok
        tok = secrets.token_urlsafe(32)
        sess["_csrf_token"] = tok
        return tok


def _post(client, path, data=None, **kwargs):
    payload = dict(data or {})
    payload["_csrf_token"] = _csrf(client)
    return client.post(path, data=payload, **kwargs)


def _stub_issue_patient_code():
    def fake_issue(patient, purpose):
        db.invalidate_patient_codes(patient["id"], purpose)
        db.create_patient_code(patient["id"], purpose, "123456",
                               int(time.time()) + 600)
        return True, "stubbed"

    webapp._issue_patient_code = fake_issue


def test_config_and_prefill():
    """Pure redcap.py logic: config precedence, live gate, record pre-fill."""
    # Not connected -> nothing live.
    cfg = redcap.RedcapConfig()
    if cfg.connected or cfg.intake_live:
        _fail("empty config", "should be neither connected nor live")

    # Connected but not attested -> connected, NOT live (compliance gate).
    cfg = redcap.RedcapConfig("https://redcap.test/api/", "tok",
                              intake_instrument="patient_intake",
                              intake_enabled=False)
    if not cfg.connected:
        _fail("connected config", "should be connected")
    if cfg.intake_live:
        _fail("live gate", "must NOT be live without IRB/consent attestation")

    # Attested -> live.
    cfg.intake_enabled = True
    if not cfg.intake_live:
        _fail("live gate", "should be live once attested + instrument chosen")
    _pass("live-form compliance gate")

    lead = {
        "id": 42, "name": "Jane Q Patient", "email": "jane@test.local",
        "phone": "+1 555 0100", "age": "51", "sex": "female",
        "condition": "Obesity", "nct": "NCT12345678",
        "record_summary": "de-identified summary", "notes": "",
    }
    rec = redcap.build_record(lead, cfg.field_map)
    if rec.get("record_id") != "42" or rec.get("first_name") != "Jane":
        _fail("prefill", f"unexpected record {rec}")
    if rec.get("email") != "jane@test.local" or rec.get("nct") != "NCT12345678":
        _fail("prefill", "email/nct not mapped")
    _pass("record pre-fill mapping")


def test_db_helpers():
    with webapp.app.app_context():
        uid = db.create_user(
            "redcap-site@test.local",
            generate_password_hash("pw-123", method="pbkdf2:sha256"),
            "RC Site", "", "")
        db.add_study_claim(uid, "NCTRC0001", "RedCap Study")

        # update_site_redcap: token write-once, blank keeps it.
        db.update_site_redcap(uid, endpoint="https://redcap.test/api/",
                              api_token="secret-token",
                              intake_instrument="patient_intake",
                              intake_enabled=True)
        db.update_site_redcap(uid, intake_instrument="screening_eligibility",
                              api_token=None)  # None = keep existing token
        prof = db.get_site_profile(uid)
        cfg = redcap.config_from_profile(prof)
        if cfg.api_token != "secret-token":
            _fail("token preserve", "blank token should keep stored secret")
        if cfg.intake_instrument != "screening_eligibility":
            _fail("instrument update", "instrument not updated")
        if not cfg.intake_live:
            _fail("profile live", "attested profile should be live")
        _pass("update_site_redcap (token write-once + live gate)")

        # Resolve the site profile for a claimed NCT.
        got = db.get_site_profile_for_nct("NCTRC0001")
        if not got or got["user_id"] != uid:
            _fail("get_site_profile_for_nct", "did not resolve claiming site")
        _pass("get_site_profile_for_nct")

        # Lead survey tracking + webhook record lookup.
        token = db.create_lead({
            "applicant_token": "apt-xyz", "nct": "NCTRC0001",
            "title": "RedCap Study", "condition": "Obesity",
            "name": "Test Person", "email": "tp@test.local", "consent": 1,
        })
        lead = db.get_lead_by_token(token)
        db.set_lead_redcap(lead["id"], record_id="RC-777", survey_status="sent")
        found = db.find_lead_by_redcap_record("RC-777")
        if not found or found["id"] != lead["id"]:
            _fail("find_lead_by_redcap_record", "record id lookup failed")
        # Security: a bare primary-key value must NOT resolve a lead. Lookup
        # matches ONLY the stored REDCap handoff record id, so a caller cannot
        # advance an arbitrary patient by guessing sequential ids. (The old
        # primary-key fallback was removed on purpose - see the db docstring.)
        if db.find_lead_by_redcap_record(str(lead["id"])) is not None:
            _fail("find_lead_by_redcap_record",
                  "primary-key fallback must be rejected (enumeration guard)")
        _pass("lead survey tracking + webhook lookup (no id fallback)")
        return uid, token


def test_screening_flow():
    _stub_issue_patient_code()
    # Force simulated mode for this flow (turn off the live-form gate) so we
    # don't call a real REDCap server. The live path is covered by unit checks.
    with webapp.app.app_context():
        prof = db.get_site_profile_for_nct("NCTRC0001")
        if prof:
            db.update_site_redcap(prof["user_id"], intake_enabled=False)
    client = webapp.app.test_client()

    # Patient signup -> verify -> onboarding.
    _post(client, "/account/signup", data={
        "full_name": "Screen Patient", "email": "screen@test.local",
        "password": "StrongPass123", "agree": "on"})
    _post(client, "/account/verify", data={"code": "123456"})
    _post(client, "/account/onboarding", data={
        "primary_interest": "obesity", "notify_email": "screen@test.local",
        "email_alerts": "on"})

    # Apply to the claimed study.
    _post(client, "/interest", data={
        "nct": "NCTRC0001", "title": "RedCap Study", "condition": "Obesity",
        "location": "Toronto, ON", "name": "Screen Patient",
        "email": "screen@test.local", "phone": "+1 416 555 0101",
        "age": "48", "sex": "female", "consent": "on"})

    with webapp.app.app_context():
        lead = db.get_db().execute(
            "SELECT * FROM leads WHERE email = ? ORDER BY id DESC LIMIT 1",
            ("screen@test.local",)).fetchone()
        if not lead:
            _fail("apply", "no lead created")
        # Advance to eligible so the screening card shows.
        db.update_lead_status(lead["id"], "eligible", "accepted")
        lead_token = lead["token"]
        lead_id = lead["id"]

    # Screening card visible in My applications.
    r = client.get("/applications")
    if "Complete screening form" not in r.get_data(as_text=True):
        _fail("screening card", "card not shown at eligible status")
    _pass("screening card shown to patient")

    # Screening handoff renders the simulated form (REDCap not connected here).
    r = client.get(f"/applications/{lead_token}/screening")
    body = r.get_data(as_text=True)
    if r.status_code != 200 or "screening form" not in body.lower():
        _fail("screening form", f"status {r.status_code}")
    # Simulated mode (no live REDCap) renders the illustrative inline form - a
    # "Sample" badge plus real input fields - not the real REDCap iframe.
    if "screening-frame-sample" not in body or 'name="s_travel"' not in body:
        _fail("screening form", "simulated form not rendered")
    # Level 2: the simulated form is wrapped in the branded embed chrome.
    if "screening-frame" not in body or "powered by your study team's REDCap" not in body:
        _fail("screening form", "branded embed chrome not rendered")
    _pass("simulated screening form served (branded embed chrome)")

    # Submit the simulated form -> screening-complete state.
    r = _post(client, f"/applications/{lead_token}/screening/complete", data={
        "s_name": "Screen Patient", "s_email": "screen@test.local",
        "s_travel": "yes", "s_other_trial": "no", "s_consent": "1"},
        follow_redirects=False)
    if r.status_code not in (302, 303):
        _fail("screening submit", f"status {r.status_code}")
    with webapp.app.app_context():
        lead = db.get_lead(lead_id)
        if lead["redcap_survey_status"] != "complete":
            _fail("screening complete", "survey_status not marked complete")
        if lead["status"] != "screening":
            _fail("screening complete", f"status is {lead['status']}")
    _pass("simulated screening completion -> screening-complete state")

    return lead_id


def test_webhook():
    with webapp.app.app_context():
        token = db.create_lead({
            "applicant_token": "apt-wh", "nct": "NCTRC0001",
            "title": "RedCap Study", "name": "Webhook Person",
            "email": "wh@test.local", "consent": 1})
        lead = db.get_lead_by_token(token)
        db.set_lead_redcap(lead["id"], record_id="WH-9001", survey_status="sent")
        lead_id = lead["id"]

    client = webapp.app.test_client()
    original_secret = redcap.WEBHOOK_SECRET
    try:
        # Security: the endpoint FAILS CLOSED. With no shared secret configured it
        # must reject every call (403) so anonymous callers can't advance patients.
        redcap.WEBHOOK_SECRET = ""
        r = client.post("/integrations/redcap/webhook", data={"record": "WH-9001"})
        if r.status_code != 403:
            _fail("webhook", f"no-secret must fail closed, got {r.status_code}")

        # With a secret configured, a call WITHOUT the token is still rejected.
        redcap.WEBHOOK_SECRET = "test-webhook-secret"
        r = client.post("/integrations/redcap/webhook", data={"record": "WH-9001"})
        if r.status_code != 403:
            _fail("webhook", f"missing token must be rejected, got {r.status_code}")

        # A wrong token is rejected.
        r = client.post("/integrations/redcap/webhook",
                        data={"record": "WH-9001"},
                        headers={"X-Redcap-Token": "wrong"})
        if r.status_code != 403:
            _fail("webhook", f"wrong token must be rejected, got {r.status_code}")

        # The correct token is accepted and marks the matching lead complete.
        r = client.post("/integrations/redcap/webhook", data={
            "record": "WH-9001", "instrument": "patient_intake",
            "patient_intake_complete": "2"},
            headers={"X-Redcap-Token": "test-webhook-secret"})
        if r.status_code != 200 or r.get_data(as_text=True) != "ok":
            _fail("webhook", f"status {r.status_code} body {r.get_data(as_text=True)}")
        with webapp.app.app_context():
            lead = db.get_lead(lead_id)
            if lead["redcap_survey_status"] != "complete":
                _fail("webhook", "lead not marked complete")
        _pass("webhook fails closed without secret, marks lead complete with it")

        # Unknown record (authenticated) is ignored, not an error.
        r = client.post("/integrations/redcap/webhook",
                        data={"record": "does-not-exist"},
                        headers={"X-Redcap-Token": "test-webhook-secret"})
        if r.status_code != 200 or r.get_data(as_text=True) != "ignored":
            _fail("webhook", "unknown record should be ignored")
        _pass("webhook ignores unknown record")
    finally:
        redcap.WEBHOOK_SECRET = original_secret


def main():
    test_config_and_prefill()
    test_db_helpers()
    test_screening_flow()
    test_webhook()
    print("")
    print("REDCap intake integration test PASSED")
    print(f"Temp DB: {_TMP_DB}")


if __name__ == "__main__":
    main()
