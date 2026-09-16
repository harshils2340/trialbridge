"""Find a local clinic / PI email when ClinicalTrials.gov only lists a name.

Sponsor inboxes (LillyTrials@, etc.) are a dead end for a patient in Phoenix.
CT.gov often lists the recruiting facility and PI with no email. This looks up
the site's public contact page and returns those addresses.
"""
from __future__ import annotations

import html as htmlmod
import re
import urllib.error
import urllib.parse
import urllib.request

import db

_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,18}", re.I)
_MAILTO_RE = re.compile(r"mailto:([^?\"'\s>]+)", re.I)

_UA = "BridgeMD/1.0 (clinical trial finder; https://bridgemd.health)"
_TIMEOUT = 8

_PREFERRED_LOCAL = (
    "recruitment", "recruiting", "recruit", "volunteers", "volunteer",
    "studies", "study", "research", "referrals", "referral", "patients",
    "inquiry", "enroll", "enrol", "contact", "info", "hello", "trials",
)

_SKIP_DOMAINS = (
    "clinicaltrials.gov", "nih.gov", "nlm.nih.gov", "gmail.com", "yahoo.com",
    "hotmail.com", "outlook.com", "icloud.com", "sentry.io", "wixpress.com",
    "example.com", "godaddy.com", "cloudflare.com", "facebook.com",
    "linkedin.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
    "wikipedia.org", "ichgcp.net", "centerwatch.com",
    # Never our own demo or test domains: a scrape that lands on one of these
    # is pointing back at us, not at a clinic.
    "northwindclinical.com", "bridgemd.local", "bridgemd.health",
)

_SPONSOR_DOMAINS = (
    "lilly.com", "pfizer.com", "novartis.com", "roche.com", "gsk.com",
    "astrazeneca.com", "merck.com", "bms.com", "sanofi.com", "jnj.com",
    "amgen.com", "novonordisk.com", "bayer.com", "boehringer-ingelheim.com",
    "abbvie.com", "gilead.com", "biogen.com", "regeneron.com", "moderna.com",
)

_NETWORK_ALIASES = (
    ("synexus", "trialmed"),
    ("ppd", "trialmed"),
    ("radiant research", "trialmed"),
)

_STOP = {
    "inc", "llc", "ltd", "pllc", "pc", "pa", "corp", "corporation",
    "incorporated", "limited", "the", "of", "and", "us", "usa",
}

_US_ABBR = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct",
    "delaware": "de", "florida": "fl", "georgia": "ga", "hawaii": "hi",
    "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia",
    "kansas": "ks", "kentucky": "ky", "louisiana": "la", "maine": "me",
    "maryland": "md", "massachusetts": "ma", "michigan": "mi",
    "minnesota": "mn", "mississippi": "ms", "missouri": "mo",
    "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm",
    "new york": "ny", "north carolina": "nc", "north dakota": "nd",
    "ohio": "oh", "oklahoma": "ok", "oregon": "or", "pennsylvania": "pa",
    "rhode island": "ri", "south carolina": "sc", "south dakota": "sd",
    "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt",
    "virginia": "va", "washington": "wa", "west virginia": "wv",
    "wisconsin": "wi", "wyoming": "wy",
}

_PAGE_PATHS = (
    "/", "/contactus", "/contact-us", "/contact", "/contacts",
    "/privacy-policy", "/privacy", "/privacy-policy/", "/terms",
)
_MEMO = {}


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _domain(email):
    return (email.split("@")[-1] or "").lower()


def _local(email):
    return (email.split("@")[0] or "").lower()


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        raw = r.read(400_000)
        charset = r.headers.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset, errors="ignore")
        except LookupError:
            return raw.decode("utf-8", errors="ignore")


def _emails_in(text):
    found = set()
    for m in _MAILTO_RE.finditer(text or ""):
        found.add(urllib.parse.unquote(m.group(1)).strip().strip(".,;"))
    for m in _EMAIL_RE.finditer(htmlmod.unescape(text or "")):
        found.add(m.group(0).strip().strip(".,;"))
    out = []
    for e in found:
        e = htmlmod.unescape(e).lower()
        e = re.sub(r"^(?:u003[ce])+", "", e)
        e = e.lstrip("<>\"'")
        if not _EMAIL_RE.fullmatch(e):
            continue
        if e.startswith("www."):
            continue
        if any(e.endswith("." + ext) for ext in ("png", "jpg", "gif", "webp", "css", "js")):
            continue
        local = e.split("@")[0]
        if "u003" in local or local in (
                "privacy", "dataprivacy", "legal", "webmaster", "admin"):
            continue
        out.append(e)
    return out


