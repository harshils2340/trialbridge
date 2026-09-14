"""Integrated E2E intake flow smoke test (patient -> site ATS).

Runs offline with a temp SQLite DB and lightweight stubs for email-code delivery.
Use this before onboarding real sites to verify the full loop:

patient signup -> verify -> onboarding -> apply
site claim study -> receive lead -> accept -> message -> schedule -> status -> reconcile
patient sees updates/messages/scheduling in My applications

Run:
  /Users/harshils/GraphMD/matcher/.venv/bin/python test_e2e_intake_flow.py
"""
from __future__ import annotations

import os
import secrets
import tempfile
import time


# Isolate test state before importing app/db modules.
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ.setdefault("SECRET_KEY", "e2e-test-secret")
os.environ.setdefault("NO_LOGIN", "0")
os.environ.setdefault("ALERTS_BACKGROUND", "0")
os.environ.setdefault("REMINDERS_BACKGROUND", "0")

from werkzeug.security import generate_password_hash  # noqa: E402

import app as webapp  # noqa: E402
import db  # noqa: E402


def _pass(name: str, detail: str = ""):
    print(f"PASS: {name}" + (f" ({detail})" if detail else ""))


def _fail(name: str, detail: str = ""):
    raise AssertionError(f"{name} failed" + (f": {detail}" if detail else ""))


def _stub_issue_patient_code():
    """Replace email sending with deterministic local 6-digit code."""

    def fake_issue(patient, purpose):
        code = "123456"
        exp = int(time.time()) + 600
        db.invalidate_patient_codes(patient["id"], purpose)
        db.create_patient_code(patient["id"], purpose, code, exp)
        return True, "stubbed"

    webapp._issue_patient_code = fake_issue


def _setup_site_account():
    """Create a site user and claim a test NCT for lead routing."""
    with webapp.app.app_context():
        existing = db.get_user_by_email("site@test.local")
        if existing:
            uid = existing["id"]
        else:
            uid = db.create_user(
                "site@test.local",
                generate_password_hash("site-pass-123", method="pbkdf2:sha256"),
                "Site Coordinator",
                "",
                "",
            )
        db.upsert_site_profile(
            uid, "Test Research Site", "Site Coordinator", "site@test.local", "+1 555 0100"
        )
        db.add_study_claim(uid, "NCTTEST001", "Test Study", verified=True)
        return uid


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


