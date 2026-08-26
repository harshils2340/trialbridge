import http.client
import json
import csv
import os
import argparse
import re

from urllib.parse import urlparse, urlencode

from openai import OpenAI


# =========================================================
# CONFIG
# =========================================================

theCacheDirectory = "cache"

theCompanyCacheDirectory = os.path.join(
    theCacheDirectory,
    "companies"
)

theHunterEmailCacheDirectory = os.path.join(
    theCacheDirectory,
    "hunter_emails"
)

theOpenAiModel = "gpt-5.6-luna"


# Keep these queries simple because Serper free accounts
# can reject complicated OR expressions.
#
# Our ICP is the small independent clinical research
# site: private practices, standalone research clinics
# and small site groups. Those places have real trials
# but no engineering team, so their trial finder is
# usually weak or missing entirely.
#
# Large academic medical centres and hospital networks
# are deliberately under-weighted here. They have long
# procurement cycles and often an in-house portal, so
# the queries lean on the vocabulary small sites use to
# describe themselves.
thePeopleSearchQueries = [
    'site:linkedin.com/in "clinical research coordinator" "research site" seattle',
    'site:linkedin.com/in "clinical research coordinator" "private practice" seattle',
    'site:linkedin.com/in "clinical trial coordinator" "research clinic" seattle',
    'site:linkedin.com/in "patient recruitment coordinator" seattle',
    'site:linkedin.com/in "site director" "clinical research" seattle'
]


# Name fragments that almost always mean the employer is
# far bigger than our ICP. These are cheap to check and
# save an OpenAI call on obviously-too-large prospects.
theLargeOrganizationKeywords = [
    "university",
    "universite",
    "college",
    "school of medicine",
    "faculty of medicine",
    "academic",
    "health network",
    "health system",
    "health authority",
    "hospital",
    "hopital",
    "medical center",
    "medical centre",
    "cancer centre",
    "cancer center",
    "children's",
    "sunnybrook",
    "mount sinai",
    "kaiser",
    "cleveland clinic",
    "mayo clinic",
    "pharmaceutical",
    "pharmaceuticals",
    "pharma",
    "biopharma",
    "therapeutics",
    "laboratories",
    "iqvia",
    "parexel",
    "icon plc",
    "syneos",
    "fortrea",
    "medpace",
    "ppd",
    "labcorp",
    "thermo fisher",
    "pfizer",
    "novartis",
    "astrazeneca",
    "johnson & johnson",
    "merck",
    "sanofi",
    "gsk",
    "roche",
    "bayer",
    "abbvie",
    "amgen",
    "eli lilly"
]


# Name fragments that suggest an independent site, which
# is exactly who we want to reach.
theSmallSiteKeywords = [
    # "research" on its own is deliberately broad. The
    # large-organization list is checked first and wins,
    # so university and hospital research arms are still
    # caught before this matches.
    "research",
    "clinical research",
    "research site",
    "research clinic",
    "research center",
    "research centre",
    "research associates",
    "research group",
    "research partners",
    "research solutions",
    "trials",
    "clinical studies",
    "medical clinic",
    "family practice",
    "family medicine",
    "private practice",
    "physicians",
    "medical group",
    "wellness",
    "dermatology",
    "psychiatry",
    "neurology",
    "cardiology",
    "rheumatology",
    "endocrinology"
]


# Leads scoring below this on site-size fit are dropped
# unless --include-large is passed.
theDefaultMinSiteFit = 4.0


theBlockedDomains = [
    "linkedin.com",
    "clinicaltrials.gov",
    "facebook.com",
    "instagram.com",
    "indeed.com",
    "glassdoor.com",
    "youtube.com",
    "wikipedia.org",
    "twitter.com",
    "x.com"
]


# =========================================================
# CACHE HELPERS
# =========================================================

def createCacheDirectories():
    os.makedirs(
        theCacheDirectory,
        exist_ok=True
    )

    os.makedirs(
        theCompanyCacheDirectory,
        exist_ok=True
    )

    os.makedirs(
        theHunterEmailCacheDirectory,
        exist_ok=True
    )


def safeFileName(aValue):
    myValue = (
        aValue or ""
    ).lower().strip()

    myValue = re.sub(
        r"[^a-z0-9]+",
        "_",
        myValue
    )

    return myValue.strip("_")


def saveJson(aPath, aData):
    with open(
        aPath,
        "w",
        encoding="utf-8"
    ) as myFile:

        json.dump(
            aData,
            myFile,
            indent=4,
            ensure_ascii=False
        )


def loadJson(aPath):
    with open(
        aPath,
        "r",
        encoding="utf-8"
    ) as myFile:

        return json.load(
            myFile
        )


# =========================================================
# URL HELPERS
# =========================================================

def getDomain(aUrl):
    try:
        myDomain = urlparse(
            aUrl or ""
        ).netloc.lower()

        if myDomain.startswith("www."):
            myDomain = myDomain[4:]

        return myDomain

    except Exception:
        return ""


def isBlockedUrl(aUrl):
    myDomain = getDomain(
        aUrl
    )

    for myBlockedDomain in theBlockedDomains:
        if (
            myDomain == myBlockedDomain
            or myDomain.endswith(
                "." + myBlockedDomain
            )
        ):
            return True

    return False


def getLinkedinHandle(aLinkedinUrl):
    if not aLinkedinUrl:
        return ""

    try:
        myPath = urlparse(
            aLinkedinUrl
        ).path

        if "/in/" not in myPath:
            return ""

        myHandle = myPath.split(
            "/in/",
            1
        )[1]

        myHandle = myHandle.split(
            "/",
            1
        )[0]

        myHandle = myHandle.split(
            "?",
            1
        )[0]

        return myHandle.strip()

    except Exception:
        return ""


