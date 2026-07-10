"""Travel/logistics assistance planning (API-ready with deterministic fallback)."""
from __future__ import annotations

import json
import os
import urllib.request


def provider():
    return (os.environ.get("LOGISTICS_PROVIDER", "sandbox") or "sandbox").strip().lower()


def live_configured():
    return bool(os.environ.get("LOGISTICS_API_URL", "").strip() and
                os.environ.get("LOGISTICS_API_KEY", "").strip())


def _req_json(payload):
    url = os.environ.get("LOGISTICS_API_URL", "").strip()
    key = os.environ.get("LOGISTICS_API_KEY", "").strip()
    hdr = os.environ.get("LOGISTICS_API_KEY_HEADER", "Authorization").strip()
    prefix = os.environ.get("LOGISTICS_API_KEY_PREFIX", "Bearer ").strip()
    headers = {"Content-Type": "application/json",
               "Accept": "application/json", "User-Agent": "BridgeMD/logistics"}
    if hdr.lower() == "authorization":
        headers[hdr] = key if key.startswith("Bearer ") else (prefix + key)
    else:
        headers[hdr] = key
    req = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads((r.read().decode("utf-8") or "{}"))


def _sandbox_plan(data):
    distance = float(data.get("distance") or 0)
    mode = (data.get("preferred_mode") or "").strip().lower()
    needs = (data.get("needs") or "").strip().lower()
    if "wheelchair" in needs or "mobility" in needs:
        return {
            "status": "assist_required",
            "note": "Mobility support likely needed; arrange accessible transport.",
            "provider": "sandbox",
            "reference": "sandbox-travel-1",
            "payload": data,
        }
    if distance >= 80:
        return {
            "status": "long_distance",
            "note": "Long distance to site; recommend mileage/lodging support review.",
            "provider": "sandbox",
            "reference": "sandbox-travel-2",
            "payload": data,
        }
    if mode in ("public", "bus", "train"):
        return {
            "status": "supported",
            "note": "Public transit likely feasible; share exact route and timing buffer.",
            "provider": "sandbox",
            "reference": "sandbox-travel-3",
            "payload": data,
        }
    return {
        "status": "basic_plan",
        "note": "No major travel blocker detected. Confirm parking and appointment windows.",
        "provider": "sandbox",
        "reference": "sandbox-travel-4",
        "payload": data,
    }


def plan(lead, data):
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
        "site": _g("site", ""),
        "distance": data.get("distance", 0),
        "preferred_mode": (data.get("preferred_mode") or "").strip(),
        "needs": (data.get("needs") or "").strip(),
        "city": (data.get("city") or "").strip(),
    }
    if live_configured():
        try:
            raw = _req_json(payload)
            status = str(raw.get("status") or raw.get("planStatus") or "").strip().lower()
            note = str(raw.get("note") or raw.get("message") or "").strip()
            ref = str(raw.get("reference") or raw.get("id") or "").strip()
            if not status:
                status = "manual_review"
            return {
                "status": status,
                "note": note or "Travel plan requested from logistics API.",
                "provider": provider(),
                "reference": ref,
                "payload": payload,
            }
        except Exception as e:
            return {
                "status": "api_error",
                "note": f"Logistics API plan failed: {str(e)[:180]}",
                "provider": provider(),
                "reference": "",
                "payload": payload,
            }
    return _sandbox_plan(payload)

