#!/usr/bin/env python3
"""Regression tests for visit calendar invite flow.

Run:
  /Users/harshils/GraphMD/matcher/.venv/bin/python test_calendar_invites.py
"""
from __future__ import annotations

import os
import secrets
import sys
import tempfile


_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ.setdefault("SECRET_KEY", "calendar-test-secret")
os.environ.setdefault("NO_LOGIN", "0")
os.environ.setdefault("ALERTS_BACKGROUND", "0")
os.environ.setdefault("REMINDERS_BACKGROUND", "0")
os.environ.setdefault("GOOGLE_CALENDAR_SYNC", "0")

import app as webapp  # noqa: E402
import calendar_invites  # noqa: E402
import db  # noqa: E402


def _pass(name: str):
    print(f"PASS: {name}")


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


def _seed_accepted_lead():
    with webapp.app.app_context():
        token = db.create_lead({
            "applicant_token": "applicant-calendar-1",
            "nct": "NCTCAL001",
            "title": "Calendar Flow Study",
            "condition": "Obesity",
            "location": "Toronto, ON",
            "site": "Calendar Site",
            "name": "Calendar Patient",
            "email": "calendar.patient@test.local",
            "phone": "+1 416 555 0101",
            "age": "40",
            "sex": "female",
            "notes": "Available weekdays",
            "consent": True,
            "source": "web",
        })
        lead = db.get_lead_by_token(token)
        if not lead:
            _fail("seed accepted lead", "lead not created")
        if not db.accept_candidate(lead["id"], "ready for screening"):
            _fail("seed accepted lead", "accept failed")
        lead = db.get_lead(lead["id"])
        return lead


def test_ics_payload_generation():
    lead = {"id": 7, "nct": "NCTTEST7", "title": "Test Trial"}
    visit = {
        "id": 11,
        "visit_at": "2026-08-15 14:30",
        "kind": "screening",
        "location": "Virtual",
        "note": "Bring medication list",
    }
    ics = calendar_invites.build_visit_ics(
        lead, visit, invite_url="https://example.com/visit/test.ics")
    checks = (
        "BEGIN:VCALENDAR",
        "BEGIN:VEVENT",
        "SUMMARY:Screening visit - Test Trial",
        "DTSTART:20260815T143000",
        "LOCATION:Virtual",
        "URL:https://example.com/visit/test.ics",
    )
    for needle in checks:
        if needle not in ics:
            _fail("ics payload generation", f"missing '{needle}'")
    _pass("ics payload generation")


def test_visit_booking_and_ics_route():
    lead = _seed_accepted_lead()
    client = webapp.app.test_client()
    r = _post(
        client,
        f"/c/{lead['site_token']}/visit",
        data={
            "visit_at": "2026-08-20T10:30",
            "kind": "screening",
            "location": "Main clinic room 2",
            "note": "Please arrive 15 minutes early",
        },
        follow_redirects=False,
    )
    if r.status_code not in (302, 303):
        _fail("visit booking", f"status {r.status_code}")

    with webapp.app.app_context():
        visits = db.get_visits(lead["id"])
        if len(visits) != 1:
            _fail("visit booking", f"unexpected visit count {len(visits)}")
        visit = visits[0]
        msgs = db.get_messages(lead["id"])
        if not msgs:
            _fail("visit booking", "no system messages logged")
        expected_fragment = f"/visit/{lead['token']}/{visit['id']}.ics"
        if not any(expected_fragment in (m["body"] or "") for m in msgs):
            _fail("visit booking", "ics link missing from patient message")

    r = client.get(f"/visit/{lead['token']}/{visit['id']}.ics")
    if r.status_code != 200:
        _fail("ics route", f"status {r.status_code}")
    if "text/calendar" not in (r.headers.get("Content-Type") or ""):
        _fail("ics route", "content-type not calendar")
    body = r.get_data(as_text=True)
    if "BEGIN:VCALENDAR" not in body or "Calendar Flow Study" not in body:
        _fail("ics route", "ics payload missing expected body")

    bad = client.get(f"/visit/{lead['token']}/99999.ics")
    if bad.status_code != 404:
        _fail("ics route invalid visit", f"status {bad.status_code}")
    _pass("visit booking and ics route")


def main():
    tests = [test_ics_payload_generation, test_visit_booking_and_ics_route]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}")
    if os.path.exists(_TMP_DB):
        os.unlink(_TMP_DB)
    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed")


if __name__ == "__main__":
    main()
