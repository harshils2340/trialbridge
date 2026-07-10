"""Payer eligibility checks (API-ready with deterministic fallback).

Goal: reduce late-stage drop-off by surfacing likely coverage blockers before
screening/enrollment. When no live API is configured, we return a conservative
"manual review" result so workflow still functions.
"""
from __future__ import annotations

import json
import os
import urllib.request


def provider():
    return (os.environ.get("PAYER_PROVIDER", "sandbox") or "sandbox").strip().lower()


def live_configured():
    return bool(os.environ.get("PAYER_API_URL", "").strip() and
                os.environ.get("PAYER_API_KEY", "").strip())


def _req_json(payload):
    url = os.environ.get("PAYER_API_URL", "").strip()
    key = os.environ.get("PAYER_API_KEY", "").strip()
    hdr = os.environ.get("PAYER_API_KEY_HEADER", "Authorization").strip()
    prefix = os.environ.get("PAYER_API_KEY_PREFIX", "Bearer ").strip()
    if not url or not key:
        return {}
    headers = {"Content-Type": "application/json",
               "Accept": "application/json", "User-Agent": "BridgeMD/payer"}
    if hdr.lower() == "authorization":
        headers[hdr] = key if key.startswith("Bearer ") else (prefix + key)
    else:
        headers[hdr] = key
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads((r.read().decode("utf-8") or "{}"))


def _sandbox_eval(data):
    payer_name = (data.get("payer_name") or "").lower()
    member_id = (data.get("member_id") or "").strip()
    if not payer_name or not member_id:
        return {
            "status": "needs_info",
            "note": "Missing payer name or member id; manual check required.",
            "provider": "sandbox",
            "reference": "",
            "payload": data,
        }
    good_words = ("blue", "aetna", "cigna", "united", "kaiser", "medicare")
    if any(w in payer_name for w in good_words):
        return {
            "status": "likely_covered",
            "note": "Plan likely active; verify prior-auth details with site billing.",
            "provider": "sandbox",
            "reference": "sandbox-elig-1",
            "payload": data,
        }
    if "medicaid" in payer_name:
        return {
            "status": "coverage_limited",
            "note": "May require site/state-specific authorization steps.",
            "provider": "sandbox",
            "reference": "sandbox-elig-2",
            "payload": data,
        }
    return {
        "status": "manual_review",
        "note": "Coverage uncertain. Ask site coordinator to run eligibility check.",
        "provider": "sandbox",
        "reference": "sandbox-elig-3",
        "payload": data,
    }


def check(lead, data):
    """Run eligibility check. Returns normalized dict:

    {"status","note","provider","reference","payload"}
    """
    def _g(k, default=""):
        if isinstance(lead, dict):
            return lead.get(k, default)
        try:
            return lead[k]
        except Exception:
            return default

    payload = {
        "lead_id": _g("id"),
        "trial_nct": _g("nct", ""),
        "trial_title": _g("title", ""),
        "site": _g("site", ""),
        "patient_zip": (data.get("zip") or "").strip(),
        "payer_name": (data.get("payer_name") or "").strip(),
        "member_id": (data.get("member_id") or "").strip(),
        "group_id": (data.get("group_id") or "").strip(),
        "dob_year": (data.get("dob_year") or "").strip(),
    }
    if live_configured():
        try:
            raw = _req_json(payload)
            status = str(raw.get("status") or raw.get("eligibility") or "").strip().lower()
            note = str(raw.get("note") or raw.get("message") or "").strip()
            ref = str(raw.get("reference") or raw.get("id") or "").strip()
            if not status:
                status = "manual_review"
            return {
                "status": status,
                "note": note or "Eligibility check submitted to payer API.",
                "provider": provider(),
                "reference": ref,
                "payload": payload,
            }
        except Exception as e:
            return {
                "status": "api_error",
                "note": f"Payer API check failed: {str(e)[:180]}",
                "provider": provider(),
                "reference": "",
                "payload": payload,
            }
    return _sandbox_eval(payload)

