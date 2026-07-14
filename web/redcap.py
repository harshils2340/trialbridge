"""Connect a site's REDCap project to BridgeMD for intake + screening.

REDCap is the most common tool research sites use to manage participants. This
module does three jobs:

1. **Push an accepted candidate** into the site's REDCap project (`push_candidate`)
   so a coordinator never re-types anything.
2. **List the project's instruments/surveys** (`export_instruments`) so a site can
   pick which one is their patient intake/screening form.
3. **Hand a patient a pre-filled survey link** (`survey_link_for_lead`) at the
   screening step: we create/lookup the record, pre-fill known fields (name,
   email, condition, nct), then ask REDCap for that record's survey URL so the
   patient completes the site's own IRB/REB-approved form with less friction.

Configuration comes from two places, in order of precedence:

- **Per-site** (preferred): stored on the site's profile - API URL, API token,
  optional field map, chosen intake instrument, and an explicit "this instrument
  is IRB/REB-approved and patients consent" flag that must be on before any LIVE
  patient form is served. See `config_from_profile`.
- **Environment default** (single-tenant / legacy):

    REDCAP_API_URL=https://redcap.yourinstitution.edu/api/
    REDCAP_API_TOKEN=<project token>
    REDCAP_WEBHOOK_SECRET=<optional shared secret for the webhook receiver>

Field mapping is intentionally minimal and defensive: we send common field names
(first_name, last_name, email, phone, condition, nct, notes) and REDCap ignores
any field the project doesn't define, so a partial map still works. Override per
site, or globally with REDCAP_FIELD_MAP='{"email":"contact_email",...}'.

Security: API tokens are secrets. They are never logged and never rendered into
HTML. Callers surface only human-readable status messages.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

# Environment default (single project). Per-site config overrides this.
ENV_API_URL = os.environ.get("REDCAP_API_URL", "").strip()
ENV_API_TOKEN = os.environ.get("REDCAP_API_TOKEN", "").strip()
WEBHOOK_SECRET = os.environ.get("REDCAP_WEBHOOK_SECRET", "").strip()
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

# Shown in the simulated (unconnected) demo so the intake flow is always
# walkable live, mirroring how the rest of the app simulates integrations.
SIMULATED_INSTRUMENTS = [
    {"name": "patient_intake", "label": "Patient Intake & Consent"},
    {"name": "screening_eligibility", "label": "Screening Eligibility Checklist"},
    {"name": "medical_history", "label": "Medical History"},
]


def _env_map():
    raw = os.environ.get("REDCAP_FIELD_MAP", "").strip()
    if not raw:
        return dict(_DEFAULT_MAP)
    try:
        m = dict(_DEFAULT_MAP)
        m.update(json.loads(raw))
        return m
    except (ValueError, TypeError):
        return dict(_DEFAULT_MAP)


def _parse_map(raw):
    """Merge a JSON field-map string over the defaults; tolerate bad input."""
    base = _env_map()
    if not raw:
        return base
    try:
        base.update(json.loads(raw))
    except (ValueError, TypeError):
        pass
    return base


def _row_get(row, key, default=""):
    """Read a column from a sqlite Row (or dict) without raising if absent."""
    if row is None:
        return default
    try:
        val = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if val is None else val


class RedcapConfig:
    """A resolved REDCap connection (env default or a specific site's project)."""

    def __init__(self, api_url="", api_token="", field_map=None,
                 intake_instrument="", project_label="", intake_enabled=False):
        self.api_url = (api_url or "").strip()
        self.api_token = (api_token or "").strip()
        self.field_map = field_map or _env_map()
        self.intake_instrument = (intake_instrument or "").strip()
        self.project_label = (project_label or "").strip()
        self.intake_enabled = bool(intake_enabled)

    @property
    def connected(self):
        """True when we can talk to REDCap (URL + token present)."""
        return bool(self.api_url and self.api_token)

    @property
    def intake_live(self):
        """True only when a real, site-confirmed intake form should be served.

        Requires a live connection, a chosen instrument, AND the explicit
        site attestation that the instrument is IRB/REB-approved with patient
        consent (compliance gate for LIVE patient forms)."""
        return bool(self.connected and self.intake_instrument
                    and self.intake_enabled)


def env_config():
    return RedcapConfig(ENV_API_URL, ENV_API_TOKEN)


def config_from_profile(profile):
    """Build a RedcapConfig from a site_profiles row, falling back to env.

    `profile` is a sqlite Row (or None). Per-site values win; anything blank
    falls back to the environment default so a single-tenant deploy still works.
    """
    api_url = _row_get(profile, "redcap_endpoint") or ENV_API_URL
    api_token = _row_get(profile, "redcap_api_token") or ENV_API_TOKEN
    field_map = _parse_map(_row_get(profile, "redcap_field_map"))
    return RedcapConfig(
        api_url=api_url,
        api_token=api_token,
        field_map=field_map,
        intake_instrument=_row_get(profile, "redcap_intake_instrument"),
        project_label=_row_get(profile, "redcap_project_label"),
        intake_enabled=bool(_row_get(profile, "redcap_intake_enabled", 0)),
    )


# --------------------------------------------------------------------------- #
# Backward-compatible module-level API (uses the environment default config).
# --------------------------------------------------------------------------- #
def configured():
    return env_config().connected


def build_record(lead, field_map=None):
    """Map a lead row -> a REDCap record dict using the (overridable) field map."""
    m = field_map or _env_map()
    first, last = _split_name(_row_get(lead, "name"))
    src = {
        "record_id": str(_row_get(lead, "id")),
        "first_name": first,
        "last_name": last,
        "email": _row_get(lead, "email"),
        "phone": _row_get(lead, "phone"),
        "age": _row_get(lead, "age"),
        "sex": _row_get(lead, "sex"),
        "condition": _row_get(lead, "condition"),
        "nct": _row_get(lead, "nct"),
        "notes": (_row_get(lead, "record_summary") or _row_get(lead, "notes")).strip(),
    }
    return {m[k]: v for k, v in src.items() if k in m}


def push_candidate(lead, cfg=None):
    """POST the candidate to REDCap. Returns (ok, message).

    Never raises: callers surface the message to the coordinator as a flash.
    """
    cfg = cfg or env_config()
    if not cfg.connected:
        return False, ("REDCap isn't connected yet. Add your REDCap API URL and "
                       "token in Site setup to push candidates automatically.")
    record = build_record(lead, cfg.field_map)
    ok, data = _api(cfg, {
        "content": "record",
        "type": "flat",
        "overwriteBehavior": "normal",
        "returnContent": "count",
        "data": json.dumps([record]),
    })
    if not ok:
        return False, data
    try:
        if int(data.get("count", 0)) >= 1:
            return True, ("Pushed to REDCap - the candidate is now in the "
                          "site's project.")
    except (ValueError, TypeError, AttributeError):
        pass
    return False, "Unexpected REDCap response."


# --------------------------------------------------------------------------- #
# Instruments / surveys
# --------------------------------------------------------------------------- #
def export_instruments(cfg):
    """List the project's data-collection instruments. Returns (ok, list, msg).

    Each item is {"name": <unique instrument name>, "label": <human label>}.
    """
    if not cfg.connected:
        return False, [], "REDCap isn't connected."
    ok, data = _api(cfg, {"content": "instrument", "format": "json"})
    if not ok:
        return False, [], data
    out = []
    if isinstance(data, list):
        for row in data:
            name = (row.get("instrument_name") or "").strip()
            if not name:
                continue
            out.append({
                "name": name,
                "label": (row.get("instrument_label") or name).strip(),
            })
    return True, out, "Loaded instruments from REDCap."


def survey_link_for_lead(cfg, lead, instrument=None):
    """Create/lookup the lead's record (pre-filling known fields) and return a
    unique survey link for it. Returns (ok, url, record_id, msg).

    This is what the patient opens at the screening step. Pre-filling reduces
    drop-off (the KPI goal for contacted -> screened).
    """
    instrument = (instrument or cfg.intake_instrument or "").strip()
    if not cfg.connected:
        return False, "", "", "REDCap isn't connected."
    if not instrument:
        return False, "", "", "No intake instrument selected in Site setup."

    record = build_record(lead, cfg.field_map)
    record_id = str(record.get(cfg.field_map.get("record_id", "record_id"),
                               _row_get(lead, "id")))
    # Import (create or update) the record so REDCap has something to link a
    # survey to, with the known fields already filled in.
    ok, data = _api(cfg, {
        "content": "record",
        "type": "flat",
        "overwriteBehavior": "normal",
        "returnContent": "ids",
        "data": json.dumps([record]),
    })
    if not ok:
        return False, "", "", data
    ok, url = _api(cfg, {
        "content": "surveyLink",
        "record": record_id,
        "instrument": instrument,
        "returnFormat": "json",
    }, expect_json=False)
    if not ok:
        return False, "", record_id, url
    url = (url or "").strip()
    if not url.startswith("http"):
        return False, "", record_id, ("REDCap did not return a survey link - "
                                       "confirm the instrument is enabled as a "
                                       "survey.")
    return True, url, record_id, "Survey link ready."


def record_complete(cfg, record_id, instrument=None):
    """Poll whether the record's instrument is marked complete. Returns
    (ok, is_complete, msg). Uses the `<instrument>_complete` status field."""
    instrument = (instrument or cfg.intake_instrument or "").strip()
    if not cfg.connected:
        return False, False, "REDCap isn't connected."
    if not (record_id and instrument):
        return False, False, "Missing record or instrument."
    status_field = f"{instrument}_complete"
    ok, data = _api(cfg, {
        "content": "record",
        "type": "flat",
        "format": "json",
        "records[0]": str(record_id),
        "fields[0]": status_field,
    })
    if not ok:
        return False, False, data
    if isinstance(data, list) and data:
        # REDCap complete-status: "2" == Complete.
        val = str(data[0].get(status_field, "")).strip()
        return True, val == "2", "Checked."
    return True, False, "No record yet."


# --------------------------------------------------------------------------- #
# Low-level API call
# --------------------------------------------------------------------------- #
def _api(cfg, params, expect_json=True):
    """POST to the REDCap API. Returns (ok, parsed_or_message).

    `params` should NOT include the token; it is added here and never logged.
    When expect_json is False the raw response body (str) is returned on success.
    """
    payload = {"token": cfg.api_token, "format": "json"}
    payload.update(params)
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        cfg.api_url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "BridgeMD/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200] if hasattr(e, "read") else ""
        return False, f"REDCap rejected the request ({e.code}). {detail}".strip()
    except Exception:
        return False, "Couldn't reach the REDCap server. Check the API URL."
    if not expect_json:
        return True, body
    try:
        return True, json.loads(body)
    except (ValueError, TypeError):
        return False, f"Unexpected REDCap response: {body[:160]}"


def _split_name(full):
    parts = (full or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])
