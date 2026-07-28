#!/usr/bin/env python3
"""Warm the condition/city SEO surface by paging through /seo/warm until done.

Each condition x city pair is checked against CT.gov (cached server-side) and we
persist whether it has LOCAL recruiting trials, so sitemap-cities.xml can list
only the indexable pages instead of the full ~4,200-URL grid (the "discovered -
currently not indexed" wall in Search Console).

Run it anywhere with network access to the deployed app:

    PUBLIC_BASE_URL=https://app.bridgemd.health \
    ALERTS_CRON_KEY=... \
    python seo_warm.py

Point a weekly external scheduler (cron-job.org, GitHub Actions, Render cron) at
this. Safe to re-run: it just refreshes the local-trial flags.
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
KEY = os.environ.get("ALERTS_CRON_KEY", "").strip()
LIMIT = int(os.environ.get("SEO_WARM_LIMIT", "80"))   # keep each call under the
#                                                       web --timeout (120s)
PAUSE = float(os.environ.get("SEO_WARM_PAUSE", "2"))  # be gentle on CT.gov


def _call(offset):
    url = f"{BASE}/seo/warm?limit={LIMIT}&offset={offset}"
    if KEY:
        url += f"&key={KEY}"
    with urllib.request.urlopen(url, timeout=180) as r:
        return json.load(r)


def main():
    offset, local_total = 0, 0
    while offset is not None:
        try:
            data = _call(offset)
        except Exception as e:                       # noqa: BLE001
            print(f"warm failed at offset={offset}: {e}", file=sys.stderr)
            return 1
        local_total += data.get("local_in_batch", 0)
        print(f"offset={data['offset']} processed={data['processed']} "
              f"local_in_batch={data['local_in_batch']} "
              f"total={data['total']}")
        offset = data.get("next_offset")
        if offset is not None:
            time.sleep(PAUSE)
    print(f"done - {local_total} indexable local city pages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
