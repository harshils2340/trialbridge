# Enrollment & Retention build (Northstar doc)

**Northstar: maximize patient enrollment AND retention in clinical trials via the platform.**

This is the living design doc. It captures the panel-derived requirements, the design
decisions, and a progress log. Update it at the end of every phase.

## THE KPI: two tiers (test EVERY feature against these before building)
BridgeMD is not a portal. It's an **enrollment optimization engine**. Safety/compliance is
the hard gate first; within it, everything ladders up to a **two-tier KPI**.

**Tier 1, North Star (outcome): Enrollment Velocity**
> **Enrollment Velocity** = the rate at which patients move through the funnel over time:
> **found → contacted → screened → enrolled → retained.**

This is the outcome the PI/coordinator is graded on and what the license is worth.

**Tier 2, Efficiency KPI (the lever): Operational Efficiency**
> **Operational Efficiency** = coordinator/PI **time & capacity per enrolled patient**, > (a) **cycle time** (decision → consent signed → enrolled; how long a doc/task sits
> `pending` before done) and (b) **capacity** (applicants/studies one coordinator can run
> without screen→enroll conversion dropping).

The admin/operations work (Documents & approvals, ATS triage, the PI workspace) lives in
Tier 2. **The link to Tier 1 is explicit and is the whole pitch: less time on admin = more
time enrolling and retaining.** Efficiency only counts when it **shortens cycle time or
raises capacity WITHOUT degrading downstream conversion**, otherwise it's busywork.

Before building ANY feature, answer: *does it move a funnel stage directly (Tier 1), or cut
coordinator/PI cycle time/capacity in a way that traces to a funnel stage (Tier 2)?* If it
moves neither, it probably shouldn't be built.

**Guardrails (a feature must pass ALL, or it fails the KPI):**
1. **Compliance/safety is the hard gate, FIRST.** No velocity OR efficiency gain justifies
   breaking `.cursor/rules/compliance.mdc` (anti-kickback, blinded consent, PHI). Ever.
   Making admin faster/easier never trumps safety.
2. **Net throughput, not vanity counts.** A feature must not inflate an upstream stage at
   the expense of a downstream one. Raising "contacted" by sending sites unqualified
   applicants *lowers* screen→enroll conversion and burns trust, that's negative velocity.
   Optimize the whole funnel, or one stage without degrading the next.
3. **Efficiency must trace to enrollment.** A Tier-2 feature must cut cycle time or raise
   capacity AND name the funnel stage the saved time is redirected into (usually
   screened→enrolled or retained). "Looks organized" is not a KPI.

Note on wording: *retention* is the opposite vector of "velocity" (keeping people in, not
pushing them through), but it lives under Tier 1 because a dropout is **negative net
enrollment**. Think "throughput minus leakage." Tier 2 (efficiency) is not a competing goal, it's how a swamped site actually achieves Tier 1.

**Agent commitment:** for every proposed feature I will state which tier it moves, an
Enrollment Velocity stage (Tier 1) and/or Operational Efficiency (Tier 2, naming the funnel
stage the saved time feeds), and flag it explicitly if it moves neither, or risks any
guardrail. See `.cursor/rules/enrollment-velocity.mdc` (always on).

## Founder execution preference (shipping)
- After completing a requested implementation batch and validating it locally, **commit and push
  immediately by default** (unless the founder explicitly says not to push yet).
- Always report clearly whether the latest changes are pushed or only local.
- Treat "push everything" as the standing default: do not pause to ask for push
  confirmation unless the founder explicitly says to hold.

## Why (evidence from the Rare Disease Day recruitment/retention panel)
- 86% of trials miss recruitment deadlines.
- ~$6K to recruit a patient; ~$19K to replace one lost to follow-up.
- Only 8% of patients are ever *asked* to join a trial.
- ~77% of sites have **no recruitment plan** / nobody owns it.
- Best-in-class sites hit 97% retention; typical is far below.
- The through-line: **failure = passivity + silence.** Patients drop out mostly because
  they stop hearing from anyone. Every fix = *be proactive + keep the communication loop alive.*

## External signal log -> KPI map -> money map
This section captures real operator feedback and translates it into
prioritized, monetizable product work. Keep adding entries over time.

### 2026-07-10 interview signal (Lora Giangregorio, UW)
Raw themes from response:
- Channels that currently work:
  - social ads (geotargeted + boosted)
  - clinician referrals (specialists, PTs)
  - owned lists/networks (local list + partner org list)
  - targeted partner outreach emails