# =========================================================
# SERPER
# =========================================================

def serperSearch(
    aQuery,
    aNumResults=10
):
    myApiKey = os.getenv(
        "SERPER_API_KEY"
    )

    if not myApiKey:
        raise Exception(
            "SERPER_API_KEY is not set.\n"
            "PowerShell:\n"
            '$env:SERPER_API_KEY="your_key_here"'
        )

    myConn = http.client.HTTPSConnection(
        "google.serper.dev"
    )

    myPayload = json.dumps({
        "q": aQuery,
        "num": aNumResults
    })

    myHeaders = {
        "X-API-KEY": myApiKey,
        "Content-Type": "application/json"
    }

    myConn.request(
        "POST",
        "/search",
        myPayload,
        myHeaders
    )

    myRes = myConn.getresponse()

    myBody = myRes.read().decode(
        "utf-8"
    )

    if myRes.status != 200:
        raise Exception(
            f"Serper failed with status "
            f"{myRes.status}: {myBody}"
        )

    return json.loads(
        myBody
    )


# =========================================================
# OPENAI
# =========================================================

def extractJson(aText):
    myText = (
        aText or ""
    ).strip()

    myText = re.sub(
        r"^```json\s*",
        "",
        myText,
        flags=re.IGNORECASE
    )

    myText = re.sub(
        r"^```\s*",
        "",
        myText
    )

    myText = re.sub(
        r"\s*```$",
        "",
        myText
    )

    myText = myText.strip()

    try:
        return json.loads(
            myText
        )

    except json.JSONDecodeError:
        pass

    myArrayStart = myText.find("[")
    myObjectStart = myText.find("{")

    if myArrayStart == -1:
        myStart = myObjectStart

    elif myObjectStart == -1:
        myStart = myArrayStart

    else:
        myStart = min(
            myArrayStart,
            myObjectStart
        )

    myArrayEnd = myText.rfind("]")
    myObjectEnd = myText.rfind("}")

    myEnd = max(
        myArrayEnd,
        myObjectEnd
    )

    if (
        myStart == -1
        or myEnd == -1
        or myEnd < myStart
    ):
        raise Exception(
            "OpenAI response did not contain valid JSON:\n"
            + myText
        )

    return json.loads(
        myText[
            myStart:myEnd + 1
        ]
    )


def askOpenAiForJson(aPrompt):
    if not os.getenv(
        "OPENAI_API_KEY"
    ):
        raise Exception(
            "OPENAI_API_KEY is not set.\n"
            "PowerShell:\n"
            '$env:OPENAI_API_KEY="your_key_here"'
        )

    myClient = OpenAI()

    myResponse = myClient.responses.create(
        model=theOpenAiModel,
        input=(
            "Return ONLY valid JSON. "
            "Do not use markdown fences. "
            "Do not include commentary before "
            "or after the JSON.\n\n"
            + aPrompt
        )
    )

    return extractJson(
        myResponse.output_text
    )


# =========================================================
# PEOPLE SEARCH
# =========================================================

def getPeopleSearchData(aRefresh):
    myCachePath = os.path.join(
        theCacheDirectory,
        "people_search.json"
    )

    if (
        not aRefresh
        and os.path.exists(
            myCachePath
        )
    ):
        print(
            "Using cached people search..."
        )

        return loadJson(
            myCachePath
        )

    print(
        "Searching Serper for clinical "
        "research coordinators..."
    )

    myAllResults = []
    mySeenLinks = set()

    for myQuery in thePeopleSearchQueries:
        print(
            f"Searching: {myQuery}"
        )

        try:
            myData = serperSearch(
                myQuery,
                10
            )

        except Exception as myError:
            print(
                f"Search failed: {myError}"
            )

            continue

        for myResult in myData.get(
            "organic",
            []
        ):
            myLink = (
                myResult.get(
                    "link"
                )
                or ""
            ).strip()

            if not myLink:
                continue

            if myLink in mySeenLinks:
                continue

            mySeenLinks.add(
                myLink
            )

            myAllResults.append(
                myResult
            )

    myCombinedData = {
        "queries":
            thePeopleSearchQueries,

        "organic":
            myAllResults
    }

    saveJson(
        myCachePath,
        myCombinedData
    )

    return myCombinedData


# =========================================================
# RAW PEOPLE CSV
# =========================================================

