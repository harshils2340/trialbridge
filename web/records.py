"""Patient-mediated health records: connect once, auto-fill everything after.

This module supports:
- sandbox (immediate synthetic profile),
- live providers (Metriport/1upHealth) via asynchronous sync + webhook update.
"""
import datetime as dt
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request

import codes
import fhir

_LABELS = {
    "sandbox": "SMART Health IT (sandbox)",
    "metriport": "Metriport",
    "1uphealth": "1upHealth",
    "1up": "1upHealth",
}

_METRIPORT_PATH_DEFAULTS = {
    "METRIPORT_CREATE_PATIENT_PATH": "/medical/v1/patient",
    "METRIPORT_START_NETWORK_QUERY_PATH": "/medical/v1/network/query",
    "METRIPORT_GET_CONSOLIDATED_PATH": "/medical/v1/fhir/patient/{patient_id}/consolidated",
    "METRIPORT_START_CONSOLIDATED_PATH": "/medical/v1/fhir/consolidated/query",
    "METRIPORT_GET_CONSOLIDATED_QUERY_PATH": "/medical/v1/fhir/consolidated/query/{query_id}",
}

_DONE_TOKENS = ("complete", "completed", "done", "success", "succeeded", "ready", "final")
_ERROR_TOKENS = ("error", "failed", "failure", "cancel", "rejected", "denied")
_SYNCING_TOKENS = ("start", "queued", "pending", "running", "processing", "syncing", "in_progress")


def provider():
    return os.environ.get("RECORDS_PROVIDER", "sandbox").lower().strip()


def provider_label():
    return _LABELS.get(provider(), "connected records")


def is_live():
    """True once a real aggregator is configured (vs. the built-in sandbox)."""
    return provider() in ("metriport", "1uphealth", "1up") and bool(
        os.environ.get("RECORDS_API_KEY"))


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _api_base():
    explicit = os.environ.get("RECORDS_API_BASE", "").strip()
    if explicit:
        return explicit.rstrip("/")
    p = provider()
    if p in ("1uphealth", "1up"):
        return os.environ.get("ONEUP_API_BASE", "https://api.1up.health").strip().rstrip("/")
    return os.environ.get("METRIPORT_API_BASE", "https://api.metriport.com").strip().rstrip("/")


def _api_key():
    return os.environ.get("RECORDS_API_KEY", "").strip()


def _api_timeout():
    try:
        return max(5, int(os.environ.get("RECORDS_TIMEOUT_SECONDS", "30")))
    except Exception:
        return 30


def _path(name, default):
    fallback = _METRIPORT_PATH_DEFAULTS.get(name, default)
    raw = os.environ.get(name, fallback)
    p = str(raw or fallback or "").strip()
    if not p:
        return str(default).strip()
    if not p.startswith("/"):
        p = "/" + p
    return p


def _json_req(method, path, payload=None, query=None):
    url = _api_base() + path
    if query:
        url += ("?" + urllib.parse.urlencode(query))
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    key = _api_key()
    key_header = os.environ.get("RECORDS_API_KEY_HEADER", "Authorization").strip()
    key_prefix = os.environ.get("RECORDS_API_KEY_PREFIX", "Bearer ").strip()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "BridgeMD/records",
    }
    if key:
        if key_header.lower() == "authorization":
            headers[key_header] = (key if key.startswith("Bearer ")
                                   else (key_prefix + key))
        else:
            headers[key_header] = key
    req = urllib.request.Request(
        url,
        data=body,
        method=method.upper(),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=_api_timeout()) as r:
            raw = r.read().decode("utf-8") or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            detail = str(e)
        raise fhir.FhirError(f"Records provider error ({e.code}). {detail[:240]}")
    except Exception:
        raise fhir.FhirError("Couldn't reach the records provider right now.")


def _split_name(full_name):
    bits = [b for b in (full_name or "").strip().split() if b]
    if not bits:
        return "", ""
    if len(bits) == 1:
        return bits[0], ""
    return bits[0], " ".join(bits[1:])


def _normalize_gender(raw):
    v = (raw or "").strip().lower()
    if v in ("male", "m"):
        return "male"
    if v in ("female", "f"):
        return "female"
    return "unknown"


def _resource_entries(bundle, kind):
    out = []
    for e in (bundle or {}).get("entry", []):
        r = e.get("resource", {})
        if r.get("resourceType") == kind:
            out.append(r)
    return out


def _cc_text(cc):
    if not cc:
        return ""
    if cc.get("text"):
        return str(cc.get("text")).strip()
    for c in cc.get("coding", []):
        if c.get("display"):
            return str(c.get("display")).strip()
    return ""


