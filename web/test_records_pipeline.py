#!/usr/bin/env python3
"""Targeted regression tests for records-sync and compensation signal logic.

Run:
  /Users/harshils/GraphMD/matcher/.venv/bin/python test_records_pipeline.py
"""
from __future__ import annotations

import os
import sys
import tempfile


_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ.setdefault("SECRET_KEY", "records-test-secret")
os.environ.setdefault("NO_LOGIN", "0")
os.environ.setdefault("ALERTS_BACKGROUND", "0")
os.environ.setdefault("REMINDERS_BACKGROUND", "0")

import app as webapp  # noqa: E402
import db  # noqa: E402
import records  # noqa: E402


def _pass(name: str):
    print(f"PASS: {name}")


def _fail(name: str, detail: str = ""):
    raise AssertionError(f"{name} failed" + (f": {detail}" if detail else ""))


def test_records_profile_roundtrip():
    with webapp.app.app_context():
        prof = {
            "provider": "SMART Health IT (sandbox)",
            "age": 43,
            "sex": "female",
            "conditions": ["Type 2 diabetes"],
            "meds": ["Metformin"],
            "labs": ["Hemoglobin A1c: 7.4 % (2026-06-01)"],
            "summary": "AGE: 43\nSEX: female\nActive problems: Type 2 diabetes.",
            "sync_status": "syncing",
            "source_status": "network_query_started",
            "external_patient_id": "pt_123",
            "external_query_id": "nq_456",
            "completeness_score": 60,
            "last_sync_error": "",
            "last_sync_at": "2026-07-09 01:00",
        }
        db.set_records_profile("app_tok_1", prof)
        got = db.get_records_profile("app_tok_1")
        if not got:
            _fail("profile roundtrip", "missing profile")
        if got.get("external_patient_id") != "pt_123":
            _fail("profile roundtrip", "external patient id mismatch")
        if got.get("external_query_id") != "nq_456":
            _fail("profile roundtrip", "external query id mismatch")
        if got.get("sync_status") != "syncing":
            _fail("profile roundtrip", "sync status mismatch")
        if int(got.get("completeness_score") or 0) != 60:
            _fail("profile roundtrip", "completeness score mismatch")
    _pass("records profile roundtrip")


def test_sync_state_and_lookup():
    with webapp.app.app_context():
        db.set_records_sync_state(
            applicant_token="app_tok_2",
            provider="SMART Health IT (sandbox)",
            sync_status="syncing",
            source_status="network_query_started",
            external_patient_id="pt_abc",
            external_query_id="nq_xyz",
            error_msg="",
        )
        a1 = db.find_applicant_by_external_patient("pt_abc")
        a2 = db.find_applicant_by_external_query("nq_xyz")
        if a1 != "app_tok_2" or a2 != "app_tok_2":
            _fail("sync lookup", f"bad lookup values: {a1}, {a2}")
    _pass("sync state and lookup")


def test_sync_state_transition_guard():
    with webapp.app.app_context():
        db.set_records_sync_state(
            applicant_token="app_tok_guard",
            provider="SMART Health IT (sandbox)",
            sync_status="connected",
            source_status="consolidated_ready",
            external_patient_id="pt_guard",
            external_query_id="nq_guard",
            error_msg="",
            allow_regress=True,
        )
        # Stale in-flight updates should not regress a terminal connected state.
        db.set_records_sync_state(
            applicant_token="app_tok_guard",
            provider="SMART Health IT (sandbox)",
            sync_status="syncing",
            source_status="provider_update",
            external_patient_id="pt_guard",
            external_query_id="nq_guard_stale",
            error_msg="",
        )
        got = db.get_records_profile("app_tok_guard") or {}
        if got.get("sync_status") != "connected":
            _fail("sync transition guard", f"unexpected status: {got.get('sync_status')}")

        # Explicit refresh/connect flows can intentionally move back to syncing.
        db.set_records_sync_state(
            applicant_token="app_tok_guard",
            provider="Metriport",
            sync_status="syncing",
            source_status="network_query_started",
            external_patient_id="pt_guard",
            external_query_id="nq_guard_fresh",
            error_msg="",
            allow_regress=True,
        )
        got2 = db.get_records_profile("app_tok_guard") or {}
        if got2.get("sync_status") != "syncing":
            _fail("sync transition guard", "allow_regress refresh did not apply")
    _pass("sync transition guard")


def test_fhir_bundle_mapping_shape():
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": {"resourceType": "Patient", "birthDate": "1985-01-01",
                          "gender": "female"}},
            {"resource": {"resourceType": "Condition", "clinicalStatus": {"text": "active"},
                          "code": {"text": "Obesity"}}},
            {"resource": {"resourceType": "MedicationRequest", "status": "active",
                          "medicationCodeableConcept": {"text": "Semaglutide"}}},
            {"resource": {"resourceType": "Observation",
                          "category": [{"coding": [{"code": "laboratory"}]}],
                          "code": {"text": "Hemoglobin A1c"},
                          "effectiveDateTime": "2026-07-01T10:00:00Z",
                          "valueQuantity": {"value": 7.1, "unit": "%"}}},
        ],
    }
    prof = records._profile_from_fhir(bundle)
    req = ("age", "sex", "conditions", "meds", "labs", "summary", "completeness_score")
    missing = [k for k in req if k not in prof]
    if missing:
        _fail("bundle mapping", f"missing keys: {missing}")
    if prof["sex"] != "female":
        _fail("bundle mapping", "sex parse mismatch")
    if "Obesity" not in prof.get("conditions", []):
        _fail("bundle mapping", "condition missing")
    if "Semaglutide" not in prof.get("meds", []):
        _fail("bundle mapping", "med missing")
    if not prof.get("labs"):
        _fail("bundle mapping", "labs missing")
    _pass("FHIR bundle mapping shape")


def test_pay_signal_scoring():
    high = webapp._pay_likelihood({
        "title": "Phase 1 inpatient healthy volunteer study",
        "briefSummary": "Participants will be compensated up to $8,500 with travel reimbursement.",
        "detailedDescription": "Includes overnight confinement and inpatient stay.",
        "criteria": "",
        "phase": "PHASE1",
        "healthyVolunteers": "YES",
    })
    none = webapp._pay_likelihood({
        "title": "Observational registry",
        "briefSummary": "No compensation is provided for participation.",
        "detailedDescription": "",
        "criteria": "",
        "phase": "",
        "healthyVolunteers": "NO",
    })
    if high.get("score", 0) < 6 or high.get("tier") not in ("high", "likely"):
        _fail("pay signal high", f"unexpected high signal: {high}")
    if none.get("score", 0) != 0 or none.get("tier") != "none":
        _fail("pay signal no-comp", f"unexpected no-comp signal: {none}")
    _pass("pay signal scoring")


def main():
    tests = [
        test_records_profile_roundtrip,
        test_sync_state_and_lookup,
        test_sync_state_transition_guard,
        test_fhir_bundle_mapping_shape,
        test_pay_signal_scoring,
    ]
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

