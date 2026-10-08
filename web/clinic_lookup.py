"""Find a local clinic's own email when ClinicalTrials.gov only lists a name.

Sponsor inboxes (LillyTrials@, clinicaltrials@novonordisk.com) are a dead end
for a patient in London, Ontario. CT.gov often lists the recruiting facility
with no contact at all. This searches the web for the clinic's own website,
checks the site really is that clinic (its name and city are on it), reads its
contact and study pages, and returns the clinic's own address.

An application carries a person's name, date of birth and phone, so a wrong
address is a privacy incident. An email is only taken from a site whose domain
matches the clinic's name and whose pages name the clinic and its city, and
only at that site's own domain.
"""
from __future__ import annotations

import base64
import html as htmlmod
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import db

# Bounded on purpose: an unbounded name before "@" rescans a long run of
# inline base64 or CSS from every position, which never finishes on a
# multi-megabyte page.
_EMAIL_RE = re.compile(
    r"[A-Z0-9._%+\-]{1,64}@[A-Z0-9.\-]{1,190}\.[A-Z]{2,18}", re.I)
_MAILTO_RE = re.compile(r"mailto:([^?\"'\s>]+)", re.I)
# "info [at] clinic [dot] ca", the other common way sites hide an address.
_AT_DOT_RE = re.compile(
    r"([a-z0-9._%+\-]{1,64})\s{0,3}[\[\(\{]\s{0,3}at\s{0,3}[\]\)\}]\s{0,3}"
    r"([a-z0-9\-]{1,63}(?:\s{0,3}[\[\(\{]\s{0,3}dot\s{0,3}[\]\)\}]\s{0,3}"
    r"[a-z0-9\-]{1,63}){1,4})", re.I)
# Cloudflare's email protection: the address is XOR-encoded in the page.
_CFEMAIL_RE = re.compile(
    r"(?:data-cfemail=[\"']|/cdn-cgi/l/email-protection#)([0-9a-f]{6,})", re.I)

_UA = "Mozilla/5.0 (compatible; BridgeMD/1.0; +https://bridgemd.health)"
_TIMEOUT = 8
# One lookup (search plus every page read) never runs longer than this, so an
# apply's background send or the boot backfill is never stuck on a slow site.
_BUDGET_SECONDS = 35
_MAX_SITES = 3
_MAX_PAGES = 8
_MAX_PAGE_BYTES = 3_000_000
_CACHE_HIT_TTL = 30 * 86400
_CACHE_MISS_TTL = 86400

_PREFERRED_LOCAL = (
    "recruitment", "recruiting", "recruit", "volunteers", "volunteer",
    "studies", "study", "research", "referrals", "referral", "patients",
    "inquiry", "enroll", "enrol", "contact", "info", "hello", "trials",
)

# A study team's own inbox (studies@, recruitment@) beats a general one
# (info@, contact@) on the same site.
_STUDY_LOCAL = ("recruit", "volunteer", "stud", "research", "trial", "enrol",
                "referral", "screen", "participa")

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

# Search results that are about a clinic but are not the clinic: listings,
# maps, reviews, social, registries, dictionaries. Their pages name the clinic
# and its city, so only this list keeps them from passing as its website.
_DIRECTORY_DOMAINS = _SKIP_DOMAINS + (
    "yelp.com", "yelp.ca", "yellowpages.com", "yellowpages.ca", "mapquest.com",
    "google.com", "bing.com", "duckduckgo.com", "apple.com", "dnb.com",
    "zoominfo.com", "birdeye.com", "firmania.ca", "eichor.com",
    "canada247.info", "cdncompanies.com", "bbb.org", "manta.com",
    "healthgrades.com", "vitals.com", "webmd.com", "ratemds.com",
    "zocdoc.com", "npiregistry.cms.hhs.gov", "npidb.org", "doximity.com",
    "usnews.com", "indeed.com", "glassdoor.com", "tripadvisor.com",
    "foursquare.com", "nextdoor.com", "crunchbase.com", "opencorporates.com",
    "bloomberg.com", "chamberofcommerce.com", "findglocal.com",
    "showmelocal.com", "loc8nearme.com", "veeva.com", "withpower.com",
    "antidote.me", "trialx.com", "clinicaltrialsregister.eu", "who.int",
    "merriam-webster.com", "cambridge.org", "vocabulary.com", "pinterest.com",
    "tiktok.com", "reddit.com", "quora.com", "medium.com", "joinastudy.ca",
    "webvent.tv", "sharecare.com", "md.com", "wellness.com", "cylex.us.com",
    "cylex-canada.ca", "411.ca", "canpages.ca", "hotfrog.com", "brownbook.net",
    "allbiz.com", "buzzfile.com", "bizapedia.com", "signalhire.com",
    "rocketreach.co", "apollo.io", "lusha.com",
)