def _age(birth):
    try:
        y, m, d = (str(birth).split("-") + ["1", "1"])[:3]
        b = dt.date(int(y), int(m), int(d))
        t = dt.date.today()
        return t.year - b.year - ((t.month, t.day) < (b.month, b.day))
    except Exception:
        return None


def _profile_from_fhir(bundle):
    patients = _resource_entries(bundle, "Patient")
    p = patients[0] if patients else {}
    sex = _normalize_gender(p.get("gender"))
    age = _age(p.get("birthDate", ""))

    conds, cseen = [], set()
    for c in _resource_entries(bundle, "Condition"):
        status = _cc_text(c.get("clinicalStatus")).lower()
        if status and status not in ("active", "recurrence", "relapse"):
            continue
        name = _cc_text(c.get("code"))
        k = name.lower().strip()
        if name and k not in cseen:
            cseen.add(k)
            conds.append(name)

    meds, mseen = [], set()
    for m in _resource_entries(bundle, "MedicationRequest"):
        status = str(m.get("status", "")).lower().strip()
        if status and status not in ("active", "completed"):
            continue
        name = _cc_text(m.get("medicationCodeableConcept"))
        k = name.lower().strip()
        if name and k not in mseen:
            mseen.add(k)
            meds.append(name)

    labs, lseen = [], set()
    for o in _resource_entries(bundle, "Observation"):
        cat = " ".join(
            (c.get("code", "") or "") for cc in (o.get("category") or [])
            for c in (cc.get("coding") or [])
        ).lower()
        if "laboratory" not in cat and "vital-signs" not in cat:
            continue
        name = _cc_text(o.get("code"))
        when = (o.get("effectiveDateTime") or "")[:10]
        if not name:
            continue
        val = ""
        if "valueQuantity" in o and o["valueQuantity"].get("value") is not None:
            vq = o["valueQuantity"]
            val = f"{vq.get('value')} {vq.get('unit','')}".strip()
        elif o.get("component"):
            parts = []
            for comp in o.get("component", []):
                cn = _cc_text(comp.get("code"))
                q = comp.get("valueQuantity", {})
                if q.get("value") is not None:
                    parts.append(f"{cn} {q.get('value')}{q.get('unit','')}".strip())
            val = ", ".join(parts)
        if not val:
            continue
        line = f"{name}: {val}" + (f" ({when})" if when else "")
        k = name.lower()
        if k in lseen:
            continue
        lseen.add(k)
        labs.append(line)
        if len(labs) >= 12:
            break

    prof = {
        "age": age,
        "sex": sex,
        "conditions": conds[:15],
        "meds": meds[:20],
        "labs": labs[:12],
    }
    prof["summary"] = fhir.profile_to_text(prof)
    have = int(bool(age is not None)) + int(sex in ("male", "female")) + \
        int(bool(prof["conditions"])) + int(bool(prof["meds"])) + int(bool(prof["labs"]))
    prof["completeness_score"] = int(round((have / 5.0) * 100))
    return prof


def _metri_create_or_get_patient(patient_user, applicant_token):
    """Create/update a Metriport patient and return provider patient id."""
    first, last = _split_name((patient_user or {}).get("full_name", ""))
    payload = {
        "externalId": applicant_token,
        "firstName": first or "Patient",
        "lastName": last or "User",
        "email": (patient_user or {}).get("email", ""),
        "genderAtBirth": _normalize_gender((patient_user or {}).get("sex", "")),
    }
    facility_id = os.environ.get("METRIPORT_FACILITY_ID", "").strip()
    if facility_id:
        payload["facilityId"] = facility_id
    path = _path("METRIPORT_CREATE_PATIENT_PATH",
                 _METRIPORT_PATH_DEFAULTS["METRIPORT_CREATE_PATIENT_PATH"])
    data = _json_req("POST", path, payload=payload)
    pid = str(data.get("id") or data.get("patientId") or "").strip()
    if not pid:
        raise fhir.FhirError(
            "Records provider did not return a patient id. Check facility + patient mapping.")
    return pid


def _metri_start_network_query(external_patient_id):
    payload = {"patientId": external_patient_id}
    path = _path("METRIPORT_START_NETWORK_QUERY_PATH",
                 _METRIPORT_PATH_DEFAULTS["METRIPORT_START_NETWORK_QUERY_PATH"])
    data = _json_req("POST", path, payload=payload)
    qid = str(data.get("id") or data.get("queryId") or data.get("networkQueryId") or "").strip()
    if not qid:
        raise fhir.FhirError(
            "Records provider did not return a query id. Check network-query endpoint.")
    return qid


