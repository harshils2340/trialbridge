#!/usr/bin/env python3
"""Recompute the homepage credibility stats from ClinicalTrials.gov.

The homepage stats bar shows two real numbers baked into ``HOME_STATS`` in
``web/app.py`` (we do NOT hit the API on every page load). Run this script to
refresh those numbers, then paste the printed floor values into ``HOME_STATS``
and update the "Refreshed" date in the comment above it.

Numbers are floored (rounded DOWN to a clean figure) so the trailing "+" in the
UI stays truthful.

    python web/tools/refresh_home_stats.py
"""
import json
import urllib.parse
import urllib.request

BASE = "https://clinicaltrials.gov/api/v2/studies"

# Recruiting trials whose public record mentions compensation OR that seek
# healthy volunteers. This is a conservative proxy for "trials that may pay
# participants" -- CT.gov rarely indexes payment wording, so the true number is
# higher, which keeps our floored "+" figure defensible.
_COMPENSATION = (
    'compensation OR compensated OR reimbursement OR reimbursed OR stipend OR '
    'honorarium OR remuneration OR "payment for participation" OR '
    '"paid for your time" OR "financial compensation"'
)
_HEALTHY = (
    '"healthy volunteers" OR "healthy volunteer" OR "healthy subjects" OR '
    '"healthy participants"'
)


def _count(term=None):
    params = {
        "filter.overallStatus": "RECRUITING",
        "countTotal": "true",
        "pageSize": "1",
    }
    if term:
        params["query.term"] = term
    url = BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "BridgeMD/1.0"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.load(resp).get("totalCount", 0)


def _floor(n, step):
    """Round DOWN to the nearest ``step`` so a trailing '+' stays true."""
    return (int(n) // step) * step


def main():
    recruiting = _count()
    paid = _count(f"({_COMPENSATION}) OR ({_HEALTHY})")

    recruiting_floor = _floor(recruiting, 1000)
    paid_floor = _floor(paid, 500)

    print(f"recruiting (raw):        {recruiting:,}")
    print(f"may-compensate (raw):    {paid:,}")
    print()
    print("Paste into HOME_STATS in web/app.py:")
    print("HOME_STATS = {")
    print(f'    "recruiting": "{recruiting_floor:,}+",')
    print(f'    "paid": "{paid_floor:,}+",')
    print("}")


if __name__ == "__main__":
    main()
