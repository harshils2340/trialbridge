"""EU CTIS (Clinical Trials Information System) provider.

CTIS (euclinicaltrials.eu) is the EU/EEA clinical-trials portal under Regulation
536/2014. Unlike ClinicalTrials.gov it has no officially documented REST API, but
its public portal is backed by a JSON service we can read:

    POST /ctis-public-api/search            -> paginated list of trials
    GET  /ctis-public-api/retrieve/<ctNum>  -> one full trial record

We use CTIS purely as a *discovery / content* source: it powers crawlable
`/study/<ctNumber>` pages and an "also recruiting in Europe" section on condition
pages. It is deliberately NOT wired into the personalised "near me" ranked search
(CTIS has no geocoded sites, only country-level locations), so it broadens
coverage without degrading the relevance of the core matcher.

Everything here is best-effort and never raises: on any network/parse error we
return empty results so callers (SEO pages hit by crawlers) never break.
"""

import json
import urllib.parse
import urllib.request

BASE = "https://euclinicaltrials.eu/ctis-public-api"
VIEW_URL = "https://euclinicaltrials.eu/ctis-public/view/{ct}"
_UA = {"User-Agent": "BridgeMD/1.0", "Accept": "application/json",
       "Content-Type": "application/json"}
_TIMEOUT = 15

# CTIS statuses we treat as "active" (worth indexing / showing). We always show
# the real status label on the page; this only gates indexability + recruiting UI.
_ACTIVE = ("ongoing", "authorised", "authorized", "recruiting")
_ENDED = ("ended", "concluded", "terminated", "revoked", "withdrawn", "suspended")


def _post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers=_UA, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.load(r)


def _get(path):
    req = urllib.request.Request(BASE + path, headers=_UA)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.load(r)


def _split_terms(raw):
    if not raw:
        return []
    out, seen = [], set()
    for part in raw.replace(";", ",").split(","):
        p = part.strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def _countries(raw):
    """['Denmark:5', 'Belgium:5'] -> ['Denmark', 'Belgium'] (deduped)."""
    out, seen = [], set()
    for c in raw or []:
        name = (c or "").split(":")[0].strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def is_ct_number(sid):
    """CTIS EU CT number, e.g. 2024-512144-39-00."""
    import re
    return bool(re.match(r"^\d{4}-\d{6}-\d{2}-\d{2}$", (sid or "").strip()))


def _search_card(criteria, size=12):
    try:
        data = _post("/search", {"pagination": {"page": 1, "size": int(size)},
                                 "searchCriteria": criteria})
    except Exception:
        return None
    return data


def search(query="", size=12):
    """Return a list of lightweight CTIS trial cards for a condition/term.
    Each card: {ctNumber, title, conditions, phase, countries, sponsor}."""
    data = _search_card({"containAny": query or ""}, size=size)
    if not data:
        return []
    cards = []
    for it in data.get("data", []) or []:
        ct = (it.get("ctNumber") or "").strip()
        if not ct:
            continue
        cards.append({
            "ctNumber": ct,
            "title": it.get("ctTitle") or it.get("shortTitle") or "",
            "conditions": _split_terms(it.get("conditions") or ""),
            "phase": it.get("trialPhase") or "",
            "countries": _countries(it.get("trialCountries")),
            "sponsor": it.get("sponsor") or "",
        })
    return cards


def fetch(ct_number):
    """Fetch one CTIS trial and normalise it to the same shape our study page
    uses for ClinicalTrials.gov trials (plus a few CTIS-specific extras).
    Returns None if it can't be fetched/parsed."""
    ct = (ct_number or "").strip()
    if not is_ct_number(ct):
        return None
    try:
        d = _get("/retrieve/" + urllib.parse.quote(ct))
    except Exception:
        return None
    if not isinstance(d, dict) or not d.get("ctNumber"):
        return None

    p1 = (((d.get("authorizedApplication") or {}).get("authorizedPartI")) or {})
    td = p1.get("trialDetails") or {}
    ti = td.get("trialInformation") or {}
    ids = td.get("clinicalTrialIdentifiers") or {}

    title = (ids.get("publicTitle") or ids.get("fullTitle")
             or ids.get("shortTitle") or "")
    objective = ((ti.get("trialObjective") or {}).get("mainObjective") or "").strip()

    # Conditions
    conds, seen = [], set()
    for mc in ((ti.get("medicalCondition") or {}).get("partIMedicalConditions") or []):
        name = (mc.get("medicalCondition") or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            conds.append(name)
    for mc in (p1.get("medicalConditions") or []):
        name = (mc.get("medicalCondition") or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            conds.append(name)

    # Eligibility (structured inclusion / exclusion)
    elig = ti.get("eligibilityCriteria") or {}
    incl = [c.get("principalInclusionCriteria", "").strip()
            for c in (elig.get("principalInclusionCriteria") or [])
            if c.get("principalInclusionCriteria")]
    excl = [c.get("principalExclusionCriteria", "").strip()
            for c in (elig.get("principalExclusionCriteria") or [])
            if c.get("principalExclusionCriteria")]

    # Sex from population flags
    pop = ti.get("populationOfTrialSubjects") or {}
    female, male = pop.get("isFemaleSubjects"), pop.get("isMaleSubjects")
    if female and male:
        sex = "ALL"
    elif female:
        sex = "FEMALE"
    elif male:
        sex = "MALE"
    else:
        sex = ""

    countries = [c.get("name", "").strip()
                 for c in (p1.get("rowCountriesInfo") or []) if c.get("name")]
    locations = [{"facility": "", "city": "", "state": "", "country": c,
                  "status": "", "contacts": [], "lat": None, "lon": None}
                 for c in countries]

    status_label = (d.get("ctStatus") or "").strip() or "Authorised"
    low = status_label.lower()
    recruiting = any(a in low for a in _ACTIVE) and not any(e in low for e in _ENDED)

    enrollment = p1.get("rowSubjectCount") or ""

    # Sponsor (nested in retrieve) with the clean search field as a fallback.
    sponsor = ""
    for sp in (p1.get("sponsors") or []):
        org = ((sp.get("publicContacts") or [{}])[0].get("organisation") or {})
        if org.get("name"):
            sponsor = org["name"]
            break
    # Phase/age-group/enrolment aren't in the retrieve payload; the
    # search-by-number card carries them (and a clean sponsor name), so enrich
    # from there. Best-effort.
    phase, ages_text = "", ""
    card = _search_card({"containAll": ct}, size=1)
    if card and card.get("data"):
        c0 = card["data"][0]
        phase = c0.get("trialPhase") or ""
        sponsor = sponsor or (c0.get("sponsor") or "")
        ages_text = c0.get("ageGroup") or ""
        enrollment = c0.get("totalNumberEnrolled") or enrollment

    return {
        "nctId": "",
        "ctNumber": ct,
        "title": title,
        "studyType": "INTERVENTIONAL",
        "phase": phase,
        "overallStatus": status_label,
        "briefSummary": objective,
        "detailedDescription": "",
        "leadSponsor": sponsor,
        "enrollment": enrollment,
        "conditions": conds,
        "criteria": "",
        "sex": sex,
        "minAge": "", "maxAge": "",
        "healthyVolunteers": "",
        "locations": locations,
        # CTIS-specific extras consumed by the study route:
        "_source": "ctis",
        "_incl": incl,
        "_excl": excl,
        "_ages_text": ages_text,
        "_recruiting": recruiting,
        "_view_url": VIEW_URL.format(ct=urllib.parse.quote(ct)),
    }