_NETWORK_ALIASES = (
    ("synexus", "trialmed"),
    ("ppd", "trialmed"),
    ("radiant research", "trialmed"),
)

_STOP = {
    "inc", "llc", "ltd", "pllc", "pc", "pa", "corp", "corporation",
    "incorporated", "limited", "the", "of", "and", "us", "usa", "at", "for",
    "in", "a", "an", "de", "la", "du", "des", "le",
}

# Words any clinic's name might carry. A clinic whose name is only these
# ("Clinical Research Associates") has to be matched by its whole name.
_GENERIC = {
    "research", "clinical", "clinic", "clinics", "center", "centre",
    "centers", "centres", "medical", "medicine", "health", "healthcare",
    "hospital", "hospitals", "institute", "trials", "trial", "group",
    "associates", "partners", "university", "college", "school", "care",
    "services", "service", "site", "sites", "investigational", "study",
    "studies", "network", "practice", "family", "physicians", "specialists",
    "specialty", "foundation", "sciences", "science", "department", "dept",
    "division", "office", "regional", "general", "community", "memorial",
    "national", "international", "global", "and", "affiliates", "llc",
    "consultants", "solutions", "systems", "management",
}

# A CT.gov site that names no clinic: there is no website to find, and a
# search would land on whatever "Research Site" page ranks first.
_ANONYMOUS_SITE_RE = re.compile(
    r"\b(investigational|investigative|research|clinical|study|trial)\s+site\b"
    r"|\blocal institution\b|^site\s*(no\.?|number|#)?\s*\d+|^\d+$", re.I)

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
_CA_ABBR = {
    "ontario": "on", "quebec": "qc", "british columbia": "bc",
    "alberta": "ab", "manitoba": "mb", "saskatchewan": "sk",
    "nova scotia": "ns", "new brunswick": "nb",
    "newfoundland and labrador": "nl", "prince edward island": "pe",
}
_REGION_ABBR = {**_US_ABBR, **_CA_ABBR}

# The domain endings a clinic in that country most likely uses, after .com.
_COUNTRY_TLDS = {
    "canada": ("ca",), "united kingdom": ("co.uk", "uk"),
    "australia": ("com.au",), "new zealand": ("co.nz",),
    "ireland": ("ie",), "germany": ("de",), "france": ("fr",),
    "spain": ("es",), "italy": ("it",), "netherlands": ("nl",),
}
# Second-level labels under a country ending (lhsc.on.ca, nhs.uk sites).
_SLD = {"co", "com", "org", "net", "ac", "gov", "edu", "nhs", "on", "qc",
        "bc", "ab", "mb", "sk", "ns", "nb", "nl", "pe"}

_SKIP_LINK_RE = re.compile(
    r"\.(pdf|jpe?g|png|gif|webp|svg|zip|docx?|xlsx?|pptx?|mp4|mp3|ics|xml)"
    r"(\?|$)|/wp-json|/feed/?$|xmlrpc|/wp-content/|/tag/|/category/|/author/"
    r"|[?&](share|replytocom)=", re.I)

_MEMO = {}


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _domain(email):
    return (email.split("@")[-1] or "").lower()


def _local(email):
    return (email.split("@")[0] or "").lower()


def _base_domain(host):
    """milestoneresearch.ca from www.milestoneresearch.ca; lhsc.on.ca stays."""
    host = (host or "").lower().split(":")[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SLD:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _host(url):
    try:
        return urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _fetch(url, timeout=_TIMEOUT):
    """(final url after redirects, page text). Raises on network errors."""
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        "Accept-Language": "en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        if ctype and "html" not in ctype and "text" not in ctype:
            return r.geturl(), ""
        # Page builders inline megabytes of CSS before the first link (the
        # Milestone Research home page has its menu 1.5 MB in), so read far.
        raw = r.read(_MAX_PAGE_BYTES)
        charset = r.headers.get_content_charset() or "utf-8"
        try:
            return r.geturl(), raw.decode(charset, errors="ignore")
        except LookupError:
            return r.geturl(), raw.decode("utf-8", errors="ignore")


def _cf_decode(hexstr):
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key)
                       for i in range(2, len(hexstr) - 1, 2))
    except ValueError:
        return ""


