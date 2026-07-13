"""Tests for demo lane 4 — "EHR background matching" (the clinic-wide EHR feed
that surfaces new trial matches from the clinic's own patients).

What it verifies:
1. /app?wf=proactive renders the dedicated EHR view (not the search dashboard),
   and shows the clinic-wide connection state + the 3-step explanation.
2. Matches are grouped by trial with per-patient "why matched" reasons and
   review/outreach actions.
3. /app (no wf) still renders the normal physician search dashboard.
4. Compliance guardrails: patients are shown de-identified (no real names) and
   the "physician reviews before anyone is contacted" note is present, so the
   demo doesn't imply auto-contacting patients.

Run: python test_ehr_lane.py   (offline; NO_LOGIN gives a demo user)
"""
import os
import re
import sys
import tempfile

os.environ["DB_PATH"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["NO_LOGIN"] = "1"
os.environ.setdefault("ALERTS_BACKGROUND", "0")
os.environ.setdefault("REMINDERS_BACKGROUND", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")

import app  # noqa: E402


def test_demo_payload_shape():
    d = app._ehr_matching_demo()
    assert d["connected"] is True
    assert d["trials"], "demo should include active trials"
    total = sum(len(t["patients"]) for t in d["trials"])
    assert d["new_matches"] == total, "new_matches must equal the patients listed"
    for t in d["trials"]:
        assert t["nct"] and t["title"] and t["condition"]
        for p in t["patients"]:
            assert p["reason"], "each match needs a why-matched reason"
    print("PASS: EHR demo payload is well-formed")


def test_proactive_renders_ehr_view():
    c = app.app.test_client()
    r = c.get("/app?wf=proactive")
    assert r.status_code == 200, r.status_code
    html = r.get_data(as_text=True)
    assert "EHR background matching" in html
    assert "EHR connected" in html, "connection state not shown"
    assert "New patient matches" in html, "match queue heading missing"
    assert "Email patient" in html and "Send to physician" in html, "actions missing"
    # grouped by trial (NCT ids present)
    assert "NCT05869903" in html
    print("PASS: /app?wf=proactive renders the EHR matching view")


def test_plain_dashboard_is_search():
    c = app.app.test_client()
    r = c.get("/app")
    assert r.status_code == 200, r.status_code
    html = r.get_data(as_text=True)
    assert "Physician matching workspace" in html, "plain /app should be the search view"
    # "New patient matches" is unique to the EHR view body (the lane label in the
    # POV switcher appears on every page, so key off the match queue instead).
    assert "New patient matches" not in html, "plain /app must not be the EHR view"
    print("PASS: plain /app stays the physician search dashboard")


def test_compliance_deidentified_and_review_note():
    c = app.app.test_client()
    html = c.get("/app?wf=proactive").get_data(as_text=True)
    # de-identified patient handles (PT-#### + initials), not real names
    assert re.search(r"PT-\d{4}", html), "patients should use de-identified refs"
    assert "physician reviews before anyone is contacted" in html, \
        "must state a clinician reviews before contact (no auto-outreach)"
    print("PASS: patients de-identified + physician-review-before-contact stated")


def main():
    tests = [
        test_demo_payload_shape,
        test_proactive_renders_ehr_view,
        test_plain_dashboard_is_search,
        test_compliance_deidentified_and_review_note,
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
    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed")


if __name__ == "__main__":
    main()
