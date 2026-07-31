#!/usr/bin/env python3
"""Phase-0 trial matcher: paste a patient case -> ranked clinical trials + why.

This is the concierge/validation tool, NOT the platform. Pipeline:
  1. FETCH all recruiting trials from ClinicalTrials.gov API v2 (paginated, free).
  2. GATE deterministically on structured fields (age, sex) -- disqualify clear
     mismatches with NO LLM cost, so we never spend tokens (or lose trust) on a
     child matched to an adult-only trial.
  3. MATCH each survivor's free-text eligibility against the patient with an LLM,
     returning met / not-met / unknown per criterion. Output is normalized so it
     can never contradict itself (e.g. "likely_eligible" with a listed blocker).
  4. RANK and print a clean one-pager you can show a physician.

The LLM is instructed to say "unknown" rather than guess -- trust dies on one
confidently-wrong match, so calibration matters more than coverage here.

Usage:
  1. Put a de-identified patient summary in matcher/patient_case.txt
     (a leading "AGE: 42 / SEX: female" header makes the gate most reliable).
  2. export LLM_API_KEY="sk-..."           # OpenAI or any OpenAI-compatible API
     export LLM_MODEL="gpt-4o-mini"        # or whatever model you use
  3. python3 match_trials.py --condition "lupus nephritis" --country Canada
     Add --require-site to keep only trials with a site in --country.

  Offline checks (no key/network):
     python3 match_trials.py --selftest
     MATCH=0 python3 match_trials.py --condition "lupus nephritis"  # fetch+gate
"""
import argparse
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
CT_API = "https://clinicaltrials.gov/api/v2/studies"