def main():
    _stub_issue_patient_code()
    site_user_id = _setup_site_account()

    patient = webapp.app.test_client()
    site = webapp.app.test_client()

    # 1) Anonymous discovery still works.
    r = patient.get("/")
    if r.status_code != 200:
        _fail("patient search page", f"status {r.status_code}")
    _pass("patient search page")

    # 2) Patient signup + verify + onboarding.
    r = _post(
        patient,
        "/account/signup",
        data={"full_name": "Mock Patient", "email": "patient@test.local",
              "password": "StrongPass123", "agree": "on"},
        follow_redirects=False,
    )
    if r.status_code not in (302, 303):
        _fail("patient signup", f"status {r.status_code}")
    _pass("patient signup", "redirected to verify")

    r = _post(patient, "/account/verify", data={"code": "123456"}, follow_redirects=False)
    if r.status_code not in (302, 303):
        _fail("patient verify", f"status {r.status_code}")
    _pass("patient verify")

    r = _post(
        patient,
        "/account/onboarding",
        data={"primary_interest": "obesity", "notify_email": "patient@test.local", "email_alerts": "on"},
        follow_redirects=False,
    )
    if r.status_code not in (302, 303):
        _fail("patient onboarding", f"status {r.status_code}")
    _pass("patient onboarding")

    # 3) Patient applies.
    apply_payload = {
        "nct": "NCTTEST001",
        "title": "Test Obesity Study",
        "condition": "Obesity",
        "location": "Toronto, ON",
        "site": "Test Research Site, Toronto, ON",
        "name": "Mock Patient",
        "email": "patient@test.local",
        "phone": "+1 416 555 0001",
        "age": "42",
        "sex": "female",
        "about": "Interested and available for screening",
        "consent": "on",
        "travel": "yes",
        "other_trial": "no",
        "pregnancy": "no",
        "consent_capable": "yes",
        "eligibility": '{"verdict":"possible","met":["Age within range"],"unknown":[],"not_met":[],"rationale":"Looks eligible"}',
    }
    r = _post(patient, "/interest", data=apply_payload, follow_redirects=False)
    if r.status_code != 200:
        _fail("patient apply", f"status {r.status_code}")
    _pass("patient apply")

    with webapp.app.app_context():
        lead = db.get_db().execute(
            "SELECT * FROM leads WHERE email = ? ORDER BY id DESC LIMIT 1",
            ("patient@test.local",),
        ).fetchone()
        if not lead:
            _fail("lead creation", "lead missing in DB")
        lead_id = lead["id"]
    _pass("lead creation", f"id {lead_id}")

    # 4) Site receives lead and works ATS actions.
    with site.session_transaction() as sess:
        sess["user_id"] = site_user_id

    r = site.get("/app/leads")
    if r.status_code != 200:
        _fail("site leads page", f"status {r.status_code}")
    if "Test Obesity Study" not in r.get_data(as_text=True):
        _fail("site lead visibility", "new lead not present in queue")
    _pass("site receives inbound lead")

    t0 = time.time()
    r = _post(site, f"/app/leads/{lead_id}/accept", data={}, follow_redirects=False)
    if r.status_code not in (302, 303):
        _fail("site accept", f"status {r.status_code}")
    _pass("site accept")

    r = _post(
        site,
        f"/app/leads/{lead_id}/message",
        data={"body": "Hi, we can offer a screening this week."},
        follow_redirects=False,
    )
    if r.status_code not in (302, 303):
        _fail("site message", f"status {r.status_code}")
    _pass("site message")

    r = _post(
        site,
        f"/app/leads/{lead_id}/schedule",
        data={"schedule_url": "https://calendly.com/test-site/screening"},
        follow_redirects=False,
    )
    if r.status_code not in (302, 303):
        _fail("site scheduling link", f"status {r.status_code}")
    _pass("site scheduling link")

    r = _post(
        site,
        f"/app/leads/{lead_id}/status",
        data={"status": "screening", "note": "Booked call pending"},
        follow_redirects=False,
    )
    if r.status_code not in (302, 303):
        _fail("site status update", f"status {r.status_code}")
    _pass("site status update")

    r = _post(
        site,
        f"/app/leads/{lead_id}/reconcile",
        data={
            "outcome": "enrolled_verified",
            "source_system": "REDCap",
            "source_ref": "test-rec-1001",
            "note": "Verified in pilot test",
        },
        follow_redirects=False,
    )
    if r.status_code not in (302, 303):
        _fail("site reconcile", f"status {r.status_code}")
    _pass("site reconcile")
    action_latency_ms = int((time.time() - t0) * 1000)

    # 5) Patient sees updates and can respond.
    r = patient.get("/applications")
    if r.status_code != 200:
        _fail("patient applications", f"status {r.status_code}")
    body = r.get_data(as_text=True)
    for needle in ("Test Obesity Study", "Book your call", "screening"):
        if needle.lower() not in body.lower():
            _fail("patient updates visible", f"missing '{needle}'")
    _pass("patient sees messaging/scheduling/status")

    # Patient replies back.
    with webapp.app.app_context():
        lead = db.get_db().execute(
            "SELECT * FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        token = lead["token"]
    r = _post(
        patient,
        f"/applications/{token}/message",
        data={"body": "Thanks, I booked for Thursday."},
        follow_redirects=False,
    )
    if r.status_code not in (302, 303):
        _fail("patient reply", f"status {r.status_code}")
    _pass("patient reply")

    print("")
    print("E2E intake flow PASSED")
    print(f"Site ATS action block runtime: {action_latency_ms} ms")
    print(f"Temp DB: {_TMP_DB}")


if __name__ == "__main__":
    main()