def _skip_email(email, sponsor=""):
    dom = _domain(email)
    local = _local(email)
    if any(dom == d or dom.endswith("." + d) for d in _SKIP_DOMAINS):
        return True
    if any(dom == d or dom.endswith("." + d) for d in _SPONSOR_DOMAINS):
        return True
    if "u003" in local or local == "admin" or any(
            k.rstrip("@") in local for k in db.NON_HUMAN_LOCALS):
        return True
    if not re.match(r"^[a-z0-9]", local):
        return True
    sp = _norm(sponsor)
    if sp and sp.split()[0] in dom.replace(".", " "):
        # "eli lilly" vs lilly.com already caught; keep generic
        pass
    return False


def _score(email, facility, city, pi_name=""):
    local = _local(email)
    dom = _domain(email)
    fac = _norm(facility)
    tokens = [t for t in fac.split() if len(t) > 2]
    score = 0
    if any(p in local for p in _PREFERRED_LOCAL):
        score += 50
    slug = "".join(tokens)
    compact_dom = dom.replace(".", "").replace("-", "")
    if slug and slug[:12] in compact_dom:
        score += 40
    for t in tokens:
        if t in compact_dom:
            score += 8
    city_n = _norm(city)
    if city_n and city_n.split()[0] in compact_dom:
        score += 6
    pi = _norm(pi_name)
    if pi:
        parts = pi.replace(" md", "").replace(" dr", "").split()
        if len(parts) >= 2 and parts[-1] in local:
            score += 20
    if local in ("admin", "webmaster", "privacy", "legal", "jobs", "careers"):
        score -= 30
    return score


def _guess_hosts(facility):
    words = [w for w in _norm(facility).split() if w not in _STOP and len(w) > 1]
    if not words:
        return []
    joined = "".join(words)
    hyphen = "-".join(words)
    hosts = []
    first = words[0]
    if first in _US_ABBR and len(words) > 1:
        hosts.append(_US_ABBR[first] + "".join(words[1:]) + ".com")
    hosts += [joined + ".com", hyphen + ".com"]
    if first not in _US_ABBR:
        hosts.append(first + ".com")
    blob = " ".join(words)
    for needle, alias in _NETWORK_ALIASES:
        if needle in blob:
            hosts.append(alias + ".com")
    out, seen = [], set()
    for h in hosts:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out[:8]


def _site_urls(facility):
    """Contact/privacy pages on guessed clinic domains, best hosts first."""
    hosts = _guess_hosts(facility)
    paths = (
        "/privacy-policy", "/privacy-policy/", "/contactus", "/contact-us",
        "/contact", "/privacy", "/",
    )
    urls = []
    for path in paths:
        for host in hosts[:3]:
            urls.append("https://www." + host + path)
            urls.append("https://" + host + path)
    return urls[:8]


def lookup_site_emails(site, sponsor=""):
    """Return [{email, name, role, facility, city, source}] for one CT.gov site."""
    facility = ((site or {}).get("facility") or "").strip()
    city = ((site or {}).get("city") or "").strip()
    if not facility:
        return []
    memo_key = (facility.lower(), city.lower(), _norm(sponsor))
    if memo_key in _MEMO:
        return _MEMO[memo_key]
    pi_name = ""
    for c in (site or {}).get("contacts") or []:
        role = (c.get("role") or "").upper()
        if "INVESTIGATOR" in role and c.get("name"):
            pi_name = c.get("name")
            break

    scored, seen = [], set()
    for page_url in _site_urls(facility):
        try:
            body = _fetch(page_url)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            continue
        for email in _emails_in(body):
            if email in seen or _skip_email(email, sponsor):
                continue
            seen.add(email)
            scored.append((_score(email, facility, city, pi_name), email))
        if any(score >= 8 for score, _ in scored):
            break
    scored.sort(key=lambda x: -x[0])
    picked = []
    pi_last = ""
    if pi_name:
        parts = _norm(pi_name).replace(" md", "").replace(" dr", "").split()
        if parts:
            pi_last = parts[-1]
    for score, email in scored:
        if score < 8:
            continue
        picked.append({
            "email": email,
            "name": pi_name if pi_last and pi_last in _local(email) else "",
            "role": "LOOKUP",
            "facility": facility,
            "city": city,
            "source": "lookup",
        })
        if len(picked) >= 1:
            break
    _MEMO[memo_key] = picked
    return picked
