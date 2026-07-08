# Enrollment & Retention build (Northstar doc)

**Northstar: maximize patient enrollment AND retention in clinical trials via the platform.**

This is the living design doc. It captures the panel-derived requirements, the design
decisions, and a progress log. Update it at the end of every phase.

## THE ONE KPI: Enrollment Velocity (test EVERY feature against this before building)
BridgeMD is not a portal. It's an **enrollment optimization engine**. There is a single
metric everything ladders up to:

> **Enrollment Velocity** = the rate at which patients move through the funnel over time:
> **found → contacted → screened → enrolled → retained.**

Before building ANY feature, answer: *which stage of this funnel does it speed up, and by
roughly how much?* If it doesn't move one of these, it probably shouldn't be built.

**Two guardrails (a feature must pass both, or it fails the KPI):**
1. **Net throughput, not vanity counts.** A feature must not inflate an upstream stage at
   the expense of a downstream one. Raising "contacted" by sending sites unqualified
   applicants *lowers* screen→enroll conversion and burns trust — that's negative velocity.
   Optimize the whole funnel, or one stage without degrading the next.
2. **Compliance is a hard gate, not a tradeoff.** No velocity gain justifies breaking the
   rules in `.cursor/rules/compliance.mdc` (anti-kickback, blinded consent, PHI). Ever.

Note on wording: *retention* is the opposite vector of "velocity" (keeping people in, not
pushing them through), but it lives under the same KPI because a dropout is **negative net
enrollment**. Think "throughput minus leakage."

**Agent commitment:** for every proposed feature I will state which funnel stage(s) it moves
and flag it explicitly if it does NOT advance Enrollment Velocity (or if it risks guardrail 1
or 2). See `.cursor/rules/enrollment-velocity.mdc` (always on).

## Founder execution preference (shipping)
- After completing a requested implementation batch and validating it locally, **commit and push
  immediately by default** (unless the founder explicitly says not to push yet).
- Always report clearly whether the latest changes are pushed or only local.

## Why (evidence from the Rare Disease Day recruitment/retention panel)
- 86% of trials miss recruitment deadlines.
- ~$6K to recruit a patient; ~$19K to replace one lost to follow-up.
- Only 8% of patients are ever *asked* to join a trial.
- ~77% of sites have **no recruitment plan** / nobody owns it.
- Best-in-class sites hit 97% retention; typical is far below.
- The through-line: **failure = passivity + silence.** Patients drop out mostly because
  they stop hearing from anyone. Every fix = *be proactive + keep the communication loop alive.*

## What already aligns (pre-build)
- Acquisition of the "never-asked" 8%: patient finder + SEO condition pages + `alerts.py`.
- Plain-language trial summaries: `summarize.py` + `trial_summary.md`.
- Pre-screen + expectation-setting: screener + eligibility display + apply flow.
- Coarse loop: `leads` pipeline + `lead_events` + stage-change notifications.
- Self-schedule handoff: booking link -> patient "Book your call".
- Concept matching: `codes.py` (RxNorm/ICD-10/MeSH) + CT.gov derived MeSH.

## The gaps this build closes
1. **Communication is stage-only + silent.** No ongoing contact, no reminders, no
   re-engagement of quiet patients, no two-way messaging. This is THE retention gap.
2. **Study-team side is an inbox, not a plan.** No funnel metrics, no drop-off visibility
   -> can't sell "we prove/improve enrollment" to sites/sponsors.
3. **Trusted-messenger channel unused.** `refer.py` (physician referral) isn't wired into
   the consumer loop as "your doctor referred you."

## Build phases (each shipped to 100% + tested before moving on)
- **Phase 1 - Messaging:** two-way message thread per application (patient <-> study team),
  visible in-app on both sides; email mirror gated by NOTIFY_LIVE. Attacks silence directly.
- **Phase 2 - Reminders/visits:** site schedules visits; auto visit-reminders; auto-nudge
  of quiet applicants; background scheduler (`reminders.py`, mirrors `alerts.py`). This is
  the "Navigator" in software - the #1 retention driver.
- **Phase 3 - Recruitment dashboard:** `analytics.py` computes per-trial conversion,
  time-in-stage, and drop-off from `lead_events`; `/app/dashboard` renders it cleanly.
  The operational fix AND the sponsor/site sales artifact.