def _metri_get_consolidated_bundle(external_patient_id):
    # 1) Try direct consolidated endpoint.
    p1 = _path("METRIPORT_GET_CONSOLIDATED_PATH",
               _METRIPORT_PATH_DEFAULTS["METRIPORT_GET_CONSOLIDATED_PATH"])
    try:
        data = _json_req("GET", p1.format(patient_id=urllib.parse.quote(external_patient_id)))
        if isinstance(data, dict) and data.get("resourceType") == "Bundle":
            return data
        if isinstance(data, dict):
            b = data.get("bundle") or data.get("data")
            if isinstance(b, dict) and b.get("resourceType") == "Bundle":
                return b
    except Exception:
        pass

    # 2) Fallback: start consolidated query + pull status result.
    p2 = _path("METRIPORT_START_CONSOLIDATED_PATH",
               _METRIPORT_PATH_DEFAULTS["METRIPORT_START_CONSOLIDATED_PATH"])
    started = _json_req("POST", p2, payload={"patientId": external_patient_id})
    cqid = str(started.get("id") or started.get("queryId") or "").strip()
    if not cqid:
        raise fhir.FhirError(
            "Consolidated data query did not return an id. Check provider endpoint paths.")
    p3 = _path("METRIPORT_GET_CONSOLIDATED_QUERY_PATH",
               _METRIPORT_PATH_DEFAULTS["METRIPORT_GET_CONSOLIDATED_QUERY_PATH"])
    got = _json_req("GET", p3.format(query_id=urllib.parse.quote(cqid)))
    bundle = got.get("bundle") or got.get("data") or {}
    if isinstance(bundle, dict) and bundle.get("resourceType") == "Bundle":
        return bundle
    raise fhir.FhirError(
        "Consolidated data not ready yet. The sync is still processing.")


def connect(patient_user=None, applicant_token=""):
    """Start records connection/sync and return profile-or-sync-state dict."""
    if is_live():
        if not applicant_token:
            raise fhir.FhirError("Missing patient identity for records sync.")
        pid = _metri_create_or_get_patient(patient_user or {}, applicant_token)
        qid = _metri_start_network_query(pid)
        return {
            "provider": provider_label(),
            "sync_status": "syncing",
            "source_status": "network_query_started",
            "external_patient_id": pid,
            "external_query_id": qid,
            "last_sync_at": _now(),
            "last_sync_error": "",
            "summary": "",
            "conditions": [],
            "meds": [],
            "labs": [],
            "completeness_score": 0,
        }

    # Sandbox: grab a synthetic patient that actually has clinical data.
    pid = fhir.pick_sample_patient()
    prof = fhir.fetch_patient_profile(pid)
    prof["provider"] = provider_label()
    prof["summary"] = fhir.profile_to_text(prof)
    prof["sync_status"] = "connected"
    prof["source_status"] = "sandbox_ready"
    prof["external_patient_id"] = pid
    prof["external_query_id"] = ""
    prof["last_sync_at"] = _now()
    prof["last_sync_error"] = ""
    prof["completeness_score"] = 100 if prof.get("conditions") else 70
    return prof


def refresh(external_patient_id):
    """Start a new network refresh for an already-connected live patient."""
    if not is_live() or not external_patient_id:
        return ""
    return _metri_start_network_query(external_patient_id)


def pull_latest(external_patient_id):
    """Pull latest consolidated profile from provider for a patient id."""
    if provider() not in ("metriport",):
        raise fhir.FhirError("Live pull is only wired for Metriport right now.")
    if not external_patient_id:
        raise fhir.FhirError("Missing external patient id.")
    bundle = _metri_get_consolidated_bundle(external_patient_id)
    prof = _profile_from_fhir(bundle)
    prof["provider"] = provider_label()
    prof["sync_status"] = "connected"
    prof["source_status"] = "consolidated_ready"
    prof["external_patient_id"] = external_patient_id
    prof["external_query_id"] = ""
    prof["last_sync_at"] = _now()
    prof["last_sync_error"] = ""
    return prof


def _first_non_empty(*vals):
    for v in vals:
        if v is None:
            continue
        t = str(v).strip()
        if t:
            return t
    return ""


def _as_dict(v):
    return v if isinstance(v, dict) else {}


def _as_status_blob(v):
    if isinstance(v, dict):
        return " ".join(str(x).strip() for x in v.values() if str(x).strip())
    if isinstance(v, (list, tuple)):
        return " ".join(str(x).strip() for x in v if str(x).strip())
    return str(v or "").strip()


