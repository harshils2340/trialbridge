#!/usr/bin/env python3
"""Focused regression tests for optional SMS notification channel.

Run:
  /Users/harshils/GraphMD/matcher/.venv/bin/python test_notifications_sms.py
"""
from __future__ import annotations

import os
import sys

import notifications


def _pass(name: str):
    print(f"PASS: {name}")


def _fail(name: str, detail: str = ""):
    raise AssertionError(f"{name} failed" + (f": {detail}" if detail else ""))


def test_e164_normalization():
    if notifications.normalize_e164("(416) 555-0100") != "+14165550100":
        _fail("e164 normalize", "10-digit NANP normalization mismatch")
    if notifications.normalize_e164("+44 20 7946 0958") != "+442079460958":
        _fail("e164 normalize", "international normalization mismatch")
    if notifications.normalize_e164("not-a-phone"):
        _fail("e164 normalize", "invalid input should return empty string")
    _pass("E.164 normalization")


def test_sms_missing_config_falls_back_to_email():
    # Explicitly request SMS, but do not provide Twilio env.
    os.environ["NOTIFY_SMS"] = "1"
    os.environ.pop("TWILIO_ACCOUNT_SID", None)
    os.environ.pop("TWILIO_AUTH_TOKEN", None)
    os.environ.pop("TWILIO_FROM_NUMBER", None)

    calls = []

    def fake_email(to_addr, subject, body):
        calls.append((to_addr, subject, body))
        return True, "sent"

    notifier = notifications.Notifier(live=True, send_email_fn=fake_email)
    ok = notifier.send(
        to_email="patient@example.com",
        to_phone="+14165550100",
        subject="BridgeMD test",
        email_body="email fallback body",
        sms_body="sms body",
    )
    if not ok:
        _fail("sms fallback", "expected email fallback to succeed")
    if len(calls) != 1:
        _fail("sms fallback", f"expected 1 email send, got {len(calls)}")
    _pass("SMS missing config falls back to email")


def main():
    tests = [
        test_e164_normalization,
        test_sms_missing_config_falls_back_to_email,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {test.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR: {test.__name__}: {type(e).__name__}: {e}")
    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed")


if __name__ == "__main__":
    main()
