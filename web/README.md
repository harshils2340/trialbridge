# TrialBridge

A doctor-facing wrapper on ClinicalTrials.gov. Paste a de-identified patient
note, get ranked recruiting trials with a plain explanation of the fit, refer a
patient in one click, and track every referral through the pipeline
(referred → contacted → screened → enrolled) for status/attribution.

> ⚠️ **Read [`../COMPLIANCE.md`](../COMPLIANCE.md) before touching referrals,
> payments, "commission", trial advertising, or patient data.** Paying physicians
> per referral/enrollment is illegal in the US (Anti-Kickback Statute/EKRA) and
> Canada (secret commissions / College fee-splitting rules). The physician-facing
> "commission" tracking in this app must be reframed as status-only or removed -
> see COMPLIANCE.md §6.

## Run locally

```bash
cd matcher
/usr/bin/python3 -m venv .venv           # first time only
.venv/bin/python -m pip install -r web/requirements.txt

# Optional but recommended: eligibility reasoning
export LLM_API_KEY="sk-..."              # OpenAI-compatible key
export LLM_MODEL="gpt-4o-mini"           # optional

.venv/bin/python web/app.py              # http://127.0.0.1:5000
```

Open http://127.0.0.1:5000, create an account, and start searching.

Without `LLM_API_KEY` the app still fetches trials and screens by age/sex, but
skips the per-trial eligibility reasoning.

## Integrated E2E smoke (patient -> site ATS)

From `matcher/web`:

```bash
/Users/harshils/GraphMD/matcher/.venv/bin/python test_e2e_intake_flow.py
```

This verifies signup/verify/onboarding/apply + site intake/accept/message/schedule/status/reconcile.
See `DISTRIBUTION_READY.md` for production launch order and checklist.

## Live staging smoke (deployed URL)

From `matcher/web`:

```bash
STAGING_BASE_URL="https://trialbridge-61vy.onrender.com" \
/Users/harshils/GraphMD/matcher/.venv/bin/python staging_smoke.py
```

This hits your live app over HTTP (`/healthz`, home + CSRF, POST `/find`, `/how`,
`/app/leads`) so you can quickly verify routing + form protection after deploy.

## Getting a patient in (three ways, so doctors don't retype)

1. **Type/paste** the de-identified summary.
2. **Upload** PDF, Word, image, or text - the app extracts the text (PDF via
   `pypdf`, Word via the .docx XML, images via a vision model) and runs an AI
   pass to **de-identify + structure** it (`AGE:`/`SEX:` headers, strips
   names/MRNs/dates). If the AI step is unavailable, the extracted text is kept
   so you can redact it manually.
3. **Import from EHR (FHIR)** - enter a patient's FHIR id (or click *try a
   sample patient*) and the app pulls their chart and builds a de-identified
   summary (age/sex + active problems + meds + recent labs/vitals). No names,
   DOB, or addresses are included. Defaults to the open SMART reference sandbox
   (synthetic data, no auth); point at a real server with `FHIR_BASE`. This is
   where a SMART-on-FHIR "launch from EHR" (OAuth) slots in later.

Press **⌘/Ctrl + Enter** to search.

## Closing the referral loop

Referring a patient captures **consent** and (optionally) patient contact +
coordinator email, then gives you a one-click **Compose email** (prefilled) and
a **secure tokenized link** for the study site. The site opens that link (no
login) to confirm receipt and update status - and those updates flow back to
your referral timeline, tagged *site* vs *you*. Track everything under
**Referrals** (search/filter, CSV export, commission).

Email sending is optional: with no config it falls back to a `mailto:` compose
link. To send directly, set `SMTP_HOST` (and `SMTP_PORT`, `SMTP_USER`,
`SMTP_PASS`, `SMTP_FROM`, `SMTP_TLS`).

## Closing the patient loop (consumer side)