def _status_kind(*parts):
    blob = " ".join(str(p or "").strip().lower() for p in parts if p).strip()
    if not blob:
        return "unknown"
    if any(tok in blob for tok in _ERROR_TOKENS):
        return "error"
    if any(tok in blob for tok in _DONE_TOKENS):
        return "done"
    if any(tok in blob for tok in _SYNCING_TOKENS):
        return "syncing"
    return "unknown"


def webhook_event_context(payload, headers=None):
    """Normalize webhook payload variants into one stable event context dict."""
    p = _as_dict(payload)
    h = headers or {}
    evt = _as_dict(p.get("event"))
    data = _as_dict(p.get("data"))
    detail = _as_dict(p.get("detail"))
    query = _as_dict(p.get("query"))
    patient = _as_dict(p.get("patient"))
    resource = _as_dict(p.get("resource"))

    event = _first_non_empty(
        p.get("type"), p.get("eventType"), p.get("name"), p.get("topic"),
        evt.get("type"), evt.get("name"), evt.get("eventType"),
        detail.get("type"), detail.get("eventType"),
    ).lower()
    source_status = _first_non_empty(
        p.get("status"), p.get("sourceStatus"), p.get("state"), p.get("result"),
        evt.get("status"), evt.get("state"),
        detail.get("status"), detail.get("state"),
        data.get("status"), data.get("state"),
        query.get("status"), query.get("state"),
        resource.get("status"), resource.get("state"),
    )
    ext_qid = _first_non_empty(
        p.get("queryId"), p.get("networkQueryId"), query.get("id"),
        data.get("queryId"), data.get("networkQueryId"),
        detail.get("queryId"), detail.get("networkQueryId"),
        resource.get("queryId"), resource.get("networkQueryId"),
    )
    ext_pid = _first_non_empty(
        p.get("patientId"), p.get("externalPatientId"),
        patient.get("id"), patient.get("patientId"),
        data.get("patientId"), data.get("externalPatientId"),
        detail.get("patientId"), detail.get("externalPatientId"),
        query.get("patientId"), query.get("externalPatientId"),
        resource.get("patientId"), resource.get("externalPatientId"),
    )
    event_id = _first_non_empty(
        p.get("eventId"),
        evt.get("id"),
        h.get("X-Webhook-Id"), h.get("X-Event-Id"), h.get("x-webhook-id"),
        h.get("x-event-id"),
    )

    kind = _status_kind(event, source_status, _as_status_blob(p.get("statusDetail")))
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "event": event,
                "status": source_status.lower(),
                "query": ext_qid,
                "patient": ext_pid,
                "payload": p,
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:20]
    idempotency_key = f"metriport:{event_id or fingerprint}"
    return {
        "event": event,
        "source_status": source_status,
        "external_query_id": ext_qid,
        "external_patient_id": ext_pid,
        "event_id": event_id,
        "idempotency_key": idempotency_key,
        "is_done": kind == "done",
        "is_error": kind == "error",
        "is_syncing": kind == "syncing",
    }


def summary_text(prof):
    """De-identified summary the study team pre-screens from."""
    if not prof:
        return ""
    if prof.get("summary"):
        return prof["summary"]
    return fhir.profile_to_text(prof)


_STOP = {"the", "and", "for", "with", "any", "all", "type", "study", "trial",
         "patient", "patients", "history", "diagnosis", "diagnosed", "current",
         "currently", "prior", "disease", "disorder", "syndrome"}


def _tokens(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 3 and w not in _STOP}


def autofill_eligibility(prof, elig):
    """Use the connected record to confirm 'unknown' inclusion criteria.

    Conservative by design: presence of a matching condition/medication can move
    an item from 'unknown' -> 'met'. We never auto-*exclude* from a record (that
    stays a human decision at the screening visit), so this only ever helps.

    Returns (new_elig, filled_count).
    """
    if not prof or not isinstance(elig, dict):
        return elig, 0
    have = _tokens(" ".join(prof.get("conditions", []) + prof.get("meds", [])))
    # Widen with RxNorm ingredients so a brand name on the record ("Ozempic")
    # still matches a criterion written with the generic ("semaglutide" / GLP-1).
    try:
        have |= codes.med_ingredients(prof.get("meds", []))
    except Exception:
        pass
    if not have:
        return elig, 0

    unknown = list(elig.get("unknown", []) or [])
    met = list(elig.get("met", []) or [])
    still_unknown, filled = [], 0
    for crit in unknown:
        if _tokens(crit) & have:
            met.append(crit + " (confirmed from your connected record)")
            filled += 1
        else:
            still_unknown.append(crit)

    if not filled:
        return elig, 0
    new = dict(elig)
    new["met"] = met
    new["unknown"] = still_unknown
    return new, filled
