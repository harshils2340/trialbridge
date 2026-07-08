"""Patient-mediated health records: connect once, auto-fill everything after.

The patient authorizes access to their records a single time. We pull a
structured, de-identified profile (age/sex + problems + meds + labs) and store
it against their applicant token. From then on, every application they submit is
auto-filled from that profile with zero extra work.

Providers
---------
- "sandbox" (default): pulls a synthetic patient from the SMART reference FHIR
  server via fhir.py, so the whole connect-once + auto-fill loop works today
  with no account, no keys, no PHI.
- "metriport" / "1uphealth": real patient-mediated aggregators. Wiring is the
  same shape (OAuth authorize -> pull FHIR R4), gated behind an API key. Flip
  RECORDS_PROVIDER + set the key to go live; the profile shape is identical, so
  nothing downstream changes.
"""
import os
import re

import codes
import fhir

PROVIDER = os.environ.get("RECORDS_PROVIDER", "sandbox").lower()

_LABELS = {
    "sandbox": "SMART Health IT (sandbox)",
    "metriport": "Metriport",
    "1uphealth": "1upHealth",
    "1up": "1upHealth",
}


def provider_label():
    return _LABELS.get(PROVIDER, "connected records")


def is_live():
    """True once a real aggregator is configured (vs. the built-in sandbox)."""
    return PROVIDER in ("metriport", "1uphealth", "1up") and bool(
        os.environ.get("RECORDS_API_KEY"))


def connect():
    """Authorize + pull the patient's chart once. Returns a profile dict:

        {"provider", "age", "sex", "conditions", "meds", "labs", "summary"}

    Raises fhir.FhirError on failure so the caller can show a friendly message.
    """
    if is_live():
        # Real aggregator path. Same result shape as the sandbox; the aggregator
        # handles the patient's OAuth login and returns FHIR R4 we parse here.
        # (Left as the single integration point to implement when going live.)
        raise fhir.FhirError(
            "Live records provider is configured but not yet enabled.")

    # Sandbox: grab a synthetic patient that actually has clinical data.
    pid = fhir.pick_sample_patient()
    prof = fhir.fetch_patient_profile(pid)
    prof["provider"] = provider_label()
    prof["summary"] = fhir.profile_to_text(prof)
    return prof


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
