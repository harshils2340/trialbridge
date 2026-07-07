"""EHR import via FHIR (SMART on FHIR style).

Given a patient's FHIR id, pull their chart from a FHIR R4 server and build a
DE-IDENTIFIED clinical summary (age/sex + problems + meds + recent labs/vitals)
ready to drop into the search box. No names, DOB, addresses, or identifiers are
included.

Defaults to the public SMART reference sandbox (open, synthetic Synthea data,
no auth) so this works out of the box. Point at a real server with:

    FHIR_BASE=https://your-fhir-server/r4

In production this is where a SMART on FHIR "launch from EHR" (OAuth) would slot
in; the summary-building logic below stays the same.
"""
import datetime as dt
import json
import os
import urllib.parse
import urllib.request

FHIR_BASE = os.environ.get("FHIR_BASE", "https://r4.smarthealthit.org").rstrip("/")
_TIMEOUT = 20


class FhirError(Exception):
    pass


def _get(path):
    url = f"{FHIR_BASE}/{path}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/fhir+json, application/json",
        "User-Agent": "TrialBridge/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FhirError("No patient found with that ID on the connected EHR.")
        raise FhirError("The EHR server returned an error. Try again.")
    except Exception:
        raise FhirError("Couldn't reach the EHR server. Try again in a moment.")


def _entries(bundle):
    return [e.get("resource", {}) for e in (bundle or {}).get("entry", [])
            if e.get("resource")]


def _num(v):
    """Round FHIR numeric values to a clinically sane precision."""
    if isinstance(v, float):
        return f"{v:.1f}".rstrip("0").rstrip(".")
    return f"{v}"


def _cc_text(cc):
    """Human label from a FHIR CodeableConcept."""
    if not cc:
        return ""
    if cc.get("text"):
        return cc["text"].strip()
    for c in cc.get("coding", []):
        if c.get("display"):
            return c["display"].strip()
    return ""


def _age(birth):
    try:
        y, m, d = (birth.split("-") + ["1", "1"])[:3]
        b = dt.date(int(y), int(m), int(d))
        t = dt.date.today()
        return t.year - b.year - ((t.month, t.day) < (b.month, b.day))
    except Exception:
        return None


def pick_sample_patient():
    """Return a patient id from the server that actually has clinical data."""
    bundle = _get("Patient?_count=20&_sort=-_lastUpdated&_format=json")
    for p in _entries(bundle):
        pid = p.get("id")
        if not pid:
            continue
        conds = _get(f"Condition?patient={pid}&_count=1&_format=json")
        if _entries(conds):
            return pid
    ents = _entries(bundle)
    if ents:
        return ents[0].get("id")
    raise FhirError("No sample patients available right now.")


def _observations(pid):
    """Recent labs + vitals as short 'Name value unit' strings."""
    out = []
    for cat in ("laboratory", "vital-signs"):
        b = _get(f"Observation?patient={pid}&category={cat}"
                 f"&_sort=-date&_count=40&_format=json")
        for o in _entries(b):
            name = _cc_text(o.get("code"))
            if not name:
                continue
            when = (o.get("effectiveDateTime") or "")[:10]
            if "valueQuantity" in o:
                v = o["valueQuantity"]
                if v.get("value") is None:
                    continue
                out.append((when, f"{name}: {_num(v['value'])} "
                            f"{v.get('unit', '')}".strip()))
            elif o.get("component"):  # e.g. blood pressure
                parts = []
                for c in o["component"]:
                    cn = _cc_text(c.get("code"))
                    v = c.get("valueQuantity", {})
                    if v.get("value") is not None:
                        parts.append(f"{cn} {_num(v['value'])}{v.get('unit', '')}")
                if parts:
                    out.append((when, f"{name}: " + ", ".join(parts)))
    # newest first, de-dup by measurement name, cap the list
    out.sort(key=lambda x: x[0], reverse=True)
    seen, keep = set(), []
    for when, s in out:
        key = s.split(":")[0]
        if key in seen:
            continue
        seen.add(key)
        keep.append(s + (f" ({when})" if when else ""))
        if len(keep) >= 10:
            break
    return keep


def fetch_patient_summary(pid):
    """Return (summary_text, top_condition). Raises FhirError on failure."""
    pid = (pid or "").strip()
    if not pid:
        raise FhirError("Enter a patient ID.")

    patient = _get(f"Patient/{urllib.parse.quote(pid)}?_format=json")
    if patient.get("resourceType") != "Patient":
        raise FhirError("No patient found with that ID on the connected EHR.")
    age = _age(patient.get("birthDate", ""))
    sex = (patient.get("gender") or "unknown").lower()

    conds, seen = [], set()
    cb = _get(f"Condition?patient={pid}&_count=100&_format=json")
    for c in _entries(cb):
        status = _cc_text(c.get("clinicalStatus")).lower()
        if status and status not in ("active", "recurrence", "relapse"):
            continue
        name = _cc_text(c.get("code"))
        if name and name.lower() not in seen:
            seen.add(name.lower())
            conds.append(name)

    meds, mseen = [], set()
    for status in ("active", ""):
        q = f"MedicationRequest?patient={pid}&_count=100&_format=json"
        if status:
            q += f"&status={status}"
        for m in _entries(_get(q)):
            name = _cc_text(m.get("medicationCodeableConcept"))
            if name and name.lower() not in mseen:
                mseen.add(name.lower())
                meds.append(name)
        if meds:
            break

    labs = _observations(pid)

    lines = [f"AGE: {age if age is not None else 'unknown'}",
             f"SEX: {sex if sex in ('male', 'female') else 'unknown'}"]
    if conds:
        lines.append("Active problems: " + "; ".join(conds[:12]) + ".")
    if meds:
        lines.append("Medications: " + "; ".join(meds[:15]) + ".")
    if labs:
        lines.append("Recent labs/vitals: " + "; ".join(labs) + ".")
    if not (conds or meds or labs):
        lines.append("No coded problems, medications, or results found for this "
                     "patient in the EHR.")
    return "\n".join(lines), (conds[0] if conds else "")
