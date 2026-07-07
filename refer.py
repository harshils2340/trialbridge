#!/usr/bin/env python3
"""Referral step: turn a matched trial into a tracked referral + status loop.

This closes the loop the research flagged as the deepest unfixed leak: a doctor
refers a patient, then never hears what happened, so they stop referring. In the
concierge pilot YOU are the loop -- you take the referral, contact the site, and
report back. This tool gives you the packet to hand off and the ledger to track
every referral through: referred -> contacted -> screened -> enrolled.

Usage:
  # 1. Create a referral for the patient in patient_case.txt to a chosen trial
  #    (set LLM_API_KEY to include the eligibility rationale; optional)
  python3 refer.py create --nct NCT06121297 --physician "Dr. A. Rheum"

  # 2. See all referrals and their current status
  python3 refer.py list

  # 3. Advance a referral as you work the loop by hand
  python3 refer.py update --id R-0001 --status contacted --note "left VM w/ coordinator"

Statuses: referred, contacted, screened, enrolled, screen_failed, declined, withdrawn
"""
import argparse
import csv
import datetime as dt
import os
import pathlib
import sys

import match_trials as mt

HERE = pathlib.Path(__file__).resolve().parent
LEDGER = HERE / "referrals.csv"
EVENTS = HERE / "referral_events.csv"
PACKETS = HERE / "referrals"

LEDGER_COLS = ["ref_id", "created", "nct", "title", "patient", "physician",
               "site", "status", "last_update"]
STATUSES = ["referred", "contacted", "screened", "enrolled",
            "screen_failed", "declined", "withdrawn"]


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def read_ledger():
    if not LEDGER.exists():
        return []
    with open(LEDGER, newline="") as f:
        return list(csv.DictReader(f))


def write_ledger(rows):
    with open(LEDGER, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_COLS)
        w.writeheader()
        w.writerows(rows)