_AT_MARK_RE = re.compile(r"[\[\(\{]\s{0,3}at\s{0,3}[\]\)\}]", re.I)


def _around(text, marks, pattern, before=64, after=320):
    """Run `pattern` only on the text around each mark ("@", "[at]"). Running
    it over a whole multi-megabyte page of inline CSS takes seconds."""
    for i in marks:
        start = max(0, i - before)
        window = text[start:i + after]
        for m in pattern.finditer(window):
            if start + m.start() <= i < start + m.end():
                yield m


def _at_positions(text):
    i = text.find("@")
    while i != -1:
        yield i
        i = text.find("@", i + 1)


def _emails_in(text):
    text = text or ""
    found = set()
    for m in _MAILTO_RE.finditer(text):
        found.add(urllib.parse.unquote(m.group(1)).strip().strip(".,;"))
    # Entity-encoded addresses (&#105;&#110;&#102;&#111;@...) need decoding
    # before they look like an email at all; some sites encode them twice.
    plain = htmlmod.unescape(htmlmod.unescape(text))
    for m in _MAILTO_RE.finditer(plain):
        found.add(urllib.parse.unquote(m.group(1)).strip().strip(".,;"))
    for m in _around(plain, _at_positions(plain), _EMAIL_RE):
        found.add(m.group(0).strip().strip(".,;"))
    for m in _CFEMAIL_RE.finditer(text):
        found.add(_cf_decode(m.group(1)))
    marks = [m.start() for m in _AT_MARK_RE.finditer(plain)]
    for m in _around(plain, marks, _AT_DOT_RE):
        dom = re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", m.group(2),
                     flags=re.I)
        found.add(f"{m.group(1)}@{dom}")
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
    return sorted(out)


# Inboxes on a clinic or hospital site that are not where a study team reads
# a referral: complaints, patient relations, donations, records, IT support.
_WRONG_DESK = ("relations", "complaint", "feedback", "concern", "experience",
               "foi", "accessib", "communicat", "foundation", "donat",
               "giving", "records", "release", "mychart",
               "helpdesk", "customercare", "orders", "store", "shop", "sales",
               "medicalrecords", "ombuds", "quality", "safety")


def _skip_email(email, sponsor=""):
    dom = _domain(email)
    local = _local(email)
    if any(k in local for k in _WRONG_DESK):
        return True
    if any(dom == d or dom.endswith("." + d) for d in _SKIP_DOMAINS):
        return True
    if any(dom == d or dom.endswith("." + d) for d in _SPONSOR_DOMAINS):
        return True
    if db.is_non_human_local(local):
        return True
    if not re.match(r"^[a-z0-9]", local):
        return True
    return False


def _score(email, facility, city, pi_name=""):
    local = _local(email)
    dom = _domain(email)
    fac = _norm(facility)
    tokens = [t for t in fac.split() if len(t) > 2]
    score = 0
    if any(p in local for p in _STUDY_LOCAL):
        score += 60
    elif any(p in local for p in _PREFERRED_LOCAL):
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


def _name_words(facility):
    return [w for w in _norm(facility).split() if w not in _STOP]


def _distinct_words(facility, city="", state=""):
    """The words that make this clinic's name its own: not generic, not the
    city or region it sits in."""
    place = set(_norm(city).split()) | set(_norm(state).split())
    return [w for w in _name_words(facility)
            if w not in _GENERIC and w not in place and len(w) >= 4]


def _guess_hosts(facility, country=""):
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
    for tld in _COUNTRY_TLDS.get(_norm(country), ()):
        hosts.append(joined + "." + tld)
    if (first not in _US_ABBR and first not in _GENERIC and len(first) >= 4
            and len(words) > 1):
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


