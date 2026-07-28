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

# Recruiting trials that pay or reimburse participants. Proxy = Phase 1 (early-
# phase / healthy-volunteer studies essentially always pay a stipend) OR the
# public record mentions compensation OR it seeks healthy volunteers. This is
# still CONSERVATIVE -- CT.gov rarely indexes payment wording and most trials
# reimburse time/travel -- so the true number is higher, which keeps our figure
# defensible. The homepage shows the word "Thousands" rather than this exact
# count so it doesn't read as a tiny fraction of the 65,000 recruiting total.
_COMPENSATION = (
    'compensation OR compensated OR reimbursement OR reimbursed OR stipend OR '
    'honorarium OR remuneration OR "payment for participation" OR '
    '"paid for your time" OR "financial compensation"'
)
_HEALTHY = (
    '"healthy volunteers" OR "healthy volunteer" OR "healthy subjects" OR '
    '"healthy participants"'
)
_PHASE1 = "AREA[Phase]PHASE1"


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
    paid = _count(f"({_PHASE1}) OR ({_COMPENSATION}) OR ({_HEALTHY})")

    recruiting_floor = _floor(recruiting, 1000)
    paid_floor = _floor(paid, 500)

    print(f"recruiting (raw):        {recruiting:,}")
    print(f"pay/reimburse (raw):     {paid:,}")
    print()
    print("Paste into HOME_STATS in web/app.py:")
    print("HOME_STATS = {")
    print(f'    "recruiting": "{recruiting_floor:,}+",')
    # We display the word "Thousands" for the pay stat so it never reads as a
    # small fraction of the recruiting total. Swap to f'"{paid_floor:,}+"' only
    # if you deliberately want the exact floored count instead.
    print('    "paid": "Thousands",   # or f"{:,}+".format(paid_floor)')
    print("}")


if __name__ == "__main__":
    main()