def _ct_get_json(url, timeout=40, attempts=4):
    """GET JSON from ClinicalTrials.gov with retry + backoff.

    CT.gov rate-limits bursty traffic (HTTP 429) and occasionally 5xx/times out.
    Without a retry a single blip returns an empty result set, so a real patient
    sees "no trials" for a query that has hundreds. We back off (honoring a
    Retry-After header when present) and retry a few times before giving up. On
    total failure we re-raise so the caller can decide (it won't be cached as an
    empty success).
    """
    last = None
    for attempt in range(attempts):
        req = urllib.request.Request(
            url, headers={"User-Agent": "trial-matcher/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
            wait = attempt + 1
            ra = e.headers.get("Retry-After") if e.headers else None
            if ra:
                try:
                    wait = max(wait, int(float(ra)))
                except (TypeError, ValueError):
                    pass
            wait = min(wait, 30) + 0.25 * attempt  # cap + jitter-ish
        except Exception as e:  # URLError, timeout, JSON decode
            last = e
            wait = 2 ** attempt
        if attempt < attempts - 1:
            time.sleep(wait)
    raise last if last else RuntimeError("CT.gov fetch failed")

def _first_env(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return ""


# Accept the common spellings people actually use for the key.
LLM_API_KEY = _first_env(
    "LLM_API_KEY", "OPENAI_API_KEY", "OPENAI_KEY",
    "OPEN_API_KEY", "OPEN_AI_KEY", "OPEN_AI_API_KEY")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
DO_MATCH = os.environ.get("MATCH", "1") != "0"

VERDICT_RANK = {"likely_eligible": 0, "possible": 1, "unlikely": 2, "error": 3}


# --------------------------------------------------------------------------- #
# 1. FETCH
# --------------------------------------------------------------------------- #
# Statuses we treat as "a patient can act on this now or very soon". Including
# NOT_YET_RECRUITING lets people find and register interest in trials about to
# open (real added quantity - ~30-50% more studies for many conditions) without
# surfacing closed/completed ones. Callers that want strictly-open studies (SEO
# pages) filter to RECRUITING afterwards.
OPEN_STATUSES = "RECRUITING,NOT_YET_RECRUITING"


def fetch_trials(condition, max_n=300, geo=None, intervention="", term="",
                 statuses=OPEN_STATUSES):
    """Fetch open trials (recruiting + not-yet-recruiting), paginating so we
    don't silently miss trials.

    `geo`, if given, is a ClinicalTrials.gov geo filter like
    'distance(43.65,-79.38,100mi)' so the API only returns studies with a site
    within that radius. `intervention`, if given, searches by drug/intervention
    (query.intr) - e.g. "semaglutide" - which is how people search the GLP-1 /
    peptide trend. `term`, if given, is a full-text (query.term) search across the
    whole study record - including the eligibility criteria - so a symptom like
    "trouble sleeping" surfaces trials that mention it even when it isn't the
    trial's condition label. `statuses` is a comma-separated CT.gov
    overallStatus filter (default recruiting + not-yet-recruiting).
    """
    trials, token = [], None
    while len(trials) < max_n:
        params = {
            "filter.overallStatus": statuses,
            "pageSize": "100",
            "format": "json",
        }
        if condition:
            params["query.cond"] = condition
        if intervention:
            params["query.intr"] = intervention
        if term:
            params["query.term"] = term
        if geo:
            params["filter.geo"] = geo
        if token:
            params["pageToken"] = token
        url = f"{CT_API}?{urllib.parse.urlencode(params)}"
        data = _ct_get_json(url, timeout=40)
        trials.extend(extract_trial(s) for s in data.get("studies", []))
        token = data.get("nextPageToken")
        if not token:
            break
    return trials[:max_n]


def count_trials(condition="", intervention="", geo=None):
    """Return the number of currently-RECRUITING trials matching the query.

    Uses CT.gov's `countTotal=true` so we get the real total in a single tiny
    request (pageSize=1) instead of paging every study. This is a cheap,
    external "how much active research is there right now" signal - useful for
    ranking trending drugs/conditions by real trial volume. Returns 0 on error.
    """
    params = {
        "filter.overallStatus": "RECRUITING",
        "countTotal": "true",
        "pageSize": "1",
        "format": "json",
    }
    if condition:
        params["query.cond"] = condition
    if intervention:
        params["query.intr"] = intervention
    if geo:
        params["filter.geo"] = geo
    url = f"{CT_API}?{urllib.parse.urlencode(params)}"
    try:
        data = _ct_get_json(url, timeout=30)
        return int(data.get("totalCount") or 0)
    except Exception:
        return 0


def extract_trial(study):
    p = study.get("protocolSection", {})
    ident = p.get("identificationModule", {})
    elig = p.get("eligibilityModule", {})
    clm = p.get("contactsLocationsModule", {})
    locs = clm.get("locations", [])
    status_mod = p.get("statusModule", {})
    # CT.gov's own canonical vocabulary: MeSH terms + the ancestor hierarchy it
    # assigns to each study. Free structured signal for concept-level matching.
    derived = study.get("derivedSection", {})
    cbm = derived.get("conditionBrowseModule", {})
    ibm = derived.get("interventionBrowseModule", {})
    mesh_terms = [m.get("term", "") for m in cbm.get("meshes", []) if m.get("term")]
    mesh_ancestors = [a.get("term", "") for a in cbm.get("ancestors", []) if a.get("term")]
    intr_mesh = [m.get("term", "") for m in ibm.get("meshes", []) if m.get("term")]
    interventions = [
        {"type": i.get("type", ""), "name": i.get("name", ""),
         "description": i.get("description", ""),
         "otherNames": i.get("otherNames", []) or []}
        for i in p.get("armsInterventionsModule", {}).get("interventions", [])
        if i.get("name")
    ]
    return {
        "nctId": ident.get("nctId", ""),
        "title": ident.get("briefTitle", ""),
        "studyType": p.get("designModule", {}).get("studyType", ""),
        "phase": ", ".join(p.get("designModule", {}).get("phases", []) or []),
        "overallStatus": status_mod.get("overallStatus", ""),
        "briefSummary": p.get("descriptionModule", {}).get("briefSummary", ""),
        "detailedDescription": p.get("descriptionModule", {})
        .get("detailedDescription", ""),
        "leadSponsor": p.get("sponsorCollaboratorsModule", {})
                        .get("leadSponsor", {}).get("name", ""),
        "enrollment": p.get("designModule", {})
                       .get("enrollmentInfo", {}).get("count", ""),
        "startDate": status_mod.get("startDateStruct", {}).get("date", ""),
        "completionDate": status_mod.get("primaryCompletionDateStruct", {})
                           .get("date", ""),
        "conditions": p.get("conditionsModule", {}).get("conditions", []),
        "meshTerms": mesh_terms,
        "meshAncestors": mesh_ancestors,
        "intrMesh": intr_mesh,
        "interventions": interventions,
        "criteria": elig.get("eligibilityCriteria", ""),
        "sex": elig.get("sex", ""),
        "minAge": elig.get("minimumAge", ""),
        "maxAge": elig.get("maximumAge", ""),
        "healthyVolunteers": elig.get("healthyVolunteers", ""),
        "centralContacts": clm.get("centralContacts", []),
        "officials": clm.get("overallOfficials", []),
        "locations": [
            {"facility": l.get("facility", ""), "city": l.get("city", ""),
             "state": l.get("state", ""), "country": l.get("country", ""),
             "status": l.get("status", ""), "contacts": l.get("contacts", []),
             "lat": (l.get("geoPoint") or {}).get("lat"),
             "lon": (l.get("geoPoint") or {}).get("lon")}
            for l in locs
        ],
    }


def fetch_study(nct):
    """Fetch a single study by NCT id and return it as an extracted trial dict."""
    url = f"{CT_API}/{urllib.parse.quote(nct)}?format=json"
    req = urllib.request.Request(url, headers={"User-Agent": "trial-matcher/0.1"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return extract_trial(json.load(r))


# --------------------------------------------------------------------------- #
# 1b. FETCH - ISRCTN (UK/international registry). Second source for breadth.
# --------------------------------------------------------------------------- #
ISRCTN_API = "https://www.isrctn.com/api/query/format/default"
ISRCTN_NS = "{http://www.67bricks.com/isrctn}"
_NCT_RE = re.compile(r"NCT\d{8}")
_PHASE_MAP = {  # ISRCTN "Phase II" -> CT.gov-style "PHASE2" so badges/filters agree
    "PHASE I": "PHASE1", "PHASE II": "PHASE2", "PHASE III": "PHASE3",
    "PHASE IV": "PHASE4", "PHASE I/II": "PHASE1, PHASE2",
    "PHASE II/III": "PHASE2, PHASE3", "EARLY PHASE 1": "EARLY_PHASE1",
}


def _isrctn_phase(raw):
    p = (raw or "").strip().upper().replace("PHASE-", "PHASE ")
    return _PHASE_MAP.get(p, "")


def _isrctn_active_status(start, end):
    """Map ISRCTN recruitment dates to a CT.gov-style status. None if ended
    (so we never surface a trial that's already stopped recruiting)."""
    today = time.strftime("%Y-%m-%d")
    s = (start or "")[:10]
    e = (end or "")[:10]
    if e and e < today:
        return None                      # recruitment window closed
    if s and s > today:
        return "NOT_YET_RECRUITING"
    return "RECRUITING"


def _txt(el, path):
    node = el.find(path)
    return (node.text or "").strip() if node is not None and node.text else ""


def _isrctn_to_trial(full, ns=ISRCTN_NS):
    """Map one ISRCTN <fullTrial> element to the same dict shape as
    extract_trial(), so the rest of the pipeline treats both sources uniformly.
    Returns None for trials whose recruitment window has already closed."""
    if full.find(f"{ns}trial") is None:
        return None
    # Search relative to <fullTrial>: some sections (sponsor, contacts) sit as
    # siblings of <trial>, not inside it, so a trial-relative XPath misses them.
    trial_el = full
    raw_xml = "".join(full.itertext())          # for NCT cross-ref lookup
    start = _txt(trial_el, f".//{ns}recruitmentStart")
    end = _txt(trial_el, f".//{ns}recruitmentEnd")
    status = _isrctn_active_status(start, end)
    if status is None:
        return None

    isrctn_id = _txt(trial_el, f".//{ns}isrctn") or ""
    reg_id = f"ISRCTN{isrctn_id}" if isrctn_id else ""
    nct = None
    m = _NCT_RE.search(raw_xml)                  # ISRCTN often lists the NCT
    if m:
        nct = m.group(0)

    title = (_txt(trial_el, f".//{ns}title")
             or _txt(trial_el, f".//{ns}scientificTitle"))
    summary = (_txt(trial_el, f".//{ns}plainEnglishSummary")
               or _txt(trial_el, f".//{ns}studyHypothesis"))
    conditions = [d.text.strip() for d in
                  trial_el.findall(f".//{ns}condition/{ns}description")
                  if d.text and d.text.strip()]
    inclusion = _txt(trial_el, f".//{ns}inclusion")
    exclusion = _txt(trial_el, f".//{ns}exclusion")
    criteria = ""
    if inclusion:
        criteria += "Inclusion criteria:\n" + inclusion
    if exclusion:
        criteria += ("\n\n" if criteria else "") + "Exclusion criteria:\n" + exclusion

    gender = (_txt(trial_el, f".//{ns}gender") or "All").upper()
    sex = {"MALE": "MALE", "FEMALE": "FEMALE"}.get(gender, "ALL")
    lo = _txt(trial_el, f".//{ns}lowerAgeLimit")
    hi = _txt(trial_el, f".//{ns}upperAgeLimit")

    def _age(v):
        v = (v or "").strip()
        if not v or v.lower() in ("not specified", "no limit"):
            return ""
        return v if re.search(r"[a-zA-Z]", v) else f"{v} Years"

    hv_raw = _txt(trial_el, f".//{ns}healthyVolunteersAllowed").lower()
    healthy = "YES" if hv_raw in ("true", "yes", "y") else ""

    countries = [c.text.strip() for c in
                 trial_el.findall(f".//{ns}recruitmentCountries/{ns}country")
                 if c.text and c.text.strip()]
    locations = [{"facility": "", "city": "", "state": "", "country": c,
                  "status": status, "contacts": [], "lat": None, "lon": None}
                 for c in countries]

    contacts = []
    for c in trial_el.findall(f".//{ns}contact"):
        name = " ".join(x for x in (_txt(c, f"{ns}forename"),
                                    _txt(c, f"{ns}surname")) if x)
        email = _txt(c, f"{ns}email")
        phone = _txt(c, f"{ns}telephone")
        if name or email or phone:
            contacts.append({"name": name, "email": email, "phone": phone})

    interventions = []
    for iv in trial_el.findall(f".//{ns}intervention"):
        nm = (_txt(iv, f"{ns}drugNames")
              or _txt(iv, f"{ns}interventionType") or "")
        if nm:
            interventions.append({"type": _txt(iv, f"{ns}interventionType") or "",
                                  "name": nm,
                                  "description": _txt(iv, f"{ns}description"),
                                  "otherNames": []})
    study_type = ("INTERVENTIONAL"
                  if (interventions
                      or trial_el.find(f".//{ns}interventionalTrialDesign") is not None)
                  else "OBSERVATIONAL")

    return {
        "nctId": nct or reg_id,             # uniform id (routes key on this)
        "registryId": reg_id, "registry": "ISRCTN", "source": "isrctn",
        "title": title, "studyType": study_type,
        "phase": _isrctn_phase(_txt(trial_el, f".//{ns}phase")),
        "overallStatus": status,
        "briefSummary": summary, "detailedDescription": "",
        "leadSponsor": _txt(trial_el, f".//{ns}sponsor/{ns}organisation"),
        "enrollment": _txt(trial_el, f".//{ns}targetEnrolment"),
        "startDate": (start or "")[:10], "completionDate": (end or "")[:10],
        "conditions": conditions, "meshTerms": [], "meshAncestors": [],
        "intrMesh": [], "interventions": interventions,
        "criteria": criteria, "sex": sex, "minAge": _age(lo), "maxAge": _age(hi),
        "healthyVolunteers": healthy,
        "centralContacts": contacts, "officials": [], "locations": locations,
    }


def fetch_isrctn(condition="", intervention="", limit=40, timeout=15):
    """Fetch open trials from the ISRCTN registry (UK/international) as trial
    dicts in the same shape as extract_trial().

    Best-effort and self-contained: any failure returns [] so ISRCTN never
    breaks the primary ClinicalTrials.gov results. Records with a closed
    recruitment window are dropped, and ones cross-referencing an NCT keep that
    NCT as their id so they dedupe against the CT.gov result set.
    """
    term = (condition or intervention or "").strip()
    if not term:
        return []
    q = f'condition:"{term}"' if condition else f'"{term}"'
    url = f"{ISRCTN_API}?q={urllib.parse.quote(q)}&limit={int(limit)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "trial-matcher/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            root = ET.parse(r).getroot()
    except Exception:
        return []
    out = []
    for full in root.findall(f"{ISRCTN_NS}fullTrial"):
        try:
            t = _isrctn_to_trial(full)
        except Exception:
            t = None
        if t and t.get("title"):
            out.append(t)
    return out


def sites_in_country(trial, country):
    if not country:
        return trial["locations"]
    return [l for l in trial["locations"]
            if country.lower() in (l.get("country", "") or "").lower()]


_CONCEPT_STOP = {"the", "and", "for", "with", "any", "all", "type", "study",
                 "trial", "disease", "disorder", "syndrome", "chronic", "acute",
                 "mellitus", "diseases", "system", "related", "conditions"}


def concept_tokens(trial):
    """Lowercased concept tokens for a trial from CT.gov's MeSH terms, ancestors
    and listed conditions. Empty when CT.gov assigned no MeSH (older/small
    studies) - callers then fall back to string matching."""
    text = " ".join(trial.get("meshTerms", []) + trial.get("meshAncestors", [])
                    + trial.get("conditions", []))
    return {w for w in re.findall(r"[a-z0-9]+", text.lower())
            if len(w) > 3 and w not in _CONCEPT_STOP}


# --------------------------------------------------------------------------- #
# 1b. PATIENT PROFILE + DETERMINISTIC HARD GATES
# --------------------------------------------------------------------------- #
def parse_age_to_years(s):
    """CT.gov age strings like '18 Years', '6 Months', '' -> years (float) or None."""
    if not s:
        return None
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(year|month|week|day)", s.strip(), re.I)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2).lower()
    return {"year": n, "month": n / 12, "week": n / 52, "day": n / 365}[unit]


def patient_profile(text):
    """Extract age (years) and sex from a free-text or headered patient summary."""
    age = None
    m = re.search(r"\bAGE\s*[:=]\s*(\d+(?:\.\d+)?)", text, re.I)
    if m:
        age = float(m.group(1))
    else:
        m = re.search(r"(\d+(?:\.\d+)?)[\s-]*(?:year|yr|yo|y/o)\b", text, re.I)
        if m:
            age = float(m.group(1))
    sex = None
    m = re.search(r"\bSEX\s*[:=]\s*(male|female|m|f)\b", text, re.I)
    if m:
        sex = "female" if m.group(1).lower() in ("female", "f") else "male"
    else:
        low = text.lower()
        if re.search(r"\b(female|woman|she|her)\b", low):
            sex = "female"
        elif re.search(r"\b(male|man|\bhe\b|his)\b", low):
            sex = "male"
    return {"age": age, "sex": sex}


def hard_gate(trial, profile):
    """Deterministic disqualifiers from STRUCTURED fields (no LLM). Returns
    (passes: bool, reasons: list[str]). Only fails on unambiguous mismatches."""
    reasons = []
    age = profile.get("age")
    if age is not None:
        lo = parse_age_to_years(trial.get("minAge"))
        hi = parse_age_to_years(trial.get("maxAge"))
        if lo is not None and age < lo:
            reasons.append(f"age {age:g} < min {trial['minAge']}")
        if hi is not None and age > hi:
            reasons.append(f"age {age:g} > max {trial['maxAge']}")
    sex = profile.get("sex")
    tsex = (trial.get("sex") or "ALL").upper()
    if sex and tsex in ("MALE", "FEMALE") and tsex != sex.upper():
        reasons.append(f"trial is {tsex}-only, patient is {sex}")
    return (not reasons, reasons)


# --------------------------------------------------------------------------- #
# 1c. LLM OUTPUT NORMALIZATION / SELF-CONSISTENCY GUARD
# --------------------------------------------------------------------------- #
VALID_VERDICTS = ("likely_eligible", "possible", "unlikely")


def _as_str_list(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if v:
        return [str(v).strip()]
    return []


def normalize_match(m):
    """Coerce/repair LLM output and enforce internal consistency so the report
    can never contradict itself (e.g. 'likely_eligible' with a listed blocker)."""
    if not isinstance(m, dict):
        m = {}
    verdict = str(m.get("verdict", "")).lower().strip()
    if verdict not in VALID_VERDICTS:
        verdict = "possible"
    try:
        score = int(round(float(m.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    met = _as_str_list(m.get("met"))
    not_met = _as_str_list(m.get("not_met"))
    unknown = _as_str_list(m.get("unknown"))
    rationale = str(m.get("rationale", "")).strip()

    # Consistency: a listed blocker means it can't be 'likely_eligible'.
    if not_met and verdict == "likely_eligible":
        verdict = "possible"
    # Score must agree with verdict banding.
    if not_met:
        score = min(score, 60)
    if verdict == "likely_eligible":
        score = max(score, 70)
    elif verdict == "unlikely":
        score = min(score, 39)
    return {"verdict": verdict, "score": score, "met": met,
            "not_met": not_met, "unknown": unknown, "rationale": rationale}


# --------------------------------------------------------------------------- #
# 2. MATCH (LLM)
# --------------------------------------------------------------------------- #
MATCH_SYSTEM = (
    "You are a careful clinical trial eligibility screener. Given a de-identified "
    "patient summary and a trial's eligibility criteria, decide how well the "
    "patient matches.\n\n"
    "CRITICAL RULES:\n"
    "1. Only use facts stated in the patient summary. Never assume or invent.\n"
    "2. INCLUSION criteria: list under 'met' only if the summary clearly shows "
    "the patient meets it; under 'not_met' only if the summary clearly shows "
    "the patient FAILS it; otherwise put a yes/no question in 'unknown'.\n"
    "3. EXCLUSION criteria: list under 'not_met' ONLY if the patient clearly "
    "TRIGGERS the exclusion (i.e. is disqualified). If the patient clearly does "
    "NOT trigger it, that is a PASS -- do not list it anywhere. If unclear, add "
    "a yes/no question to 'unknown'. Never list an exclusion the patient passes "
    "as if it were a failure.\n"
    "4. verdict: 'likely_eligible' only if the patient clearly meets the core "
    "inclusion criteria AND triggers no exclusions AND few unknowns remain; "
    "'unlikely' if the patient clearly fails any hard inclusion or triggers any "
    "hard exclusion; 'possible' otherwise (meaningful unknowns remain).\n"
    "5. SCORING: use the FULL 0-100 range and DISCRIMINATE between trials. Do "
    "not default to one number. Rough anchors: 90-100 = clearly meets everything, "
    "no blockers; 70-89 = strong fit, a few unknowns; 40-69 = plausible but many "
    "unknowns or a soft mismatch; 10-39 = likely fails; 0-9 = clearly disqualified. "
    "A trial with many unknowns must score LOWER than one where the patient "
    "clearly meets the criteria.\n"
    "Respond with strict JSON only."
)

MATCH_SCHEMA = (
    '{"verdict":"likely_eligible|possible|unlikely",'
    '"score":0-100,'
    '"met":["criteria the patient clearly meets"],'
    '"not_met":["criteria the patient clearly fails"],'
    '"unknown":["short yes/no questions to confirm eligibility"],'
    '"rationale":"one sentence"}'
)


def llm_chat(system, user, retries=2):
    """Minimal single-turn chat call (plain text out)."""
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0,
    }).encode()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                f"{LLM_BASE_URL}/chat/completions", data=body,
                headers={"Authorization": f"Bearer {LLM_API_KEY}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)["choices"][0]["message"]["content"].strip()
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(1)
    raise last


def _extract_json(text):
    """Parse JSON, repairing by grabbing the outermost {...} if needed."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e != -1 and e > s:
            return json.loads(text[s:e + 1])
        raise


def llm_match(patient, trial, retries=3):
    prompt = (
        f"PATIENT SUMMARY:\n{patient}\n\n"
        f"TRIAL: {trial['title']} ({trial['nctId']}, phase {trial['phase'] or 'NA'})\n"
        f"Sex: {trial['sex']}  Age: {trial['minAge']}-{trial['maxAge']}  "
        f"Healthy volunteers: {trial['healthyVolunteers']}\n\n"
        f"ELIGIBILITY CRITERIA:\n{trial['criteria'][:6000]}\n\n"
        f"Return JSON exactly in this shape:\n{MATCH_SCHEMA}"
    )
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": MATCH_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }).encode()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                f"{LLM_BASE_URL}/chat/completions", data=body,
                headers={"Authorization": f"Bearer {LLM_API_KEY}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                resp = json.load(r)
            return normalize_match(_extract_json(
                resp["choices"][0]["message"]["content"]))
        except Exception as e:  # network, rate-limit, JSON - back off and retry
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise last


# --------------------------------------------------------------------------- #
# 2b. TRIAL-SPECIFIC PRE-SCREEN QUESTIONS (LLM)
# --------------------------------------------------------------------------- #
PRESCREEN_SYSTEM = (
    "You turn a clinical trial's eligibility criteria into a SHORT list of plain "
    "yes/no pre-screening questions that a layperson patient can answer about "
    "themselves in seconds.\n\n"
    "RULES:\n"
    "1. Only produce questions a patient can honestly answer WITHOUT a doctor, "
    "lab test, imaging, genetic/biomarker result, or medical record. Turn "
    "criteria about things like specific lab values (eGFR, HbA1c thresholds a "
    "patient wouldn't know), biomarkers, mutations, imaging findings, or "
    "'investigator's judgment' into NOTHING -- skip them entirely.\n"
    "2. GOOD questions cover: age range, pregnancy/breastfeeding, a diagnosis the "
    "patient would know they have, prior/current medications, prior surgeries, "
    "being in another trial, ability to attend visits, major conditions a person "
    "is aware of.\n"
    "3. Phrase each as a short second-person question ('you'/'your'), one fact "
    "each, answerable strictly Yes / No / Not sure.\n"
    "4. For each question set 'flag_if' to the answer that signals a POSSIBLE "
    "eligibility problem: for an EXCLUSION criterion that is 'yes' (they have the "
    "disqualifier); for an INCLUSION criterion that is 'no' (they don't meet it). "
    "Use \"\" if neither answer is clearly a concern.\n"
    "5. Return AT MOST the requested number of questions, most decisive first. "
    "Do not invent criteria that aren't in the text.\n"
    "Respond with strict JSON only."
)

PRESCREEN_SCHEMA = (
    '{"questions":[{"q":"short yes/no question in plain language","flag_if":"yes|no|"}]}'
)


def _normalize_prescreen(data, max_q):
    """Coerce LLM output into a clean, capped list of {q, flag_if} dicts."""
    if isinstance(data, dict):
        items = data.get("questions")
    else:
        items = data
    if not isinstance(items, list):
        return []
    out, seen = [], set()
    for it in items:
        if isinstance(it, str):
            q, flag = it, ""
        elif isinstance(it, dict):
            q = str(it.get("q") or it.get("question") or "").strip()
            flag = str(it.get("flag_if") or "").strip().lower()
        else:
            continue
        if not q:
            continue
        if flag not in ("yes", "no"):
            flag = ""
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"q": q, "flag_if": flag})
        if len(out) >= max_q:
            break
    return out


# Pre-screen generation latency is dominated by the number of questions produced
# (output tokens) and, secondarily, by how much criteria text we make the model
# read (prefill). Both are tuned down from their original 6 questions / 6000 chars
# to cut cold-generation time ~30%+ and shorten the patient form. Env-overridable
# so we can retune without a deploy.
PRESCREEN_MAX_Q = int(os.environ.get("PRESCREEN_MAX_Q", "4"))
PRESCREEN_CRITERIA_CHARS = int(os.environ.get("PRESCREEN_CRITERIA_CHARS", "3500"))
# Compact JSON for ~4 short questions is well under this; the cap only guards the
# latency tail against a runaway generation.
PRESCREEN_MAX_TOKENS = int(os.environ.get("PRESCREEN_MAX_TOKENS", "350"))


def prescreen_questions(trial, max_q=None, retries=2):
    """Generate up to `max_q` patient-answerable yes/no pre-screen questions from
    a trial's eligibility criteria. Returns [] when no LLM key or no criteria, so
    callers can fall back to the generic screener."""
    if not LLM_API_KEY:
        return []
    if max_q is None:
        max_q = PRESCREEN_MAX_Q
    criteria = (trial.get("criteria") or "").strip()
    if not criteria:
        return []
    prompt = (
        f"TRIAL: {trial.get('title', '')} ({trial.get('nctId', '')})\n"
        f"Sex: {trial.get('sex', '')}  Age: {trial.get('minAge', '')}-"
        f"{trial.get('maxAge', '')}\n\n"
        f"ELIGIBILITY CRITERIA:\n{criteria[:PRESCREEN_CRITERIA_CHARS]}\n\n"
        f"Produce at most {max_q} questions.\n"
        f"Return JSON exactly in this shape:\n{PRESCREEN_SCHEMA}"
    )
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": PRESCREEN_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": PRESCREEN_MAX_TOKENS,
    }).encode()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                f"{LLM_BASE_URL}/chat/completions", data=body,
                headers={"Authorization": f"Bearer {LLM_API_KEY}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.load(r)
            return _normalize_prescreen(_extract_json(
                resp["choices"][0]["message"]["content"]), max_q)
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise last


# --------------------------------------------------------------------------- #
# 3. RANK + REPORT
# --------------------------------------------------------------------------- #
def _fmt_site(s):
    parts = [p for p in (s.get("facility"), s.get("city"), s.get("state")) if p]
    return ", ".join(parts)


def site_line(trial, country):
    """Honest site label: distinguish 'has a site in <country>' from fallback."""
    local = sites_in_country(trial, country)
    if local:
        extra = f" (+{len(local) - 1} more)" if len(local) > 1 else ""
        return f"- **Site in {country}:** {_fmt_site(local[0])}{extra}"
    if country and trial["locations"]:
        s = trial["locations"][0]
        return (f"- **No {country} site** - nearest listed: "
                f"{_fmt_site(s)}, {s.get('country')}")
    if trial["locations"]:
        s = trial["locations"][0]
        return f"- **Site:** {_fmt_site(s)}, {s.get('country')}"
    return "- **Site:** none listed"


def _type_tag(trial):
    if (trial.get("studyType") or "").upper() == "OBSERVATIONAL":
        return "observational/registry - not a treatment"
    return f"phase {trial['phase'] or 'NA'}"


def render(trial, m, country):
    lines = [
        f"### {trial['title']}",
        f"- **Trial:** {trial['nctId']} · {_type_tag(trial)} · "
        f"https://clinicaltrials.gov/study/{trial['nctId']}",
        site_line(trial, country),
        f"- **Verdict:** {m.get('verdict')} (score {m.get('score')})",
        f"- **Why:** {m.get('rationale','')}",
    ]
    if m.get("met"):
        lines.append("- **Meets:** " + "; ".join(m["met"][:6]))
    if m.get("not_met"):
        lines.append("- **Fails:** " + "; ".join(m["not_met"][:6]))
    if m.get("unknown"):
        lines.append("- **Confirm with patient:** " + "; ".join(m["unknown"][:6]))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", help='e.g. "lupus nephritis"')
    ap.add_argument("--country", default="Canada", help="filter sites (or '')")
    ap.add_argument("--case", default=str(HERE / "patient_case.txt"))
    ap.add_argument("--max-trials", type=int, default=15,
                    help="max trials to send to the LLM after gating")
    ap.add_argument("--fetch-cap", type=int, default=300,
                    help="max recruiting trials to pull before gating")
    ap.add_argument("--require-site", action="store_true",
                    help="only keep trials that have a site in --country")
    ap.add_argument("--selftest", action="store_true",
                    help="run offline unit checks (no network/LLM) and exit")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.condition:
        sys.exit("--condition is required (or use --selftest)")

    patient = pathlib.Path(args.case).read_text().strip() if os.path.exists(args.case) else ""
    if DO_MATCH and not patient:
        sys.exit(f"Put a de-identified patient summary in {args.case}")
    if DO_MATCH and not LLM_API_KEY:
        sys.exit("Set LLM_API_KEY (or run with MATCH=0 to just list trials).")

    profile = patient_profile(patient) if patient else {"age": None, "sex": None}
    if patient:
        print(f"Patient profile parsed: age={profile['age']} sex={profile['sex']}",
              file=sys.stderr)

    print(f"Fetching recruiting trials for '{args.condition}'...", file=sys.stderr)
    trials = fetch_trials(args.condition, max_n=args.fetch_cap)
    n_fetched = len(trials)
    print(f"Fetched {n_fetched} recruiting trials.", file=sys.stderr)

    if args.country and args.require_site:
        trials = [t for t in trials if sites_in_country(t, args.country)]
    elif args.country:
        trials.sort(key=lambda t: not bool(sites_in_country(t, args.country)))

    # Deterministic gate BEFORE spending any LLM calls.
    candidates, gated = [], []
    for t in trials:
        ok, reasons = hard_gate(t, profile)
        (candidates if ok else gated).append((t, reasons))
    print(f"Hard-gate: {len(candidates)} pass, {len(gated)} disqualified "
          f"(age/sex).", file=sys.stderr)

    candidates = candidates[:args.max_trials]

    if not DO_MATCH:
        print("\n-- PASS (would go to LLM) --")
        for t, _ in candidates:
            n = len(sites_in_country(t, args.country))
            print(f"  {t['nctId']}  [{t['phase'] or 'NA'}]  {t['title'][:64]}  "
                  f"({n} {args.country} sites)")
        print("\n-- GATED OUT (deterministic) --")
        for t, reasons in gated[:20]:
            print(f"  {t['nctId']}  {t['title'][:50]}  -> {'; '.join(reasons)}")
        return

    results = []
    for i, (t, _) in enumerate(candidates, 1):
        print(f"  matching {i}/{len(candidates)} {t['nctId']}...", file=sys.stderr)
        try:
            m = llm_match(patient, t)
        except Exception as e:
            m = {"verdict": "error", "score": 0, "rationale": str(e)[:120],
                 "met": [], "not_met": [], "unknown": []}
        results.append((t, m))

    def rank_key(tm):
        t, m = tm
        has_local = bool(sites_in_country(t, args.country)) if args.country else True
        observational = (t.get("studyType") or "").upper() == "OBSERVATIONAL"
        return (
            VERDICT_RANK.get(m.get("verdict"), 3),
            0 if has_local else 1,
            0 if not observational else 1,   # therapeutic trials before registries
            len(m.get("not_met") or []),
            len(m.get("unknown") or []),
            -int(m.get("score") or 0),
        )

    results.sort(key=rank_key)

    shown_blocks, shown = [], 0
    for t, m in results:
        if m.get("verdict") in ("unlikely", "error"):
            continue
        shown_blocks.append(render(t, m, args.country) + "\n")
        shown += 1
    out = [
        "# Trial matches\n",
        f"_Condition: {args.condition} · sites in: {args.country or 'any'} · "
        f"patient age {profile['age']} {profile['sex'] or ''}_\n",
        f"_Screened {n_fetched} recruiting trials · {len(gated)} ruled out by "
        f"age/sex · {len(candidates)} clinically reviewed · {shown} shown below._\n",
    ]
    out += shown_blocks
    if not shown:
        out.append("_No likely/possible matches after screening._\n")
    report = "\n".join(out)
    (HERE / "matches_output.md").write_text(report)
    print("\n" + report)
    print(f"\nSaved -> {HERE / 'matches_output.md'}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Offline self-tests (no network, no LLM) - run with --selftest
# --------------------------------------------------------------------------- #
def selftest():
    ok = True

    def check(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    # age parsing
    check("parse '18 Years'", parse_age_to_years("18 Years") == 18)
    check("parse '6 Months'", abs(parse_age_to_years("6 Months") - 0.5) < 1e-9)
    check("parse ''", parse_age_to_years("") is None)

    # profile parsing
    p = patient_profile("42-year-old female with lupus nephritis")
    check("profile age 42", p["age"] == 42)
    check("profile sex female", p["sex"] == "female")
    p2 = patient_profile("AGE: 7\nSEX: M\npediatric patient")
    check("profile header age 7", p2["age"] == 7)
    check("profile header sex male", p2["sex"] == "male")

    # hard gate
    peds = {"nctId": "X", "sex": "ALL", "minAge": "18 Years", "maxAge": "",
            "locations": []}
    check("gate: child fails adult trial",
          hard_gate(peds, {"age": 7, "sex": None})[0] is False)
    check("gate: adult passes adult trial",
          hard_gate(peds, {"age": 42, "sex": None})[0] is True)
    female_only = {"nctId": "Y", "sex": "FEMALE", "minAge": "", "maxAge": "",
                   "locations": []}
    check("gate: male fails female-only",
          hard_gate(female_only, {"age": 30, "sex": "male"})[0] is False)
    check("gate: unknown age never gates",
          hard_gate(peds, {"age": None, "sex": None})[0] is True)

    # normalization / consistency
    n = normalize_match({"verdict": "likely_eligible", "score": 95,
                         "not_met": ["fails eGFR"], "met": [], "unknown": []})
    check("normalize: blocker downgrades verdict", n["verdict"] == "possible")
    check("normalize: blocker caps score", n["score"] <= 60)
    n2 = normalize_match({"verdict": "banana", "score": "150"})
    check("normalize: bad verdict -> possible", n2["verdict"] == "possible")
    check("normalize: score clamped to 100", n2["score"] == 100)
    n3 = normalize_match({"verdict": "unlikely", "score": 88})
    check("normalize: unlikely caps score <40", n3["score"] <= 39)

    # json repair
    check("json repair strips prose",
          _extract_json('here you go: {"a": 1} thanks')["a"] == 1)

    print("\nSELFTEST:", "ALL PASS" if ok else "FAILURES PRESENT")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
