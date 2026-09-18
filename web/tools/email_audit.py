#!/usr/bin/env python3
"""Audit BridgeMD's real applicant emails against the real applicants.

Produces one Markdown document that answers, per applicant: what study they
applied to, where they are, what they entered, which emails we sent them and
to the clinic, whether those emails were delivered, and whether the content
follows the current rules (thread link, no booking or calendar links, no em
dashes, right study in the subject).

Inputs:
  1. Applicants: the JSON from export_live_applicants.py --json
       On Render:  DB_PATH=/var/data/<db file> python export_live_applicants.py --json > live_applicants.json
  2. Sent emails: the Resend account, read live with RESEND_API_KEY, or an
     offline dump via --emails (from `resend emails list --json`).

Run:
  RESEND_API_KEY=re_xxx python tools/email_audit.py \
      --applicants live_applicants.json -o applicant_email_report.md

Nothing is sent and nothing is written to any database. Read-only on both
sides. The report contains applicant names and emails, keep it local.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

API = "https://api.resend.com"

# Every subject the app can send, so each email in the account gets named.
SUBJECT_KINDS = [
    ("We got your application", "apply confirmation (to applicant)"),
    ("Message the study team", "thread link / connect (to applicant)"),
    ("New application:", "owner heads-up (internal)"),
    ("New applicant for", "clinic candidate notice (to clinic)"),
    ("Patient interested in", "coordinator forward (to clinic)"),
    ("Trial referral", "referral (to clinic)"),
    ("New message from the study team", "chat notify (to applicant)"),
    ("New message from an applicant", "chat notify (to team)"),
    ("Book your screening call", "OLD booking email (should never send)"),
    ("Visit booked", "visit confirmation (to applicant)"),
    ("Reminder: your visit", "visit reminder (to applicant)"),
    ("BridgeMD weekly trial update", "saved-alert digest (to subscriber)"),
    ("A study team wants to move forward", "status: accepted (to applicant)"),
    ("Update on your trial application", "status: update (to applicant)"),
    ("Your trial application:", "status: screening/enrolled (to applicant)"),
    ("Direct contacts for your trial", "founder connect (to applicant)"),
    # Human subjects (no registry codes) from Sep 18 on.
    ("Your application to the", "apply confirmation (to applicant)"),
    ("How to reach the", "founder connect (to applicant)"),
    ("Messaging the", "thread link / connect (to applicant)"),
    ("New application:", "owner heads-up (internal)"),
    ("The ", "chat notify (to applicant)"),
]


def _is_candidate_subject(subject):
    return " applied to your " in (subject or "")

# Content rules. An applicant email must carry the /a/<token> thread link and
# must not carry a booking or calendar link (commit f1fbb84), an em dash, or
# an instruction to email the clinic themselves (commit 7254e79).
FORBIDDEN = [
    ("calendly link", re.compile(r"calendly\.com", re.I)),
    ("cal.com link", re.compile(r"\bcal\.com", re.I)),
    ("google calendar link", re.compile(r"calendar\.google", re.I)),
    ("booking-page link", re.compile(r"/schedule/|/book/", re.I)),
    ("asks them to pick a time", re.compile(
        r"(?<!asking you to )pick a (?:screening )?time", re.I)),
    ("em or en dash", re.compile(r"[\u2014\u2013]")),
    ("asks them to email the clinic", re.compile(
        r"(?:email|write|contact) the (?:clinic|site|study team) (?:at|directly at) \S+@",
        re.I)),
]
THREAD_LINK = re.compile(r"https?://\S*/a/[A-Za-z0-9_-]+")


def _get(path, key):
    req = urllib.request.Request(API + path,
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_emails(key, pages=5):
    """Newest-first list of sent emails, up to pages x 100."""
    out, after = [], ""
    for _ in range(pages):
        path = "/emails?limit=100" + (f"&after={after}" if after else "")
        data = _get(path, key)
        rows = data.get("data") or []
        out.extend(rows)
        if not rows or not data.get("has_more"):
            break
        after = rows[-1].get("id") or ""
        if not after:
            break
    return out


def fetch_body(key, email_id):
    try:
        d = _get(f"/emails/{email_id}", key)
        return (d.get("text") or "") or re.sub(r"<[^>]+>", " ", d.get("html") or "")
    except Exception as e:  # noqa: BLE001
        return f"(could not fetch body: {e})"


def kind_of(subject):
    if _is_candidate_subject(subject):
        return "clinic candidate notice (to clinic)"
    if " sent you a message about " in (subject or ""):
        return "chat notify (to team)"
    for prefix, label in SUBJECT_KINDS:
        if (subject or "").startswith(prefix):
            return label
    return "other"


def recipients(row):
    to = row.get("to") or []
    if isinstance(to, str):
        to = [to]
    return [t.strip().lower() for t in to if t]


def check_body(body):
    problems = []
    for label, rx in FORBIDDEN:
        if rx.search(body or ""):
            problems.append(label)
    return problems


def _md_escape(s):
    return str(s or "").replace("|", "\\|").replace("\n", " ")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--applicants", required=True,
                    help="JSON from export_live_applicants.py --json")
    ap.add_argument("--emails", help="Offline dump from `resend emails list --json` "
                                     "(otherwise fetched live with RESEND_API_KEY)")
    ap.add_argument("--limit", type=int, default=10,
                    help="How many recent applicants get the deep body check")
    ap.add_argument("-o", "--output", default="applicant_email_report.md")
    args = ap.parse_args()

    with open(args.applicants, encoding="utf-8") as f:
        apps = json.load(f)
    apps.sort(key=lambda a: a.get("created_at") or "", reverse=True)

    key = os.environ.get("RESEND_API_KEY", "").strip()
    if args.emails:
        with open(args.emails, encoding="utf-8") as f:
            raw = json.load(f)
        emails = raw.get("data") if isinstance(raw, dict) else raw
    elif key:
        emails = fetch_emails(key)
    else:
        print("Need RESEND_API_KEY or --emails", file=sys.stderr)
        return 1

    by_rcpt = {}
    for row in emails:
        for r in recipients(row):
            by_rcpt.setdefault(r, []).append(row)

    known_meta = {"id", "name", "email", "phone", "status", "nct", "title",
                  "created_at", "is_live", "is_new", "token", "applicant_token",
                  "owner_user_id", "revealed", "updated_at", "linked_lead_id"}

    lines = ["# BridgeMD live applicants and the emails behind them", "",
             f"{len(apps)} live applicants. {len(emails)} emails in the Resend "
             f"account (newest {len(emails)} fetched). Deep content check on the "
             f"most recent {min(args.limit, len(apps))} applicants.", ""]

    # ---- summary tables ------------------------------------------------------
    def tally(field):
        t = {}
        for a in apps:
            v = (a.get(field) or "unknown").strip() or "unknown"
            t[v] = t.get(v, 0) + 1
        return sorted(t.items(), key=lambda kv: -kv[1])

    lines += ["## Where they applied", "", "| Study | Applicants |", "|---|---|"]
    lines += [f"| {_md_escape(k)} | {v} |" for k, v in tally("title")]
    lines += ["", "## Regions", "", "| Region | Applicants |", "|---|---|"]
    lines += [f"| {_md_escape(k)} | {v} |" for k, v in tally("location")]
    lines += ["", "## How they found it", "", "| Source | Applicants |", "|---|---|"]
    lines += [f"| {_md_escape(k)} | {v} |" for k, v in tally("found_via")]

    kinds = {}
    for row in emails:
        k = kind_of(row.get("subject"))
        kinds[k] = kinds.get(k, 0) + 1
    lines += ["", "## What the account has been sending", "",
              "| Email type | Count |", "|---|---|"]
    lines += [f"| {_md_escape(k)} | {v} |"
              for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])]

    # ---- per-applicant sections ----------------------------------------------
    findings = []
    old_booking = [r for r in emails
                   if (r.get("subject") or "").startswith("Book your screening call")]
    if old_booking:
        findings.append(f"{len(old_booking)} old 'Book your screening call' "
                        "email(s) exist in the account (last: "
                        f"{old_booking[0].get('created_at')}). None should be "
                        "recent; the sender was removed in commit 4cee6b3.")

    lines += ["", "## Recent applicants, one by one", ""]
    for i, a in enumerate(apps[: args.limit]):
        email = (a.get("email") or "").strip().lower()
        sent = by_rcpt.get(email, [])
        name = a.get("name") or "(no name)"
        lines += [f"### {i + 1}. {name} · {a.get('created_at')}", "",
                  f"- Study: {a.get('title') or a.get('nct')} ({a.get('nct')})",
                  f"- Region: {a.get('location') or 'not given'}",
                  f"- Condition: {a.get('condition') or 'not given'}",
                  f"- Contact: {a.get('email') or 'no email'}"
                  + (f", {a.get('phone')}" if a.get("phone") else ""),
                  f"- Found via: {a.get('found_via') or 'Direct'}"
                  + (f" (searched: \"{a.get('search_q')}\")" if a.get("search_q") else ""),
                  f"- Pre-screen verdict: {a.get('verdict') or 'none'}"
                  + (f" · flags: {a.get('flags')}" if a.get("flags") else ""),
                  f"- Status: {a.get('status') or 'new'}"]
        extras = {k: v for k, v in a.items()
                  if k not in known_meta and v not in (None, "", 0, [], {})
                  and k not in ("found_via", "search_q", "verdict", "flags",
                                "location", "condition", "age", "sex",
                                "records", "registry_opt_in")}
        basics = ", ".join(f"{f}: {a[f]}" for f in ("age", "sex") if a.get(f))
        if basics:
            lines.append(f"- They entered: {basics}")
        for k, v in sorted(extras.items()):
            lines.append(f"- {k}: {_md_escape(json.dumps(v, default=str)[:200])}")

        if not email:
            lines += ["", "No email address, so nothing could be sent to them.", ""]
            findings.append(f"{name}: applied without an email address.")
            continue
        if not sent:
            lines += ["", "**No email in the Resend account for this address.**", ""]
            findings.append(f"{name} ({email}): no email was ever sent to them.")
            continue

        lines += ["", "| Sent | Type | Subject | Status |", "|---|---|---|---|"]
        got_confirmation = False
        for row in sorted(sent, key=lambda r: r.get("created_at") or ""):
            subj = row.get("subject") or ""
            k = kind_of(subj)
            status = row.get("last_event") or row.get("status") or "?"
            lines.append(f"| {row.get('created_at', '')[:16]} | {k} | "
                         f"{_md_escape(subj)} | {status} |")
            if subj.startswith(("We got your application", "Message the study team")):
                got_confirmation = True
                title = (a.get("title") or "")[:40]
                if title and title.split(" - ")[0][:25].lower() not in subj.lower() \
                        and (a.get("nct") or "zzz") not in subj:
                    findings.append(f"{name}: confirmation subject does not "
                                    f"mention their study ({subj!r}).")
            if status in ("bounced", "complained", "failed"):
                findings.append(f"{name}: '{subj}' has status {status}.")
            if key:
                body = fetch_body(key, row.get("id"))
                problems = check_body(body)
                if k.endswith("(to applicant)") and not THREAD_LINK.search(body):
                    problems.append("no /a/<token> thread link")
                if problems:
                    lines.append(f"|  |  | problems: {', '.join(problems)} |  |")
                    findings.append(f"{name}: '{subj}' - {', '.join(problems)}.")
        if not got_confirmation:
            findings.append(f"{name} ({email}): never received an apply "
                            "confirmation or thread link.")
        lines.append("")

    lines += ["## Findings", ""]
    lines += ([f"- {f}" for f in findings] or
              ["- Every checked applicant got a correct, delivered email. "
               "No forbidden content found."])

    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Wrote {args.output}: {len(apps)} applicants, {len(emails)} emails, "
          f"{len(findings)} finding(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
