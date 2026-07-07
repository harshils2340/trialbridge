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
| `SMTP_HOST` (+ `SMTP_PORT`/`USER`/`PASS`/`FROM`/`TLS`) | Send coordinator emails directly |
| `PORT`, `FLASK_DEBUG`, `WEB_MAX_MATCH` | Server port, debug, LLM calls per search |

## Privacy

Only paste **de-identified** notes. The local SQLite DB stores the notes you
enter; it is gitignored. This is a validation tool, not a HIPAA/PHIPA-cleared
system of record.
