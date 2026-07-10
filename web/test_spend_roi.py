#!/usr/bin/env python3
"""Regression checks for recruitment spend tracking + ROI summary."""
from __future__ import annotations

import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ.setdefault("SECRET_KEY", "spend-test-secret")
os.environ.setdefault("NO_LOGIN", "0")
os.environ.setdefault("ALERTS_BACKGROUND", "0")
os.environ.setdefault("REMINDERS_BACKGROUND", "0")

import app as webapp  # noqa: E402
import db  # noqa: E402


def _pass(name: str):
    print(f"PASS: {name}")


def _fail(name: str, detail: str = ""):
    raise AssertionError(f"{name} failed" + (f": {detail}" if detail else ""))


def test_spend_summary():
    with webapp.app.app_context():
        uid = db.create_user("site@example.com", "x", "Site User", verified=True)
        db.add_study_claim(uid, "NCT10000001", "Study A")
        db.add_recruitment_spend(uid, "NCT10000001", "web", "Meta", 1200, "2026-07-09", "")
        db.add_recruitment_spend(uid, "NCT10000001", "referral", "HCP", 300, "2026-07-09", "")
        s = db.spend_summary_for_user(uid, ncts={"NCT10000001"})
        if round(float(s.get("total_usd") or 0), 2) != 1500.00:
            _fail("spend total", str(s))
        by = {r["source"]: r["amount_usd"] for r in s.get("by_source", [])}
        if round(float(by.get("web") or 0), 2) != 1200.00:
            _fail("spend by source", str(by))
    _pass("spend summary")


def main():
    test_spend_summary()
    print("PASS: spend ROI tests")


if __name__ == "__main__":
    main()