- **Phase 4 - Physician referral wire-in:** clinician creates a referral that mints a
  patient invite link ("Dr. X referred you"), prefilled + attributed. Trusted-messenger channel.
- **Phase 5 - Seed + polish + full E2E test.**

## Design principles
- Works with **no email** (NOTIFY_LIVE off) in demo: every mechanic has an in-app surface.
- Reuse existing infra: `alerts.py` scheduler pattern, `lead_events` timeline, `_notify` switch.
- No PHI leaks: messaging respects the blinded model - the site only messages a candidate
  once accepted/revealed (before that they're de-identified).
- UI/UX first: calm, clinical, single-column, existing design tokens in `style.css`.

## UI/UX references applied (patterns, not copied)
- Application tracker + thread: Indeed "your applications" + status stepper.
- Messaging: linear thread, newest at bottom, role-labeled bubbles (patient portal norm).
- Scheduling/reminders: Calendly confirmation + reminder cadence (T-24h).
- Funnel dashboard: classic acquisition funnel (counts + step conversion %) + median
  time-in-stage bars; drop-off highlighted.

## Progress log
- [2026-07-08] Doc created. Starting Phase 1.
- [2026-07-08] **Phase 1 DONE + tested.** Two-way messaging shipped:
  `messages` table + helpers; patient composer on `/applications`, study-team composer +
  visit booking on `/c/<token>`; system messages for automated events; unread badges
  (patient nav + site board + per-row); blinded rule enforced (site can only message a
  revealed candidate). Also shipped `lead_visits` + booking UI (feeds Phase 2 reminders).
  Verified E2E: site msg -> system visit msg -> patient reply, status->screening, unread
  counts + read-on-view all correct, both threads render, no errors.
  Starting Phase 2 (reminders/quiet-nudge engine).
- [2026-07-08] **Phase 2 DONE + tested.** `reminders.py` background sweep (mirrors
  alerts.py) + `/reminders/run` for cron/testing. Visit reminders: for visits within
  REMINDERS_VISIT_WINDOW_HOURS (24h default) -> system message in thread + patient email +
  marked reminded (de-dupes). Quiet-nudges: active-funnel apps idle > REMINDERS_QUIET_DAYS
  (3 default) with no recent nudge -> gentle check-in message + email; `nudged_at` suppresses
  repeats. New db: `nudged_at`, `list_active_stage_leads`, `last_activity_at`, `set_nudged`,
  visit reminder queries. Verified: 1 visit reminded + de-duped on re-run; 6 quiet apps
  nudged. Everything surfaces in-app (system messages) so it works email-off.
  Config: REMINDERS_BACKGROUND (default on), REMINDERS_INTERVAL_SECONDS,
  REMINDERS_VISIT_WINDOW_HOURS, REMINDERS_QUIET_DAYS. Starting Phase 3 (funnel dashboard).
- [2026-07-08] **Phase 3 DONE + tested.** `analytics.py` -> `funnel_stats()` computes, from
  `leads` + `lead_events`: stage funnel (reached counts, step conversion %, % of top),
  overall conversion, median time-in-stage per transition, biggest drop-off, per-trial
  breakdown, and engagement (messages/visits). New `/app/dashboard` (study POV) renders it
  as clean bars/table (`recruitment.html`). Study-team nav gets a "Recruitment" link + unread
  badge on Candidate review. NOTE/FIX: initially overwrote the clinician `dashboard.html`;
  restored it from git (repo root is `matcher/`, not `GraphMD/`) and moved the study view to
  `recruitment.html`; pov routing updated so `/app/dashboard` reads as study. Verified both
  `/app` (clinician "Find a trial") and `/app/dashboard` (funnel: 8->8->3->2->1, drop-off +
  time-in-stage + per-trial) render with no errors. Starting Phase 4 (physician referral wire-in).
- [2026-07-08] **Phase 4 DONE + tested.** Physician "invite a patient" trusted-messenger
  channel: `invites` table + helpers; clinician mints a per-trial link at `/app/invite`
  (nav link added), patient opens `/i/<token>` -> "Referred by Dr. X" landing (`invite_landing.html`)
  + attribution cookie `tb_invite`; on apply, the lead gets `source=referral`, `referred_by`,
  `invite_token`. Clinician's invite list shows opened/applied/active/enrolled per link (they
  finally hear what happened - the refer.py loop, for patients). Attribution badges: patient
  applications ("Referred by X"), study-team candidate view ("Physician-referred" trust signal).
  Verified E2E: create -> open (click counted) -> apply -> attributed -> counts + badges all
  correct, no errors. Starting Phase 5 (seed + full E2E + polish).
