# BridgeMD

BridgeMD is an operating system for clinical trials. It has three parts that feed
each other:

1. **Find a trial (patient side)** — "Indeed for clinical trials."
2. **Run a site (study-team side)** — the coordinator's workspace.
3. **Outreach & growth** — how sites and patients discover BridgeMD.

The whole thing is one Flask app in [`web/`](web/). Everything else at the repo
root is shared code or deploy config.

---

## 1. Find a trial (patient side)

A plain-English way for patients to find and apply to trials, built on public
ClinicalTrials.gov data.

- Search by condition or describe your situation in plain English.
- BridgeMD matches recruiting trials near you and pre-checks the ones you likely
  fit, so you skip the ones you don't.
- Apply in a few minutes. No doctor referral needed. You control what you share
  and when a study team can contact you.

Core code: `match_trials.py` (fetch + eligibility matching), `web/summarize.py`
(patient-safe trial summaries), patient templates in `web/templates/`.

## 2. Run a site (study-team side)

The workspace coordinators live in. It takes the repetitive admin off their plate
so they spend the day enrolling, not chasing. Everything lives under `/app`.

- **Intake inbox** — applicants from every source in one place, pre-screened
  against the protocol before anyone opens a chart.
- **Matches** — ranked candidates per study.
- **Calendar & scheduling** — book visits, protocol windows, `.ics`/Google sync.
- **Protocol schedule (SoE)** — the master visit/procedure template per study.
- **Documents & e-sign** — the paperwork back-and-forth, versioned.
- **Payments** — participant stipends/reimbursement, FMV rules (see COMPLIANCE.md).
- **Updates & reporting** — sponsor-facing funnel and status.
- **Bridget** — the AI copilot that triages the queue, surfaces funnel leaks, and
  drafts messages/booking links. A human approves anything that goes out.

Core code: `web/app.py` (routes), `web/db.py` (data), `web/copilot/` (Bridget),
`web/calendar_invites.py`, `web/payments.py`, `web/intake.py`, `web/records.py`.

## 3. Outreach & growth

How patients and sites find BridgeMD, and how sites run recruitment.

- **Blog / SEO** — plain-English articles and condition/city pages that pull
  organic traffic. Content in `web/blog_posts.py`.
- **Campaigns** — paste a trial, get a recommended ad, post it, and track spend
  and ROI to close the loop. Code in `web/campaigns.py`, `web/adrender.py`.
- **Analytics** — visitor funnel and source attribution (`web/analytics.py`).
- **Marketing site** — `/for-sites` explains the product to study teams.

---

## Where the code lives

```
matcher/                 <- repo root (this is the GitHub repo root)
├── web/                 <- the Flask app. THIS is what deploys.
│   ├── app.py           <-   live server (Render runs `gunicorn app:app`)
│   ├── db.py            <-   SQLite data layer + migrations
│   ├── copilot/         <-   Bridget AI agent (registry, planner, tools)
│   ├── templates/       <-   Jinja pages (patient + site + marketing + blog)
│   ├── static/          <-   CSS / JS / images
│   ├── blog_posts.py    <-   outreach blog content
│   ├── requirements.txt <-   Python deps
│   └── test_*.py        <-   tests + smoke scripts
├── match_trials.py      <- shared: CT.gov fetch + LLM eligibility matching
├── refer.py             <- shared: referral / secure-link logic
├── render.yaml          <- Render deploy blueprint
├── COMPLIANCE.md        <- legal guardrails (read before referral/payment/PHI work)
└── TESTING.md           <- running test log
```

`web/app.py` adds the repo root to its path so it can import the shared
`match_trials` and `refer` modules.

## Run locally

```bash
cd matcher
/usr/bin/python3 -m venv .venv                 # first time only
.venv/bin/python -m pip install -r web/requirements.txt

export LLM_API_KEY="sk-..."                    # optional, enables AI matching
NO_LOGIN=1 SITE_DEMO=1 .venv/bin/python web/app.py
```

Open http://127.0.0.1:5001. `NO_LOGIN=1` opens both sides for local demo;
`SITE_DEMO=1` seeds the study-team side with fake studies/applicants.

## Deploy

Render, via `render.yaml` (`rootDir: web`). Push to `main` and Render redeploys.
Keep `NO_LOGIN=0` in production; `SITE_DEMO=1` runs the seeded public demo until
real sites onboard (then set it to `0`).

## More detail

`web/README.md` is the technical reference: full environment-variable table,
notification/SMS setup, ops endpoints, and backup/restore drills.
Read `COMPLIANCE.md` before changing anything that touches referrals, payments,
advertising, or patient data.
