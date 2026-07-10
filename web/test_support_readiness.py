#!/usr/bin/env python3
"""Regression checks for payer/travel readiness workflow.

Run:
  /Users/harshils/GraphMD/matcher/.venv/bin/python test_support_readiness.py
"""
from __future__ import annotations

import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ.setdefault("SECRET_KEY", "support-test-secret")
os.environ.setdefault("NO_LOGIN", "0")
os.environ.setdefault("ALERTS_BACKGROUND", "0")
os.environ.setdefault("REMINDERS_BACKGROUND", "0")

import app as webapp  # noqa: E402
import db  # noqa: E402
import logistics  # noqa: E402
import payer  # noqa: E402


def _pass(name: str):
    print(f"PASS: {name}")


def _fail(name: str, detail: str = ""):
    raise AssertionError(f"{name} failed" + (f": {detail}" if detail else ""))


def _make_lead():
    return db.get_lead_by_token(db.create_lead({
        "applicant_token": "app_tok_support",
        "nct": "NCT12345678",
        "title": "Support workflow trial",
        "condition": "obesity",
        "location": "Toronto",
        "site": "General Hospital",
        "name": "Test Person",
        "email": "test@example.com",
        "phone": "+14165550100",
        "age": "34",
        "sex": "female",
        "notes": "support flow",
        "consent": 1,
        "source": "web",
        "records_connected": 0,
    }))


def test_sandbox_payer_and_travel():
    with webapp.app.app_context():
        lead = _make_lead()
        cov = payer.check(lead, {
            "payer_name": "Blue Cross",
            "member_id": "ABC1234",
            "group_id": "G1",
            "zip": "10001",
            "dob_year": "1992",
        })
        if cov.get("status") != "likely_covered":
            _fail("payer sandbox", str(cov))
        trav = logistics.plan(lead, {
            "distance": "120",
            "preferred_mode": "car",
            "needs": "",
            "city": "Toronto",
        })
        if trav.get("status") != "long_distance":
            _fail("travel sandbox", str(trav))
    _pass("sandbox payer/travel")


def test_db_support_roundtrip():
    with webapp.app.app_context():
        lead = _make_lead()
        ok1 = db.set_lead_coverage_check(
            lead["id"], "likely_covered", "Looks good.",
            payload={"payer_name": "Blue Cross", "member_id": "ABC"},
            provider="sandbox", ref="cov-1")
        ok2 = db.set_lead_travel_check(
            lead["id"], "assist_required", "Needs mobility ride.",
            payload={"needs": "wheelchair", "distance": 32},
            provider="sandbox", ref="trav-1")
        if not ok1 or not ok2:
            _fail("db support upsert", "write failed")
        sup = db.get_lead_support(lead["id"])
        if sup.get("coverage_status") != "likely_covered":
            _fail("db support roundtrip", str(sup))
        if sup.get("travel_status") != "assist_required":
            _fail("db support roundtrip", str(sup))
    _pass("db support roundtrip")


def main():
    test_sandbox_payer_and_travel()
    test_db_support_roundtrip()
    print("PASS: support readiness tests")


if __name__ == "__main__":
    main()