A patient applies to a trial from the public site (`/find` -> "I'm interested").
That creates a **de-identified candidate** in the study-team review board
(`/app/leads`). Each candidate has a **secure tokenized link** (`/c/<token>`) with
expiry/revoke controls that a
real site coordinator can open with **no login** to review eligibility and
**accept/decline**. Contact details unlock **only on accept** (mutual consent).
The patient tracks status any time at `/applications` (cookie-based, no login).

**Delivery is wired but OFF by default** so nothing is emailed while testing.
Going live is a ~2 minute env change (no code):

1. `NOTIFY_LIVE=1`
2. `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_FROM`
3. `SITE_NOTIFY_EMAIL` - inbox that receives new blinded candidates (with the
   `/c/<token>` link)
4. `PUBLIC_BASE_URL` - e.g. `https://trialbridge.onrender.com` so email links
   are absolute

While OFF, the loop still works end to end: the operator copies the secure link
from `/app/leads` and hands it to the site. On accept/decline/screening/enrolled
the applicant is emailed (only when `NOTIFY_LIVE=1`). Site emails are always
de-identified - no patient name/contact is ever in them.

## Persistence

SQLite lives at `DB_PATH` (defaults to `web/trialbridge.db`). For a real pilot,
point `DB_PATH` at a mounted disk so applications survive redeploys (see the
commented `disk:` block in `render.yaml`).

## What's inside

- `app.py` - Flask app: auth, search, EHR import, refer + consent, notify,
  tracking, public coordinator page, CSV export.
- `db.py` - SQLite schema + helpers with idempotent migrations
  (`trialbridge.db`, created on first run).
- `fhir.py` - EHR import via FHIR R4. `mailer.py` - optional SMTP email.
- `templates/`, `static/` - polished UI.
- Reuses `../match_trials.py` (fetch + gate + LLM scoring) and `../refer.py`
  (site/contact selection).

## Environment variables

| Var | Purpose |
| --- | --- |
| `LLM_API_KEY` (+ `LLM_MODEL`, `LLM_BASE_URL`) | Eligibility reasoning + de-identification |
| `FHIR_BASE` | FHIR R4 server for EHR import (default: SMART open sandbox) |
| `SMTP_HOST` (+ `SMTP_PORT`/`USER`/`PASS`/`FROM`/`TLS`) | Send emails directly |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Enable patient "Continue with Google" OAuth |
| `NOTIFY_LIVE` | `1` turns on real email delivery for the patient loop (default off) |
| `SITE_NOTIFY_EMAIL` | Coordinator inbox that receives new blinded candidates |
| `PUBLIC_BASE_URL` | Base URL used for links inside emails |
| `DB_PATH` | SQLite file location (point at a persistent disk in prod) |
| `NO_LOGIN` | `1` opens the clinician tool with no login (default `0`; keep off in prod) |
| `ALERTS_BACKGROUND`, `REMINDERS_BACKGROUND` | Background schedulers (`0` recommended with multi-worker gunicorn + cron) |
| `ALERTS_CRON_KEY` | Shared key protecting `/alerts/run` and `/reminders/run` cron triggers |
| `RATE_LIMIT_WINDOW_SECONDS` | IP rate-limit window for sensitive POSTs (default `300`) |
| `RATE_LIMIT_SIGNUP_MAX` | Max POSTs/IP/window for `/account/signup` (default `8`) |
| `RATE_LIMIT_LOGIN_MAX` | Max POSTs/IP/window for `/account/login` (default `10`) |
| `RATE_LIMIT_VERIFY_MAX` | Max POSTs/IP/window for `/account/verify` (default `10`) |
| `RATE_LIMIT_VERIFY_RESEND_MAX` | Max POSTs/IP/window for `/account/verify/resend` (default `5`) |
| `RATE_LIMIT_INTEREST_MAX` | Max POSTs/IP/window for `/interest` (default `12`) |
| `PORT`, `FLASK_DEBUG`, `WEB_MAX_MATCH` | Server port, debug, LLM calls per search |

## Privacy

Only paste **de-identified** notes. The local SQLite DB stores the notes you
enter; it is gitignored. This is a validation tool, not a HIPAA/PHIPA-cleared
system of record.