- Operational pattern:
  - they usually hit targets eventually
  - they run feasibility tests to validate channels before full trial launch
- Biggest pain points:
  - low male participation in their cohorts
  - over-representation of white / higher-income / higher-education participants
  - hard to reach less health-engaged populations outside healthcare networks

### 2026-07-09 interview signal (Katherine Chan, KITE)
Raw themes from response:
- Channels that currently work:
  - community outreach organizations (e.g., SCI Ontario, March of Dimes)
  - KITE Research Institute recruitment website
- Handoff/drop-off:
  - in their current model, candidates mostly self-identify directly from outreach;
    less explicit inter-team handoff in the intake path
- What improved enrollment:
  - moving away from passive clinic flyers toward community-org partnerships
  - launch of a dedicated recruitment website increased inbound interest volume

### 2026-07-21 interview signal (coordinator/PI workflow debrief)
Raw themes from response:
- Channels, ranked by effectiveness:
  - **physician referral (most effective)**, doctor refers on the patient's behalf;
    works because they already know the medical history, so screen->enroll is high.
  - **hospital ads**, mainly yields *healthy controls*.
  - **ClinicalTrials.gov (least effective)**, keep as free reach, don't over-invest.
- Current intake path: doctor refers -> give the lab's email -> RedCap questionnaire
  to confirm eligibility. (Referral -> intake -> prescreen is the money path.)
- Tooling reality: everything runs off a **master Excel sheet** today.

Feature backlog from this debrief (triaged; not yet built, prioritized notes only):
- **Master Excel -> ATS (the wedge).** Import their master sheet AND export back so
  they trust us as the single source of truth. KPI: efficiency/capacity + all stages.
