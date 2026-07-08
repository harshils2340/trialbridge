"""Push an accepted candidate into the site's REDCap project.

REDCap is the most common tool research sites use to manage participants. Once a
study team accepts a candidate, this drops that person straight into their
REDCap project via the REDCap API so the coordinator never re-types anything.

Setup (per site): the site enables the API on their project and shares its
API token. Point at their instance with:

    REDCAP_API_URL=https://redcap.yourinstitution.edu/api/
    REDCAP_API_TOKEN=<project token>

Field mapping is intentionally minimal and defensive: we send common field
names (first_name, last_name, email, phone, condition, nct, notes) and REDCap
ignores any field the project doesn't define, so a partial map still works.
Override the map with REDCAP_FIELD_MAP='{"email":"contact_email",...}'.
"""
import json
import os
import urllib.parse
import urllib.request

API_URL = os.environ.get("REDCAP_API_URL", "").strip()
API_TOKEN = os.environ.get("REDCAP_API_TOKEN", "").strip()
_TIMEOUT = 20

_DEFAULT_MAP = {
    "record_id": "record_id",
    "first_name": "first_name",
    "last_name": "last_name",
    "email": "email",
    "phone": "phone",
    "age": "age",
    "sex": "sex",
    "condition": "condition",
    "nct": "nct",
    "notes": "notes",
}


def configured():
    return bool(API_URL and API_TOKEN)


def _field_map():
    raw = os.environ.get("REDCAP_FIELD_MAP", "").strip()
    if not raw:
        return dict(_DEFAULT_MAP)
    try:
        m = dict(_DEFAULT_MAP)
        m.update(json.loads(raw))
        return m
    except (ValueError, TypeError):
        return dict(_DEFAULT_MAP)


def _split_name(full):
    parts = (full or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def build_record(lead):
    """Map a lead row -> a REDCap record dict using the (overridable) field map."""
    m = _field_map()
    first, last = _split_name(lead["name"])
    src = {
        "record_id": str(lead["id"]),
        "first_name": first,
        "last_name": last,
        "email": lead["email"] or "",
        "phone": lead["phone"] or "",
        "age": lead["age"] or "",
        "sex": lead["sex"] or "",
        "condition": lead["condition"] or "",
        "nct": lead["nct"] or "",
        "notes": (lead["record_summary"] or lead["notes"] or "").strip(),
    }
    return {m[k]: v for k, v in src.items() if k in m}


def push_candidate(lead):
    """POST the candidate to REDCap. Returns (ok, message).

    Never raises: callers surface the message to the coordinator as a flash.
    """
    if not configured():
        return False, ("REDCap isn't connected yet. Add REDCAP_API_URL and "
                       "REDCAP_API_TOKEN to push candidates automatically.")
    record = build_record(lead)
    data = urllib.parse.urlencode({
        "token": API_TOKEN,
        "content": "record",
        "format": "json",
        "type": "flat",
        "overwriteBehavior": "normal",
        "returnContent": "count",
        "data": json.dumps([record]),
    }).encode()
    req = urllib.request.Request(
        API_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "BridgeMD/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200] if hasattr(e, "read") else ""
        return False, f"REDCap rejected the push ({e.code}). {detail}".strip()
    except Exception:
        return False, "Couldn't reach the REDCap server. Check the API URL."
    try:
        if int(json.loads(body).get("count", 0)) >= 1:
            return True, "Pushed to REDCap - the candidate is now in the site's project."
    except (ValueError, TypeError, AttributeError):
        pass
    return False, f"Unexpected REDCap response: {body[:160]}"
