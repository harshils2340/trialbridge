#!/usr/bin/env python3
"""Export live application inbox rows (contact + study) to CSV or JSON.

Uses the same filter as /internal/inbox — real patient applies only, not demo.

Run on Render shell (production DB):
  cd web
  DB_PATH=/var/data/bridgemd-fieve11.db python export_live_applicants.py

Locally (after downloading a backup):
  DB_PATH=/path/to/bridgemd.db python export_live_applicants.py -o live_applicants.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

# Run from web/ or repo root
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import db  # noqa: E402
import app as webapp  # noqa: E402


FIELDS = [
    "id",
    "name",
    "email",
    "phone",
    "age",
    "sex",
    "location",
    "condition",
    "nct",
    "title",
    "status",
    "found_via",
    "search_q",
    "verdict",
    "flags",
    "records",
    "registry_opt_in",
    "created_at",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Export live applicants from BridgeMD DB")
    parser.add_argument("-o", "--output", help="Write CSV here (default: stdout)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of CSV")
    args = parser.parse_args()

    db.init_db()
    apps = webapp._live_operator_apps()

    if args.json:
        payload = json.dumps(apps, indent=2, default=str)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(payload)
            print(f"Wrote {len(apps)} live applicant(s) to {args.output}", file=sys.stderr)
        else:
            print(payload)
        return 0

    out = open(args.output, "w", newline="", encoding="utf-8") if args.output else sys.stdout
    try:
        w = csv.DictWriter(out, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for a in apps:
            w.writerow({k: a.get(k, "") for k in FIELDS})
    finally:
        if args.output and out is not sys.stdout:
            out.close()

    dest = args.output or "stdout"
    print(f"Exported {len(apps)} live applicant(s) to {dest}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