- [2026-07-08] **Phase 5 DONE + tested. BUILD COMPLETE.** `db.seed_demo_engagement()` seeds
  messages, visits, and one attributed physician referral on top of the demo leads (idempotent;
  runs at startup in NO_LOGIN after the demo user exists). Full E2E verified on a fresh DB:
  all 8 surfaces return 200 (`/`, `/how-it-works`, `/applications`, `/alerts`, `/app`,
  `/app/leads`, `/app/dashboard`, `/app/invite`); dashboard funnel/drop-off/time-in-stage/
  engagement render; candidate workspace shows visit form + thread + physician-referred badge;
  invite list shows outcomes; patient applications show thread + "Referred by" + booked visit;
  reminders sweep runs clean (2 nudges); existing search-cache regression tests still pass (4/4).
  No errors in any log.
- [2026-07-08] **Supply-side capture layer DONE + tested.** Added lightweight site onboarding
  and claimed-study scoping so demand can be monetized: new `site_profiles` + `study_claims`
  tables, `/app/site` setup page (org/contact + claim NCT), study-team nav link, and scoped
  candidate review/dashboard (`/app/leads`, `/app/dashboard`) by claimed NCTs only. Lead actions
  (`accept/decline/schedule/redcap/status`) now enforce claim ownership (403 if unclaimed). Site
  notifications now route by claimed NCT contact email (fallback to `SITE_NOTIFY_EMAIL`). Demo
  seeding now includes profile + claims so the no-login walkthrough is live by default.
  Verified E2E on fresh DB: setup/leads/dashboard all render, claims persist, scoped counts are
  correct (5/8 leads visible after scoping), unauthorized post-block works, and no errors.
- [2026-07-08] **Commercial proof layer DONE.** Added auditable outcome reconciliation:
  `lead_reconciliations` table + `/app/leads/<id>/reconcile` workflow so study teams can
  record source-backed proof (REDCap/CTMS/manual reference) for enrolled/retained outcomes.
  `analytics.funnel_stats()` now reports verified-enrollment counts/rate plus channel-level
  source throughput (`source_breakdown`) so distribution quality is measurable by source, not
  just top-of-funnel volume.

## How to run / demo (morning)
```
cd matcher/web
DB_PATH=/tmp/bridgemd.db NO_LOGIN=1 PORT=8098 ../.venv/bin/python app.py
```
Then walk the three POVs (switcher bar is visible in NO_LOGIN):
- **Patient**: `/` search -> apply -> `/applications` (status stepper, message the team, see booked
  visits, "Referred by" if they came via a doctor). `/alerts` to get pushed new trials.
- **Study team**: `/app/leads` (blinded review) -> Open workspace `/c/<token>` (accept -> unlock
  contact, message, book a visit) -> `/app/dashboard` (live funnel, drop-off, time-in-stage).
- **Clinician**: `/app` find trials, `/app/invite` mint a "your doctor referred you" link and
  watch outcomes come back.

## New env vars (all optional; sensible defaults)
- REMINDERS_BACKGROUND (default 1), REMINDERS_INTERVAL_SECONDS (3600),
  REMINDERS_VISIT_WINDOW_HOURS (24), REMINDERS_QUIET_DAYS (3).
- Email for messages/reminders/nudges reuses the existing switch: set NOTIFY_LIVE=1 + SMTP_* to
  turn on real delivery. With it off, everything still works in-app (system messages in the thread).
- Cron alternative to the background threads: GET `/reminders/run` (and `/alerts/run`), keyed by
  ALERTS_CRON_KEY in prod.

## Note / caution
- Repo root is `matcher/` (git), not `GraphMD/`. During Phase 3 the clinician `dashboard.html`
  was briefly overwritten and restored from git HEAD; if it had unpushed local edits before this
  session, double-check it renders as expected (verified it renders "Find a trial" fine here).
