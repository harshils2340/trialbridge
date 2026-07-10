#!/usr/bin/env python3
"""Regression checks for eligibility-output quality gating."""
from __future__ import annotations

import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ.setdefault("SECRET_KEY", "quality-gate-test-secret")
os.environ.setdefault("NO_LOGIN", "0")
os.environ.setdefault("ALERTS_BACKGROUND", "0")
os.environ.setdefault("REMINDERS_BACKGROUND", "0")

import app as webapp  # noqa: E402


def _pass(name: str):
    print(f"PASS: {name}")


def _fail(name: str, detail: str = ""):
    raise AssertionError(f"{name} failed" + (f": {detail}" if detail else ""))


def test_contradiction_is_gated():
    raw = {
        "verdict": "possible",
        "score": 78,
        "met": [],
        "not_met": ["Patient has Alzheimer's disease (exclusion criterion)."],
        "unknown": [],
        "rationale": "You might qualify for this study based on the details provided.",
    }
    gated = webapp._quality_gated_match(raw)
    if gated.get("quality_ok"):
        _fail("contradiction gate", str(gated))
    if gated.get("verdict") != "unlikely":
        _fail("contradiction verdict rewrite", str(gated))
    if "Potential blocker identified" not in (gated.get("rationale") or ""):
        _fail("contradiction rationale rewrite", str(gated))
    _pass("contradiction output is quality gated")


def test_high_quality_output_passes():
    raw = {
        "verdict": "possible",
        "score": 66,
        "met": ["Age falls in allowed range."],
        "not_met": [],
        "unknown": ["Are you currently taking an SGLT2 inhibitor?"],
        "rationale": ("Basic details look aligned, but medication history still "
                      "needs confirmation with the study team."),
    }
    gated = webapp._quality_gated_match(raw)
    if not gated.get("quality_ok"):
        _fail("high quality should pass", str(gated))
    if int(gated.get("quality_score") or 0) < int(webapp.MATCH_QUALITY_MIN):
        _fail("quality score unexpectedly low", str(gated))
    _pass("high-quality output passes gate")


def test_short_low_signal_output_is_gated():
    raw = {
        "verdict": "likely_eligible",
        "score": 90,
        "met": [],
        "not_met": [],
        "unknown": [],
        "rationale": "Looks good.",
    }
    gated = webapp._quality_gated_match(raw)
    if gated.get("quality_ok"):
        _fail("short output should fail", str(gated))
    if not gated.get("quality_flags"):
        _fail("quality flags should be present", str(gated))
    _pass("short low-signal output is quality gated")


def main():
    tests = [
        test_contradiction_is_gated,
        test_high_quality_output_passes,
        test_short_low_signal_output_is_gated,
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
        raise SystemExit(1)
    print(f"\nAll {len(tests)} tests passed")


if __name__ == "__main__":
    main()
