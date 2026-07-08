"""Map a patient's messy words to standard clinical codes - for free.

Patients type "Ozempic" or "sugar diabetes"; ClinicalTrials.gov tags studies
with canonical vocabularies (MeSH terms + ancestors, and interventions). If we
translate the patient's input into the same vocabulary we can match on concepts
instead of strings - "Ozempic" and "Wegovy" both become the ingredient
`semaglutide`, "type 2 diabetes" lands in the MeSH family CT.gov actually uses.

All three sources are free NLM web services, no key required:
  - RxNorm (rxnav.nlm.nih.gov): drug name -> RxCUI -> ingredient.
  - Clinical Tables ICD-10-CM: condition text -> ICD-10 codes + official names.
  - MeSH lookup (id.nlm.nih.gov): condition text -> MeSH descriptor labels.

Everything is best-effort: any network hiccup returns empties so the caller
falls back to the existing string-based behaviour. Results are memoised in
process so repeated lookups (same drug across many patients) are instant.
"""
import json
import re
import threading
import urllib.parse
import urllib.request

_RXNAV = "https://rxnav.nlm.nih.gov/REST"
_TABLES = "https://clinicaltables.nlm.nih.gov/api"
_MESH = "https://id.nlm.nih.gov/mesh/lookup/descriptor"
_TIMEOUT = 8

_cache = {}
_lock = threading.Lock()

_STOP = {"the", "and", "for", "with", "any", "all", "type", "study", "trial",
         "disease", "disorder", "syndrome", "chronic", "acute", "mellitus",
         "unspecified", "other", "without", "complications", "due", "primary",
         "secondary", "system", "diseases", "agents", "related"}


def _get(url):
    with _lock:
        if url in _cache:
            return _cache[url]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BridgeMD/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            data = json.load(r)
    except Exception:
        data = None
    with _lock:
        _cache[url] = data
    return data


def _tokens(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 3 and w not in _STOP}


# --------------------------------------------------------------------------- #
# Drugs -> RxNorm ingredient
# --------------------------------------------------------------------------- #
def normalize_med(name):
    """"Ozempic 1mg" -> {'input','rxcui','ingredient'}. Brand/misspelling safe."""
    name = (name or "").strip()
    if not name:
        return {"input": name, "rxcui": "", "ingredient": ""}
    q = urllib.parse.quote(name)
    rxcui = ""
    # Exact name first, then fuzzy approximateTerm for messy input.
    exact = _get(f"{_RXNAV}/rxcui.json?name={q}&search=2")
    ids = (((exact or {}).get("idGroup") or {}).get("rxnormId") or [])
    if ids:
        rxcui = ids[0]
    else:
        approx = _get(f"{_RXNAV}/approximateTerm.json?term={q}&maxEntries=1")
        cand = (((approx or {}).get("approximateGroup") or {}).get("candidate") or [])
        if cand:
            rxcui = cand[0].get("rxcui", "")
    ingredient = ""
    if rxcui:
        rel = _get(f"{_RXNAV}/rxcui/{rxcui}/related.json?tty=IN")
        for grp in (((rel or {}).get("relatedGroup") or {}).get("conceptGroup") or []):
            for cp in (grp.get("conceptProperties") or []):
                ingredient = cp.get("name", "")
                break
            if ingredient:
                break
    return {"input": name, "rxcui": rxcui, "ingredient": ingredient.lower()}


def med_ingredients(items):
    """Set of ingredient names (+ originals) for a list/iterable of drug texts.
    Used to widen eligibility token matching, e.g. Ozempic -> semaglutide."""
    out = set()
    for it in items or []:
        it = (it or "").strip()
        if not it:
            continue
        out.add(it.lower())
        ing = normalize_med(it).get("ingredient")
        if ing:
            out.add(ing)
    return out


# --------------------------------------------------------------------------- #
# Conditions -> ICD-10 + MeSH
# --------------------------------------------------------------------------- #
def icd10(term):
    """[(code, name)] for a condition phrase via ICD-10-CM. [] if none."""
    term = (term or "").strip()
    if not term:
        return []
    q = urllib.parse.quote(term)
    data = _get(f"{_TABLES}/icd10cm/v3/search?sf=code,name&terms={q}&maxList=5")
    if not isinstance(data, list) or len(data) < 4:
        return []
    return [(row[0], row[1]) for row in (data[3] or []) if len(row) >= 2]


def mesh_labels(term):
    """Canonical MeSH descriptor labels for a condition phrase. Falls back to the
    strongest token when the full phrase doesn't resolve (MeSH is picky)."""
    term = (term or "").strip()
    if not term:
        return []

    def _lookup(text):
        q = urllib.parse.quote(text)
        data = _get(f"{_MESH}?label={q}&match=contains&limit=8")
        if not isinstance(data, list):
            return []
        return [d.get("label", "") for d in data if d.get("label")]

    labels = _lookup(term)
    if not labels:
        for tok in sorted(_tokens(term), key=len, reverse=True):
            labels = _lookup(tok)
            if labels:
                break
    return labels


def condition_codes(term):
    """Everything we can resolve about a condition, for display + matching."""
    labels = mesh_labels(term)
    codes = icd10(term)
    return {"input": term, "mesh": labels, "icd10": codes,
            "tokens": condition_tokens(term, labels)}


def condition_tokens(term, labels=None):
    """Concept token set for a condition: the phrase itself + its MeSH labels.
    This is what we intersect with a trial's MeSH tokens to score relevance."""
    toks = set(_tokens(term))
    if labels is None:
        labels = mesh_labels(term)
    for lab in labels:
        toks |= _tokens(lab)
    return toks


def relevance(patient_tokens, trial_tokens):
    """Overlap count between patient concept tokens and a trial's MeSH tokens."""
    if not patient_tokens or not trial_tokens:
        return 0
    return len(patient_tokens & trial_tokens)