def log_event(ref_id, status, note=""):
    new = not EVENTS.exists()
    with open(EVENTS, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ref_id", "timestamp", "status", "note"])
        w.writerow([ref_id, now(), status, note])


def next_ref_id(rows):
    return f"R-{len(rows) + 1:04d}"


def best_site(trial, country):
    local = mt.sites_in_country(trial, country) if country else trial["locations"]
    pool = local or trial["locations"]
    # prefer a recruiting site
    pool = sorted(pool, key=lambda l: l.get("status") != "RECRUITING")
    return pool[0] if pool else None


def contact_block(trial, site):
    lines = []
    coord = pi = None
    for c in (site or {}).get("contacts", []):
        if c.get("role") == "PRINCIPAL_INVESTIGATOR" and not pi:
            pi = c
        elif not coord:
            coord = c
    if coord:
        ext = f" x{coord['phoneExt']}" if coord.get("phoneExt") else ""
        lines.append(f"- **Site coordinator:** {coord.get('name','')} · "
                     f"{coord.get('phone','')}{ext} · {coord.get('email','')}")
    if pi:
        lines.append(f"- **Principal investigator:** {pi.get('name','')}")
    if not coord and trial.get("centralContacts"):
        c = trial["centralContacts"][0]
        ext = f" x{c['phoneExt']}" if c.get("phoneExt") else ""
        lines.append(f"- **Central contact:** {c.get('name','')} · "
                     f"{c.get('phone','')}{ext} · {c.get('email','')}")
    return lines or ["- **Contact:** none listed on ClinicalTrials.gov"]


def build_packet(ref_id, trial, site, match, patient, physician, country):
    s = site or {}
    site_str = ", ".join(p for p in (s.get("facility"), s.get("city"),
                                     s.get("state"), s.get("country")) if p)
    ttype = ("observational/registry" if (trial.get("studyType") or "").upper()
             == "OBSERVATIONAL" else f"phase {trial['phase'] or 'NA'}")
    out = [
        f"# Trial Referral {ref_id}",
        f"_Created {now()} · Referring physician: {physician or 'TBD'}_\n",
        "## Patient (de-identified)",
        patient.strip() + "\n",
        "## Trial",
        f"- **{trial['title']}**",
        f"- {trial['nctId']} · {ttype} · "
        f"https://clinicaltrials.gov/study/{trial['nctId']}",
        f"- **Site:** {site_str or 'none listed'}"
        + (f"  ·  _{s.get('status','')}_" if s.get("status") else ""),
    ]
    out += contact_block(trial, site)
    if match:
        out.append("\n## Why this patient may be eligible")
        out.append(f"- **Screen verdict:** {match.get('verdict')} "
                   f"(score {match.get('score')})")
        if match.get("rationale"):
            out.append(f"- **Summary:** {match['rationale']}")
        if match.get("met"):
            out.append("- **Appears to meet:** " + "; ".join(match["met"][:8]))
        if match.get("not_met"):
            out.append("- **Potential blockers:** " + "; ".join(match["not_met"][:8]))
        if match.get("unknown"):
            out.append("- **Confirm before referral:** "
                       + "; ".join(match["unknown"][:8]))
    else:
        out.append("\n_(Run with LLM_API_KEY set to include the eligibility "
                   "rationale.)_")
    out += ["\n## Referral status log",
            f"- {now()} — **referred**"]
    return "\n".join(out) + "\n"


def cmd_create(args):
    case_path = pathlib.Path(args.case)
    if not case_path.exists():
        sys.exit(f"No patient case at {case_path}")
    patient = case_path.read_text().strip()
    profile = mt.patient_profile(patient)
    patient_tag = args.label or (
        f"{int(profile['age']) if profile['age'] else '?'}"
        f"{(profile['sex'] or '?')[0].upper()}")

    print(f"Fetching {args.nct} ...", file=sys.stderr)
    trial = mt.fetch_study(args.nct)
    if not trial.get("nctId"):
        sys.exit(f"Could not fetch {args.nct}")

    match = None
    if mt.LLM_API_KEY:
        print("Scoring eligibility ...", file=sys.stderr)
        try:
            match = mt.llm_match(patient, trial)
        except Exception as e:
            print(f"(match skipped: {e})", file=sys.stderr)
    else:
        print("(no LLM_API_KEY — packet will omit eligibility rationale)",
              file=sys.stderr)

    site = best_site(trial, args.country)
    rows = read_ledger()
    ref_id = next_ref_id(rows)
    packet = build_packet(ref_id, trial, site, match, patient,
                          args.physician, args.country)
    PACKETS.mkdir(exist_ok=True)
    (PACKETS / f"{ref_id}.md").write_text(packet)

    site_str = ", ".join(p for p in ((site or {}).get("facility"),
                                      (site or {}).get("city")) if p)
    rows.append({
        "ref_id": ref_id, "created": now(), "nct": trial["nctId"],
        "title": trial["title"][:80], "patient": patient_tag,
        "physician": args.physician or "", "site": site_str,
        "status": "referred", "last_update": now(),
    })
    write_ledger(rows)
    log_event(ref_id, "referred", "created")

    print(f"\nCreated {ref_id} -> {PACKETS / (ref_id + '.md')}")
    print(f"  {trial['nctId']} · {trial['title'][:70]}")
    print(f"  site: {site_str or 'none'}  · status: referred")
    print("\n--- packet ---\n")
    print(packet)


def cmd_list(args):
    rows = read_ledger()
    if not rows:
        print("No referrals yet. Create one with:  refer.py create --nct <NCT...>")
        return
    w = max(len(r["title"]) for r in rows)
    w = min(w, 50)
    print(f"{'REF':7} {'STATUS':13} {'PATIENT':7} {'NCT':12} TITLE")
    for r in rows:
        print(f"{r['ref_id']:7} {r['status']:13} {r['patient']:7} "
              f"{r['nct']:12} {r['title'][:w]}")
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\nfunnel:", "  ".join(f"{s}={counts.get(s,0)}"
                                 for s in STATUSES if counts.get(s)))


def cmd_update(args):
    if args.status not in STATUSES:
        sys.exit(f"status must be one of: {', '.join(STATUSES)}")
    rows = read_ledger()
    row = next((r for r in rows if r["ref_id"] == args.id), None)
    if not row:
        sys.exit(f"No referral {args.id} (see: refer.py list)")
    old = row["status"]
    row["status"] = args.status
    row["last_update"] = now()
    write_ledger(rows)
    log_event(args.id, args.status, args.note or "")
    # append to the packet's status log if present
    pkt = PACKETS / f"{args.id}.md"
    if pkt.exists():
        note = f" — {args.note}" if args.note else ""
        with open(pkt, "a") as f:
            f.write(f"- {now()} — **{args.status}**{note}\n")
    print(f"{args.id}: {old} -> {args.status}"
          + (f"  ({args.note})" if args.note else ""))


def main():
    ap = argparse.ArgumentParser(description="Trial referral + status loop")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create a referral packet for a trial")
    c.add_argument("--nct", required=True)
    c.add_argument("--case", default=str(HERE / "patient_case.txt"))
    c.add_argument("--country", default="Canada")
    c.add_argument("--physician", default="")
    c.add_argument("--label", default="", help="short patient tag for the ledger")
    c.set_defaults(func=cmd_create)

    l = sub.add_parser("list", help="list all referrals + funnel")
    l.set_defaults(func=cmd_list)

    u = sub.add_parser("update", help="advance a referral's status")
    u.add_argument("--id", required=True)
    u.add_argument("--status", required=True, help=", ".join(STATUSES))
    u.add_argument("--note", default="")
    u.set_defaults(func=cmd_update)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