def saveRawPeopleCsv(aResults):
    with open(
        "people_raw.csv",
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as myFile:

        myWriter = csv.writer(
            myFile
        )

        myWriter.writerow([
            "title",
            "subtitle",
            "linkedin_url",
            "snippet"
        ])

        for myResult in aResults:
            myWriter.writerow([
                myResult.get(
                    "title"
                ) or "",

                myResult.get(
                    "subtitle"
                ) or "",

                myResult.get(
                    "link"
                ) or "",

                myResult.get(
                    "snippet"
                ) or ""
            ])


# =========================================================
# CLEAN PEOPLE USING OPENAI
# =========================================================

def getCleanPeople(
    aResults,
    aRefresh
):
    myCachePath = os.path.join(
        theCacheDirectory,
        "people_clean.json"
    )

    if (
        not aRefresh
        and os.path.exists(
            myCachePath
        )
    ):
        print(
            "Using cached cleaned people..."
        )

        return loadJson(
            myCachePath
        )

    print(
        "Cleaning people with OpenAI..."
    )

    myPrompt = f"""
We are finding potential customers for BridgeMD,
a clinical-trial patient recruitment product.

Extract relevant people from these PUBLIC Google
search results.

For every relevant person return:

- name
- title
- company
- location
- linkedin_url

Relevant areas include:

- clinical research coordination
- clinical trial coordination
- patient recruitment
- clinical research site operations
- clinical research management

Our ideal customer works at a SMALL, INDEPENDENT
clinical research site: a private practice, a
standalone research clinic, or a small site group.

Prefer people at those employers over people at
hospital networks, academic medical centres, large
CROs or pharmaceutical sponsors. Still extract the
larger-employer people when they appear, since they
are scored and filtered later.

Record the employer exactly as written in the source,
without expanding abbreviations or substituting a
parent organization.

Prefer Ontario / Greater Toronto Area.

IMPORTANT:

- Do not invent information.
- If something is unknown, use an empty string.
- NEVER use null.
- Preserve the LinkedIn URL from the source result.
- Only use information supported by the supplied results.

Return:

[
    {{
        "name": "",
        "title": "",
        "company": "",
        "location": "",
        "linkedin_url": ""
    }}
]

SEARCH RESULTS:

{json.dumps(aResults, ensure_ascii=False)}
"""

    myPeople = askOpenAiForJson(
        myPrompt
    )

    if not isinstance(
        myPeople,
        list
    ):
        raise Exception(
            "Expected OpenAI to return "
            "a JSON array."
        )

    myCleanPeople = []
    mySeenLinks = set()

    for myPerson in myPeople:
        if not isinstance(
            myPerson,
            dict
        ):
            continue

        myLink = (
            myPerson.get(
                "linkedin_url"
            )
            or ""
        ).strip()

        if (
            myLink
            and myLink in mySeenLinks
        ):
            continue

        if myLink:
            mySeenLinks.add(
                myLink
            )

        myCleanPeople.append({
            "name":
                (
                    myPerson.get(
                        "name"
                    )
                    or ""
                ).strip(),

            "title":
                (
                    myPerson.get(
                        "title"
                    )
                    or ""
                ).strip(),

            "company":
                (
                    myPerson.get(
                        "company"
                    )
                    or ""
                ).strip(),

            "location":
                (
                    myPerson.get(
                        "location"
                    )
                    or ""
                ).strip(),

            "linkedin_url":
                myLink
        })

    saveJson(
        myCachePath,
        myCleanPeople
    )

    return myCleanPeople


def saveCleanPeopleCsv(aPeople):
    with open(
        "people_clean.csv",
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as myFile:

        myFields = [
            "name",
            "title",
            "company",
            "location",
            "linkedin_url"
        ]

        myWriter = csv.DictWriter(
            myFile,
            fieldnames=myFields
        )

        myWriter.writeheader()

        for myPerson in aPeople:
            myWriter.writerow({
                myField:
                    myPerson.get(
                        myField
                    )
                    or ""

                for myField in myFields
            })


# =========================================================
# COMPANY SEARCH
# =========================================================

def filterCompanyResults(aResults):
    myResults = []

    for myResult in aResults:
        myUrl = (
            myResult.get(
                "link"
            )
            or ""
        )

        if isBlockedUrl(
            myUrl
        ):
            continue

        myResults.append(
            myResult
        )

    return myResults


def searchCompanyTrialPages(aCompany):
    myQuery = (
        f'"{aCompany}" "clinical trials"'
    )

    myData = serperSearch(
        myQuery,
        10
    )

    myResults = filterCompanyResults(
        myData.get(
            "organic",
            []
        )
    )

    if not myResults:
        myFallbackQuery = (
            f'"{aCompany}" "research studies"'
        )

        myData = serperSearch(
            myFallbackQuery,
            10
        )

        myResults = filterCompanyResults(
            myData.get(
                "organic",
                []
            )
        )

    return myResults


def getCompanySearchResults(
    aCompany,
    aRefresh
):
    mySafeCompany = safeFileName(
        aCompany
    )

    myCachePath = os.path.join(
        theCompanyCacheDirectory,
        f"{mySafeCompany}_search.json"
    )

    if (
        not aRefresh
        and os.path.exists(
            myCachePath
        )
    ):
        print(
            f"Using cached search "
            f"for {aCompany}..."
        )

        return loadJson(
            myCachePath
        )

    print(
        f"Searching trial pages "
        f"for {aCompany}..."
    )

    try:
        myResults = searchCompanyTrialPages(
            aCompany
        )

    except Exception as myError:
        print(
            f"Company search failed for "
            f"{aCompany}: {myError}"
        )

        myResults = []

    saveJson(
        myCachePath,
        myResults
    )

    return myResults


# =========================================================
# ORGANIZATION SIZE HEURISTIC
# =========================================================

def getNameSizeHint(aCompany):
    """
    Cheap name-only read on how big an employer is.

    Returns a hint dictionary the model prompt can lean
    on, so obviously-large organizations are recognised
    even when the search snippets are thin.
    """

    myCompany = (
        aCompany
        or ""
    ).lower()

    myLargeMatches = []
    mySmallMatches = []

    for myKeyword in theLargeOrganizationKeywords:
        if myKeyword in myCompany:
            myLargeMatches.append(
                myKeyword
            )

    for myKeyword in theSmallSiteKeywords:
        if myKeyword in myCompany:
            mySmallMatches.append(
                myKeyword
            )

    if myLargeMatches:
        myHint = "likely_large"

    elif mySmallMatches:
        myHint = "likely_small"

    else:
        myHint = "unknown"

    return {
        "hint":
            myHint,

        "large_matches":
            myLargeMatches,

        "small_matches":
            mySmallMatches
    }


# =========================================================
# COMPANY TRIAL FINDER SCORE
# =========================================================

def scoreCompanyTrialFinder(
    aCompany,
    aSearchResults
):
    myNameHint = getNameSizeHint(
        aCompany
    )

    if not aSearchResults:
        # No evidence either way on size, so fall back to
        # the name hint rather than assuming a good fit.
        if myNameHint["hint"] == "likely_large":
            myFallbackFit = 2.0
            myFallbackType = "large_organization"

        elif myNameHint["hint"] == "likely_small":
            myFallbackFit = 7.0
            myFallbackType = "independent_site"

        else:
            myFallbackFit = 5.0
            myFallbackType = "unknown"

        return {
            "company":
                aCompany,

            "trial_finder_url":
                "",

            "trial_finder_weakness":
                10,

            "organization_type":
                myFallbackType,

            "site_size_fit":
                myFallbackFit,

            "reason":
                (
                    "No obvious patient-facing "
                    "clinical trial finder was found."
                ),

            "confidence":
                "low"
        }

    myPrompt = f"""
We are evaluating whether this company is a strong
sales prospect for BridgeMD, a patient-friendly
clinical-trial discovery and recruitment platform.

Our ideal customer is a SMALL, INDEPENDENT clinical
research site: a private practice, a standalone
research clinic, or a small site group running a
handful of trials. They have real studies but no
engineering team, so their trial discovery experience
is usually weak.

Large academic medical centres, hospital networks,
big CROs and pharmaceutical sponsors are POOR fits.
They move slowly and often already have an in-house
portal.

COMPANY:

{aCompany}

NAME-BASED SIZE HINT (heuristic, may be wrong):

{json.dumps(myNameHint, ensure_ascii=False)}

PUBLIC GOOGLE SEARCH RESULTS:

{json.dumps(aSearchResults, ensure_ascii=False)}

TASK ONE:

Determine which result, if any, appears to be the
company's primary patient-facing page for finding
or browsing clinical trials.

Then score the apparent trial discovery experience.

SCORE:

0 = excellent
2 = very good
4 = acceptable
6 = noticeably difficult
8 = very poor
10 = no meaningful patient-facing trial finder

WEAK SIGNALS:

- no condition search
- no location filtering
- no meaningful filtering
- giant tables
- PDFs
- long unstructured lists
- technical study descriptions
- unclear eligibility
- no clear "I'm interested" action
- poor signup/contact flow
- sends patients elsewhere
- sends patients to ClinicalTrials.gov
- research-oriented rather than patient-oriented
- no finder

STRONG SIGNALS:

- condition search
- location search
- understandable study cards
- useful filters
- eligibility summaries
- easy signup or express-interest flow

TASK TWO:

Classify the organization into exactly one
organization_type:

- "independent_site"
    a private practice, standalone research clinic
    or single-location research centre

- "small_site_group"
    a small network of a few research locations

- "large_organization"
    hospital network, academic medical centre,
    health system, large CRO or pharmaceutical
    sponsor

- "unknown"
    the evidence genuinely does not say

Then score site_size_fit, how well the organization
matches our small-site ICP:

10 = clearly a small independent research site
8  = small site group, a few locations
5  = unclear from the evidence
2  = large regional or multi-site organization
0  = major hospital network, academic centre,
     large CRO or pharmaceutical sponsor

LARGE-ORGANIZATION SIGNALS:

- affiliated with a university or medical school
- described as a hospital or health network
- many departments or many locations
- a careers page with a large job board
- press releases and investor relations pages
- named as a trial sponsor rather than a site

SMALL-SITE SIGNALS:

- one or a few clinic locations
- a named physician or small group of physicians
- a simple brochure-style website
- runs trials alongside regular patient care
- described as a "research site" or "site network"
- contact goes to a coordinator, not a department

IMPORTANT:

Only use the provided evidence.

The name-based size hint is a weak prior. Prefer the
search results when they disagree with it.

If evidence is limited, use low confidence.

Do not use null.

Return:

{{
    "company": "{aCompany}",
    "trial_finder_url": "",
    "trial_finder_weakness": 0,
    "organization_type": "unknown",
    "site_size_fit": 5,
    "reason": "",
    "confidence": "low"
}}
"""

    myResult = askOpenAiForJson(
        myPrompt
    )

    if not isinstance(
        myResult,
        dict
    ):
        raise Exception(
            "Expected company score "
            "to be a JSON object."
        )

    return {
        "company":
            (
                myResult.get(
                    "company"
                )
                or aCompany
            ),

        "trial_finder_url":
            (
                myResult.get(
                    "trial_finder_url"
                )
                or ""
            ),

        "trial_finder_weakness":
            myResult.get(
                "trial_finder_weakness",
                5
            ),

        "organization_type":
            (
                myResult.get(
                    "organization_type"
                )
                or "unknown"
            ),

        "site_size_fit":
            myResult.get(
                "site_size_fit",
                5
            ),

        "reason":
            (
                myResult.get(
                    "reason"
                )
                or ""
            ),

        "confidence":
            (
                myResult.get(
                    "confidence"
                )
                or "low"
            )
    }


def getCompanyScore(
    aCompany,
    aSearchResults,
    aRefresh
):
    mySafeCompany = safeFileName(
        aCompany
    )

    # Bumped to v2 when site-size scoring was added, so
    # older cached scores without those fields are not
    # silently reused.
    myCachePath = os.path.join(
        theCompanyCacheDirectory,
        f"{mySafeCompany}_score_v2.json"
    )

    if (
        not aRefresh
        and os.path.exists(
            myCachePath
        )
    ):
        print(
            f"Using cached score "
            f"for {aCompany}..."
        )

        return loadJson(
            myCachePath
        )

    print(
        f"Scoring trial finder "
        f"for {aCompany}..."
    )

    try:
        myScore = scoreCompanyTrialFinder(
            aCompany,
            aSearchResults
        )

    except Exception as myError:
        print(
            f"Scoring failed for "
            f"{aCompany}: {myError}"
        )

        myScore = {
            "company":
                aCompany,

            "trial_finder_url":
                "",

            "trial_finder_weakness":
                5,

            "organization_type":
                "unknown",

            "site_size_fit":
                5,

            "reason":
                "Could not automatically score.",

            "confidence":
                "low"
        }

    saveJson(
        myCachePath,
        myScore
    )

    return myScore


# =========================================================
# COMPANY DOMAIN
# =========================================================

def getOfficialCompanyDomain(
    aSearchResults,
    aCompanyScore
):
    myTrialFinderUrl = (
        aCompanyScore.get(
            "trial_finder_url"
        )
        or ""
    )

    myDomain = getDomain(
        myTrialFinderUrl
    )

    if myDomain:
        return myDomain

    for myResult in aSearchResults:
        myUrl = (
            myResult.get(
                "link"
            )
            or ""
        )

        if isBlockedUrl(
            myUrl
        ):
            continue

        myDomain = getDomain(
            myUrl
        )

        if myDomain:
            return myDomain

    return ""


# =========================================================
# HUNTER
# =========================================================

def hunterRequest(aParams):
    myApiKey = os.getenv(
        "HUNTER_API_KEY"
    )

    if not myApiKey:
        raise Exception(
            "HUNTER_API_KEY is not set.\n"
            "PowerShell:\n"
            '$env:HUNTER_API_KEY="your_key_here"'
        )

    myConn = http.client.HTTPSConnection(
        "api.hunter.io"
    )

    myQueryString = urlencode(
        aParams
    )

    myPath = (
        "/v2/email-finder?"
        + myQueryString
    )

    myHeaders = {
        "X-API-KEY": myApiKey,
        "Accept": "application/json"
    }

    myConn.request(
        "GET",
        myPath,
        headers=myHeaders
    )

    myRes = myConn.getresponse()

    myBody = myRes.read().decode(
        "utf-8"
    )

    try:
        myData = json.loads(
            myBody
        )

    except json.JSONDecodeError:
        myData = {
            "raw":
                myBody
        }

    return (
        myRes.status,
        myData
    )


def parseHunterResult(
    aStatus,
    aResponse,
    aMethod
):
    # Hunter returns 451 when the person has
    # requested that their data not be processed.
    if aStatus == 451:
        return {
            "email":
                "",

            "hunter_score":
                "",

            "hunter_verification":
                "suppressed",

            "hunter_method":
                aMethod,

            "hunter_sources":
                "",

            "hunter_status":
                "suppressed"
        }

    if aStatus == 404:
        return {
            "email":
                "",

            "hunter_score":
                "",

            "hunter_verification":
                "not_found",

            "hunter_method":
                aMethod,

            "hunter_sources":
                "",

            "hunter_status":
                "not_found"
        }

    if aStatus == 429:
        return {
            "email":
                "",

            "hunter_score":
                "",

            "hunter_verification":
                "quota_exceeded",

            "hunter_method":
                aMethod,

            "hunter_sources":
                "",

            "hunter_status":
                "quota_exceeded"
        }

    if aStatus != 200:
        return {
            "email":
                "",

            "hunter_score":
                "",

            "hunter_verification":
                "error",

            "hunter_method":
                aMethod,

            "hunter_sources":
                "",

            "hunter_status":
                f"http_{aStatus}"
        }

    myData = (
        aResponse.get(
            "data"
        )
        or {}
    )

    myEmail = (
        myData.get(
            "email"
        )
        or ""
    ).strip()

    if not myEmail:
        return {
            "email":
                "",

            "hunter_score":
                "",

            "hunter_verification":
                "not_found",

            "hunter_method":
                aMethod,

            "hunter_sources":
                "",

            "hunter_status":
                "not_found"
        }

    myVerification = (
        myData.get(
            "verification"
        )
        or {}
    )

    myVerificationStatus = (
        myVerification.get(
            "status"
        )
        or ""
    )

    mySources = (
        myData.get(
            "sources"
        )
        or []
    )

    mySourceUrls = []

    for mySource in mySources:
        if not isinstance(
            mySource,
            dict
        ):
            continue

        myUrl = (
            mySource.get(
                "uri"
            )
            or ""
        )

        if myUrl:
            mySourceUrls.append(
                myUrl
            )

    return {
        "email":
            myEmail,

        "hunter_score":
            myData.get(
                "score"
            )
            or "",

        "hunter_verification":
            myVerificationStatus,

        "hunter_method":
            aMethod,

        "hunter_sources":
            " | ".join(
                mySourceUrls[:5]
            ),

        "hunter_status":
            "found"
    }


def findHunterEmail(
    aName,
    aCompany,
    aLinkedinUrl,
    aCompanyDomain,
    aRefreshEmails
):
    myCacheKey = safeFileName(
        f"{aName}_{aCompany}_{aLinkedinUrl}"
    )

    myCachePath = os.path.join(
        theHunterEmailCacheDirectory,
        f"{myCacheKey}.json"
    )

    if (
        not aRefreshEmails
        and os.path.exists(
            myCachePath
        )
    ):
        print(
            f"Using cached Hunter email "
            f"for {aName}..."
        )

        return loadJson(
            myCachePath
        )

    print(
        f"Searching Hunter for "
        f"{aName}..."
    )

    myLinkedinHandle = getLinkedinHandle(
        aLinkedinUrl
    )

    # -----------------------------------------------------
    # METHOD 1:
    # LinkedIn handle
    # -----------------------------------------------------

    if myLinkedinHandle:
        try:
            myStatus, myResponse = hunterRequest({
                "linkedin_handle":
                    myLinkedinHandle,

                "max_duration":
                    10
            })

            myResult = parseHunterResult(
                myStatus,
                myResponse,
                "linkedin_handle"
            )

            if (
                myResult["hunter_status"]
                == "found"
            ):
                saveJson(
                    myCachePath,
                    myResult
                )

                return myResult

            # If the person has explicitly requested
            # suppression, do not try alternative lookups.
            if (
                myResult["hunter_status"]
                == "suppressed"
            ):
                saveJson(
                    myCachePath,
                    myResult
                )

                return myResult

            # If Hunter quota is exceeded, don't keep going.
            if (
                myResult["hunter_status"]
                == "quota_exceeded"
            ):
                saveJson(
                    myCachePath,
                    myResult
                )

                return myResult

        except Exception as myError:
            print(
                f"Hunter LinkedIn lookup failed "
                f"for {aName}: {myError}"
            )

    # -----------------------------------------------------
    # METHOD 2:
    # Domain + full name
    # -----------------------------------------------------

    if (
        aCompanyDomain
        and aName
    ):
        try:
            myStatus, myResponse = hunterRequest({
                "domain":
                    aCompanyDomain,

                "full_name":
                    aName,

                "max_duration":
                    10
            })

            myResult = parseHunterResult(
                myStatus,
                myResponse,
                "domain_and_name"
            )

            if (
                myResult["hunter_status"]
                == "found"
            ):
                saveJson(
                    myCachePath,
                    myResult
                )

                return myResult

            if (
                myResult["hunter_status"]
                in [
                    "suppressed",
                    "quota_exceeded"
                ]
            ):
                saveJson(
                    myCachePath,
                    myResult
                )

                return myResult

        except Exception as myError:
            print(
                f"Hunter domain lookup failed "
                f"for {aName}: {myError}"
            )

    # -----------------------------------------------------
    # METHOD 3:
    # Company + full name
    # -----------------------------------------------------

    if (
        aCompany
        and aName
    ):
        try:
            myStatus, myResponse = hunterRequest({
                "company":
                    aCompany,

                "full_name":
                    aName,

                "max_duration":
                    10
            })

            myResult = parseHunterResult(
                myStatus,
                myResponse,
                "company_and_name"
            )

            saveJson(
                myCachePath,
                myResult
            )

            return myResult

        except Exception as myError:
            print(
                f"Hunter company lookup failed "
                f"for {aName}: {myError}"
            )

    myResult = {
        "email":
            "",

        "hunter_score":
            "",

        "hunter_verification":
            "not_found",

        "hunter_method":
            "",

        "hunter_sources":
            "",

        "hunter_status":
            "not_found"
    }

    saveJson(
        myCachePath,
        myResult
    )

    return myResult


# =========================================================
# ROLE SCORE
# =========================================================

def getRoleScore(aTitle):
    myTitle = (
        aTitle
        or ""
    ).lower()

    myScore = 5.0

    if "recruit" in myTitle:
        myScore += 3.0

    if "senior" in myTitle:
        myScore += 1.0

    if "manager" in myTitle:
        myScore += 1.0

    if (
        "clinical research coordinator"
        in myTitle
    ):
        myScore += 1.0

    if (
        "clinical trial coordinator"
        in myTitle
    ):
        myScore += 1.0

    return min(
        myScore,
        10.0
    )


def safeFloat(
    aValue,
    aDefault=5.0
):
    try:
        if aValue is None:
            return aDefault

        return float(
            aValue
        )

    except (
        TypeError,
        ValueError
    ):
        return aDefault


# =========================================================
# BUILD LEADS
# =========================================================

def generateFinalLeads(
    aPeople,
    aRefresh,
    aMinSiteFit
):
    myCompanyMemory = {}
    myFinalLeads = []
    myDroppedForSize = 0

    for myIndex, myPerson in enumerate(
        aPeople,
        start=1
    ):
        myName = (
            myPerson.get(
                "name"
            )
            or ""
        ).strip()

        myTitle = (
            myPerson.get(
                "title"
            )
            or ""
        ).strip()

        myCompany = (
            myPerson.get(
                "company"
            )
            or ""
        ).strip()

        myLocation = (
            myPerson.get(
                "location"
            )
            or ""
        ).strip()

        myLinkedinUrl = (
            myPerson.get(
                "linkedin_url"
            )
            or ""
        ).strip()

        if not myCompany:
            print(
                f"[{myIndex}/{len(aPeople)}] "
                f"{myName}: skipping because "
                f"company is missing."
            )

            continue

        print()

        print(
            f"[{myIndex}/{len(aPeople)}] "
            f"{myName} at {myCompany}"
        )

        myCompanyKey = (
            myCompany.lower()
        )

        if (
            myCompanyKey
            in myCompanyMemory
        ):
            myCompanyData = (
                myCompanyMemory[
                    myCompanyKey
                ]
            )

        else:
            mySearchResults = (
                getCompanySearchResults(
                    myCompany,
                    aRefresh
                )
            )

            myCompanyScore = (
                getCompanyScore(
                    myCompany,
                    mySearchResults,
                    aRefresh
                )
            )

            myCompanyDomain = (
                getOfficialCompanyDomain(
                    mySearchResults,
                    myCompanyScore
                )
            )

            myCompanyData = {
                "search_results":
                    mySearchResults,

                "score":
                    myCompanyScore,

                "domain":
                    myCompanyDomain
            }

            myCompanyMemory[
                myCompanyKey
            ] = myCompanyData

        myCompanyScore = (
            myCompanyData[
                "score"
            ]
        )

        myFinderWeakness = safeFloat(
            myCompanyScore.get(
                "trial_finder_weakness"
            ),
            5.0
        )

        myFinderWeakness = max(
            0.0,
            min(
                10.0,
                myFinderWeakness
            )
        )

        mySiteSizeFit = safeFloat(
            myCompanyScore.get(
                "site_size_fit"
            ),
            5.0
        )

        mySiteSizeFit = max(
            0.0,
            min(
                10.0,
                mySiteSizeFit
            )
        )

        myNameHint = getNameSizeHint(
            myCompany
        )

        # The model only sees trial-page snippets, which
        # often say nothing about headcount. When the
        # employer name itself screams "hospital network"
        # or "pharma", pull the fit down so those leads
        # cannot survive on a weak trial finder alone.
        if myNameHint["hint"] == "likely_large":
            mySiteSizeFit = min(
                mySiteSizeFit,
                3.0
            )

        myOrganizationType = (
            myCompanyScore.get(
                "organization_type"
            )
            or "unknown"
        )

        if myOrganizationType == "large_organization":
            mySiteSizeFit = min(
                mySiteSizeFit,
                2.0
            )

        if mySiteSizeFit < aMinSiteFit:
            myDroppedForSize += 1

            print(
                f"    Dropping: site fit "
                f"{mySiteSizeFit:.1f} is below "
                f"{aMinSiteFit:.1f} "
                f"({myOrganizationType})."
            )

            continue

        myRoleScore = getRoleScore(
            myTitle
        )

        # Site-size fit now carries real weight. A weak
        # trial finder at a hospital network is not the
        # same opportunity as a weak trial finder at a
        # two-doctor research clinic.
        myBridgeMdScore = (
            myFinderWeakness * 0.45
            + mySiteSizeFit * 0.35
            + myRoleScore * 0.20
        )

        myFinalLeads.append({
            "name":
                myName,

            "title":
                myTitle,

            "company":
                myCompany,

            "company_domain":
                myCompanyData[
                    "domain"
                ],

            "location":
                myLocation,

            "linkedin_url":
                myLinkedinUrl,

            "email":
                "",

            "hunter_score":
                "",

            "hunter_verification":
                "",

            "hunter_method":
                "",

            "hunter_status":
                "",

            "hunter_sources":
                "",

            "trial_finder_url":
                (
                    myCompanyScore.get(
                        "trial_finder_url"
                    )
                    or ""
                ),

            "trial_finder_weakness":
                round(
                    myFinderWeakness,
                    1
                ),

            "organization_type":
                myOrganizationType,

            "site_size_fit":
                round(
                    mySiteSizeFit,
                    1
                ),

            "role_score":
                round(
                    myRoleScore,
                    1
                ),

            "bridgemd_score":
                round(
                    myBridgeMdScore,
                    1
                ),

            "reason":
                (
                    myCompanyScore.get(
                        "reason"
                    )
                    or ""
                ),

            "confidence":
                (
                    myCompanyScore.get(
                        "confidence"
                    )
                    or "low"
                )
        })

    myFinalLeads.sort(
        key=lambda aLead:
            aLead[
                "bridgemd_score"
            ],
        reverse=True
    )

    if myDroppedForSize:
        print()

        print(
            f"Dropped {myDroppedForSize} lead(s) for "
            f"being too large for the small-site ICP."
        )

    return myFinalLeads


# =========================================================
# HUNTER EMAIL ENRICHMENT
# =========================================================

def enrichHunterEmails(
    aLeads,
    aEmailLimit,
    aRefreshEmails
):
    if aEmailLimit <= 0:
        return aLeads

    myLimit = min(
        aEmailLimit,
        len(aLeads)
    )

    print()

    print(
        f"Finding Hunter emails for "
        f"top {myLimit} leads..."
    )

    for myIndex in range(
        myLimit
    ):
        myLead = aLeads[
            myIndex
        ]

        print()

        print(
            f"[HUNTER {myIndex + 1}/{myLimit}] "
            f"{myLead['name']} - "
            f"{myLead['company']}"
        )

        myEmailData = findHunterEmail(
            myLead[
                "name"
            ],

            myLead[
                "company"
            ],

            myLead[
                "linkedin_url"
            ],

            myLead[
                "company_domain"
            ],

            aRefreshEmails
        )

        myLead["email"] = (
            myEmailData.get(
                "email"
            )
            or ""
        )

        myLead["hunter_score"] = (
            myEmailData.get(
                "hunter_score"
            )
            or ""
        )

        myLead["hunter_verification"] = (
            myEmailData.get(
                "hunter_verification"
            )
            or ""
        )

        myLead["hunter_method"] = (
            myEmailData.get(
                "hunter_method"
            )
            or ""
        )

        myLead["hunter_status"] = (
            myEmailData.get(
                "hunter_status"
            )
            or ""
        )

        myLead["hunter_sources"] = (
            myEmailData.get(
                "hunter_sources"
            )
            or ""
        )

        if myLead["email"]:
            print(
                f"FOUND: "
                f"{myLead['email']} "
                f"(score "
                f"{myLead['hunter_score']}, "
                f"{myLead['hunter_verification']})"
            )

        else:
            print(
                f"No email found "
                f"({myLead['hunter_status']})"
            )

    return aLeads


# =========================================================
# SAVE FINAL CSV
# =========================================================

def saveFinalLeads(aLeads):
    myFields = [
        "name",
        "title",
        "company",
        "company_domain",
        "location",
        "linkedin_url",

        "email",
        "hunter_score",
        "hunter_verification",
        "hunter_method",
        "hunter_status",
        "hunter_sources",

        "trial_finder_url",
        "trial_finder_weakness",
        "organization_type",
        "site_size_fit",
        "role_score",
        "bridgemd_score",
        "reason",
        "confidence"
    ]

    with open(
        "final_leads.csv",
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as myFile:

        myWriter = csv.DictWriter(
            myFile,
            fieldnames=myFields
        )

        myWriter.writeheader()

        for myLead in aLeads:
            myWriter.writerow(
                myLead
            )


# =========================================================
# ARGUMENTS
# =========================================================

def parseArguments():
    myParser = argparse.ArgumentParser()

    myParser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Ignore Serper/OpenAI/company caches "
            "and fetch everything again."
        )
    )

    myParser.add_argument(
        "--refresh-emails",
        action="store_true",
        help=(
            "Redo Hunter lookups without "
            "refreshing everything else."
        )
    )

    myParser.add_argument(
        "--email-limit",
        type=int,
        default=20,
        help=(
            "Number of top ranked leads "
            "to enrich with Hunter. "
            "Default: 20."
        )
    )

    myParser.add_argument(
        "--skip-emails",
        action="store_true",
        help=(
            "Do not call Hunter."
        )
    )

    myParser.add_argument(
        "--min-site-fit",
        type=float,
        default=theDefaultMinSiteFit,
        help=(
            "Drop leads whose employer scores below "
            "this on small-site fit (0-10). "
            f"Default: {theDefaultMinSiteFit}."
        )
    )

    myParser.add_argument(
        "--include-large",
        action="store_true",
        help=(
            "Keep hospital networks, academic centres, "
            "CROs and pharma sponsors instead of "
            "filtering them out."
        )
    )

    return myParser.parse_args()


# =========================================================
# MAIN
# =========================================================

def main():
    myArgs = parseArguments()

    createCacheDirectories()

    if myArgs.refresh:
        print(
            "REFRESH MODE: "
            "ignoring Serper/OpenAI caches."
        )

    else:
        print(
            "CACHE MODE: "
            "reusing Serper/OpenAI caches."
        )

    if myArgs.refresh_emails:
        print(
            "HUNTER REFRESH MODE: "
            "ignoring Hunter email cache."
        )

    print()

    # -----------------------------------------------------
    # 1. PEOPLE SEARCH
    # -----------------------------------------------------

    mySearchData = (
        getPeopleSearchData(
            myArgs.refresh
        )
    )

    myOrganicResults = (
        mySearchData.get(
            "organic",
            []
        )
    )

    print(
        f"Found "
        f"{len(myOrganicResults)} "
        f"unique public search results."
    )

    saveRawPeopleCsv(
        myOrganicResults
    )

    print(
        "Saved people_raw.csv"
    )

    # -----------------------------------------------------
    # 2. CLEAN PEOPLE
    # -----------------------------------------------------

    myPeople = getCleanPeople(
        myOrganicResults,
        myArgs.refresh
    )

    saveCleanPeopleCsv(
        myPeople
    )

    print(
        f"Have "
        f"{len(myPeople)} "
        f"clean people."
    )

    print(
        "Saved people_clean.csv"
    )

    # -----------------------------------------------------
    # 3. SCORE COMPANIES
    # -----------------------------------------------------

    if myArgs.include_large:
        myMinSiteFit = 0.0

        print(
            "ICP FILTER OFF: "
            "keeping large organizations."
        )

    else:
        myMinSiteFit = myArgs.min_site_fit

        print(
            f"ICP FILTER: small clinical research "
            f"sites, minimum site fit "
            f"{myMinSiteFit:.1f}."
        )

    myFinalLeads = generateFinalLeads(
        myPeople,
        myArgs.refresh,
        myMinSiteFit
    )

    # Save before Hunter too, so company research
    # isn't lost if Hunter errors.
    saveFinalLeads(
        myFinalLeads
    )

    # -----------------------------------------------------
    # 4. HUNTER
    # -----------------------------------------------------

    if not myArgs.skip_emails:
        myRefreshHunter = (
            myArgs.refresh
            or myArgs.refresh_emails
        )

        myFinalLeads = (
            enrichHunterEmails(
                myFinalLeads,
                myArgs.email_limit,
                myRefreshHunter
            )
        )

    # -----------------------------------------------------
    # 5. SAVE FINAL RESULTS
    # -----------------------------------------------------

    saveFinalLeads(
        myFinalLeads
    )

    print()

    print(
        "DONE"
    )

    print(
        "Saved final_leads.csv"
    )

    print()

    print(
        "TOP LEADS"
    )

    print(
        "------------------------------------------------------------"
    )

    for myLead in myFinalLeads[:10]:
        myEmail = (
            myLead["email"]
            or "NO EMAIL"
        )

        print(
            f"{myLead['bridgemd_score']:>4.1f} | "
            f"fit {myLead['site_size_fit']:>4.1f} | "
            f"{myLead['organization_type']:<20} | "
            f"{myLead['name']} | "
            f"{myLead['company']} | "
            f"{myEmail}"
        )


if __name__ == "__main__":
    main()