"""Thin notification channels abstraction for email + optional Twilio SMS.

SMS is guarded by env flags and must be explicitly enabled:
  NOTIFY_SMS=1
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER

If SMS is enabled but misconfigured/unavailable, sends safely fall back to email
when email delivery is configured.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request
try:
    from copy_sanitize import sanitize_copy
except ImportError:  # imported outside the app, without the repo root on sys.path
    import pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
    from copy_sanitize import sanitize_copy


def _env_on(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default) == "1"


def _normalize_country_code(raw: str) -> str:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    return f"+{digits}" if digits else "+1"


def normalize_e164(raw_phone: str, default_country_code: str = "") -> str:
    """Normalize a phone-like string into E.164 or return "" if invalid."""
    if not raw_phone:
        return ""
    raw = raw_phone.strip()
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    if raw.startswith("+"):
        digits = "".join(ch for ch in raw[1:] if ch.isdigit())
        candidate = f"+{digits}"
    else:
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) == 11 and digits.startswith("1"):
            candidate = f"+{digits}"
        elif len(digits) == 10:
            cc = _normalize_country_code(
                default_country_code or os.environ.get("SMS_DEFAULT_COUNTRY_CODE", "+1")
            )
            candidate = cc + digits
        else:
            return ""
    e164_digits = candidate[1:]
    if len(e164_digits) < 8 or len(e164_digits) > 15:
        return ""
    return candidate


def sms_configured() -> bool:
    return bool(
        os.environ.get("TWILIO_ACCOUNT_SID")
        and os.environ.get("TWILIO_AUTH_TOKEN")
        and os.environ.get("TWILIO_FROM_NUMBER")
    )


def send_sms(to_phone: str, body: str) -> tuple[bool, str]:
    """Send an SMS via Twilio REST API. Returns (ok, message)."""
    if not sms_configured():
        return False, "Twilio SMS is not configured."
    to_e164 = normalize_e164(to_phone)
    if not to_e164:
        return False, "Invalid recipient phone number."
    if not body.strip():
        return False, "Empty SMS body."
    body = sanitize_copy(body)

    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = normalize_e164(os.environ.get("TWILIO_FROM_NUMBER", ""))
    if not from_number:
        return False, "Invalid TWILIO_FROM_NUMBER."

    data = urllib.parse.urlencode(
        {"To": to_e164, "From": from_number, "Body": body}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data=data,
        method="POST",
    )
    auth = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = getattr(resp, "status", 0)
            payload = json.loads(resp.read().decode("utf-8") or "{}")
        if 200 <= int(status) < 300:
            return True, payload.get("sid", "SMS sent.")
        return False, payload.get("message", "Twilio rejected message.")
    except Exception as e:  # noqa: BLE001
        return False, f"Couldn't send SMS: {e}"


class Notifier:
    """Minimal multi-channel sender with env-guarded SMS."""

    def __init__(self, *, live: bool, send_email_fn=None):
        self.live = bool(live)
        self._send_email_fn = send_email_fn

    def email_ready(self) -> bool:
        return self.live and callable(self._send_email_fn)

    def sms_ready(self) -> bool:
        return self.live and _env_on("NOTIFY_SMS", "0") and sms_configured()

    def send(
        self,
        *,
        to_email: str = "",
        to_phone: str = "",
        subject: str = "",
        email_body: str = "",
        sms_body: str = "",
        allow_email: bool = True,
        allow_sms: bool = True,
    ) -> bool:
        """Try allowed channels and succeed if any send succeeds."""
        if not self.live:
            return False

        ok_sms = False
        ok_email = False
        if allow_sms and sms_body and to_phone and self.sms_ready():
            ok_sms, _ = send_sms(to_phone, sms_body)

        # Safe fallback: if SMS isn't sent (or unavailable), email can still send.
        if allow_email and to_email and subject and email_body and self.email_ready():
            ok_email, _ = self._send_email_fn(to_email, subject, email_body)
        return bool(ok_sms or ok_email)