def _exact_name_domain(host, facility):
    """The domain is the clinic's whole name, a guess built from it, or its
    initials: milestoneresearch.ca, azresearchcenter.com, lhsc.on.ca."""
    label = re.sub(r"[^a-z0-9]", "", _base_domain(host).split(".")[0])
    words = _name_words(facility)
    if len(label) < 3 or not words:
        return False
    slug = "".join(words)
    if label == slug or (len(slug) >= 6 and slug in label):
        return True
    if any(label == re.sub(r"[^a-z0-9]", "", g.split(".")[0])
           for g in _guess_hosts(facility)):
        return True
    initials = "".join(w[0] for w in words)
    return len(initials) >= 3 and label.startswith(initials)


def _domain_matches(host, facility, city="", state="", strict=False):
    """True when the site's domain is plainly this clinic's: its whole name,
    a distinctive word of it, or its initials (lhsc.on.ca). `strict` is for
    search results, where one shared word is not enough (velocitytruck.ca is
    not Velocity Clinical Research)."""
    label = re.sub(r"[^a-z0-9]", "", _base_domain(host).split(".")[0])
    if len(label) < 3:
        return False
    words = _name_words(facility)
    if not words:
        return False
    slug = "".join(words)
    # The whole name inside the domain (milestoneresearchinc), never a piece
    # of it: research.com is not Milestone Research.
    if label == slug or (len(slug) >= 6 and slug in label):
        return True
    for guess in _guess_hosts(facility):
        if label == re.sub(r"[^a-z0-9]", "", guess.split(".")[0]):
            return True
    initials = "".join(w[0] for w in words)
    if len(initials) >= 3 and label.startswith(initials):
        return True
    distinct = _distinct_words(facility, city, state)
    if strict:
        hits = [w for w in words if len(w) >= 3 and w in label]
        return any(label == w for w in distinct) or (
            len(hits) >= 2 and any(w in label for w in distinct)) or any(
            len(w) >= 7 and label.startswith(w) for w in distinct)
    return any(label.startswith(w) or label.endswith(w)
               or (len(w) >= 6 and w in label) for w in distinct)


def _page_text(body):
    body = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", body or "")
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return " " + _norm(htmlmod.unescape(body)) + " "


def _site_verified(text, facility, city, state="", zip_code=""):
    """The site names this clinic and the city it is in (and its region or
    postal code, when the listing gives one)."""
    c = _norm(city)
    if c and f" {c} " not in text:
        return False
    st = _norm(state)
    if st:
        abbr = _REGION_ABBR.get(st, "")
        z = re.sub(r"[^a-z0-9]", "", (zip_code or "").lower())
        compact = text.replace(" ", "")
        if not (f" {st} " in text
                or (abbr and c and f" {c} {abbr} " in text)
                or (len(z) >= 5 and z in compact)):
            return False
    phrase = " ".join(_name_words(facility))
    if phrase and f" {phrase} " in text:
        return True
    distinct = _distinct_words(facility, city, state)
    return bool(distinct) and all(f" {w} " in text for w in distinct)


def _condition_stems(conditions):
    """Short stems that find a condition's page: obesity -> obesi."""
    skip = {"disease", "diseases", "disorder", "disorders", "syndrome",
            "chronic", "acute", "with", "without", "type", "people",
            "patients", "adults", "adult", "excess", "body", "loss", "healthy",
            "volunteers", "condition", "conditions", "other", "mild",
            "moderate", "severe", "stage", "advanced", "early"}
    stems = []
    for cond in conditions or ():
        for w in _norm(cond).split():
            if len(w) < 4 or w in skip or w in _GENERIC:
                continue
            stem = w[:5] if len(w) >= 5 else w
            if stem not in stems:
                stems.append(stem)
    return stems[:8]


def _links(body, page_url, base):
    out = []
    for m in re.finditer(
            r"(?is)<a\s[^>]{0,2000}?href\s*=\s*[\"']([^\"'#]{1,2000})[^\"']{0,2000}"
            r"[\"'][^>]{0,2000}>(.{0,3000}?)</a>",
            body or ""):
        href = htmlmod.unescape(m.group(1)).strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        url = urllib.parse.urljoin(page_url, href)
        if not url.lower().startswith(("http://", "https://")):
            continue
        if _base_domain(_host(url)) != base or _SKIP_LINK_RE.search(url):
            continue
        anchor = _norm(re.sub(r"(?s)<[^>]+>", " ", m.group(2)))
        out.append((url.split("#")[0], anchor))
    return out