- **Audit-ready export (CSV + PDF).** One button to hand an auditor recruitment
  activity, applicant paths, document reviews. KPI: efficiency + sales trust. Low risk.
  *(Priority #1, safe, high demo value.)*
- **Do-not-recruit / suppression list.** Never re-contact opt-outs / flagged people;
  checked on intake + apply. KPI: efficiency + protects screen->enroll. Compliance-positive.
  *(Priority #2.)*
- **Referral-path polish.** Tighten physician-referral -> intake -> prescreen since it's
  the #1 channel. KPI: found->contacted->screened. *(Priority #3.)*
- **Per-person call notes / call log.** Freeform internal note on a lead. KPI: efficiency. Small.
- **Integrated scheduling.** Booking link for screening visits (we already have visits +
  reminders + calendar invites). KPI: screened->enrolled. Build later.
- **Patient copilot (Q&A over site data).** BUILD WITH GUARDRAILS: retrieve ONLY that
  account's own data, never send PHI to a model that trains on it (BAA / zero-retention
  endpoint), log every query. KPI: efficiency. Not a quick item.

Compliance red flags from this debrief (hard gate, do not ship without IRB/counsel):
- **"Add patients on behalf of consent w/ a family member", do NOT build as stated.**
  A coordinator may *enter a record*, but consent cannot be manufactured. Surrogate/family
  consent is valid only for an incapacitated patient via a Legally Authorized Representative
  under the IRB-approved process (HIPAA + Common Rule). Reframe: data entry is fine; consent
  is still captured from the patient or a documented LAR. Needs IRB/counsel before any build.
- **Copilot = PHI surface.** Requires BAA / zero-retention model, per-account data scoping,
  and query audit logging before it touches patient data.

### Problem frequency map (from field signals so far)
Use this simple scale each time: `high`, `medium`, `low`.

- **High frequency**
  - channel uncertainty before launch ("what will actually recruit?")
  - leakage during handoff from outreach -> screening
  - low diversity in final enrolled cohorts
  - passive channel underperformance (flyers/general poster tactics)
- **Medium frequency**
  - under-recruitment in specific demographics (e.g., men for osteoporosis/exercise)
  - over-reliance on existing health-aware networks
  - weak community-partner distribution in early trial setup
- **Low frequency (but still tracked)**
  - total recruitment failure (many sites still eventually hit, but late)

### Money map (how each problem hits revenue/cost)
Use panel baselines already in this doc: ~$6K to recruit, ~$19K to replace dropout.

- **Problem: channel uncertainty**
  - Money impact: wasted spend on low-yield channels + slower enrollment velocity.
  - KPI impact: lowers found->contacted and contacted->screened efficiency.
  - Product response: per-channel attribution + feasibility mode + pre-launch channel scorecard.
- **Problem: handoff/drop-off**
  - Money impact: screened candidates fail to convert; replacement cost accumulates.
  - KPI impact: biggest hit on screened->enrolled and enrolled->retained.
  - Product response: ATS queue discipline, response-time SLA tracking, reminders, two-way messaging.
- **Problem: passive channel underperformance**
  - Money impact: low-yield outreach spend + slower top-of-funnel fill.
  - KPI impact: slows found->contacted velocity and increases time-to-first-screen.
  - Product response: community-partner templates, source-level conversion reporting, and channel playbooks that de-prioritize low-yield flyer-style tactics.
- **Problem: low cohort diversity / demographic imbalance**
  - Money impact: longer recruitment timelines, protocol amendment risk, sponsor dissatisfaction.
  - KPI impact: slows contacted->screened throughput for underrepresented groups.
  - Product response: demographic gap dashboard + targeted channel recommendations by missing segment.

### "Switch if needed" decision rules (vendor/process)
Keep decisions objective using KPI + cost signals:

- **Keep current path** when:
  - enrollment velocity improves for 2+ consecutive cycles, and
  - cost per screened and cost per enrolled trend down or flat.
- **Pilot alternative (tool/vendor/channel)** when:
  - any stage conversion degrades for 2 consecutive cycles, or
  - median time to enrolled increases while spend rises.
- **Switch** when:
  - pilot shows >=15% improvement in either screened->enrolled conversion or
    time-to-enrolled, without hurting retention or compliance.

### Flexpa fit in this map (plain)
Which Flexpa connection type is useful first for this workflow:

- **Use first: EHR connection**
  - Why: most useful for fast pre-screen verification (diagnosis, meds, recent labs).
  - KPI stage moved: contacted->screened, screened->enrolled.
- **Use second: Payer connection**
  - Why: supports utilization/coverage context and can reduce dead-end screenings.
  - KPI stage moved: screened->enrolled (fewer late disqualifications).
- **Use later: TEFCA / IAL2 flow**
  - Why: broadest reach but higher identity friction; run after base funnel is stable.
  - KPI risk: can hurt conversion if added too early in the flow.

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
- [2026-07-09] **SCRS finding -> funded recruitment proof shipped.**
  Added sponsor-facing recruitment spend logging by trial/source and ROI surface
  in `/app/dashboard` (total spend, cost per screened, cost per enrolled, spend
  by source), plus CSV export integration and regression test
  (`test_spend_roi.py`). This directly supports budget justification loops from
  site to sponsor/CRO.
- [2026-07-09] **Sponsor onboarding + ops hardening slice DONE + tested.**
  Added `/ops/readiness` (keyed operational posture endpoint), sponsor-facing
  reporting exports (`/app/dashboard/export.csv`, `/app/dashboard/summary.json`),
  expanded site setup profile fields (intake SLA, escalation email, CTMS/REDCap
  endpoints), plus backup/recovery scripts (`backup_db.py`, `restore_drill.py`).
  Updated staging smoke to probe ops readiness when key is supplied and added
  production onboarding/legal checklists under `web/`.
- [2026-07-09] **Pre-enrollment readiness checks DONE + tested (coverage + travel).**
  Added a dedicated support-readiness layer to reduce late-stage drop-off before
  enrollment: new `lead_support_checks` storage, payer eligibility checks (`payer.py`),
  travel/logistics planning (`logistics.py`), patient self-serve forms on
  `/applications`, and study-team refresh actions on `/app/leads`. Results are
  persisted per candidate, surfaced to both sides, and appended into the lead
  timeline so blockers are visible early. API adapters are live-ready through
  env vars (`PAYER_*`, `LOGISTICS_*`) with deterministic sandbox fallback so the
  full UX works immediately without vendor keys.
- [2026-07-09] **Patient acquisition boost (optional compensation signal) DONE.**
  Added a conservative "compensation likelihood" helper on patient search results
  and trial detail pages, plus an optional "Higher-pay potential" sort. This is
  designed to increase top-of-funnel discovery without degrading downstream
  throughput: default ranking remains match-first (eligibility + distance), and
  pay is guidance-only with explicit "confirm with site" messaging because
  ClinicalTrials.gov has no reliable structured pay field.
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
