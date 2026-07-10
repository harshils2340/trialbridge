"""Patient-mediated records support for BridgeMD.

Current mode is preview-only synthetic records (SMART sandbox profile) used for
workflow demos and autofill behavior. Live aggregator pull is intentionally
disabled in this codebase.
"""
import datetime as dt
import os
import re

import codes
import fhir

_LABELS = {
    "sandbox": "SMART Health IT (sandbox)",
}


def provider():
    return "sandbox"


def provider_label():
    return _LABELS["sandbox"]


def live_requested():
    return False


def live_usecase_allowed():
    return False


def live_blocked_reason():
    if os.environ.get("RECORDS_API_KEY", "").strip():
        return ("Live records aggregation is disabled. "
                "Use preview records or upload records manually.")
    return ""


def is_live():
    return False


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


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


def connect(patient_user=None, applicant_token=""):
    """Connect preview records and return a synthetic but realistic profile."""
    _ = (patient_user, applicant_token)
    pid = fhir.pick_sample_patient()
    prof = fhir.fetch_patient_profile(pid)
    prof["provider"] = _LABELS["sandbox"]
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
    """Preview mode has no async refresh pipeline."""
    _ = external_patient_id
    return ""


def pull_latest(external_patient_id):
    _ = external_patient_id
    raise fhir.FhirError("Live records pull is disabled in preview mode.")


def webhook_event_context(payload, headers=None):
    """Compatibility stub: live webhooks are disabled in preview mode."""
    _ = (payload, headers)
    return {
        "event": "",
        "source_status": "",
        "external_query_id": "",
        "external_patient_id": "",
        "event_id": "",
        "idempotency_key": "",
        "is_done": False,
        "is_error": True,
        "is_syncing": False,
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