def _link_score(url, anchor, stems, city):
    path = urllib.parse.urlsplit(url).path.lower()
    blob = path + " " + (anchor or "")
    score = 0
    if stems and any(s in blob for s in stems):
        score += 6
    c = _norm(city).replace(" ", "")
    if c and c in blob.replace(" ", "").replace("-", ""):
        score += 3
    if "contact" in blob:
        score += 4
    if re.search(r"stud|trial|research|participa|volunteer|enrol|patient"
                 r"|join|recruit|location", blob):
        score += 2
    if re.search(r"privacy|terms|career|jobs?\b|blog|news|press|login|cart"
                 r"|shop|donat|sitemap", blob):
        score -= 5
    return score


def _search_ddg(query, deadline):
    data = urllib.parse.urlencode({"q": query}).encode()
    req = urllib.request.Request(
        "https://html.duckduckgo.com/html/", data=data,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
                 " AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=_remaining(deadline)) as r:
        body = r.read(400_000).decode("utf-8", errors="ignore")
    out = []
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', body):
        href = htmlmod.unescape(href)
        m = re.search(r"uddg=([^&]+)", href)
        out.append(urllib.parse.unquote(m.group(1)) if m else href)
    return out


def _search_bing(query, deadline):
    req = urllib.request.Request(
        "https://www.bing.com/search?" + urllib.parse.urlencode(
            {"q": query, "setlang": "en"}),
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
                 " AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=_remaining(deadline)) as r:
        body = r.read(600_000).decode("utf-8", errors="ignore")
    out = []
    for href in re.findall(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"', body):
        href = htmlmod.unescape(href)
        m = re.search(r"[?&]u=a1([^&]+)", href)
        if m:
            enc = m.group(1)
            try:
                href = base64.urlsafe_b64decode(
                    enc + "=" * (-len(enc) % 4)).decode("utf-8", "ignore")
            except ValueError:
                continue
        out.append(href)
    return out


def _search_ddg_lite(query, deadline):
    data = urllib.parse.urlencode({"q": query}).encode()
    req = urllib.request.Request(
        "https://lite.duckduckgo.com/lite/", data=data,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
                 " AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=_remaining(deadline)) as r:
        body = r.read(400_000).decode("utf-8", errors="ignore")
    out = []
    for href in re.findall(r'<a[^>]+href="([^"]+)"[^>]*class=.result-link', body):
        href = htmlmod.unescape(href)
        m = re.search(r"uddg=([^&]+)", href)
        out.append(urllib.parse.unquote(m.group(1)) if m else href)
    return out


_SEARCHERS = (("duckduckgo", _search_ddg), ("duckduckgo-lite", _search_ddg_lite),
              ("bing", _search_bing))


def _remaining(deadline):
    return max(1.0, min(_TIMEOUT, deadline - time.monotonic()))


def _is_directory(host):
    dom = _base_domain(host)
    h = (host or "").lower()
    return any(dom == d or h == d or h.endswith("." + d)
               for d in _DIRECTORY_DOMAINS)


def _candidate_sites(facility, city, state, country, deadline, trail):
    """Hosts that might be the clinic's website, best first: search results
    whose domain matches the clinic, then guesses from its name. Returns
    (hosts, searched) where searched is True when a search engine answered."""
    query = " ".join(p for p in (facility, city, state or country) if p)
    exact, loose, searched = [], [], False
    if os.environ.get("CLINIC_SEARCH", "1") != "0":
        for name, fn in _SEARCHERS:
            if time.monotonic() >= deadline:
                break
            try:
                urls = fn(query, deadline)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                trail.append(f"search {name} failed: {type(e).__name__}")
                continue
            trail.append(f"search {name}: {len(urls)} results")
            # Bing's answers to a script are often unrelated; only a
            # DuckDuckGo answer is trusted enough to remember a miss by.
            if urls and name != "bing":
                searched = True
            for u in urls:
                host = _host(u)
                if not host or _is_directory(host):
                    continue
                if _exact_name_domain(host, facility):
                    exact.append(host)
                elif _domain_matches(host, facility, city, state, strict=True):
                    loose.append(host)
            if exact:
                break
    # The clinic's whole name as a domain first, then guesses built from it,
    # then search results that only share a distinctive word with it.
    found = []
    for host in exact + _guess_hosts(facility, country) + loose:
        if _base_domain(host) not in [_base_domain(h) for h in found]:
            found.append(host)
    return found, searched


def _crawl_site(host, facility, city, state, zip_code, stems, sponsor,
                pi_name, deadline, trail, crawled=None):
    """Read up to _MAX_PAGES pages of one site, most promising first. Returns
    (verified, [(score, email, page_url)])."""
    start = f"https://{host}/"
    try:
        final, body = _fetch(start, timeout=_remaining(deadline))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        trail.append(f"{host}: unreachable ({type(e).__name__})")
        return False, []
    base = _base_domain(_host(final))
    if _is_directory(base) or not _domain_matches(base, facility, city, state):
        trail.append(f"{host}: redirected to {base}, not the clinic's domain")
        return False, []
    if crawled is not None:
        if base in crawled:
            trail.append(f"{host}: same site as {base}, already read")
            return False, []
        crawled.add(base)
    pages = [(final, body, "")]
    seen = {final.rstrip("/"), start.rstrip("/")}
    frontier = {}
    verified = False
    text_seen = ""
    found = []
    fetched = 1
    on_condition_page = False
    while pages:
        url, body, anchor = pages.pop()
        text = _page_text(body)
        text_seen += text
        title = _norm(" ".join(re.findall(r"(?is)<title[^>]*>(.*?)</title>", body)))
        about = (urllib.parse.urlsplit(url).path.lower() + " " + anchor
                 + " " + title)
        is_home = urllib.parse.urlsplit(url).path.strip("/") == ""
        relevant = is_home or bool(re.search(
            r"research|trial|stud|clinical|contact|volunteer|participa|enrol"
            r"|recruit", about)) or bool(stems and any(s in about for s in stems))
        weight = 0
        if stems and any(s in about for s in stems):
            weight += 30
        elif stems and sum(text.count(f" {s}") for s in stems) >= 3:
            weight += 15
        if "contact" in about:
            weight += 10
        c = _norm(city).replace(" ", "")
        if c and c in about.replace(" ", "").replace("-", ""):
            weight += 10
        for email in (_emails_in(body) if relevant else ()):
            if _skip_email(email, sponsor):
                continue
            edom = _domain(email)
            if _base_domain(edom) != base and not _domain_matches(
                    edom, facility, city, state):
                continue
            found.append((_score(email, facility, city, pi_name) + weight,
                          email, url))
            if weight >= 30:
                on_condition_page = True
        if not verified and _site_verified(text_seen, facility, city, state,
                                           zip_code):
            verified = True
        for link, a in _links(body, url, base):
            key = link.rstrip("/")
            if key in seen:
                continue
            s = _link_score(link, a, stems, city)
            if s > frontier.get(key, (-99, "", ""))[0]:
                frontier[key] = (s, link, a)
        # A study page for this condition with an address on it is the best
        # answer there is; stop reading once one is in hand.
        if verified and on_condition_page:
            break
        if fetched >= _MAX_PAGES or time.monotonic() >= deadline:
            break
        # A clinic's name and city are on its home or contact page. Three
        # pages without them is some other organization with a similar name.
        if not verified and fetched >= 3:
            break
        ranked = sorted(frontier.values(), key=lambda x: -x[0])
        nxt = next((x for x in ranked if x[0] > 0), None)
        if not nxt:
            break
        frontier.pop(nxt[1].rstrip("/"), None)
        seen.add(nxt[1].rstrip("/"))
        try:
            got_url, got_body = _fetch(nxt[1], timeout=_remaining(deadline))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            fetched += 1
            continue
        fetched += 1
        if _base_domain(_host(got_url)) != base:
            continue
        pages.append((got_url, got_body, nxt[2]))
    trail.append(f"{base}: read {fetched} page(s), verified={verified}, "
                 f"emails={sorted({e for _, e, _ in found})}")
    return verified, found


def _cache_key(facility, city, sponsor, stems):
    return json.dumps([_norm(facility), _norm(city), _norm(sponsor),
                       sorted(stems)])


def _cache_get(key):
    try:
        return db.get_clinic_lookup(key, _CACHE_HIT_TTL, _CACHE_MISS_TTL)
    except Exception:
        return None


def _cache_put(key, recs):
    try:
        db.save_clinic_lookup(key, recs)
    except Exception:
        pass


# "Clinical Research Institute, Merz Investigational Site #0010487": a real
# clinic with the sponsor's site code tacked on. Search for the clinic.
_SITE_CODE_SUFFIX_RE = re.compile(
    r"\s*[,\-\u2013]\s*[^,]*\b(investigational|investigative|research|study"
    r"|clinical|trial)\s+site\b[^,]*$", re.I)


def _clean_facility(facility):
    facility = (facility or "").strip()
    trimmed = _SITE_CODE_SUFFIX_RE.sub("", facility).strip(" ,-")
    return trimmed or facility


def _site_parts(site):
    site = site or {}
    facility = _clean_facility(site.get("facility"))
    city = (site.get("city") or "").strip()
    state = (site.get("state") or "").strip()
    country = (site.get("country") or "").strip()
    zip_code = (site.get("zip") or "").strip()
    pi_name = ""
    for c in site.get("contacts") or []:
        role = (c.get("role") or "").upper()
        if "INVESTIGATOR" in role and c.get("name"):
            pi_name = c.get("name")
            break
    return facility, city, state, country, zip_code, pi_name


def _is_anonymous(facility, sponsor=""):
    if not _norm(facility) or _ANONYMOUS_SITE_RE.search(facility):
        return True
    # "Pfizer Site 1234": the sponsor's name for an unnamed site, not a clinic.
    sp = {w for w in _norm(sponsor).split() if w not in _STOP}
    names = _name_words(facility)
    rest = [w for w in names
            if w not in sp and w not in _GENERIC and not w.isdigit()]
    return bool(sp & set(names)) and not rest


def cached_site_emails(site, sponsor="", conditions=()):
    """Only what an earlier lookup already found; never touches the network.
    For pages that render while someone waits."""
    facility, city, *_ = _site_parts(site)
    if not facility:
        return []
    key = _cache_key(facility, city, sponsor, _condition_stems(conditions))
    if key in _MEMO:
        return _MEMO[key]
    hit = _cache_get(key)
    return hit if hit is not None else []


def lookup_site_emails(site, sponsor="", conditions=(), trail=None,
                       use_cache=True):
    """Return [{email, name, role, facility, city, source, page}] for one
    CT.gov site: the clinic's own address from its own website, or []."""
    facility, city, state, country, zip_code, pi_name = _site_parts(site)
    trail = trail if trail is not None else []
    if not facility:
        return []
    if _is_anonymous(facility, sponsor):
        trail.append(f"{facility!r} names no clinic; not searched")
        return []
    stems = _condition_stems(conditions)
    key = _cache_key(facility, city, sponsor, stems)
    if use_cache and key in _MEMO:
        return _MEMO[key]
    hit = _cache_get(key) if use_cache else None
    if hit is not None:
        trail.append("cached")
        _MEMO[key] = hit
        return hit

    deadline = time.monotonic() + _BUDGET_SECONDS
    hosts, searched = _candidate_sites(facility, city, state, country,
                                       deadline, trail)
    best, verified_any, tried, crawled = None, False, 0, set()
    for host in hosts:
        if tried >= _MAX_SITES or time.monotonic() >= deadline:
            break
        tried += 1
        verified, found = _crawl_site(host, facility, city, state, zip_code,
                                      stems, sponsor, pi_name, deadline, trail,
                                      crawled)
        if not verified:
            continue
        verified_any = True
        if found:
            found.sort(key=lambda x: (-x[0], x[1]))
            best = found[0]
            break

    picked = []
    if best:
        _, email, page = best
        pi_last = ""
        if pi_name:
            parts = _norm(pi_name).replace(" md", "").replace(" dr", "").split()
            pi_last = parts[-1] if parts else ""
        picked.append({
            "email": email,
            "name": pi_name if pi_last and pi_last in _local(email) else "",
            "role": "LOOKUP",
            "facility": facility,
            "city": city,
            "source": "lookup",
            "page": page,
        })
    _MEMO[key] = picked
    # Remember a miss only when the search really ran or the site was read,
    # so a search engine that was down does not hide a clinic for a day.
    if picked or searched or verified_any:
        _cache_put(key, picked)
    return picked
