"""SQLite storage for BridgeMD: users, referrals, and status history.

One connection per request via Flask's `g`. Schema is created on first run
(idempotent), so there is no separate migration step to run.
"""
import datetime as dt
import json
import os
import pathlib
import re
import secrets
import sqlite3
import time

from flask import g

# DB_PATH is configurable so production can point at a persistent disk
# (e.g. a Render mounted disk). Defaults to a file next to this module.
DB_PATH = pathlib.Path(
    os.environ.get("DB_PATH")
    or (pathlib.Path(__file__).resolve().parent / "bridgemd.db"))

# Referral pipeline. "received" = the site confirmed it got the referral.
# "enrolled" is the commission-eligible terminal state.
STATUSES = ["referred", "received", "contacted", "screened", "enrolled",
            "screen_failed", "declined", "withdrawn"]
OPEN_STATUSES = {"referred", "received", "contacted", "screened"}
# Who can move a referral into each status (site = coordinator via token link).
SITE_STATUSES = ["received", "contacted", "screened", "enrolled", "screen_failed"]

# Patient "application" pipeline - the real clinical-trial funnel, Indeed-style.
# applied -> pre-screen (records/questions) -> likely eligible (site confirms)
# -> screening visit (forms + in-person checks) -> enrolled.
LEAD_PIPELINE = ["submitted", "prescreen", "eligible", "screening", "enrolled"]
LEAD_CLOSED = ["closed", "withdrawn"]
LEAD_STATUSES = LEAD_PIPELINE + LEAD_CLOSED
LEAD_LABELS = {
    "submitted": "Application submitted",
    "prescreen": "Pre-screening",
    "eligible": "Likely eligible",
    "screening": "Screening visit",
    "enrolled": "Enrolled",
    "closed": "Not a match / closed",
    "withdrawn": "Withdrawn",
}
RECON_OUTCOMES = [
    "enrolled_verified",
    "enrolled_rejected",
    "retained_30d",
    "retained_90d",
]
RECON_LABELS = {
    "enrolled_verified": "Enrollment verified",
    "enrolled_rejected": "Enrollment not confirmed",
    "retained_30d": "30-day retained",
    "retained_90d": "90-day retained",
}
# Plain one-liners shown to the patient under each stage.
LEAD_BLURB = {
    "submitted": "We've saved your interest and shared it with the study team.",
    "prescreen": "The study team is checking your basic details - and any records "
                 "you've connected - to see if you might fit.",
    "eligible": "Good news: the study team thinks you're likely eligible and wants "
                "to take the next step with you.",
    "screening": "Next is the screening visit - some forms and in-person checks to "
                 "confirm you qualify before you start.",
    "enrolled": "You've been enrolled in the study. Congratulations!",
    "closed": "This study isn't moving forward with your application right now - it's "
              "worth applying to other matching trials.",
    "withdrawn": "You withdrew this application.",
}
# Legacy status keys -> current pipeline (applied idempotently on startup).
_LEAD_STATUS_REMAP = {"reviewing": "prescreen", "contacted": "eligible"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    name          TEXT NOT NULL,
    specialty     TEXT DEFAULT '',
    institution   TEXT DEFAULT '',
    verified      INTEGER DEFAULT 0,
    verified_at   TEXT DEFAULT '',
    oauth_provider TEXT DEFAULT '',
    oauth_sub     TEXT,
    oauth_picture TEXT DEFAULT '',
    created_at    TEXT NOT NULL
);

-- Patient accounts (separate from clinician/study-team users).
CREATE TABLE IF NOT EXISTS patient_users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    email             TEXT UNIQUE NOT NULL,
    password_hash     TEXT NOT NULL,
    full_name         TEXT DEFAULT '',
    oauth_provider    TEXT DEFAULT '',
    oauth_sub         TEXT,
    oauth_picture     TEXT DEFAULT '',
    applicant_token   TEXT UNIQUE NOT NULL,
    verified          INTEGER DEFAULT 0,
    verified_at       TEXT DEFAULT '',
    onboarding_done   INTEGER DEFAULT 0,
    primary_interest  TEXT DEFAULT '',
    notify_email      TEXT DEFAULT '',
    email_alerts      INTEGER DEFAULT 1,
    created_at        TEXT NOT NULL
);

-- Short-lived email codes for patient signup/login verification.
CREATE TABLE IF NOT EXISTS patient_auth_codes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id    INTEGER NOT NULL,
    purpose       TEXT NOT NULL,              -- 'signup' | 'login'
    code          TEXT NOT NULL,
    expires_ts    INTEGER NOT NULL,
    used_at       TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    FOREIGN KEY (patient_id) REFERENCES patient_users(id)
);

-- Short-lived email codes for clinician signup/login verification.
CREATE TABLE IF NOT EXISTS user_auth_codes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    purpose       TEXT NOT NULL,              -- 'signup' | 'login'
    code          TEXT NOT NULL,
    expires_ts    INTEGER NOT NULL,
    used_at       TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Per-IP counters for lightweight POST rate limiting on sensitive endpoints.
CREATE TABLE IF NOT EXISTS ip_rate_limits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    route_key     TEXT NOT NULL,
    ip            TEXT NOT NULL,
    window_start  INTEGER NOT NULL,
    hit_count     INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    UNIQUE (route_key, ip)
);

-- Study-team account profile (organization + contact info).
CREATE TABLE IF NOT EXISTS site_profiles (
    user_id       INTEGER PRIMARY KEY,
    org_name      TEXT DEFAULT '',
    contact_name  TEXT DEFAULT '',
    contact_email TEXT DEFAULT '',
    contact_phone TEXT DEFAULT '',
    intake_sla_hours TEXT DEFAULT '',
    escalation_email TEXT DEFAULT '',
    ctms_endpoint TEXT DEFAULT '',
    redcap_endpoint TEXT DEFAULT '',
    redcap_project_label TEXT DEFAULT '',
    redcap_api_token TEXT DEFAULT '',
    redcap_field_map TEXT DEFAULT '',
    redcap_intake_instrument TEXT DEFAULT '',
    redcap_intake_enabled INTEGER DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- A study team claims NCTs they manage. Leads are scoped by these claims.
-- `verified` gates access: a claim only exposes applicant PHI once approved,
-- so a self-serve account can't harvest a trial's applicants by claiming its NCT.
CREATE TABLE IF NOT EXISTS study_claims (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    nct         TEXT NOT NULL,
    title       TEXT DEFAULT '',
    notify_email TEXT DEFAULT '',
    verified    INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    UNIQUE (user_id, nct),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Studies posted directly by sites (not yet on ClinicalTrials.gov). These are
-- surfaced in patient search and routed through the same lead pipeline.
CREATE TABLE IF NOT EXISTS site_posted_studies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    nct           TEXT UNIQUE NOT NULL,
    title         TEXT NOT NULL,
    condition     TEXT DEFAULT '',
    brief_summary TEXT DEFAULT '',
    eligibility   TEXT DEFAULT '',
    location      TEXT DEFAULT '',
    site_name     TEXT DEFAULT '',
    contact_email TEXT DEFAULT '',
    contact_phone TEXT DEFAULT '',
    phase         TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'recruiting',
    lat           REAL,
    lon           REAL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS referrals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    token           TEXT UNIQUE,
    nct             TEXT NOT NULL,
    title           TEXT NOT NULL,
    patient_label   TEXT DEFAULT '',
    patient_summary TEXT DEFAULT '',
    condition       TEXT DEFAULT '',
    country         TEXT DEFAULT '',
    site            TEXT DEFAULT '',
    coordinator     TEXT DEFAULT '',
    coordinator_email TEXT DEFAULT '',
    patient_name    TEXT DEFAULT '',
    patient_contact TEXT DEFAULT '',
    consent         INTEGER DEFAULT 0,
    consent_at      TEXT DEFAULT '',
    notified_at     TEXT DEFAULT '',
    verdict         TEXT DEFAULT '',
    score           INTEGER DEFAULT 0,
    rationale       TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'referred',
    commission_cents INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS referral_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    referral_id INTEGER NOT NULL,
    status      TEXT NOT NULL,
    note        TEXT DEFAULT '',
    actor       TEXT DEFAULT 'you',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (referral_id) REFERENCES referrals(id)
);

-- Patient-initiated interest ("apply to a trial"). This is the consumer funnel:
-- a normal person finds a trial and asks to be contacted. Consented leads are
-- what sponsors/sites pay for. No PHI beyond what the patient volunteers.
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT UNIQUE,
    site_token      TEXT UNIQUE,
    site_token_expires_at TEXT DEFAULT '',
    site_token_revoked INTEGER DEFAULT 0,
    applicant_token TEXT DEFAULT '',
    nct             TEXT DEFAULT '',
    title           TEXT DEFAULT '',
    condition       TEXT DEFAULT '',
    location        TEXT DEFAULT '',
    site            TEXT DEFAULT '',
    name            TEXT DEFAULT '',
    email           TEXT DEFAULT '',
    phone           TEXT DEFAULT '',
    age             TEXT DEFAULT '',
    sex             TEXT DEFAULT '',
    notes           TEXT DEFAULT '',
    consent         INTEGER DEFAULT 0,
    source          TEXT DEFAULT 'web',
    status          TEXT NOT NULL DEFAULT 'submitted',
    records_connected INTEGER DEFAULT 0,
    record_summary  TEXT DEFAULT '',
    records_authorized_at TEXT DEFAULT '',
    screener        TEXT DEFAULT '',
    eligibility     TEXT DEFAULT '',
    prescreen_readiness TEXT DEFAULT '',
    decision        TEXT DEFAULT '',
    decision_reason TEXT DEFAULT '',
    decided_at      TEXT DEFAULT '',
    revealed        INTEGER DEFAULT 0,
    schedule_url    TEXT DEFAULT '',
    nudged_at       TEXT DEFAULT '',
    nudge_count     INTEGER DEFAULT 0,
    referred_by     TEXT DEFAULT '',
    invite_token    TEXT DEFAULT '',
    registry_opt_in INTEGER DEFAULT 0,
    registry_consent_at TEXT DEFAULT '',
    registry_consent_version TEXT DEFAULT '',
    redcap_record_id TEXT DEFAULT '',
    redcap_survey_status TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT DEFAULT ''
);

-- Physician "invite a patient" links (the trusted-messenger channel). A clinician
-- mints a link for a specific trial; a patient who opens it and applies is
-- attributed back so the clinician can see what happened (closes the refer loop).
CREATE TABLE IF NOT EXISTS invites (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    token          TEXT UNIQUE NOT NULL,
    clinician_id   INTEGER,
    clinician_name TEXT DEFAULT '',
    nct            TEXT DEFAULT '',
    title          TEXT DEFAULT '',
    condition      TEXT DEFAULT '',
    note           TEXT DEFAULT '',
    clicks         INTEGER DEFAULT 0,
    created_at     TEXT NOT NULL
);

-- Two-way messages between a patient and the study team for an application.
-- 'system' messages are automated nudges/reminders. Retention lives or dies on
-- this staying alive; every message also has an in-app surface so it works with
-- email off. Blinded model: the site may only message once the candidate is
-- revealed (accepted); the patient may message their own application anytime.
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id      INTEGER NOT NULL,
    sender       TEXT NOT NULL,          -- 'patient' | 'site' | 'system'
    body         TEXT NOT NULL,
    read_patient INTEGER DEFAULT 0,
    read_site    INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

-- Files shared inside a candidate's private thread (consent forms, insurance
-- cards, questionnaires). Scoped to one lead so a document only ever crosses
-- between that patient and their study team - never a shared/participant view.
CREATE TABLE IF NOT EXISTS message_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id      INTEGER NOT NULL,
    uploaded_by  TEXT NOT NULL,          -- 'site' | 'patient'
    orig_name    TEXT NOT NULL,
    stored_name  TEXT NOT NULL,
    mime         TEXT DEFAULT '',
    size_bytes   INTEGER DEFAULT 0,
    note         TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

-- Per-candidate to-do checklist. The study team assigns action items ("sign
-- consent", "upload insurance card") to a patient so both sides can track what
-- is outstanding without email ping-pong. assigned_to = who must act.
CREATE TABLE IF NOT EXISTS lead_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id      INTEGER NOT NULL,
    title        TEXT NOT NULL,
    assigned_to  TEXT DEFAULT 'patient', -- 'patient' | 'site'
    status       TEXT DEFAULT 'open',    -- 'open' | 'done'
    created_by   TEXT DEFAULT 'site',
    created_at   TEXT NOT NULL,
    done_at      TEXT DEFAULT '',
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

-- Internal, team-only notes on a candidate. Never shown to the patient - this is
-- the coordinator/PI/CRO scratchpad (context, call outcomes, screening judgment).
CREATE TABLE IF NOT EXISTS lead_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL,
    body       TEXT NOT NULL,
    author     TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

-- Specific records the study team asks a candidate for (e.g. "pathology report
-- confirming diagnosis", "records of prior therapy"). Unlike a free-text to-do,
-- each request tracks a status and links the uploaded file, so a coordinator can
-- see at a glance what is still outstanding before the screening window closes.
CREATE TABLE IF NOT EXISTS doc_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id       INTEGER NOT NULL,
    title         TEXT NOT NULL,
    note          TEXT DEFAULT '',
    status        TEXT DEFAULT 'requested', -- requested | received | accepted | rejected
    attachment_id INTEGER,                  -- message_attachments.id once uploaded
    review_note   TEXT DEFAULT '',          -- reason shown to patient on reject
    created_by    TEXT DEFAULT 'site',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

-- Internal team room: staff-only collaboration keyed by trial (NCT). Not
-- patient-visible. Lets recruiters/coordinators coordinate and share working
-- documents among the study team.
CREATE TABLE IF NOT EXISTS team_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    nct          TEXT NOT NULL,
    sender_user_id INTEGER,
    sender_name  TEXT DEFAULT '',
    body         TEXT DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_message_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    team_message_id INTEGER NOT NULL,
    orig_name    TEXT NOT NULL,
    stored_name  TEXT NOT NULL,
    mime         TEXT DEFAULT '',
    size_bytes   INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (team_message_id) REFERENCES team_messages(id)
);

-- Scheduled visits (screening, follow-up) a site books for a candidate. Drives
-- automatic reminders (see reminders.py) and the patient's "upcoming visit" view.
CREATE TABLE IF NOT EXISTS lead_visits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL,
    kind        TEXT DEFAULT 'screening',
    visit_at    TEXT NOT NULL,
    location    TEXT DEFAULT '',
    note        TEXT DEFAULT '',
    reminded_at TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

-- Extra ATTENDEES on a visit beyond the fixed two (the participant and the
-- coordinator/organizer). Lets a coordinator add/edit/remove guests - a
-- sub-investigator, PI, interpreter, caregiver, or sponsor monitor - and switch
-- a guest's role inline. Record-only scheduling metadata: guests are NOT emailed
-- automatically (avoids sending anything to an unverified address).
CREATE TABLE IF NOT EXISTS visit_guests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    visit_id   INTEGER NOT NULL,
    name       TEXT DEFAULT '',
    email      TEXT DEFAULT '',
    role       TEXT DEFAULT 'guest',   -- see GUEST_ROLES in code
    created_at TEXT NOT NULL,
    FOREIGN KEY (visit_id) REFERENCES lead_visits(id)
);

-- Participant PAYMENTS (stipends / reimbursement for time & travel).
-- COMPLIANCE (see COMPLIANCE.md §5): these pay STUDY SUBJECTS only - never a
-- referral source, and never tied to enrollment. Every rule carries its
-- IRB/REB-approved amount + attestation; the ledger is the auditable record and
-- the tax basis (US 1099 threshold). A payment rule pays a fixed amount per
-- completed visit of a given kind (auto-queued from the calendar).
CREATE TABLE IF NOT EXISTS payment_rules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    nct          TEXT DEFAULT '',
    kind         TEXT DEFAULT 'screening',   -- visit kind this rule pays for
    label        TEXT DEFAULT '',            -- e.g. "Screening visit - time & travel"
    amount_cents INTEGER DEFAULT 0,
    currency     TEXT DEFAULT 'USD',
    method       TEXT DEFAULT 'gift_card',   -- gift_card | manual | ach
    irb_approved INTEGER DEFAULT 0,          -- hard gate before it can auto-issue
    irb_note     TEXT DEFAULT '',            -- approval ref / consent section
    active       INTEGER DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
-- One row per amount owed/paid to a participant (usually per completed visit).
CREATE TABLE IF NOT EXISTS participant_payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id      INTEGER NOT NULL,
    nct          TEXT DEFAULT '',
    visit_id     INTEGER,                    -- the completed visit that triggered it
    rule_id      INTEGER,
    kind         TEXT DEFAULT '',
    label        TEXT DEFAULT '',
    amount_cents INTEGER DEFAULT 0,
    currency     TEXT DEFAULT 'USD',
    method       TEXT DEFAULT 'gift_card',
    -- queued (owed, not sent) | issued (sent to provider) | paid (confirmed)
    -- | void (cancelled) | failed
    status       TEXT DEFAULT 'queued',
    provider     TEXT DEFAULT '',            -- manual | tremendous | tango | ...
    provider_ref TEXT DEFAULT '',            -- external disbursement id
    note         TEXT DEFAULT '',
    created_by   TEXT DEFAULT 'system',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    issued_at    TEXT DEFAULT '',
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);
-- Tax / payout profile per participant. Totals are derived from the ledger; this
-- only holds the W-9 collection state that gates crossing the 1099 threshold.
CREATE TABLE IF NOT EXISTS payment_recipients (
    lead_id        INTEGER PRIMARY KEY,
    w9_status      TEXT DEFAULT 'not_needed', -- not_needed | requested | collected
    w9_collected_at TEXT DEFAULT '',
    payout_email   TEXT DEFAULT '',
    updated_at     TEXT DEFAULT '',
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);
-- Append-only audit trail for money movement (never updated/deleted).
CREATE TABLE IF NOT EXISTS payment_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id  INTEGER NOT NULL,
    action      TEXT DEFAULT '',   -- queued|issued|paid|void|failed|w9_requested|w9_collected
    actor       TEXT DEFAULT '',
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_lead ON participant_payments(lead_id);
CREATE INDEX IF NOT EXISTS idx_payments_nct ON participant_payments(nct, status);
CREATE INDEX IF NOT EXISTS idx_payment_rules_user ON payment_rules(user_id, nct);
CREATE INDEX IF NOT EXISTS idx_payment_events_pay ON payment_events(payment_id);

-- Sponsor -> site UPDATES (amendments, safety letters, doc requests, bulletins).
-- The #1 site burden is protocol amendments: one sponsor change forces every
-- site to acknowledge, submit to IRB, update the ICF, RE-CONSENT enrolled
-- participants, and retrain. This models that as one item per site with an
-- explicit checklist state, so nothing falls through the cracks. In this MVP an
-- update is owned by the site (user_id); a future sponsor side will broadcast
-- one update to many sites. The re-consent step books real visits on the
-- calendar (lead_visits.update_id links them back). KPI: Tier-2 efficiency +
-- retention (staying compliant through an amendment avoids dropouts/deviations).
CREATE TABLE IF NOT EXISTS sponsor_updates (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    nct            TEXT DEFAULT '',
    type           TEXT DEFAULT 'amendment',  -- amendment|safety|doc_request|bulletin
    version        TEXT DEFAULT '',           -- e.g. "Protocol v3.0"
    title          TEXT DEFAULT '',
    summary        TEXT DEFAULT '',           -- plain-language "what changed"
    source         TEXT DEFAULT '',           -- sponsor/CRO name (free text in MVP)
    received_at    TEXT DEFAULT '',
    due_at         TEXT DEFAULT '',
    requires_ack   INTEGER DEFAULT 1,
    -- Checklist state. '' or 'pending' = to do; 'not_required' hides the step.
    ack_status         TEXT DEFAULT 'pending',   -- pending|done
    ack_at             TEXT DEFAULT '',
    irb_status         TEXT DEFAULT 'pending',   -- pending|submitted|approved|not_required
    irb_submitted_at   TEXT DEFAULT '',
    irb_approved_at    TEXT DEFAULT '',
    icf_status         TEXT DEFAULT 'pending',   -- pending|done|not_required
    icf_note           TEXT DEFAULT '',
    reconsent_status   TEXT DEFAULT 'pending',   -- pending|in_progress|complete|not_required
    retrain_status     TEXT DEFAULT 'pending',   -- pending|done|not_required
    created_by     TEXT DEFAULT 'you',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS update_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    update_id   INTEGER NOT NULL,
    action      TEXT DEFAULT '',
    actor       TEXT DEFAULT '',
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sponsor_updates_user ON sponsor_updates(user_id, nct);
CREATE INDEX IF NOT EXISTS idx_update_events_upd ON update_events(update_id);

-- Controlled-document version history per study (Protocol, ICF, IB, …). An
-- amendment introduces a NEW version (status 'pending' until IRB-approved), then
-- it becomes 'current' and the prior 'current' version is 'superseded'. Lets a
-- site see exactly which document version is in effect (and its trail) instead of
-- hunting through email - the #1 cause of consenting someone on a stale ICF.
-- These are STUDY documents (not PHI, not ads); review access is site-team only.
CREATE TABLE IF NOT EXISTS study_doc_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    nct           TEXT DEFAULT '',
    doc_type      TEXT DEFAULT 'protocol',   -- protocol|icf|ib|other
    version       TEXT DEFAULT '',           -- "v4.0"
    label         TEXT DEFAULT '',           -- optional human label
    status        TEXT DEFAULT 'current',    -- pending|current|superseded
    effective_at  TEXT DEFAULT '',
    update_id     INTEGER,                   -- amendment that introduced it
    note          TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_doc_versions_study ON study_doc_versions(user_id, nct, doc_type);
CREATE INDEX IF NOT EXISTS idx_doc_versions_update ON study_doc_versions(update_id);

-- One-way calendar SYNC: every study visit published as a live iCalendar feed the
-- coordinator subscribes to from Google/Outlook/Apple (read-only, auto-refreshing),
-- so visits appear in the calendar they already live in - no double entry, fewer
-- missed visits. `token` is the per-user feed secret (the URL is the auth). Events
-- are DE-IDENTIFIED by default (initials + code, never full name) so no PHI leaves
-- BridgeMD to a non-BAA calendar vendor. KPI: Tier-2 efficiency + retention.
CREATE TABLE IF NOT EXISTS calendar_feeds (
    user_id        INTEGER PRIMARY KEY,
    token          TEXT NOT NULL,             -- secret path segment for the .ics URL
    provider       TEXT DEFAULT '',           -- ''|google|outlook|apple|ics (connected app)
    connected_at   TEXT DEFAULT '',
    last_synced_at TEXT DEFAULT '',           -- updated each time the feed is fetched
    deidentify     INTEGER DEFAULT 1,         -- 1 = initials+code only (compliant default)
    created_at     TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_calendar_feeds_token ON calendar_feeds(token);

-- Payer eligibility + travel/logistics readiness checks per lead. These reduce
-- late-stage drop-off by surfacing blockers (coverage/travel) earlier.
CREATE TABLE IF NOT EXISTS lead_support_checks (
    lead_id              INTEGER PRIMARY KEY,
    coverage_status      TEXT DEFAULT '',
    coverage_note        TEXT DEFAULT '',
    coverage_payload     TEXT DEFAULT '',
    coverage_provider    TEXT DEFAULT '',
    coverage_ref         TEXT DEFAULT '',
    coverage_checked_at  TEXT DEFAULT '',
    travel_status        TEXT DEFAULT '',
    travel_note          TEXT DEFAULT '',
    travel_payload       TEXT DEFAULT '',
    travel_provider      TEXT DEFAULT '',
    travel_ref           TEXT DEFAULT '',
    travel_checked_at    TEXT DEFAULT '',
    updated_at           TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

-- Status history for a patient application (drives the "My applications" timeline).
CREATE TABLE IF NOT EXISTS lead_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL,
    status      TEXT NOT NULL,
    note        TEXT DEFAULT '',
    actor       TEXT DEFAULT 'you',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

-- Outcome reconciliation events: auditable proof of enrollment/retention tied
-- to an external source (CTMS/REDCap/manual confirmation).
CREATE TABLE IF NOT EXISTS lead_reconciliations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id       INTEGER NOT NULL,
    outcome       TEXT NOT NULL,
    source_system TEXT DEFAULT '',
    source_ref    TEXT DEFAULT '',
    note          TEXT DEFAULT '',
    actor         TEXT DEFAULT 'site',
    created_at    TEXT NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

-- Sponsor-funded recruitment activity spend. Lets sites prove ROI by source
-- and justify future budget with actual cost-per-screened/enrolled evidence.
CREATE TABLE IF NOT EXISTS recruitment_spend (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    nct         TEXT DEFAULT '',
    source      TEXT DEFAULT '',
    campaign    TEXT DEFAULT '',
    amount_usd  REAL DEFAULT 0,
    spend_date  TEXT DEFAULT '',
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS search_stats (
    term_key    TEXT PRIMARY KEY,
    term        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    hits        INTEGER DEFAULT 0,
    last_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trend_cache (
    kind        TEXT PRIMARY KEY,
    terms       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Privacy-safe web analytics: one row per tracked on-site action so we can see
-- traffic, where clicks come from, what people search, and the visit->search->
-- view->apply drop-off. NO PHI: visitor is a random anon cookie id; detail holds
-- only non-identifying facts (search term, result count, nct).
CREATE TABLE IF NOT EXISTS web_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    visitor     TEXT DEFAULT '',
    name        TEXT NOT NULL,
    path        TEXT DEFAULT '',
    source      TEXT DEFAULT '',
    medium      TEXT DEFAULT '',
    campaign    TEXT DEFAULT '',
    referrer    TEXT DEFAULT '',
    detail      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_web_events_ts ON web_events(ts);

-- Scoped SEO surface for per-study pages: the set of trials we consider worth
-- indexing (populated from our condition/city SEO pages). Keeps /study/<nct>
-- pages and the sitemap bounded to real, on-topic recruiting trials instead of
-- every study on ClinicalTrials.gov.
CREATE TABLE IF NOT EXISTS seo_study_index (
    nct         TEXT PRIMARY KEY,
    title       TEXT DEFAULT '',
    condition   TEXT DEFAULT '',
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seo_study_updated ON seo_study_index(updated_at);
CREATE INDEX IF NOT EXISTS idx_web_events_name ON web_events(name);

-- Which condition/city landing pages actually have LOCAL recruiting trials.
-- Only these are worth listing in sitemap-cities.xml: a page with no local
-- trials self-noindexes, so listing it just burns crawl budget and trains
-- Google to ignore the whole programmatic surface (the "discovered - currently
-- not indexed" wall). Refreshed as pages are rendered / by the /seo/warm cron.
CREATE TABLE IF NOT EXISTS seo_city_page (
    slug        TEXT NOT NULL,
    city_slug   TEXT NOT NULL,
    is_local    INTEGER NOT NULL DEFAULT 0,
    trials      INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (slug, city_slug)
);
CREATE INDEX IF NOT EXISTS idx_seo_city_local ON seo_city_page(is_local);

-- Tiny key/value store for small bits of persistent app state (e.g. the SEO
-- warm cursor, so a stateless external scheduler can self-advance through the
-- condition x city grid one slice per ping instead of needing to run a loop).
CREATE TABLE IF NOT EXISTS app_kv (
    k           TEXT PRIMARY KEY,
    v           TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Plain-English trial summaries (see web/summarize.py). Cached per study so we
-- only rewrite each description once.
CREATE TABLE IF NOT EXISTS trial_summaries (
    nct         TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Ranked patient-search results, keyed by a short id. Persisted (not in-process
-- memory) so a trial detail page opened on a DIFFERENT gunicorn worker than the
-- one that ran the search can still read it. Pruned by age/count on write.
CREATE TABLE IF NOT EXISTS search_cache (
    sid         TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL
);

-- Patient-mediated health records, connected ONCE per applicant (see records.py).
-- Stored against the applicant token so every future application auto-fills.
CREATE TABLE IF NOT EXISTS records_profiles (
    applicant_token TEXT PRIMARY KEY,
    provider        TEXT DEFAULT '',
    age             TEXT DEFAULT '',
    sex             TEXT DEFAULT '',
    sync_status     TEXT DEFAULT '',
    source_status   TEXT DEFAULT '',
    external_patient_id TEXT DEFAULT '',
    external_query_id   TEXT DEFAULT '',
    completeness_score INTEGER DEFAULT 0,
    last_sync_error TEXT DEFAULT '',
    last_sync_at    TEXT DEFAULT '',
    data            TEXT NOT NULL,
    summary         TEXT DEFAULT '',
    connected_at    TEXT NOT NULL
);

-- Deduplicate provider webhook events (at-least-once delivery safe).
CREATE TABLE IF NOT EXISTS records_webhook_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key       TEXT UNIQUE NOT NULL,
    applicant_token TEXT DEFAULT '',
    provider        TEXT DEFAULT '',
    event_type      TEXT DEFAULT '',
    source_status   TEXT DEFAULT '',
    created_at      TEXT NOT NULL
);

-- Trial alerts (saved searches). A patient registers interests once; a
-- background job (see alerts.py) watches ClinicalTrials.gov and notifies them
-- when NEW matching recruiting trials appear -> push, not pull.
CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    applicant_token TEXT NOT NULL,
    label           TEXT DEFAULT '',
    condition       TEXT DEFAULT '',
    intervention    TEXT DEFAULT '',
    location        TEXT DEFAULT '',
    lat             REAL,
    lon             REAL,
    cc              TEXT DEFAULT '',
    radius          INTEGER DEFAULT 50,
    unit            TEXT DEFAULT 'km',
    email           TEXT DEFAULT '',
    active          INTEGER DEFAULT 1,
    strong_only     INTEGER DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_checked_at TEXT DEFAULT '',
    last_notified_at TEXT DEFAULT '',
    notify_min_days INTEGER DEFAULT 7
);

-- Trials an alert has already seen. Baseline (is_new=0) is seeded at creation so
-- we never spam the patient with trials that already existed; genuinely new
-- matches land with is_new=1 until surfaced.
CREATE TABLE IF NOT EXISTS alert_matches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id    INTEGER NOT NULL,
    nct         TEXT NOT NULL,
    title       TEXT DEFAULT '',
    is_new      INTEGER DEFAULT 1,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (alert_id) REFERENCES alerts(id)
);

CREATE INDEX IF NOT EXISTS idx_referrals_user ON referrals(user_id);
CREATE INDEX IF NOT EXISTS idx_events_ref ON referral_events(referral_id);
CREATE INDEX IF NOT EXISTS idx_patient_email ON patient_users(email);
CREATE INDEX IF NOT EXISTS idx_patient_codes ON patient_auth_codes(patient_id, purpose);
CREATE INDEX IF NOT EXISTS idx_user_codes ON user_auth_codes(user_id, purpose);
CREATE INDEX IF NOT EXISTS idx_ip_rate_window ON ip_rate_limits(window_start);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);
CREATE INDEX IF NOT EXISTS idx_lead_events_lead ON lead_events(lead_id);
CREATE INDEX IF NOT EXISTS idx_support_cov_status ON lead_support_checks(coverage_status);
CREATE INDEX IF NOT EXISTS idx_support_travel_status ON lead_support_checks(travel_status);
CREATE INDEX IF NOT EXISTS idx_recon_lead ON lead_reconciliations(lead_id);
CREATE INDEX IF NOT EXISTS idx_spend_user ON recruitment_spend(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_spend_nct ON recruitment_spend(nct);
CREATE INDEX IF NOT EXISTS idx_search_stats_kind ON search_stats(kind, hits);
CREATE INDEX IF NOT EXISTS idx_search_cache_created ON search_cache(created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_applicant ON alerts(applicant_token);
CREATE INDEX IF NOT EXISTS idx_alert_matches_alert ON alert_matches(alert_id);
CREATE INDEX IF NOT EXISTS idx_messages_lead ON messages(lead_id);
CREATE INDEX IF NOT EXISTS idx_visits_lead ON lead_visits(lead_id);
CREATE INDEX IF NOT EXISTS idx_invites_clinician ON invites(clinician_id);
CREATE INDEX IF NOT EXISTS idx_claims_user ON study_claims(user_id);
CREATE INDEX IF NOT EXISTS idx_claims_nct ON study_claims(nct);
CREATE INDEX IF NOT EXISTS idx_records_webhook_created ON records_webhook_events(created_at);

-- ATS capture: each claimed study gets ONE unique inbound email address so a site
-- can forward applicants from ANY source (their own inbox, ClinicalTrials.gov,
-- ad lead forms, physician referrals) into a single per-trial queue - without
-- changing how patients apply. `address` is the local-part token; the full
-- address is <address>@<INTAKE_EMAIL_DOMAIN>. Scoped to the owning user+nct so
-- inbound mail only ever lands in that team's queue. These emails carry PHI: a
-- BAA with the inbound-email provider is required before production (COMPLIANCE.md).
-- KPI: speeds contacted -> screened (nothing rots in a personal inbox).
CREATE TABLE IF NOT EXISTS intake_addresses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    nct         TEXT NOT NULL,
    address     TEXT UNIQUE NOT NULL,
    label       TEXT DEFAULT '',
    active      INTEGER DEFAULT 1,
    created_at  TEXT NOT NULL,
    UNIQUE (user_id, nct),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Recruitment campaigns (the "smart AI agency" side). A study team drafts a
-- campaign for a channel; AI may draft the creative, but it stays irb_approved=0
-- until a human confirms the copy is IRB/REB-approved and truthful. A campaign
-- may NOT move to 'active' before then - a compliance hard gate (COMPLIANCE.md).
-- `track_token` tags inbound applicants back to the campaign (leads.campaign_id)
-- so we compute cost-per-applicant and cost-per-enrolled from the REAL funnel,
-- not vanity clicks. KPI: found -> contacted.
CREATE TABLE IF NOT EXISTS campaigns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    nct          TEXT NOT NULL,
    name         TEXT DEFAULT '',
    channel      TEXT DEFAULT '',        -- meta | google | reddit | campus | email | other
    status       TEXT NOT NULL DEFAULT 'draft', -- draft | active | paused | ended
    budget_usd   REAL DEFAULT 0,
    headline     TEXT DEFAULT '',
    body         TEXT DEFAULT '',
    landing_copy TEXT DEFAULT '',
    irb_approved INTEGER DEFAULT 0,
    track_token  TEXT UNIQUE NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
-- A single campaign is posted to many PLACES the site already uses (r/ADHD, a
-- campus board, an IG post, their newsletter). Each placement gets its OWN
-- tracking token so an applicant who arrives via that link is attributed to both
-- the campaign and the exact place - answering "which channel actually enrolled
-- patients", not just "which got clicks". Sites post manually where they like;
-- we just hand them a copy-ready blurb + a trackable link.
CREATE TABLE IF NOT EXISTS campaign_placements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  INTEGER NOT NULL,
    label        TEXT DEFAULT '',        -- where it was posted (free text)
    channel      TEXT DEFAULT '',        -- overrides campaign channel if set
    track_token  TEXT UNIQUE NOT NULL,
    posted_url   TEXT DEFAULT '',        -- link to the live post (optional)
    posted_at    TEXT DEFAULT '',
    clicks       INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);
CREATE INDEX IF NOT EXISTS idx_intake_addr_user ON intake_addresses(user_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_user ON campaigns(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_campaigns_nct ON campaigns(nct);
CREATE INDEX IF NOT EXISTS idx_placements_campaign ON campaign_placements(campaign_id);

-- PI / coordinator document workspace ("review & approve", NOT a legally-binding
-- 21 CFR Part 11 e-signature - see COMPLIANCE.md). A study team tracks the trial's
-- essential documents (ICH-GCP essential-documents set + patient consent), routes
-- them for the investigator to REVIEW and APPROVE, and every action writes an
-- append-only row to document_events (the audit trail). Binding e-signatures and
-- patient eConsent are deliberately deferred to a validated vendor (DocuSign Life
-- Sciences / Part 11); this surface only records internal approvals + status.
-- KPI tie-in: shortens the consent/paperwork wait in screened -> enrolled.
CREATE TABLE IF NOT EXISTS trial_documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    nct         TEXT DEFAULT '',
    lead_id     INTEGER,                 -- set for a patient-specific doc (consent etc.)
    category    TEXT DEFAULT 'regulatory', -- consent | regulatory | patient | site
    doc_type    TEXT DEFAULT '',         -- short code: icf, hipaa, form_1572, doa_log ...
    title       TEXT DEFAULT '',
    party       TEXT DEFAULT 'investigator', -- who owns/signs: patient|investigator|coordinator|sponsor
    party_name  TEXT DEFAULT '',
    version     TEXT DEFAULT 'v1.0',
    status      TEXT NOT NULL DEFAULT 'pending', -- pending|in_review|approved|returned|signed
    due_at      TEXT DEFAULT '',
    summary     TEXT DEFAULT '',         -- short plain-language description (demo preview)
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
-- Append-only audit ledger for every document action. We ONLY ever INSERT here;
-- rows are never updated or deleted (the tamper-evident trail an auditor expects).
CREATE TABLE IF NOT EXISTS document_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    action      TEXT DEFAULT '',         -- created|viewed|sent|received|reviewed|approved|returned
    meaning     TEXT DEFAULT '',         -- Review | Approval | Authorship (manifestation of signature)
    actor       TEXT DEFAULT '',
    actor_role  TEXT DEFAULT '',
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES trial_documents(id)
);
CREATE INDEX IF NOT EXISTS idx_trial_docs_user ON trial_documents(user_id, nct);
CREATE INDEX IF NOT EXISTS idx_doc_events_doc ON document_events(document_id);

-- IRB/REB recruitment-material submissions. Recruitment materials (ads, flyers,
-- social posts, phone-screening scripts, patient-facing copy) are ADVERTISING and
-- must be reviewed + approved by the study's ethics board BEFORE use (21 CFR 50/56;
-- Health Canada REB / TCPS 2 - see COMPLIANCE.md §5). This models the real workflow
-- a coordinator runs: assemble a package, submit it to the IRB (central like WCG /
-- Advarra, or a local/academic board), track the review (initial | modification |
-- continuing review), and record the APPROVED version + its expiry (approvals lapse
-- on an annual continuing-review cycle). On approval we flip the linked campaigns'
-- irb_approved gate so - and only so - they can go live. KPI: compresses the
-- submit->approved cycle that blocks found/contacted, without weakening the gate.
CREATE TABLE IF NOT EXISTS irb_submissions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    nct             TEXT DEFAULT '',
    title           TEXT DEFAULT '',
    irb_name        TEXT DEFAULT '',            -- WCG IRB, Advarra, local board name
    irb_kind        TEXT DEFAULT 'central',     -- central | local
    submission_type TEXT DEFAULT 'initial',     -- initial | modification | continuing
    status          TEXT NOT NULL DEFAULT 'draft', -- draft|submitted|in_review|revisions|approved|expired
    pi_name         TEXT DEFAULT '',
    protocol_version TEXT DEFAULT '',
    submission_ref  TEXT DEFAULT '',            -- IRB tracking # (assigned on submit)
    approved_version TEXT DEFAULT '',           -- the exact stamped version cleared for use
    approved_at     TEXT DEFAULT '',
    expires_at      TEXT DEFAULT '',            -- continuing-review expiration
    notes           TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
-- Line items in a submission package: each recruitment material (or a linked
-- campaign creative) with its own version, so the packet lists exactly what the IRB
-- is reviewing.
CREATE TABLE IF NOT EXISTS irb_submission_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL,
    kind          TEXT DEFAULT 'material',  -- flyer|social|script|consent|protocol|campaign|material
    campaign_id   INTEGER,                  -- set when the item IS a campaign's creative
    label         TEXT DEFAULT '',
    version       TEXT DEFAULT 'v1.0',
    detail        TEXT DEFAULT '',          -- short description / copy preview
    created_at    TEXT NOT NULL,
    FOREIGN KEY (submission_id) REFERENCES irb_submissions(id)
);
-- Append-only audit trail for a submission (mirrors document_events): only INSERTs.
CREATE TABLE IF NOT EXISTS irb_submission_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL,
    action        TEXT DEFAULT '',  -- created|item_added|submitted|in_review|revisions|approved|expired|note
    meaning       TEXT DEFAULT '',
    actor         TEXT DEFAULT '',
    actor_role    TEXT DEFAULT '',
    note          TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    FOREIGN KEY (submission_id) REFERENCES irb_submissions(id)
);
CREATE INDEX IF NOT EXISTS idx_irb_sub_user ON irb_submissions(user_id, nct);
CREATE INDEX IF NOT EXISTS idx_irb_items_sub ON irb_submission_items(submission_id);
CREATE INDEX IF NOT EXISTS idx_irb_events_sub ON irb_submission_events(submission_id);

-- Team workspace: a study team is an ORGANIZATION. Every study-team user belongs
-- to exactly one org (their lab/site). All members share FULL VISIBILITY of the
-- org's applicants, documents, and campaigns (like a shared Google-Doc space);
-- roles only gate a few sensitive actions (sign/approve, manage the team).
-- Compliance note: this is HIPAA "minimum-necessary" access control + a per-member
-- audit trail, so it strengthens the posture rather than adding PHI risk.
CREATE TABLE IF NOT EXISTS organizations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
-- Roles: 'coordinator' (admin - manages team + everything), 'pi' (investigator -
-- reviews/approves/signs), 'student' (grad/undergrad - full visibility + all
-- day-to-day work, but cannot sign/approve documents or manage the team).
CREATE TABLE IF NOT EXISTS memberships (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    role        TEXT NOT NULL DEFAULT 'coordinator',
    role_label  TEXT DEFAULT '',  -- optional custom display name for the role
    created_at  TEXT NOT NULL,
    UNIQUE (org_id, user_id),
    FOREIGN KEY (org_id) REFERENCES organizations(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
-- Pending invites (join link by email). Accepted when the invitee signs in and
-- claims the token; role is assigned by the inviter.
CREATE TABLE IF NOT EXISTS org_invites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL,
    email       TEXT DEFAULT '',
    role        TEXT NOT NULL DEFAULT 'student',
    role_label  TEXT DEFAULT '',  -- optional custom display name for the role
    token       TEXT UNIQUE NOT NULL,
    invited_by  INTEGER,
    created_at  TEXT NOT NULL,
    accepted_at TEXT DEFAULT '',
    FOREIGN KEY (org_id) REFERENCES organizations(id)
);
CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships(user_id);
CREATE INDEX IF NOT EXISTS idx_memberships_org ON memberships(org_id);

-- Shared marketing inbox. Provider OAuth/webhooks can populate these tables,
-- while the workspace remains useful in demo mode without external credentials.
CREATE TABLE IF NOT EXISTS marketing_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL,
    channel         TEXT NOT NULL,             -- email | instagram | google_ads
    label           TEXT DEFAULT '',
    identifier      TEXT NOT NULL,
    connection_mode TEXT NOT NULL DEFAULT 'demo', -- demo | live
    status          TEXT NOT NULL DEFAULT 'connected', -- connected | disconnected
    created_by      INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (org_id, channel, identifier),
    FOREIGN KEY (org_id) REFERENCES organizations(id),
    FOREIGN KEY (created_by) REFERENCES users(id)
);

-- Provider credentials and sync cursors are isolated from the source rows that
-- are routinely rendered in the UI. Token values are encrypted before insert.
CREATE TABLE IF NOT EXISTS marketing_connections (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id                   INTEGER NOT NULL,
    source_id                INTEGER NOT NULL UNIQUE,
    provider                 TEXT NOT NULL, -- gmail | instagram | google_ads
    external_account_id      TEXT NOT NULL,
    account_identifier       TEXT NOT NULL,
    access_token_encrypted   TEXT NOT NULL DEFAULT '',
    refresh_token_encrypted  TEXT NOT NULL DEFAULT '',
    granted_scopes           TEXT NOT NULL DEFAULT '[]',
    token_expires_at         TEXT DEFAULT '',
    gmail_history_id         TEXT DEFAULT '',
    gmail_watch_expires_at   TEXT DEFAULT '',
    instagram_account_id     TEXT DEFAULT '',
    last_successful_sync_at  TEXT DEFAULT '',
    status                   TEXT NOT NULL DEFAULT 'connected',
    last_error_code          TEXT DEFAULT '',
    last_error_message       TEXT DEFAULT '',
    last_error_at            TEXT DEFAULT '',
    created_by               INTEGER,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    UNIQUE (org_id, provider, external_account_id),
    FOREIGN KEY (org_id) REFERENCES organizations(id),
    FOREIGN KEY (source_id) REFERENCES marketing_sources(id),
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS marketing_threads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id         INTEGER NOT NULL,
    source_id      INTEGER,
    external_ref   TEXT DEFAULT '',
    contact_name   TEXT DEFAULT '',
    contact_handle TEXT DEFAULT '',
    subject        TEXT DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'open', -- open | resolved
    assigned_to    INTEGER,
    unread         INTEGER NOT NULL DEFAULT 1,
    priority       TEXT NOT NULL DEFAULT 'normal',
    nct            TEXT DEFAULT '',  -- study this conversation is about ('' = general)
    study_label    TEXT DEFAULT '',
    pipeline_stage TEXT NOT NULL DEFAULT 'new',
    linked_lead_id INTEGER,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    FOREIGN KEY (org_id) REFERENCES organizations(id),
    FOREIGN KEY (source_id) REFERENCES marketing_sources(id),
    FOREIGN KEY (assigned_to) REFERENCES users(id),
    FOREIGN KEY (linked_lead_id) REFERENCES leads(id)
);

CREATE TABLE IF NOT EXISTS marketing_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL,
    thread_id       INTEGER NOT NULL,
    external_ref    TEXT DEFAULT '', -- provider message ID for idempotent sync
    kind            TEXT NOT NULL, -- inbound | outbound | note
    body            TEXT NOT NULL,
    author_user_id  INTEGER,
    author_name     TEXT DEFAULT '',
    delivery_status TEXT DEFAULT '', -- received | saved | sent | internal
    created_at      TEXT NOT NULL,
    FOREIGN KEY (org_id) REFERENCES organizations(id),
    FOREIGN KEY (thread_id) REFERENCES marketing_threads(id),
    FOREIGN KEY (author_user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS marketing_webhook_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    provider            TEXT NOT NULL,
    event_key           TEXT NOT NULL,
    account_external_id TEXT DEFAULT '',
    payload_json        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    error_message       TEXT DEFAULT '',
    created_at          TEXT NOT NULL,
    processed_at        TEXT DEFAULT '',
    UNIQUE (provider, event_key)
);

-- Meta requires a public status URL for each data-deletion callback. Receipts
-- deliberately retain no provider user ID or request payload after erasure.
CREATE TABLE IF NOT EXISTS marketing_data_deletions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    provider          TEXT NOT NULL,
    request_key       TEXT NOT NULL,
    confirmation_code TEXT NOT NULL UNIQUE,
    status            TEXT NOT NULL DEFAULT 'completed',
    requested_at      TEXT NOT NULL,
    completed_at      TEXT DEFAULT '',
    UNIQUE (provider, request_key)
);

CREATE TABLE IF NOT EXISTS marketing_handoffs (
    org_id          INTEGER PRIMARY KEY,
    primary_user_id INTEGER,
    cover_user_id   INTEGER,
    vacation_mode   INTEGER NOT NULL DEFAULT 0,
    away_until      TEXT DEFAULT '',
    note            TEXT DEFAULT '',
    updated_by      INTEGER,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (org_id) REFERENCES organizations(id),
    FOREIGN KEY (primary_user_id) REFERENCES users(id),
    FOREIGN KEY (cover_user_id) REFERENCES users(id),
    FOREIGN KEY (updated_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_marketing_sources_org
    ON marketing_sources(org_id, status);
CREATE INDEX IF NOT EXISTS idx_marketing_connections_org
    ON marketing_connections(org_id, provider, status);
CREATE INDEX IF NOT EXISTS idx_marketing_threads_org
    ON marketing_threads(org_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_marketing_messages_thread
    ON marketing_messages(thread_id, id);
CREATE INDEX IF NOT EXISTS idx_marketing_webhook_pending
    ON marketing_webhook_events(provider, status, created_at);

-- Auto-routing rules (the "Cursor for your inbox" glue). When a new inquiry
-- lands, we match it against an org's rules top-down and auto-assign the thread
-- to a teammate, so it shows up in THEIR inbox with zero manual triage. A rule
-- matches on channel (email/meta/google/ctgov/referral/... or 'any') AND
-- optionally a study (nct, '' = any study). First match wins (lowest position).
-- KPI: Tier-2 efficiency -> contacted -> screened (no unassigned pile, no lag
-- deciding who owns an inquiry). Internal staff routing only - no PHI leaves the
-- org, no referral/payment logic.
CREATE TABLE IF NOT EXISTS routing_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL,
    channel     TEXT NOT NULL DEFAULT 'any',   -- any | email_intake | meta | google | ctgov | reddit | referral
    nct         TEXT NOT NULL DEFAULT '',      -- '' = any study
    assignee_id INTEGER NOT NULL,              -- teammate the thread routes to
    position    INTEGER NOT NULL DEFAULT 0,    -- eval order, lowest first
    active      INTEGER NOT NULL DEFAULT 1,
    created_by  INTEGER,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (org_id) REFERENCES organizations(id),
    FOREIGN KEY (assignee_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_routing_rules_org ON routing_rules(org_id, active, position);

-- Copilot action proposals. The assistant never acts on its own: it writes a
-- PROPOSED action here, the human confirms it in the rail, and only then is it
-- executed (and marked confirmed). This gives human-in-the-loop control, an
-- audit record of what was proposed vs. done, idempotency (a token can execute
-- once), and expiry. The action's target (lead/recipients) is stored server-side
-- so a client can never redirect a confirmed action to a different patient.
CREATE TABLE IF NOT EXISTS copilot_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token        TEXT UNIQUE NOT NULL,
    user_id      INTEGER NOT NULL,          -- the team member who proposed it
    org_id       INTEGER,                   -- team scope snapshot
    kind         TEXT NOT NULL,             -- send_message | send_booking_link | bulk_booking_reminder
    lead_id      INTEGER,                   -- single-lead actions
    payload      TEXT DEFAULT '',           -- JSON (text, url, lead_ids, label, ...)
    status       TEXT NOT NULL DEFAULT 'proposed', -- proposed | confirmed | canceled
    created_at   TEXT NOT NULL,
    expires_at   TEXT DEFAULT '',
    confirmed_at TEXT DEFAULT '',
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_copilot_actions_token ON copilot_actions(token);

-- Audit trail for Bridget's inbox drafts: what was asked, how the conversation
-- was triaged, and whether a draft was written, held, blocked or not
-- understood. One row per request, whichever entry point it came through.
CREATE TABLE IF NOT EXISTS copilot_drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    org_id       INTEGER,
    thread_id    INTEGER,
    instruction  TEXT DEFAULT '',
    category     TEXT DEFAULT 'routine',   -- routine | pricing | medical_legal | distress | opt_out
    outcome      TEXT NOT NULL,            -- draft | hold | blocked | unclear | error
    reasons      TEXT DEFAULT '',          -- JSON list of rule keys / flags
    model_used   INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_copilot_drafts_thread ON copilot_drafts(thread_id);

-- Internal patient->trial matching. A connected clinic's own patients, surfaced
-- as candidates for a trial by the matching engine. This is the "found +
-- pre-screened" supply that feeds the ATS. Rows are DE-IDENTIFIED (initials +
-- age/sex + a short problem-list snapshot) - matching runs under the site's own
-- ethics approval, and a real name is only re-identified after physician review
-- and (for external) patient consent. `kind` splits studies the clinic runs
-- itself (internal -> becomes an applicant) from partner-site trials
-- (external -> a secure referral after consent).
CREATE TABLE IF NOT EXISTS patient_matches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,        -- owning clinic account (team-scoped)
    patient_ref   TEXT DEFAULT '',         -- de-identified label, e.g. "R.M."
    full_name     TEXT DEFAULT '',         -- re-identified on approve (demo: seeded)
    age           TEXT DEFAULT '',
    sex           TEXT DEFAULT '',
    summary       TEXT DEFAULT '',         -- de-identified problem-list snapshot
    source_label  TEXT DEFAULT '',         -- e.g. "Upcoming visit · Tue" / "Problem list"
    nct           TEXT DEFAULT '',
    trial_title   TEXT DEFAULT '',
    condition     TEXT DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'internal',  -- internal | external
    site_name     TEXT DEFAULT '',         -- external: the partner site
    site_location TEXT DEFAULT '',
    verdict       TEXT DEFAULT '',         -- likely_eligible | possible
    score         INTEGER DEFAULT 0,
    met           TEXT DEFAULT '',         -- JSON list
    unknown       TEXT DEFAULT '',         -- JSON list
    not_met       TEXT DEFAULT '',         -- JSON list
    rationale     TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'new',  -- new | approved | referred | dismissed
    lead_id       INTEGER,                 -- set when an internal match converts to an applicant
    created_at    TEXT NOT NULL,
    updated_at    TEXT DEFAULT '',
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_patient_matches_user ON patient_matches(user_id, status);

-- Protocol SCHEDULE OF EVENTS (per study). The visit template a coordinator runs
-- every participant against: ordered visits, each with a target day relative to
-- Day 1 (baseline), an allowable window (+/- days), duration, and the procedures
-- due at that visit. When a participant is enrolled we MATERIALIZE these into real
-- lead_visits with computed windows, so the calendar can flag out-of-window
-- (protocol-deviation) risk - no double entry. This is PROTOCOL METADATA, not PHI;
-- scoped to the site team by user_id + nct. AI can draft it from the protocol PDF.
-- KPI: Tier-2 efficiency (book a whole schedule once) feeding retention (fewer
-- missed/out-of-window visits = fewer deviations and dropouts).
CREATE TABLE IF NOT EXISTS soe_visits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    nct           TEXT DEFAULT '',
    seq           INTEGER DEFAULT 0,          -- display / visit order
    name          TEXT DEFAULT '',            -- "Screening", "Baseline / Day 1", "Week 4"
    day_offset    INTEGER DEFAULT 0,          -- target day vs Day 1 (screening negative)
    window_before INTEGER DEFAULT 0,          -- allowed days early
    window_after  INTEGER DEFAULT 0,          -- allowed days late
    duration_min  INTEGER DEFAULT 30,
    procedures    TEXT DEFAULT '',            -- one procedure per line
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_soe_user ON soe_visits(user_id, nct, seq);

-- @mentions: pulling a teammate into a thread without CC-ing them on an email
-- and without transferring ownership of the thread. Polymorphic on purpose - a
-- mention means the same thing on an applicant note and on an inbox internal
-- note, and one table keeps the "Mentioned me" view a single query. Mentions
-- live ONLY on internal, team-only notes; nothing recorded here is ever
-- patient-visible, which is why the note_type is constrained to the two
-- staff-only surfaces rather than to `messages` (that table IS patient-visible).
CREATE TABLE IF NOT EXISTS mentions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id        INTEGER NOT NULL,
    object_type   TEXT NOT NULL,          -- lead | marketing_thread
    object_id     INTEGER NOT NULL,
    note_type     TEXT NOT NULL,          -- lead_note | marketing_message
    note_id       INTEGER NOT NULL,
    mentioned_id  INTEGER NOT NULL,       -- teammate pulled in
    author_id     INTEGER NOT NULL,
    excerpt       TEXT DEFAULT '',        -- the note text, for the mentions list
    read_at       TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    FOREIGN KEY (org_id) REFERENCES organizations(id),
    FOREIGN KEY (mentioned_id) REFERENCES users(id),
    FOREIGN KEY (author_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_mentions_inbox
    ON mentions(mentioned_id, read_at, created_at);
CREATE INDEX IF NOT EXISTS idx_mentions_object
    ON mentions(object_type, object_id);

-- Per-user away coverage. When a recruiter goes out, her OPEN work moves to a
-- cover teammate and NEW work routes there too; on `until` it all comes back
-- automatically. One row per (user, period) so several people can be away at
-- once - the older org-keyed marketing_handoffs table has org_id as its PRIMARY
-- KEY and could therefore only ever hold one handoff for a whole team, which is
-- the bug this table exists to fix. marketing_handoffs stays as a read-only
-- legacy fallback; every new handoff is written here.
CREATE TABLE IF NOT EXISTS away_periods (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id         INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,           -- who is away
    cover_user_id  INTEGER NOT NULL,           -- who is covering for them
    starts_at      TEXT NOT NULL,
    until          TEXT NOT NULL DEFAULT '',   -- 'YYYY-MM-DD'; '' = open-ended
    note           TEXT DEFAULT '',            -- what the cover should know
    status         TEXT NOT NULL DEFAULT 'active',  -- active | ended
    moved_leads    INTEGER NOT NULL DEFAULT 0,
    moved_threads  INTEGER NOT NULL DEFAULT 0,
    created_by     INTEGER,
    created_at     TEXT NOT NULL,
    ended_at       TEXT DEFAULT '',
    seen_at        TEXT DEFAULT '',            -- returning user dismissed the recap
    FOREIGN KEY (org_id) REFERENCES organizations(id),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (cover_user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_away_active
    ON away_periods(org_id, status, user_id);

-- What a handoff actually moved, so it can be handed back EXACTLY and so the
-- returning recruiter sees a real list rather than a number. Hand-back reads
-- this ledger and only reverses items the cover person still owns - if they
-- deliberately passed something to a third teammate, that stands.
CREATE TABLE IF NOT EXISTS handoff_moves (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    away_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,                  -- lead | marketing_thread
    object_id  INTEGER NOT NULL,
    from_user  INTEGER,
    to_user    INTEGER,
    direction  TEXT NOT NULL DEFAULT 'out',    -- out (handoff) | back (return)
    created_at TEXT NOT NULL,
    FOREIGN KEY (away_id) REFERENCES away_periods(id)
);
CREATE INDEX IF NOT EXISTS idx_handoff_moves_away
    ON handoff_moves(away_id, kind, direction);

-- A sent blast: one message fanned out to a filtered audience inside ONE study.
-- Persisted so a send is auditable (who sent what, to how many, on which filter)
-- and so a coordinator can see "I already nudged these 12 on Tuesday" instead of
-- double-messaging. The recipient list is stored as the resolved lead ids, not
-- as a live query, so the record can never silently change meaning later.
-- Single-study by design: protocol materials are IRB-approved per study, so a
-- cross-trial send is refused rather than supported (see db.blast_audience).
CREATE TABLE IF NOT EXISTS blasts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id       INTEGER NOT NULL,
    sent_by      INTEGER NOT NULL,
    nct          TEXT NOT NULL,
    audience     TEXT NOT NULL DEFAULT '{}',   -- JSON of the filter that built it
    label        TEXT DEFAULT '',              -- human summary, e.g. "Screening"
    body         TEXT DEFAULT '',
    task_title   TEXT DEFAULT '',
    lead_ids     TEXT NOT NULL DEFAULT '',     -- CSV of leads actually messaged
    recipients   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (org_id) REFERENCES organizations(id),
    FOREIGN KEY (sent_by) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_blasts_org ON blasts(org_id, created_at);

-- Recruiter-issued patient portal. A coordinator hands one applicant a link and
-- a one-time password so they can follow their OWN application - no signup, no
-- email round-trip, which is what makes it usable for someone the team is
-- already messaging. Separate from patient_users (the self-serve account behind
-- /applications) on purpose: access is granted per lead and can be revoked.
-- Only the password HASH is stored; the plaintext is shown to the recruiter once
-- at creation and is unrecoverable afterwards. must_change forces rotation on
-- first sign-in, so the credential typed into a chat stops working as soon as
-- the patient actually uses it.
CREATE TABLE IF NOT EXISTS portal_access (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER NOT NULL UNIQUE,     -- one portal per application
    token           TEXT UNIQUE NOT NULL,        -- the /portal/<token> link
    password_hash   TEXT NOT NULL,
    must_change     INTEGER NOT NULL DEFAULT 1,  -- force rotation on first use
    status          TEXT NOT NULL DEFAULT 'active',   -- active | revoked
    created_by      INTEGER,
    created_at      TEXT NOT NULL,
    first_seen_at   TEXT DEFAULT '',
    last_seen_at    TEXT DEFAULT '',
    password_set_at TEXT DEFAULT '',
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT DEFAULT '',
    FOREIGN KEY (lead_id) REFERENCES leads(id),
    FOREIGN KEY (created_by) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_portal_token ON portal_access(token, status);
"""

# Recognized team roles + display labels. 'coordinator' is the admin role.
ORG_ROLES = ("coordinator", "pi", "student")
ORG_ROLE_LABELS = {
    "coordinator": "Coordinator",
    "pi": "Principal Investigator",
    "student": "Research student",
}
# Roles allowed to perform privileged actions. Everything else is open to all
# members (full visibility + day-to-day collaboration).
_ROLE_CAN_MANAGE_TEAM = {"coordinator", "pi"}
_ROLE_CAN_APPROVE_DOCS = {"coordinator", "pi"}

MARKETING_CHANNELS = ("email", "instagram", "google_ads")
MARKETING_CHANNEL_LABELS = {
    "email": "Email",
    "instagram": "Instagram",
    "google_ads": "Google Ads",
}
MARKETING_CONNECTION_PROVIDERS = ("gmail", "instagram", "google_ads")
MARKETING_CONNECTION_STATUSES = (
    "connected", "disconnected", "needs_reauth", "error")


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def get_db():
    if "db" not in g:
        # timeout + busy_timeout: with multiple gunicorn workers (and cron sweeps
        # writing across every alert), a plain connect throws "database is locked"
        # the instant another writer holds the lock. Wait/retry for up to 15s
        # instead. WAL lets readers and a single writer run concurrently, which
        # is what removes the intermittent 500s on /alerts/run and /reminders/run.
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 15000")
        g.db.execute("PRAGMA journal_mode = WAL")
        g.db.execute("PRAGMA synchronous = NORMAL")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def gen_token():
    return secrets.token_urlsafe(16)


def _site_token_expiry(days=None):
    try:
        days = int(days or os.environ.get("SITE_TOKEN_TTL_DAYS", "30"))
    except Exception:
        days = 30
    return (dt.datetime.now() + dt.timedelta(days=max(1, days))).strftime(
        "%Y-%m-%d %H:%M")


def _parse_ts(raw):
    try:
        return dt.datetime.strptime((raw or "").strip(), "%Y-%m-%d %H:%M")
    except Exception:
        return None


def site_token_active(lead):
    if not lead:
        return False
    if int(lead["site_token_revoked"] or 0):
        return False
    exp = _parse_ts(lead["site_token_expires_at"])
    return bool(exp and exp >= dt.datetime.now())


# New columns added after the first release - applied idempotently so existing
# databases upgrade without a manual migration step.
_MIGRATIONS = {
    # `author` is a display name; a mention needs a real user id to
    # attribute the note and to scope who may see it.
    "lead_notes": {"author_user_id": "INTEGER"},
    "marketing_threads": {
        "nct": "TEXT DEFAULT ''",
        "study_label": "TEXT DEFAULT ''",
        "pipeline_stage": "TEXT NOT NULL DEFAULT 'new'",
        "linked_lead_id": "INTEGER",
    },
    "marketing_messages": {
        "external_ref": "TEXT DEFAULT ''",
    },
    "referrals": {
        "token": "TEXT",
        "coordinator_email": "TEXT DEFAULT ''",
        "patient_name": "TEXT DEFAULT ''",
        "patient_contact": "TEXT DEFAULT ''",
        "consent": "INTEGER DEFAULT 0",
        "consent_at": "TEXT DEFAULT ''",
        "notified_at": "TEXT DEFAULT ''",
    },
    "referral_events": {"actor": "TEXT DEFAULT 'you'"},
    "users": {
        "ehr_connected": "INTEGER DEFAULT 0",
        "ehr_provider": "TEXT DEFAULT ''",
        "ehr_connected_at": "TEXT DEFAULT ''",
        "verified": "INTEGER DEFAULT 0",
        "verified_at": "TEXT DEFAULT ''",
        "oauth_provider": "TEXT DEFAULT ''",
        "oauth_sub": "TEXT",
        "oauth_picture": "TEXT DEFAULT ''",
        # Home organization (team workspace) this study-team user belongs to.
        "org_id": "INTEGER",
    },
    "patient_users": {
        "oauth_provider": "TEXT DEFAULT ''",
        "oauth_sub": "TEXT",
        "oauth_picture": "TEXT DEFAULT ''",
    },
    "site_profiles": {
        "intake_sla_hours": "TEXT DEFAULT ''",
        "escalation_email": "TEXT DEFAULT ''",
        # The coordinator's own booking calendar (Calendly/Cal.com/Google
        # appointment schedule). Patients book straight onto their calendar; it's
        # the account-wide default the applicant screen falls back to.
        "calendar_url": "TEXT DEFAULT ''",
        "ctms_endpoint": "TEXT DEFAULT ''",
        "redcap_endpoint": "TEXT DEFAULT ''",
        "redcap_project_label": "TEXT DEFAULT ''",
        "redcap_api_token": "TEXT DEFAULT ''",
        "redcap_field_map": "TEXT DEFAULT ''",
        "redcap_intake_instrument": "TEXT DEFAULT ''",
        "redcap_intake_enabled": "INTEGER DEFAULT 0",
        # Connector-first outbound: where a reply to a social channel (IG DM,
        # Messenger, Facebook, WhatsApp) is POSTed so a Zapier/Make/native
        # connector delivers it back to that channel. Lets a coordinator reply to
        # ad responses WITHOUT leaving the app. The secret signs the payload so
        # the receiving connector can verify it's really us (never rendered back).
        "connector_webhook_url": "TEXT DEFAULT ''",
        "connector_secret": "TEXT DEFAULT ''",
    },
    "study_claims": {
        "notify_email": "TEXT DEFAULT ''",
        # Which recruitment channels THIS study is connected to (CSV of channel
        # keys from ROUTING_CHANNELS, e.g. "instagram,facebook,email_intake").
        # Empty = every source. Drives the inbox's per-trial source filters so a
        # trial only shows the logos/channels it actually recruits through.
        "connected_sources": "TEXT DEFAULT ''",
        # Existing rows backfill to verified (1) so current demos keep working;
        # new self-serve claims are inserted with verified=0 (pending approval).
        "verified": "INTEGER NOT NULL DEFAULT 1",
        # Reusable per-study booking link (Calendly/Acuity/Cal.com). Set once on
        # the study; the applicant screen prefills it so a coordinator sends the
        # same self-schedule link with one click. KPI: speeds screened->enrolled.
        "schedule_url": "TEXT DEFAULT ''",
    },
    "records_profiles": {
        "sync_status": "TEXT DEFAULT ''",
        "source_status": "TEXT DEFAULT ''",
        "external_patient_id": "TEXT DEFAULT ''",
        "external_query_id": "TEXT DEFAULT ''",
        "completeness_score": "INTEGER DEFAULT 0",
        "last_sync_error": "TEXT DEFAULT ''",
        "last_sync_at": "TEXT DEFAULT ''",
    },
    "alerts": {
        "last_notified_at": "TEXT DEFAULT ''",
        "notify_min_days": "INTEGER DEFAULT 7",
        "strong_only": "INTEGER DEFAULT 1",
    },
    "leads": {
        "applicant_token": "TEXT DEFAULT ''",
        "site_token": "TEXT DEFAULT ''",
        "site_token_expires_at": "TEXT DEFAULT ''",
        "site_token_revoked": "INTEGER DEFAULT 0",
        "updated_at": "TEXT DEFAULT ''",
        "records_connected": "INTEGER DEFAULT 0",
        "record_summary": "TEXT DEFAULT ''",
        "records_authorized_at": "TEXT DEFAULT ''",
        "screener": "TEXT DEFAULT ''",
        "eligibility": "TEXT DEFAULT ''",
        "prescreen_readiness": "TEXT DEFAULT ''",
        "decision": "TEXT DEFAULT ''",
        "decision_reason": "TEXT DEFAULT ''",
        "decided_at": "TEXT DEFAULT ''",
        "revealed": "INTEGER DEFAULT 0",
        "schedule_url": "TEXT DEFAULT ''",
        # Video call link (Zoom/Google Meet) for this candidate's screening visit.
        "video_url": "TEXT DEFAULT ''",
        "nudged_at": "TEXT DEFAULT ''",
        "nudge_count": "INTEGER DEFAULT 0",
        "referred_by": "TEXT DEFAULT ''",
        "invite_token": "TEXT DEFAULT ''",
        # Consented cross-study matching pool ("registry"). Opt-in is SEPARATE
        # from the per-trial contact consent above: the applicant explicitly
        # agrees to be matched to and contacted about FUTURE studies. Timestamp +
        # version are stored so consent is auditable and revocable.
        "registry_opt_in": "INTEGER DEFAULT 0",
        "registry_consent_at": "TEXT DEFAULT ''",
        "registry_consent_version": "TEXT DEFAULT ''",
        "redcap_record_id": "TEXT DEFAULT ''",
        "redcap_survey_status": "TEXT DEFAULT ''",
        "conv_tags": "TEXT DEFAULT ''",
        # Messaging opt-out: the applicant asked not to be contacted. Honored by
        # every send path (manual + copilot) before a message goes out. This is a
        # hard compliance gate (TCPA/CAN-SPAM style); it is set, never cleared,
        # by an unsubscribe/STOP, and is independent of per-trial consent.
        "contact_opt_out": "INTEGER DEFAULT 0",
        "contact_opt_out_at": "TEXT DEFAULT ''",
        # Attribution: which recruitment campaign produced this applicant (NULL
        # for organic/direct). Lets the campaign engine measure cost-per-enrolled.
        "campaign_id": "INTEGER",
        # The specific placement (posting location) that produced this applicant,
        # so a site sees which channel/place actually converts to enrolled.
        "placement_id": "INTEGER",
        # Inbox triage (decision-support only, a human still acts). intent is what
        # the latest inbound message is ABOUT (new_inquiry|scheduling|question|
        # document|opt_out|spam|other); priority is how urgently it needs a human
        # (high|normal|low). Set by the AI triage pass on inbound + backfill.
        "triage_intent": "TEXT DEFAULT ''",
        "triage_priority": "TEXT DEFAULT ''",
        "triage_at": "TEXT DEFAULT ''",
        # Shared workspace: which teammate owns this thread right now (NULL =
        # unassigned). Any org member can pick it up; keeps two coordinators from
        # double-replying to the same applicant.
        "assigned_user_id": "INTEGER",
        # Workspace ownership. Historically a lead was reachable only via a
        # VERIFIED claim on its NCT; that gate stops an account harvesting a
        # public trial's applicants. But the inbox product also captures a site's
        # OWN forwarded/imported mail, which may have no study yet. owner_user_id
        # ties such a lead directly to the workspace that received it, so it shows
        # in that team's inbox with zero study setup - and NEVER leaks to anyone
        # else (scoping is org-membership based).
        "owner_user_id": "INTEGER",
        # Clinics emailed on apply (JSON list of {email, facility, source}).
        # Lets the operator see that the local site was notified without a
        # manual forward, and keeps a record if SMTP was off at apply time.
        "clinic_notify_json": "TEXT DEFAULT ''",
        # When BridgeMD emailed the applicant their no-sign-in thread. Used so
        # a boot backfill can send once to existing applies and then stop.
        "connect_emailed_at": "TEXT DEFAULT ''",
        # Channel-side identifier to address an outbound reply back to (Instagram
        # username, Messenger PSID, WhatsApp number, ...). Populated by the intake
        # connector for social leads; the connector uses it to route our reply to
        # the right person on the right platform. Email/phone stay in their own
        # columns; this is for channels email/SMS can't reach.
        "external_ref": "TEXT DEFAULT ''",
    },
    "lead_visits": {
        # Lifecycle so the calendar can show/flag state, not just a date.
        "status": "TEXT DEFAULT 'scheduled'",   # scheduled|completed|missed|cancelled
        # Protocol visit window (e.g. "Day 28 +/-3"): the allowed booking range.
        # A visit whose window is closing is a protocol-deviation (dropout) risk.
        "window_start": "TEXT DEFAULT ''",
        "window_end": "TEXT DEFAULT ''",
        "duration_min": "INTEGER DEFAULT 30",
        # Optional explicit title (else derived from kind).
        "title": "TEXT DEFAULT ''",
        # Prep checklist shown to the patient in reminders (one item per line):
        # "Fast 8h before", "Bring current meds", "Arrive 15 min early".
        "prep": "TEXT DEFAULT ''",
        # Set when this visit was booked to re-consent a participant for a
        # specific amendment (sponsor_updates.id), so the update can track
        # re-consent progress from real calendar visits.
        "update_id": "INTEGER",
        # Coordinator-facing agenda ("what to go over" this visit), one item per
        # line. Distinct from `prep` (which is shown to the patient).
        "agenda": "TEXT DEFAULT ''",
        # Recurrence: visits sharing a series_id are one repeating set; recurrence
        # holds the cadence label (weekly|biweekly|monthly) for display + shifting.
        "series_id": "TEXT DEFAULT ''",
        "recurrence": "TEXT DEFAULT ''",
    },
    "memberships": {
        # Optional custom role name (e.g. "Data manager"); permissions still come
        # from the base `role`, so a custom label can't grant new access.
        "role_label": "TEXT DEFAULT ''",
    },
    "org_invites": {
        "role_label": "TEXT DEFAULT ''",
    },
    "web_events": {
        # User-agent kept for bot auditing only (not PHI). Bot traffic is
        # filtered before insert, so this should only ever hold real browsers.
        "ua": "TEXT DEFAULT ''",
        # Coarse geo derived from the visitor IP at log time (analytics only).
        # The raw IP is NEVER stored - only city/region/country, so we can see
        # roughly WHERE demand is without holding personal data.
        "city": "TEXT DEFAULT ''",
        "region": "TEXT DEFAULT ''",
        "country": "TEXT DEFAULT ''",
    },
    "payment_rules": {
        # Payout structure this rule drives:
        #   visit      = per-visit time & travel stipend (auto-queues on a
        #                completed visit of `kind`).
        #   completion = lump sum queued when the participant completes the study
        #                (optionally prorated for early withdrawal - see prorate).
        #   travel     = expense reimbursement template (receipt-based); logged
        #                ad hoc, never a pay-to-enroll incentive.
        "rule_type": "TEXT DEFAULT 'visit'",
        # For completion rules only: pay a proportional amount if the participant
        # withdraws early (completed / total protocol visits). 0 = pay in full.
        "prorate": "INTEGER DEFAULT 0",
    },
    "payment_recipients": {
        # How this participant chose to be paid (informational + prefill for a
        # live rail): gift_card | prepaid_card | ach | check | cash.
        "payout_method": "TEXT DEFAULT ''",
    },
}


def _migrate(con):
    for table, cols in _MIGRATIONS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    # Backfill tokens for any pre-existing referrals.
    for row in con.execute("SELECT id FROM referrals WHERE token IS NULL").fetchall():
        con.execute("UPDATE referrals SET token = ? WHERE id = ?",
                    (gen_token(), row[0]))
    _scrub_em_dashes(con)


def _scrub_em_dashes(con):
    """Rewrite em dashes out of every text cell in the database.

    The no-em-dash rule is enforced on LLM output and on the seed strings, but
    rows seeded BEFORE a string was fixed keep the old text forever, and demo
    threads seeded that way kept surfacing an em-dashed "Period-migraine study,
    still enrolling?" subject long after the source said otherwise. Runs on every
    start; it is one LIKE per text column and a no-op once the data is clean.
    Returns the number of cells rewritten.
    """
    try:
        from copy_sanitize import sanitize_copy
    except ImportError:  # run from a tool without the repo root on sys.path
        import sys
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
        from copy_sanitize import sanitize_copy
    fixed = 0
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')
                if not any(k in (r[2] or "").upper()
                           for k in ("INT", "REAL", "BLOB", "NUM", "BOOL", "DATE"))]
        for col in cols:
            rows = con.execute(
                f'SELECT rowid, "{col}" FROM "{table}" '
                f'WHERE "{col}" LIKE ? OR "{col}" LIKE ? OR "{col}" LIKE ?',
                ("%\u2014%", "%&mdash;%", "%&#8212;%")).fetchall()  # noqa: dash
            for rowid, val in rows:
                clean = sanitize_copy(val)
                if clean != val:
                    con.execute(f'UPDATE "{table}" SET "{col}" = ? WHERE rowid = ?',
                                (clean, rowid))
                    fixed += 1
    return fixed
    # Normalize legacy lead status and backfill updated_at + an initial event.
    con.execute("UPDATE leads SET status = 'submitted' WHERE status = 'new'")
    for old, new in _LEAD_STATUS_REMAP.items():
        con.execute("UPDATE leads SET status = ? WHERE status = ?", (new, old))
        con.execute("UPDATE lead_events SET status = ? WHERE status = ?", (new, old))
    con.execute("UPDATE leads SET updated_at = created_at "
                "WHERE updated_at IS NULL OR updated_at = ''")
    for row in con.execute(
            "SELECT id, status, created_at FROM leads WHERE id NOT IN "
            "(SELECT DISTINCT lead_id FROM lead_events)").fetchall():
        con.execute(
            "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
            "VALUES (?,?,?,?,?)",
            (row[0], row[1] or "submitted", "application submitted", "you",
             row[2]))
    # Backfill separate public study-team tokens for candidate workspace links.
    for row in con.execute(
            "SELECT id FROM leads WHERE site_token IS NULL OR site_token = ''"
    ).fetchall():
        con.execute(
            "UPDATE leads SET site_token = ?, site_token_expires_at = ? "
            "WHERE id = ?",
            (gen_token(), _site_token_expiry(), row[0]))
    con.execute(
        "UPDATE leads SET site_token_expires_at = ? "
        "WHERE site_token_expires_at IS NULL OR site_token_expires_at = ''",
        (_site_token_expiry(),))
    # Applicants created from a shared-inbox conversation were being stored
    # blinded (revealed = 0), which left them in the queue as "Candidate #0117"
    # with no contact details and made them invisible to blast_audience - so the
    # site could not message the person it was mid-conversation with. Contact
    # permission was already attested at creation (consent = 1), so drop the
    # blinding. Scoped to inbox-created rows only and safe to re-run.
    con.execute("UPDATE leads SET revealed = 1 WHERE revealed = 0 "
                "AND consent = 1 AND (applicant_token LIKE 'inbox-%' "
                "OR applicant_token LIKE 'demo-inbox-%')")
    # Index depends on a migrated column, so create it after the ALTERs above.
    con.execute("CREATE INDEX IF NOT EXISTS idx_leads_applicant "
                "ON leads(applicant_token)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_leads_invite "
                "ON leads(invite_token)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_leads_campaign "
                "ON leads(campaign_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_leads_placement "
                "ON leads(placement_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_leads_nct_email "
                "ON leads(nct, email)")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_site_token "
                "ON leads(site_token)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_patient_oauth "
                "ON patient_users(oauth_provider, oauth_sub)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_user_oauth "
                "ON users(oauth_provider, oauth_sub)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_records_external_patient "
                "ON records_profiles(external_patient_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_records_external_query "
                "ON records_profiles(external_query_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_site_posted_user "
                "ON site_posted_studies(user_id, created_at)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_site_posted_status "
                "ON site_posted_studies(status)")
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_marketing_thread_external "
        "ON marketing_threads(source_id, external_ref) "
        "WHERE external_ref IS NOT NULL AND external_ref != ''")
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_marketing_message_external "
        "ON marketing_messages(thread_id, external_ref) "
        "WHERE external_ref IS NOT NULL AND external_ref != ''")
    _backfill_orgs(con)


def _backfill_orgs(con):
    """Give every existing study-team user a home organization + a coordinator
    membership, so the workspace model applies to accounts created before it
    existed. Idempotent: only touches users without an org yet."""
    ts = now()
    rows = con.execute(
        "SELECT id, name FROM users WHERE org_id IS NULL").fetchall()
    for uid, name in rows:
        org_name = (name or "").strip()
        org_name = f"{org_name}'s team" if org_name else "My team"
        cur = con.execute(
            "INSERT INTO organizations (name, created_at) VALUES (?,?)",
            (org_name, ts))
        oid = cur.lastrowid
        con.execute("UPDATE users SET org_id = ? WHERE id = ?", (oid, uid))
        con.execute(
            "INSERT OR IGNORE INTO memberships (org_id, user_id, role, created_at) "
            "VALUES (?,?,?,?)", (oid, uid, "coordinator", ts))


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    _migrate(con)
    con.commit()
    con.close()


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def create_user(email, password_hash, name, specialty="", institution="",
                verified=False, oauth_provider="", oauth_sub=None,
                oauth_picture=""):
    db = get_db()
    cur = db.execute(
        "INSERT INTO users (email, password_hash, name, specialty, institution, "
        "verified, verified_at, oauth_provider, oauth_sub, oauth_picture, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (email.lower().strip(), password_hash, name.strip(), specialty.strip(),
         institution.strip(), 1 if verified else 0, now() if verified else "",
         (oauth_provider or "").strip(), oauth_sub, (oauth_picture or "").strip(),
         now()))
    uid = cur.lastrowid
    # A brand-new account starts as its own single-person team (they can invite
    # teammates later). Coordinator = the admin role.
    nm = name.strip()
    org_name = f"{nm}'s team" if nm else "My team"
    ocur = db.execute("INSERT INTO organizations (name, created_at) VALUES (?,?)",
                      (org_name, now()))
    oid = ocur.lastrowid
    db.execute("UPDATE users SET org_id = ? WHERE id = ?", (oid, uid))
    db.execute("INSERT OR IGNORE INTO memberships (org_id, user_id, role, created_at) "
               "VALUES (?,?,?,?)", (oid, uid, "coordinator", now()))
    db.commit()
    return uid


# --------------------------------------------------------------------------- #
# Team workspace (organizations, memberships, invites, roles)
# --------------------------------------------------------------------------- #
def user_org_id(user_id):
    """The org this user belongs to. Self-heals a missing org (older/edge rows)
    by creating a personal one, so callers can always assume an org exists."""
    row = get_db().execute("SELECT org_id FROM users WHERE id = ?",
                           (user_id,)).fetchone()
    if row and row["org_id"]:
        return row["org_id"]
    return _ensure_personal_org(user_id)


def _ensure_personal_org(user_id):
    db = get_db()
    u = db.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
    nm = ((u["name"] if u else "") or "").strip()
    org_name = f"{nm}'s team" if nm else "My team"
    cur = db.execute("INSERT INTO organizations (name, created_at) VALUES (?,?)",
                     (org_name, now()))
    oid = cur.lastrowid
    db.execute("UPDATE users SET org_id = ? WHERE id = ?", (oid, user_id))
    db.execute("INSERT OR IGNORE INTO memberships (org_id, user_id, role, created_at) "
               "VALUES (?,?,?,?)", (oid, user_id, "coordinator", now()))
    db.commit()
    return oid


def org_member_ids(user_id):
    """All user_ids on this user's team (including themselves). Reads union
    across these so the whole team shares full visibility."""
    oid = user_org_id(user_id)
    rows = get_db().execute(
        "SELECT user_id FROM memberships WHERE org_id = ?", (oid,)).fetchall()
    ids = {r["user_id"] for r in rows}
    ids.add(user_id)
    return sorted(ids)


def list_org_members(user_id):
    """Team roster (for the Team page): each member's id, name, email, role."""
    oid = user_org_id(user_id)
    return get_db().execute(
        "SELECT m.user_id, m.role, m.role_label, m.created_at, u.name, u.email "
        "FROM memberships m JOIN users u ON u.id = m.user_id "
        "WHERE m.org_id = ? ORDER BY "
        "CASE m.role WHEN 'coordinator' THEN 0 WHEN 'pi' THEN 1 ELSE 2 END, "
        "u.name", (oid,)).fetchall()


def member_role(user_id):
    """This user's role in their org ('' if somehow unset)."""
    oid = user_org_id(user_id)
    row = get_db().execute(
        "SELECT role FROM memberships WHERE org_id = ? AND user_id = ?",
        (oid, user_id)).fetchone()
    return (row["role"] if row else "") or ""


def member_role_label(user_id):
    """This user's display title - the custom role_label if set (e.g. 'Site
    Director / President'), else the base-role label ('Coordinator')."""
    oid = user_org_id(user_id)
    row = get_db().execute(
        "SELECT role, role_label FROM memberships WHERE org_id = ? AND user_id = ?",
        (oid, user_id)).fetchone()
    if not row:
        return ""
    return (row["role_label"] or ORG_ROLE_LABELS.get(row["role"], "")) or ""


def can_manage_team(user_id):
    return member_role(user_id) in _ROLE_CAN_MANAGE_TEAM


def can_approve_docs(user_id):
    return member_role(user_id) in _ROLE_CAN_APPROVE_DOCS


def set_member_role(actor_id, target_user_id, role, role_label=""):
    """Change a teammate's role. Only same-org and a valid base role; an optional
    custom label renames it for display without changing permissions."""
    if role not in ORG_ROLES:
        return False
    role_label = (role_label or "").strip()[:40]
    oid = user_org_id(actor_id)
    db = get_db()
    row = db.execute("SELECT id FROM memberships WHERE org_id = ? AND user_id = ?",
                     (oid, target_user_id)).fetchone()
    if not row:
        return False
    db.execute("UPDATE memberships SET role = ?, role_label = ? WHERE id = ?",
               (role, role_label, row["id"]))
    db.commit()
    return True


def remove_member(actor_id, target_user_id):
    """Remove a teammate from the org. Can't remove the last coordinator, and a
    removed member gets a fresh personal org so they're never orphaned."""
    oid = user_org_id(actor_id)
    if actor_id == target_user_id:
        return False
    db = get_db()
    row = db.execute("SELECT role FROM memberships WHERE org_id = ? AND user_id = ?",
                     (oid, target_user_id)).fetchone()
    if not row:
        return False
    if row["role"] == "coordinator":
        n = db.execute("SELECT COUNT(*) c FROM memberships WHERE org_id = ? "
                       "AND role = 'coordinator'", (oid,)).fetchone()["c"]
        if n <= 1:
            return False
    db.execute("DELETE FROM memberships WHERE org_id = ? AND user_id = ?",
               (oid, target_user_id))
    db.execute("UPDATE users SET org_id = NULL WHERE id = ?", (target_user_id,))
    db.commit()
    _ensure_personal_org(target_user_id)
    return True


def create_org_invite(actor_id, email, role="student", role_label=""):
    """Create a join link for a teammate. Returns the token."""
    if role not in ORG_ROLES:
        role = "student"
    role_label = (role_label or "").strip()[:40]
    oid = user_org_id(actor_id)
    token = gen_token()
    db = get_db()
    db.execute(
        "INSERT INTO org_invites (org_id, email, role, role_label, token, "
        "invited_by, created_at) VALUES (?,?,?,?,?,?,?)",
        (oid, (email or "").strip().lower(), role, role_label, token, actor_id, now()))
    db.commit()
    return token


def get_org_invite(token):
    if not token:
        return None
    return get_db().execute(
        "SELECT * FROM org_invites WHERE token = ?", (token,)).fetchone()


def list_org_invites(user_id):
    """Outstanding (unaccepted) invites for this user's org."""
    oid = user_org_id(user_id)
    return get_db().execute(
        "SELECT * FROM org_invites WHERE org_id = ? AND (accepted_at IS NULL "
        "OR accepted_at = '') ORDER BY created_at DESC", (oid,)).fetchall()


def revoke_org_invite(actor_id, token):
    oid = user_org_id(actor_id)
    db = get_db()
    db.execute("DELETE FROM org_invites WHERE org_id = ? AND token = ?",
               (oid, token))
    db.commit()
    return True


def create_copilot_action(user_id, kind, lead_id, payload, ttl_minutes=30):
    """Persist a PROPOSED copilot action and return its token. The target is
    stored here (never trusted from the client at confirm time)."""
    token = gen_token()
    ts = now()
    expires = (dt.datetime.now() + dt.timedelta(minutes=max(1, ttl_minutes))
               ).strftime("%Y-%m-%d %H:%M")
    db = get_db()
    db.execute(
        "INSERT INTO copilot_actions (token, user_id, org_id, kind, lead_id, "
        "payload, status, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (token, user_id, user_org_id(user_id), kind, lead_id,
         json.dumps(payload or {}), "proposed", ts, expires))
    db.commit()
    return token


def get_copilot_action(token):
    if not token:
        return None
    return get_db().execute(
        "SELECT * FROM copilot_actions WHERE token = ?", (token,)).fetchone()


def log_copilot_draft(user_id, thread_id, instruction, category, outcome,
                      reasons=None, model_used=False):
    """One audit row per draft request. Never raises: the audit trail must not
    be the thing that stops a coordinator from getting a reply."""
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO copilot_drafts (user_id, org_id, thread_id, instruction, "
            "category, outcome, reasons, model_used, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, user_org_id(user_id), thread_id,
             (instruction or "")[:400], category or "routine", outcome,
             json.dumps(list(reasons or [])), 1 if model_used else 0, now()))
        conn.commit()
    except Exception:
        pass


def list_copilot_drafts(user_id, thread_id=None, limit=50):
    """Recent audit rows for the team, newest first (optionally one thread)."""
    oid = user_org_id(user_id)
    if thread_id:
        return get_db().execute(
            "SELECT * FROM copilot_drafts WHERE org_id = ? AND thread_id = ? "
            "ORDER BY id DESC LIMIT ?", (oid, thread_id, limit)).fetchall()
    return get_db().execute(
        "SELECT * FROM copilot_drafts WHERE org_id = ? ORDER BY id DESC LIMIT ?",
        (oid, limit)).fetchall()


def mark_copilot_action(token, status):
    db = get_db()
    db.execute(
        "UPDATE copilot_actions SET status = ?, confirmed_at = ? WHERE token = ?",
        (status, now() if status == "confirmed" else "", token))
    db.commit()


def copilot_action_expired(row):
    if not row:
        return True
    exp = _parse_ts(row["expires_at"]) if row["expires_at"] else None
    return bool(exp and exp < dt.datetime.now())


def accept_org_invite(user_id, token):
    """Move a user into the invite's org with the invited role. Their previous
    (personal) org is left behind. Returns the org_id on success, else None."""
    inv = get_org_invite(token)
    if not inv or (inv["accepted_at"] or "").strip():
        return None
    oid = inv["org_id"]
    db = get_db()
    db.execute(
        "INSERT INTO memberships (org_id, user_id, role, role_label, created_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(org_id, user_id) DO UPDATE SET "
        "role = excluded.role, role_label = excluded.role_label",
        (oid, user_id, inv["role"],
         (inv["role_label"] if "role_label" in inv.keys() else ""), now()))
    db.execute("UPDATE users SET org_id = ? WHERE id = ?", (oid, user_id))
    db.execute("UPDATE org_invites SET accepted_at = ? WHERE id = ?",
               (now(), inv["id"]))
    db.commit()
    return oid


# --------------------------------------------------------------------------- #
# Shared marketing inbox
# --------------------------------------------------------------------------- #
def record_marketing_webhook_event(provider, event_key, payload_json,
                                   account_external_id=""):
    """Store a provider event once so retries cannot duplicate processing."""
    provider = (provider or "").strip().lower()
    event_key = (event_key or "").strip()[:128]
    if provider not in MARKETING_CONNECTION_PROVIDERS or not event_key:
        return False
    conn = get_db()
    cur = conn.execute(
        "INSERT OR IGNORE INTO marketing_webhook_events "
        "(provider, event_key, account_external_id, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        (provider, event_key, (account_external_id or "").strip()[:255],
         payload_json or "{}", now()))
    conn.commit()
    return bool(cur.rowcount)


def finish_marketing_webhook_event(provider, event_key, status="processed",
                                   error_message=""):
    if status not in ("processed", "ignored", "error"):
        status = "error"
    conn = get_db()
    conn.execute(
        "UPDATE marketing_webhook_events SET status = ?, error_message = ?, "
        "processed_at = ? WHERE provider = ? AND event_key = ?",
        (status, (error_message or "")[:500], now(),
         (provider or "").strip().lower(), (event_key or "").strip()[:128]))
    conn.commit()


def deauthorize_marketing_account(provider, external_account_id):
    """Erase provider credentials without deleting retained inbox history."""
    provider = (provider or "").strip().lower()
    external_account_id = (external_account_id or "").strip()
    if provider not in MARKETING_CONNECTION_PROVIDERS or not external_account_id:
        return 0
    conn = get_db()
    rows = conn.execute(
        "SELECT id, source_id FROM marketing_connections WHERE provider = ? "
        "AND (external_account_id = ? OR instagram_account_id = ?)",
        (provider, external_account_id, external_account_id)).fetchall()
    if not rows:
        return 0
    ts = now()
    connection_ids = [row["id"] for row in rows]
    source_ids = [row["source_id"] for row in rows]
    conn.executemany(
        "UPDATE marketing_connections SET access_token_encrypted = '', "
        "refresh_token_encrypted = '', token_expires_at = '', "
        "gmail_watch_expires_at = '', status = 'disconnected', "
        "last_error_code = '', last_error_message = '', last_error_at = '', "
        "updated_at = ? WHERE id = ?",
        [(ts, connection_id) for connection_id in connection_ids])
    conn.executemany(
        "UPDATE marketing_sources SET status = 'disconnected', updated_at = ? "
        "WHERE id = ?", [(ts, source_id) for source_id in source_ids])
    conn.commit()
    return len(connection_ids)


def delete_marketing_account_data(provider, external_account_id, request_key,
                                  confirmation_code):
    """Erase one provider identity and return an idempotent deletion receipt."""
    provider = (provider or "").strip().lower()
    external_account_id = (external_account_id or "").strip()
    request_key = (request_key or "").strip()[:128]
    confirmation_code = (confirmation_code or "").strip()[:128]
    if (provider not in MARKETING_CONNECTION_PROVIDERS or not external_account_id
            or not request_key or not confirmation_code):
        return None
    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM marketing_data_deletions "
        "WHERE provider = ? AND request_key = ?",
        (provider, request_key)).fetchone()
    if existing:
        return dict(existing)

    rows = conn.execute(
        "SELECT id, source_id FROM marketing_connections WHERE provider = ? "
        "AND (external_account_id = ? OR instagram_account_id = ?)",
        (provider, external_account_id, external_account_id)).fetchall()
    source_ids = sorted({row["source_id"] for row in rows})
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        conn.execute(
            "DELETE FROM marketing_messages WHERE thread_id IN "
            f"(SELECT id FROM marketing_threads WHERE source_id IN ({placeholders}))",
            source_ids)
        conn.execute(
            f"DELETE FROM marketing_threads WHERE source_id IN ({placeholders})",
            source_ids)
        conn.execute(
            f"DELETE FROM marketing_connections WHERE source_id IN ({placeholders})",
            source_ids)
        conn.execute(
            f"DELETE FROM marketing_sources WHERE id IN ({placeholders})",
            source_ids)
    conn.execute(
        "DELETE FROM marketing_webhook_events WHERE provider = ? "
        "AND account_external_id = ?", (provider, external_account_id))
    ts = now()
    conn.execute(
        "INSERT OR IGNORE INTO marketing_data_deletions "
        "(provider, request_key, confirmation_code, status, requested_at, "
        "completed_at) VALUES (?,?,?,'completed',?,?)",
        (provider, request_key, confirmation_code, ts, ts))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM marketing_data_deletions "
        "WHERE provider = ? AND request_key = ?",
        (provider, request_key)).fetchone()
    return dict(row) if row else None


def get_marketing_data_deletion(confirmation_code):
    confirmation_code = (confirmation_code or "").strip()[:128]
    if not confirmation_code:
        return None
    return get_db().execute(
        "SELECT * FROM marketing_data_deletions WHERE confirmation_code = ?",
        (confirmation_code,)).fetchone()


def get_marketing_source(user_id, source_id):
    oid = user_org_id(user_id)
    return get_db().execute(
        "SELECT * FROM marketing_sources WHERE id = ? AND org_id = ?",
        (source_id, oid)).fetchone()


def list_marketing_sources(user_id, nct=""):
    oid = user_org_id(user_id)
    # When a trial is active, per-account badges count only that trial's threads,
    # plus provider threads not assigned to any trial yet. Unassigned Gmail must
    # remain visible until a coordinator explicitly classifies it.
    nct_open = (
        " AND (t.nct = ? OR COALESCE(t.nct, '') = '')" if nct else "")
    nct_unread = (
        " AND (t.nct = ? OR COALESCE(t.nct, '') = '')" if nct else "")
    args = [oid]
    if nct:
        args = [nct, nct, oid]
    return get_db().execute(
        "SELECT s.*, c.id AS connection_id, c.provider, "
        "c.external_account_id, c.account_identifier AS connected_identifier, "
        "c.granted_scopes, c.token_expires_at, c.gmail_history_id, "
        "c.gmail_watch_expires_at, c.instagram_account_id, "
        "c.last_successful_sync_at, c.status AS connection_status, "
        "c.last_error_code, c.last_error_message, c.last_error_at, "
        "CASE WHEN s.status != 'connected' OR (s.connection_mode = 'live' "
        "AND COALESCE(c.status, '') != 'connected') THEN 0 ELSE "
        "(SELECT COUNT(*) FROM marketing_threads t "
        " WHERE t.source_id = s.id AND t.status = 'open'" + nct_open + ") END AS open_count, "
        "CASE WHEN s.status != 'connected' OR (s.connection_mode = 'live' "
        "AND COALESCE(c.status, '') != 'connected') THEN 0 ELSE "
        "(SELECT COUNT(*) FROM marketing_threads t "
        " WHERE t.source_id = s.id AND t.unread = 1" + nct_unread + ") END AS unread_count "
        "FROM marketing_sources s "
        "LEFT JOIN marketing_connections c ON c.source_id = s.id "
        "WHERE s.org_id = ? "
        "ORDER BY CASE s.channel WHEN 'email' THEN 0 WHEN 'instagram' THEN 1 "
        "ELSE 2 END, s.created_at",
        tuple(args)).fetchall()


def create_marketing_source(user_id, channel, label, identifier,
                            connection_mode="demo"):
    if channel not in MARKETING_CHANNELS:
        return None
    oid = user_org_id(user_id)
    label = (label or "").strip()[:80]
    identifier = (identifier or "").strip()[:160]
    if not identifier:
        return None
    mode = "live" if connection_mode == "live" else "demo"
    ts = now()
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM marketing_sources "
        "WHERE org_id = ? AND channel = ? AND lower(identifier) = lower(?)",
        (oid, channel, identifier)).fetchone()
    if row:
        conn.execute(
            "UPDATE marketing_sources SET label = ?, status = 'connected', "
            "connection_mode = ?, updated_at = ? WHERE id = ?",
            (label, mode, ts, row["id"]))
        source_id = row["id"]
    else:
        cur = conn.execute(
            "INSERT INTO marketing_sources "
            "(org_id, channel, label, identifier, connection_mode, status, "
            "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (oid, channel, label, identifier, mode, "connected", user_id, ts, ts))
        source_id = cur.lastrowid
    conn.commit()
    return source_id


def get_marketing_connection(user_id, connection_id):
    oid = user_org_id(user_id)
    return get_db().execute(
        "SELECT * FROM marketing_connections WHERE id = ? AND org_id = ?",
        (connection_id, oid)).fetchone()


def get_marketing_connection_for_source(user_id, source_id):
    oid = user_org_id(user_id)
    return get_db().execute(
        "SELECT * FROM marketing_connections WHERE source_id = ? AND org_id = ?",
        (source_id, oid)).fetchone()


def get_marketing_connection_by_account(user_id, provider,
                                        external_account_id):
    oid = user_org_id(user_id)
    return get_db().execute(
        "SELECT * FROM marketing_connections "
        "WHERE org_id = ? AND provider = ? AND external_account_id = ?",
        (oid, (provider or "").strip(),
         (external_account_id or "").strip())).fetchone()


def get_marketing_connection_global(provider, external_account_id):
    """Provider webhook lookup. Caller must already have verified its signature."""
    return get_db().execute(
        "SELECT c.*, s.channel, s.label AS source_label, "
        "s.identifier AS source_identifier FROM marketing_connections c "
        "JOIN marketing_sources s ON s.id = c.source_id "
        "WHERE c.provider = ? AND c.external_account_id = ? "
        "AND c.status = 'connected' AND s.status = 'connected'",
        ((provider or "").strip(), (external_account_id or "").strip())
    ).fetchone()


def _marketing_scopes_json(granted_scopes):
    if isinstance(granted_scopes, str):
        scopes = granted_scopes.replace(",", " ").split()
    else:
        scopes = list(granted_scopes or [])
    clean = sorted({str(scope).strip() for scope in scopes if str(scope).strip()})
    return json.dumps(clean, separators=(",", ":"))


def connect_marketing_account(user_id, *, provider, channel,
                              external_account_id, account_identifier,
                              access_token_encrypted,
                              refresh_token_encrypted="", granted_scopes=None,
                              token_expires_at="", gmail_history_id="",
                              gmail_watch_expires_at="",
                              instagram_account_id="",
                              last_successful_sync_at="", label=""):
    """Create or reconnect one provider account and its renderable source."""
    provider = (provider or "").strip().lower()
    channel = (channel or "").strip().lower()
    expected_channel = {
        "gmail": "email", "instagram": "instagram", "google_ads": "google_ads",
    }.get(provider)
    external_account_id = (external_account_id or "").strip()[:255]
    account_identifier = (account_identifier or "").strip()[:320]
    if (provider not in MARKETING_CONNECTION_PROVIDERS
            or channel != expected_channel or not external_account_id
            or not account_identifier or not access_token_encrypted):
        return None

    oid = user_org_id(user_id)
    conn = get_db()
    ts = now()
    existing = conn.execute(
        "SELECT * FROM marketing_connections "
        "WHERE org_id = ? AND provider = ? AND external_account_id = ?",
        (oid, provider, external_account_id)).fetchone()

    source = None
    if existing:
        source = conn.execute(
            "SELECT * FROM marketing_sources WHERE id = ? AND org_id = ?",
            (existing["source_id"], oid)).fetchone()
    if not source:
        source = conn.execute(
            "SELECT * FROM marketing_sources WHERE org_id = ? AND channel = ? "
            "AND lower(identifier) = lower(?)",
            (oid, channel, account_identifier)).fetchone()

    clean_label = (label or "").strip()[:80]
    default_label = (
        "Gmail inbox" if provider == "gmail" else MARKETING_CHANNEL_LABELS[channel])
    if source:
        source_id = source["id"]
        source_label = (
            (source["label"] or "").strip() or clean_label or default_label)
        conn.execute(
            "UPDATE marketing_sources SET label = ?, identifier = ?, "
            "connection_mode = 'live', status = 'connected', updated_at = ? "
            "WHERE id = ? AND org_id = ?",
            (source_label, account_identifier, ts, source_id, oid))
    else:
        source_label = clean_label or default_label
        cur = conn.execute(
            "INSERT INTO marketing_sources "
            "(org_id, channel, label, identifier, connection_mode, status, "
            "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (oid, channel, source_label, account_identifier, "live", "connected",
             user_id, ts, ts))
        source_id = cur.lastrowid

    if not existing:
        existing = conn.execute(
            "SELECT * FROM marketing_connections "
            "WHERE source_id = ? AND org_id = ?", (source_id, oid)).fetchone()
    same_identity = bool(
        existing and existing["provider"] == provider
        and existing["external_account_id"] == external_account_id)
    refresh_encrypted = (refresh_token_encrypted or "").strip()
    if not refresh_encrypted and same_identity:
        refresh_encrypted = existing["refresh_token_encrypted"] or ""
    watch_expires = (gmail_watch_expires_at or "").strip()
    last_sync = (last_successful_sync_at or "").strip()
    if same_identity:
        watch_expires = watch_expires or existing["gmail_watch_expires_at"] or ""
        last_sync = last_sync or existing["last_successful_sync_at"] or ""

    values = (
        oid, source_id, provider, external_account_id, account_identifier,
        access_token_encrypted, refresh_encrypted,
        _marketing_scopes_json(granted_scopes),
        (token_expires_at or "").strip()[:40],
        (gmail_history_id or "").strip()[:64], watch_expires[:40],
        (instagram_account_id or "").strip()[:255], last_sync[:40],
        "connected", "", "", "", user_id, ts, ts,
    )
    if existing:
        conn.execute(
            "UPDATE marketing_connections SET org_id = ?, source_id = ?, "
            "provider = ?, external_account_id = ?, account_identifier = ?, "
            "access_token_encrypted = ?, refresh_token_encrypted = ?, "
            "granted_scopes = ?, token_expires_at = ?, gmail_history_id = ?, "
            "gmail_watch_expires_at = ?, instagram_account_id = ?, "
            "last_successful_sync_at = ?, status = ?, last_error_code = ?, "
            "last_error_message = ?, last_error_at = ?, created_by = ?, "
            "updated_at = ? WHERE id = ? AND org_id = ?",
            values[:-2] + (ts, existing["id"], oid))
        connection_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO marketing_connections "
            "(org_id, source_id, provider, external_account_id, "
            "account_identifier, access_token_encrypted, "
            "refresh_token_encrypted, granted_scopes, token_expires_at, "
            "gmail_history_id, gmail_watch_expires_at, instagram_account_id, "
            "last_successful_sync_at, status, last_error_code, "
            "last_error_message, last_error_at, created_by, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values)
        connection_id = cur.lastrowid
    conn.commit()
    return {"source_id": source_id, "connection_id": connection_id}


def update_marketing_connection_tokens(user_id, connection_id, *,
                                       access_token_encrypted,
                                       token_expires_at,
                                       refresh_token_encrypted=None,
                                       granted_scopes=None):
    oid = user_org_id(user_id)
    existing = get_marketing_connection(user_id, connection_id)
    if not existing or not access_token_encrypted:
        return False
    refresh = existing["refresh_token_encrypted"]
    if refresh_token_encrypted is not None:
        refresh = refresh_token_encrypted
    scopes = existing["granted_scopes"]
    if granted_scopes is not None:
        scopes = _marketing_scopes_json(granted_scopes)
    conn = get_db()
    cur = conn.execute(
        "UPDATE marketing_connections SET access_token_encrypted = ?, "
        "refresh_token_encrypted = ?, granted_scopes = ?, token_expires_at = ?, "
        "status = 'connected', last_error_code = '', last_error_message = '', "
        "last_error_at = '', updated_at = ? WHERE id = ? AND org_id = ?",
        (access_token_encrypted, refresh, scopes,
         (token_expires_at or "").strip()[:40], now(), connection_id, oid))
    if cur.rowcount:
        conn.execute(
            "UPDATE marketing_sources SET status = 'connected', updated_at = ? "
            "WHERE id = ? AND org_id = ?",
            (now(), existing["source_id"], oid))
    conn.commit()
    return bool(cur.rowcount)


def set_marketing_connection_error(user_id, connection_id, code, message,
                                   status="error"):
    if status not in ("error", "needs_reauth"):
        status = "error"
    oid = user_org_id(user_id)
    conn = get_db()
    row = conn.execute(
        "SELECT source_id FROM marketing_connections WHERE id = ? AND org_id = ?",
        (connection_id, oid)).fetchone()
    if not row:
        return False
    ts = now()
    conn.execute(
        "UPDATE marketing_connections SET status = ?, last_error_code = ?, "
        "last_error_message = ?, last_error_at = ?, updated_at = ? "
        "WHERE id = ? AND org_id = ?",
        (status, (code or "unknown")[:80], (message or "")[:500], ts, ts,
         connection_id, oid))
    conn.execute(
        "UPDATE marketing_sources SET status = 'disconnected', updated_at = ? "
        "WHERE id = ? AND org_id = ?", (ts, row["source_id"], oid))
    conn.commit()
    return True


def record_marketing_connection_sync_error(user_id, connection_id, code,
                                           message):
    """Record a retryable provider failure without disabling the connection."""
    oid = user_org_id(user_id)
    ts = now()
    conn = get_db()
    cur = conn.execute(
        "UPDATE marketing_connections SET last_error_code = ?, "
        "last_error_message = ?, last_error_at = ?, updated_at = ? "
        "WHERE id = ? AND org_id = ? AND status = 'connected'",
        ((code or "sync_failed")[:80], (message or "")[:500], ts, ts,
         connection_id, oid))
    conn.commit()
    return bool(cur.rowcount)


def mark_marketing_connection_synced(user_id, connection_id, *,
                                     gmail_history_id=None,
                                     gmail_watch_expires_at=None):
    oid = user_org_id(user_id)
    connection = get_marketing_connection(user_id, connection_id)
    if not connection:
        return False
    fields = ["last_successful_sync_at = ?", "status = 'connected'",
              "last_error_code = ''", "last_error_message = ''",
              "last_error_at = ''", "updated_at = ?"]
    ts = now()
    args = [ts, ts]
    if gmail_history_id is not None:
        fields.append("gmail_history_id = ?")
        args.append((gmail_history_id or "")[:64])
    if gmail_watch_expires_at is not None:
        fields.append("gmail_watch_expires_at = ?")
        args.append((gmail_watch_expires_at or "")[:40])
    args.extend([connection_id, oid])
    conn = get_db()
    cur = conn.execute(
        f"UPDATE marketing_connections SET {', '.join(fields)} "
        "WHERE id = ? AND org_id = ?", args)
    if cur.rowcount:
        conn.execute(
            "UPDATE marketing_sources SET status = 'connected', updated_at = ? "
            "WHERE id = ? AND org_id = ?",
            (ts, connection["source_id"], oid))
    conn.commit()
    return bool(cur.rowcount)


def disconnect_marketing_connection(user_id, connection_id,
                                    purge_imported_threads=False):
    oid = user_org_id(user_id)
    conn = get_db()
    row = conn.execute(
        "SELECT source_id FROM marketing_connections WHERE id = ? AND org_id = ?",
        (connection_id, oid)).fetchone()
    if not row:
        return False
    ts = now()
    conn.execute(
        "UPDATE marketing_connections SET access_token_encrypted = '', "
        "refresh_token_encrypted = '', token_expires_at = '', "
        "gmail_watch_expires_at = '', status = 'disconnected', "
        "last_error_code = '', last_error_message = '', last_error_at = '', "
        "updated_at = ? WHERE id = ? AND org_id = ?",
        (ts, connection_id, oid))
    conn.execute(
        "UPDATE marketing_sources SET status = 'disconnected', updated_at = ? "
        "WHERE id = ? AND org_id = ?", (ts, row["source_id"], oid))
    deleted_threads = 0
    if purge_imported_threads:
        thread_ids = [thread["id"] for thread in conn.execute(
            "SELECT id FROM marketing_threads WHERE source_id = ? AND org_id = ?",
            (row["source_id"], oid)).fetchall()]
        if thread_ids:
            placeholders = ",".join("?" for _ in thread_ids)
            conn.execute(
                f"DELETE FROM marketing_messages WHERE thread_id IN ({placeholders})",
                thread_ids)
            deleted_threads = conn.execute(
                "DELETE FROM marketing_threads WHERE source_id = ? AND org_id = ?",
                (row["source_id"], oid)).rowcount
    conn.commit()
    return {"source_id": row["source_id"],
            "deleted_threads": int(deleted_threads or 0)}


def set_marketing_source_status(user_id, source_id, status):
    if status not in ("connected", "disconnected"):
        return False
    oid = user_org_id(user_id)
    conn = get_db()
    cur = conn.execute(
        "UPDATE marketing_sources SET status = ?, updated_at = ? "
        "WHERE id = ? AND org_id = ?",
        (status, now(), source_id, oid))
    conn.commit()
    return bool(cur.rowcount)


def get_marketing_handoff(user_id):
    oid = user_org_id(user_id)
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM marketing_handoffs WHERE org_id = ?", (oid,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO marketing_handoffs "
            "(org_id, primary_user_id, vacation_mode, updated_by, updated_at) "
            "VALUES (?,?,?,?,?)", (oid, user_id, 0, user_id, now()))
        conn.commit()
        row = conn.execute(
            "SELECT * FROM marketing_handoffs WHERE org_id = ?", (oid,)).fetchone()
    member_ids = set(org_member_ids(user_id))
    if row["primary_user_id"] not in member_ids:
        conn.execute(
            "UPDATE marketing_handoffs SET primary_user_id = ?, "
            "vacation_mode = 0, cover_user_id = NULL, updated_by = ?, "
            "updated_at = ? WHERE org_id = ?",
            (user_id, user_id, now(), oid))
        conn.commit()
        row = conn.execute(
            "SELECT * FROM marketing_handoffs WHERE org_id = ?", (oid,)).fetchone()
    return row


def marketing_active_owner_id(settings):
    """Who new inbox conversations should land on.

    Two mechanisms coexist. `away_periods` (per user, several at once) is the
    current one and wins; `marketing_handoffs` is the older org-keyed row - its
    PRIMARY KEY is org_id, so a whole team could only ever have one handoff - and
    is now read-only legacy kept so existing rows keep working."""
    if not settings:
        return None
    primary = settings["primary_user_id"]
    covered = effective_assignee(primary)
    if covered != primary:
        return covered
    if settings["vacation_mode"] and settings["cover_user_id"]:
        return settings["cover_user_id"]
    return primary


def set_marketing_handoff(user_id, primary_user_id, cover_user_id=None,
                          vacation_mode=False, away_until="", note=""):
    oid = user_org_id(user_id)
    members = set(org_member_ids(user_id))
    primary_user_id = primary_user_id or user_id
    if primary_user_id not in members:
        return False
    if cover_user_id not in members or cover_user_id == primary_user_id:
        cover_user_id = None
    if vacation_mode and not cover_user_id:
        return False
    conn = get_db()
    conn.execute(
        "INSERT INTO marketing_handoffs "
        "(org_id, primary_user_id, cover_user_id, vacation_mode, away_until, "
        "note, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(org_id) DO UPDATE SET "
        "primary_user_id = excluded.primary_user_id, "
        "cover_user_id = excluded.cover_user_id, "
        "vacation_mode = excluded.vacation_mode, "
        "away_until = excluded.away_until, note = excluded.note, "
        "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
        (oid, primary_user_id, cover_user_id, 1 if vacation_mode else 0,
         (away_until or "").strip()[:10], (note or "").strip()[:500],
         user_id, now()))
    conn.commit()
    return True


def create_marketing_thread(user_id, source_id, contact_name, contact_handle,
                            subject, body):
    source = get_marketing_source(user_id, source_id)
    body = (body or "").strip()[:4000]
    if not source or source["status"] != "connected" or not body:
        return None
    oid = user_org_id(user_id)
    settings = get_marketing_handoff(user_id)
    assigned_to = marketing_active_owner_id(settings)
    ts = now()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO marketing_threads "
        "(org_id, source_id, contact_name, contact_handle, subject, status, "
        "assigned_to, unread, created_at, updated_at) "
        "VALUES (?,?,?,?,?,'open',?,1,?,?)",
        (oid, source_id, (contact_name or "").strip()[:100],
         (contact_handle or "").strip()[:160],
         (subject or "New conversation").strip()[:180], assigned_to, ts, ts))
    thread_id = cur.lastrowid
    conn.execute(
        "INSERT INTO marketing_messages "
        "(org_id, thread_id, kind, body, author_name, delivery_status, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (oid, thread_id, "inbound", body,
         (contact_name or contact_handle or "Contact").strip()[:100],
         "received", ts))
    conn.commit()
    return thread_id


def upsert_gmail_thread(user_id, source_id, *, external_ref, contact_name,
                        contact_handle, subject, messages):
    """Persist one Gmail thread and any unseen messages in one transaction."""
    source = get_marketing_source(user_id, source_id)
    connection = get_marketing_connection_for_source(user_id, source_id)
    thread_ref = (external_ref or "").strip()[:255]
    if (not source or not connection or connection["provider"] != "gmail"
            or source["status"] != "connected"
            or connection["status"] != "connected" or not thread_ref):
        return None

    clean_messages = []
    for item in messages or []:
        message_ref = str(item.get("external_ref") or "").strip()[:255]
        kind = (item.get("kind") or "").strip()
        body = (item.get("body") or "").strip()[:4000]
        if not message_ref or kind not in ("inbound", "outbound") or not body:
            continue
        clean_messages.append({
            "external_ref": message_ref,
            "kind": kind,
            "body": body,
            "author_name": (item.get("author_name") or "").strip()[:100],
            "delivery_status": (
                item.get("delivery_status") or
                ("received" if kind == "inbound" else "sent"))[:30],
            "created_at": (item.get("created_at") or now()).strip()[:16],
            "unread": bool(item.get("unread")),
        })
    if not clean_messages:
        return None
    clean_messages.sort(key=lambda item: (
        item["created_at"], item["external_ref"]))

    oid = user_org_id(user_id)
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM marketing_threads WHERE org_id = ? AND source_id = ? "
        "AND external_ref = ?", (oid, source_id, thread_ref)).fetchone()
    inserted_thread = False
    if not row:
        settings = get_marketing_handoff(user_id)
        assigned_to = marketing_active_owner_id(settings)
        first_at = clean_messages[0]["created_at"]
        last_at = clean_messages[-1]["created_at"]
        initial_unread = int(any(
            item["kind"] == "inbound" and item["unread"]
            for item in clean_messages))
        cur = conn.execute(
            "INSERT OR IGNORE INTO marketing_threads "
            "(org_id, source_id, external_ref, contact_name, contact_handle, "
            "subject, status, assigned_to, unread, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,'open',?,?,?,?)",
            (oid, source_id, thread_ref,
             (contact_name or "").strip()[:100],
             (contact_handle or "").strip()[:160],
             (subject or "(no subject)").strip()[:180], assigned_to,
             initial_unread, first_at, last_at))
        inserted_thread = bool(cur.rowcount)
        row = conn.execute(
            "SELECT * FROM marketing_threads WHERE org_id = ? AND source_id = ? "
            "AND external_ref = ?", (oid, source_id, thread_ref)).fetchone()
    if not row:
        conn.rollback()
        return None

    inserted = []
    for item in clean_messages:
        cur = conn.execute(
            "INSERT OR IGNORE INTO marketing_messages "
            "(org_id, thread_id, external_ref, kind, body, author_name, "
            "delivery_status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (oid, row["id"], item["external_ref"], item["kind"], item["body"],
             item["author_name"], item["delivery_status"], item["created_at"]))
        if cur.rowcount:
            inserted.append(item)

    latest_at = max(
        [row["updated_at"] or ""] +
        [item["created_at"] for item in clean_messages])
    has_new_inbound = any(item["kind"] == "inbound" for item in inserted)
    has_new_unread = any(
        item["kind"] == "inbound" and item["unread"] for item in inserted)
    conn.execute(
        "UPDATE marketing_threads SET contact_name = ?, contact_handle = ?, "
        "subject = ?, status = CASE WHEN ? THEN 'open' ELSE status END, "
        "unread = CASE WHEN ? THEN 1 ELSE unread END, updated_at = ? "
        "WHERE id = ? AND org_id = ?",
        ((contact_name or row["contact_name"] or "").strip()[:100],
         (contact_handle or row["contact_handle"] or "").strip()[:160],
         (subject or row["subject"] or "(no subject)").strip()[:180],
         1 if has_new_inbound else 0, 1 if has_new_unread else 0,
         latest_at, row["id"], oid))
    conn.commit()
    return {
        "thread_id": row["id"],
        "inserted_thread": inserted_thread,
        "inserted_messages": len(inserted),
        "latest_at": max(item["created_at"] for item in clean_messages),
    }


def ingest_instagram_message(account_external_id, sender_id, message_id, body,
                             created_at):
    """Idempotently turn one verified Instagram webhook message into a thread."""
    connection = get_marketing_connection_global(
        "instagram", account_external_id)
    sender_id = (sender_id or "").strip()[:255]
    message_id = (message_id or "").strip()[:255]
    body = (body or "").strip()[:4000]
    if not connection or not sender_id or not message_id or not body:
        return None
    conn = get_db()
    oid, source_id = connection["org_id"], connection["source_id"]
    row = conn.execute(
        "SELECT * FROM marketing_threads WHERE org_id = ? AND source_id = ? "
        "AND external_ref = ?", (oid, source_id, sender_id)).fetchone()
    if not row:
        handoff = conn.execute(
            "SELECT * FROM marketing_handoffs WHERE org_id = ?", (oid,)).fetchone()
        assigned_to = None
        if handoff:
            assigned_to = (handoff["cover_user_id"] if handoff["vacation_mode"]
                           and handoff["cover_user_id"]
                           else handoff["primary_user_id"])
        cur = conn.execute(
            "INSERT OR IGNORE INTO marketing_threads "
            "(org_id, source_id, external_ref, contact_name, contact_handle, "
            "subject, status, assigned_to, unread, pipeline_stage, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,'open',?,1,'new',?,?)",
            (oid, source_id, sender_id, "Instagram contact", sender_id,
             "Instagram message", assigned_to, created_at, created_at))
        row = conn.execute(
            "SELECT * FROM marketing_threads WHERE org_id = ? AND source_id = ? "
            "AND external_ref = ?", (oid, source_id, sender_id)).fetchone()
        if not cur.rowcount and not row:
            conn.rollback()
            return None
    cur = conn.execute(
        "INSERT OR IGNORE INTO marketing_messages "
        "(org_id, thread_id, external_ref, kind, body, author_name, "
        "delivery_status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (oid, row["id"], message_id, "inbound", body, "Instagram contact",
         "received", created_at))
    if cur.rowcount:
        conn.execute(
            "UPDATE marketing_threads SET status = 'open', unread = 1, "
            "updated_at = ? WHERE id = ? AND org_id = ?",
            (created_at, row["id"], oid))
    conn.commit()
    return {"thread_id": row["id"], "inserted": bool(cur.rowcount)}


def get_marketing_thread(user_id, thread_id):
    oid = user_org_id(user_id)
    return get_db().execute(
        "SELECT t.*, s.channel, s.label AS source_label, "
        "s.identifier AS source_identifier, s.connection_mode, "
        "s.status AS source_status, c.id AS connection_id, c.provider, "
        "c.status AS connection_status, c.external_account_id, "
        "c.instagram_account_id, c.access_token_encrypted, "
        "u.name AS assigned_name, "
        "u.email AS assigned_email "
        "FROM marketing_threads t "
        "LEFT JOIN marketing_sources s ON s.id = t.source_id "
        "LEFT JOIN marketing_connections c ON c.source_id = s.id "
        "LEFT JOIN users u ON u.id = t.assigned_to "
        "WHERE t.id = ? AND t.org_id = ? AND s.status = 'connected' "
        "AND (s.connection_mode != 'live' OR c.status = 'connected')",
        (thread_id, oid)).fetchone()


def list_marketing_threads(user_id, status="open", channel="", query="",
                           source_id=None, nct="", assignee=""):
    oid = user_org_id(user_id)
    where = ["t.org_id = ?", "s.status = 'connected'",
             "(s.connection_mode != 'live' OR c.status = 'connected')"]
    args = [oid]
    if status in ("open", "resolved"):
        where.append("t.status = ?")
        args.append(status)
    if channel in MARKETING_CHANNELS:
        where.append("s.channel = ?")
        args.append(channel)
    if source_id:
        where.append("t.source_id = ?")
        args.append(source_id)
    if nct:
        where.append("(t.nct = ? OR COALESCE(t.nct, '') = '')")
        args.append(nct)
    # Ownership triage: "mine" = threads routed to me, "unassigned" = threads
    # nobody owns yet (so nothing sits unclaimed). Unlike nct, unassigned is NOT
    # folded into every view - the whole point is to isolate the unclaimed queue.
    if assignee == "mine":
        where.append("t.assigned_to = ?")
        args.append(user_id)
    elif assignee == "unassigned":
        where.append("t.assigned_to IS NULL")
    query = (query or "").strip().lower()[:100]
    if query:
        where.append(
            "(lower(t.contact_name) LIKE ? OR lower(t.contact_handle) LIKE ? "
            "OR lower(t.subject) LIKE ? OR EXISTS (SELECT 1 FROM "
            "marketing_messages qm WHERE qm.thread_id = t.id "
            "AND lower(qm.body) LIKE ?))")
        like = f"%{query}%"
        args.extend([like, like, like, like])
    sql = (
        "SELECT t.*, s.channel, s.label AS source_label, "
        "s.identifier AS source_identifier, s.connection_mode, "
        "u.name AS assigned_name, "
        "COALESCE((SELECT m.body FROM marketing_messages m "
        "WHERE m.thread_id = t.id ORDER BY m.created_at DESC, m.id DESC "
        "LIMIT 1), '') AS preview, "
        "COALESCE((SELECT m.kind FROM marketing_messages m "
        "WHERE m.thread_id = t.id ORDER BY m.created_at DESC, m.id DESC "
        "LIMIT 1), '') AS last_kind, "
        # Kind of the last real MESSAGE (ignores internal notes) so we can tell
        # "patient wrote, we still owe a reply" (inbound) from "we already
        # replied" (outbound) - an internal note must not look like a response.
        "COALESCE((SELECT m.kind FROM marketing_messages m "
        "WHERE m.thread_id = t.id AND m.kind != 'note' "
        "ORDER BY m.created_at DESC, m.id DESC LIMIT 1), '') AS last_reply_kind, "
        "(SELECT COUNT(*) FROM marketing_messages m "
        "WHERE m.thread_id = t.id) AS message_count "
        "FROM marketing_threads t "
        "LEFT JOIN marketing_sources s ON s.id = t.source_id "
        "LEFT JOIN marketing_connections c ON c.source_id = s.id "
        "LEFT JOIN users u ON u.id = t.assigned_to WHERE "
        + " AND ".join(where) +
        " ORDER BY t.unread DESC, t.updated_at DESC, t.id DESC")
    rows = get_db().execute(sql, tuple(args)).fetchall()
    # Triage default: float threads we still OWE a reply to (open + last real
    # message inbound) to the top, so an aging owed reply can't get buried under
    # newer threads we've already answered. Stable, so the SQL order (unread,
    # then recency) is preserved WITHIN the owed / not-owed groups. This is the
    # only place this ordering lives; revert this sort to restore pure recency.
    return sorted(
        rows,
        key=lambda r: 0 if (r["status"] == "open"
                            and r["last_reply_kind"] == "inbound") else 1)


def marketing_thread_counts(user_id, nct="", assignee=""):
    oid = user_org_id(user_id)
    where = ["t.org_id = ?", "s.status = 'connected'",
             "(s.connection_mode != 'live' OR c.status = 'connected')"]
    args = [oid]
    if nct:
        where.append("(t.nct = ? OR COALESCE(t.nct, '') = '')")
        args.append(nct)
    # Keep the status-tab counts honest when the owner triage filter is active,
    # so "Open 5" matches the 5 rows shown (mirrors the list's assignee filter).
    if assignee == "mine":
        where.append("t.assigned_to = ?")
        args.append(user_id)
    elif assignee == "unassigned":
        where.append("t.assigned_to IS NULL")
    row = get_db().execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN t.status = 'open' THEN 1 ELSE 0 END) AS open_count, "
        "SUM(CASE WHEN t.status = 'resolved' THEN 1 ELSE 0 END) AS resolved_count, "
        "SUM(CASE WHEN t.unread = 1 THEN 1 ELSE 0 END) AS unread_count "
        "FROM marketing_threads t "
        "JOIN marketing_sources s ON s.id = t.source_id "
        "LEFT JOIN marketing_connections c ON c.source_id = s.id WHERE "
        + " AND ".join(where),
        tuple(args)).fetchone()
    return {
        "total": int(row["total"] or 0),
        "open": int(row["open_count"] or 0),
        "resolved": int(row["resolved_count"] or 0),
        "unread": int(row["unread_count"] or 0),
    }


def marketing_unread_by_study(user_id):
    """Unread thread counts grouped by study NCT, for the top-bar switcher badges
    (so a coordinator sees which trial has activity and jumps straight to it).
    One query; returns {nct: count}. Unassigned (no NCT) threads are omitted."""
    oid = user_org_id(user_id)
    rows = get_db().execute(
        "SELECT t.nct, "
        "SUM(CASE WHEN t.unread = 1 THEN 1 ELSE 0 END) AS unread_count "
        "FROM marketing_threads t "
        "JOIN marketing_sources s ON s.id = t.source_id "
        "LEFT JOIN marketing_connections c ON c.source_id = s.id "
        "WHERE t.org_id = ? AND COALESCE(t.nct, '') != '' "
        "AND s.status = 'connected' "
        "AND (s.connection_mode != 'live' OR c.status = 'connected') "
        "GROUP BY t.nct", (oid,)).fetchall()
    return {r["nct"]: int(r["unread_count"] or 0) for r in rows}


def list_marketing_messages(user_id, thread_id):
    thread = get_marketing_thread(user_id, thread_id)
    if not thread:
        return []
    return get_db().execute(
        "SELECT m.*, u.name AS user_name FROM marketing_messages m "
        "LEFT JOIN users u ON u.id = m.author_user_id "
        "WHERE m.thread_id = ? AND m.org_id = ? "
        "ORDER BY m.created_at, m.id",
        (thread_id, thread["org_id"])).fetchall()


def mark_marketing_thread_read(user_id, thread_id):
    oid = user_org_id(user_id)
    conn = get_db()
    cur = conn.execute(
        "UPDATE marketing_threads SET unread = 0 WHERE id = ? AND org_id = ?",
        (thread_id, oid))
    conn.commit()
    return bool(cur.rowcount)


def add_marketing_message(user_id, thread_id, body, kind="outbound", *,
                          delivery_status=None, external_ref=""):
    if kind not in ("outbound", "note"):
        return None
    thread = get_marketing_thread(user_id, thread_id)
    body = (body or "").strip()[:4000]
    if not thread or not body:
        return None
    user = get_db().execute(
        "SELECT name, email FROM users WHERE id = ?", (user_id,)).fetchone()
    author = ((user["name"] or user["email"]) if user else "Team member")
    delivery = "internal" if kind == "note" else (
        delivery_status if delivery_status in ("saved", "sent") else "saved")
    message_ref = (external_ref or "").strip()[:255] if kind == "outbound" else ""
    ts = now()
    conn = get_db()
    cur = conn.execute(
        "INSERT OR IGNORE INTO marketing_messages "
        "(org_id, thread_id, external_ref, kind, body, author_user_id, "
        "author_name, delivery_status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (thread["org_id"], thread_id, message_ref, kind, body, user_id,
         author, delivery, ts))
    message_id = cur.lastrowid
    if not cur.rowcount and message_ref:
        existing = conn.execute(
            "SELECT id FROM marketing_messages WHERE thread_id = ? "
            "AND external_ref = ?", (thread_id, message_ref)).fetchone()
        message_id = existing["id"] if existing else None
    if not message_id:
        conn.rollback()
        return None
    conn.execute(
        "UPDATE marketing_threads SET unread = 0, status = 'open', "
        "updated_at = ? WHERE id = ? AND org_id = ?",
        (ts, thread_id, thread["org_id"]))
    conn.commit()
    return message_id


def assign_marketing_thread(user_id, thread_id, assignee_id=None):
    oid = user_org_id(user_id)
    if assignee_id is not None and assignee_id not in set(org_member_ids(user_id)):
        return False
    conn = get_db()
    cur = conn.execute(
        "UPDATE marketing_threads SET assigned_to = ?, updated_at = ? "
        "WHERE id = ? AND org_id = ?",
        (assignee_id, now(), thread_id, oid))
    conn.commit()
    return bool(cur.rowcount)


def set_marketing_thread_status(user_id, thread_id, status):
    if status not in ("open", "resolved"):
        return False
    oid = user_org_id(user_id)
    conn = get_db()
    cur = conn.execute(
        "UPDATE marketing_threads SET status = ?, unread = 0, updated_at = ? "
        "WHERE id = ? AND org_id = ?", (status, now(), thread_id, oid))
    conn.commit()
    return bool(cur.rowcount)


MARKETING_PIPELINE_STAGES = (
    "new", "outreach", "prescreen", "screening", "enrolled",
    "disqualified", "archived",
)


def set_marketing_thread_stage(user_id, thread_id, stage):
    """Persist the recruitment stage without conflating it with open/resolved."""
    if stage not in MARKETING_PIPELINE_STAGES:
        return False
    oid = user_org_id(user_id)
    conn = get_db()
    cur = conn.execute(
        "UPDATE marketing_threads SET pipeline_stage = ?, updated_at = ? "
        "WHERE id = ? AND org_id = ?", (stage, now(), thread_id, oid))
    conn.commit()
    return bool(cur.rowcount)


def link_marketing_thread_lead(user_id, thread_id, lead_id):
    """Explicitly link an org-owned inbox thread to an accessible applicant.

    Identity is never inferred from name/email. The caller must provide the lead
    created or selected after consent and study ownership have been checked.
    """
    thread = get_marketing_thread(user_id, thread_id)
    lead = get_lead(lead_id)
    if not thread or not lead:
        return False
    member_ids = set(org_member_ids(user_id))
    owner_id = lead["owner_user_id"] if "owner_user_id" in lead.keys() else None
    if owner_id not in member_ids:
        claimed = set(user_claimed_ncts(user_id))
        if not lead["nct"] or lead["nct"] not in claimed:
            return False
    conn = get_db()
    cur = conn.execute(
        "UPDATE marketing_threads SET linked_lead_id = ?, pipeline_stage = ?, "
        "updated_at = ? WHERE id = ? AND org_id = ?",
        (lead_id, lead["status"] if lead["status"] in MARKETING_PIPELINE_STAGES
         else "prescreen", now(), thread_id, thread["org_id"]))
    conn.commit()
    return bool(cur.rowcount)


def authorize_lead_records(lead_id):
    lead = get_lead(lead_id)
    if not lead:
        return False
    conn = get_db()
    conn.execute(
        "UPDATE leads SET records_authorized_at = ?, updated_at = ? WHERE id = ?",
        (now(), now(), lead_id))
    conn.commit()
    return True


# The account the study-team demo runs as (see app._DEMO_EMAIL). The rich
# marketing-hub demo is only ever seeded onto this throwaway account.
_DEMO_LOGIN_NAME = "Riley Patel"
_MARKETING_DEMO_EMAIL = "rpatel@northwindclinical.com"
# Fictional site roster. Emails stay on the demo domain so login keeps working;
# display names are invented and must never match a real site's staff.
_DEMO_TEAM = (
    ("dchen@northwindclinical.com", "David Chen, MD", "pi",
     "Principal Investigator", ("peder@fieveclinical.com",)),
    ("mbrooks@northwindclinical.com", "Maya Brooks, PMHNP-BC", "pi",
     "Sub-Investigator", ("swomack@fieveclinical.com",)),
    ("evargas@northwindclinical.com", "Elena Vargas, JD, CCRC", "coordinator",
     "Site Director / President", ("vfieve@fieveclinical.com",)),
    ("pshah@northwindclinical.com", "Priya Shah, MD, CCRC", "coordinator",
     "Director of Clinical Operations", ("mhenderson@fieveclinical.com",)),
    ("rpatel@northwindclinical.com", "Riley Patel", "coordinator",
     "Clinical Research Coordinator", ("dejosama@fieveclinical.com",)),
    ("akim@northwindclinical.com", "Avery Kim, MPH", "student",
     "Clinical Research Coordinator", ("kwalsh@fieveclinical.com",)),
)
# Longest strings first so "Paul Eder, MD" is rewritten before "Paul Eder".
_DEMO_STAFF_REWRITES = (
    ("Sharita D. Womack, PMHNP-BC", "Maya Brooks, PMHNP-BC"),
    ("Vanessa Fieve, JD, CCRC", "Elena Vargas, JD, CCRC"),
    ("Margaret Henderson, MD, CCRC", "Priya Shah, MD, CCRC"),
    ("Danny-Elle Josama", "Riley Patel"),
    ("Kara Walsh, MPH", "Avery Kim, MPH"),
    ("Paul Eder, MD", "David Chen, MD"),
    ("Margaret Henderson, MD", "Priya Shah, MD"),
    ("Sharita D. Womack", "Maya Brooks"),
    ("Vanessa Fieve", "Elena Vargas"),
    ("Margaret Henderson", "Priya Shah"),
    ("Danny Josama", "Riley Patel"),
    ("Kara Walsh", "Avery Kim"),
    ("Paul Eder", "David Chen"),
    ("Dr. Eder", "Dr. Chen"),
    ("@danny-elle", "@riley"),
    ("@Danny-Elle", "@Riley"),
    ("danny-elle", "riley"),
    ("@Vanessa", "@Elena"),
    ("@Margaret", "@Priya"),
    ("@Kara", "@Avery"),
    ("@Paul", "@David"),
)
_DEMO_STAFF_TEXT_COLUMNS = (
    ("users", "name"),
    ("site_profiles", "contact_name"),
    ("lead_notes", "author"),
    ("lead_notes", "body"),
    ("team_messages", "sender_name"),
    ("team_messages", "body"),
    ("trial_documents", "party_name"),
    ("document_events", "actor"),
    ("irb_submissions", "pi_name"),
    ("irb_submission_events", "actor"),
    ("marketing_messages", "author_name"),
    ("marketing_messages", "body"),
    ("mentions", "excerpt"),
)
# Sentinel: presence of this source means the rich demo has already been seeded,
# so we never re-seed (and never trample edits made live during a demo).
_MARKETING_DEMO_SENTINEL = "recruit@northwindclinical.com"
# Recognizable contact in the denser default-trial scenario.
_MARKETING_DEMO_WORKFLOW_SENTINEL = "emily.carter@gmail.com"
_MARKETING_DEMO_STUDIES = {
    "NCT05711940": "COMP360 Psilocybin in Treatment-Resistant Depression",
    "NCT07645924": "Elismetrep (K-304) for the Acute Treatment of Migraine",
    "NCT06559306": "Adjunctive Seltorexant in MDD With Insomnia Symptoms",
    "NCT07076407": "Azetukalner vs Placebo in Major Depressive Disorder (X-NOVA3)",
    "NCT06922110": "Azetukalner Open-Label Extension in Major Depressive Disorder",
    "NCT07573176": "Seltorexant Monotherapy in Major Depressive Disorder",
    "NCT07674654": "Elismetrep (K-304) Long-Term Safety in Acute Migraine",
    "NCT06417775": "Ubrogepant for Menstrual Migraine",
}


# The demo site used to carry a real clinic's name and email domain. Fresh
# seeds no longer do, but existing DBs keep users, sources, notes and audit
# rows across restarts, so the old strings would stay on screen forever.
# Longest first so "Fieve Clinical Research" is rewritten as one unit; the bare
# "Fieve" at the end catches anything left (a leftover real surname included).
_DEMO_SITE_REWRITES = (
    ("Fieve Clinical Research", "Northwind Clinical Research"),
    ("Fieve Migraine Search", "Northwind Migraine Search"),
    ("Fieve Depression Search", "Northwind Depression Search"),
    ("@fieveclinical.com", "@northwindclinical.com"),
    ("@fieveclinical", "@northwindclinical"),
    ("fieveclinical", "northwindclinical"),
    ("Fieve", "Northwind"),
    ("fieve", "northwind"),
)
_DEMO_SITE_REWRITE_DONE = False


def _rewrite_demo_site_everywhere(db):
    """Sweep every text column in the DB once per process (see the note on
    _DEMO_SITE_REWRITES). Brute force on purpose: the strings sat in emails,
    source identifiers, org and site names, JSON metadata and message bodies,
    and a curated column list would miss one."""
    global _DEMO_SITE_REWRITE_DONE
    if _DEMO_SITE_REWRITE_DONE:
        return
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        for col in db.execute(f'PRAGMA table_info("{table}")').fetchall():
            ctype = (col[2] or "").upper()
            if ctype and not any(t in ctype for t in ("TEXT", "CHAR", "CLOB")):
                continue
            name = col[1]
            for old, new in _DEMO_SITE_REWRITES:
                try:
                    db.execute(
                        f'UPDATE "{table}" SET "{name}" = REPLACE("{name}", ?, ?) '
                        f'WHERE instr("{name}", ?) > 0', (old, new, old))
                except sqlite3.IntegrityError:
                    # e.g. a user with the new address already exists; the
                    # staff migration above reconciles those by name.
                    pass
    _DEMO_SITE_REWRITE_DONE = True


def migrate_demo_staff_identities():
    """Rewrite a previously seeded real roster to the fictional demo staff.

    Existing local/demo DBs keep users, notes, and audit rows across restarts,
    so changing seed strings alone would leave the old names on screen.
    """
    db = get_db()
    for email, name, _role, _label, old_emails in _DEMO_TEAM:
        email = email.lower().strip()
        target = db.execute(
            "SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        for old in old_emails:
            old = (old or "").lower().strip()
            if not old or old == email:
                continue
            row = db.execute(
                "SELECT id FROM users WHERE email = ?", (old,)).fetchone()
            if not row:
                continue
            if target and target["id"] != row["id"]:
                db.execute("UPDATE users SET name = ? WHERE id = ?",
                           (name, row["id"]))
            else:
                db.execute("UPDATE users SET email = ?, name = ? WHERE id = ?",
                           (email, name, row["id"]))
                target = {"id": row["id"]}
        db.execute("UPDATE users SET name = ? WHERE email = ?", (name, email))
    known = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for table, column in _DEMO_STAFF_TEXT_COLUMNS:
        if table not in known:
            continue
        for old, new in _DEMO_STAFF_REWRITES:
            db.execute(
                f"UPDATE {table} SET {column} = REPLACE({column}, ?, ?) "
                f"WHERE instr({column}, ?) > 0",
                (old, new, old))
    _rewrite_demo_site_everywhere(db)
    db.commit()


def _top_up_demo_marketing_workflows(conn, user_id, oid):
    """Add richer per-trial demo queues without touching connected live sources."""
    claimed_ncts = {row["nct"] for row in list_team_studies(user_id)}
    for nct, title in _MARKETING_DEMO_STUDIES.items():
        if nct not in claimed_ncts:
            add_study_claim(user_id, nct, title, verified=True)

    source_rows = conn.execute(
        "SELECT id, lower(identifier) AS identifier FROM marketing_sources "
        "WHERE org_id = ? AND connection_mode = 'demo'", (oid,)).fetchall()
    source_by_identifier = {
        row["identifier"]: row["id"] for row in source_rows}
    source_ids = {
        "recruit": source_by_identifier.get(_MARKETING_DEMO_SENTINEL.lower()),
        "instagram": source_by_identifier.get("@northwindclinical"),
        "ads": source_by_identifier.get("northwind migraine search"),
    }
    if not all(source_ids.values()):
        return 0

    existing_rows = conn.execute(
        "SELECT id, lower(contact_handle) AS contact_handle, nct, linked_lead_id "
        "FROM marketing_threads WHERE org_id = ?", (oid,)).fetchall()
    existing = {
        (row["contact_handle"], row["nct"] or ""): row
        for row in existing_rows
    }
    specs = [
        # Plain questions from people who saw an ad, one per pipeline stage,
        # so every scoped trial shows a working queue. No protocol jargon.
        ("instagram", "NCT07645924", "Keisha Morgan", "@keisha.migraine",
         "Evening screening calls", "outreach", 58,
         "are there any phone screening times after 5pm during the week?", None),
        ("recruit", "NCT07645924", "Peter Young", "peter.young@gmail.com",
         "Migraine screening confirmation", "screening", 470,
         "I can attend the Tuesday screening. Should I bring my migraine diary "
         "and a list of what I take?", 42),

        # Adjunctive Seltorexant with insomnia.
        ("recruit", "NCT06559306", "Camille Foster", "camille.foster@gmail.com",
         "I can't sleep, is this study for that?", "new", 12,
         "My depression has not improved and I wake up throughout the night. "
         "Is this study for that?", None),
        ("instagram", "NCT06559306", "Ethan Miller", "@ethan.restless",
         "Study visits while working nights", "outreach", 94,
         "i work overnight shifts. are screening visits only during the day?", None),
        ("ads", "NCT06559306", "Sonia Blake", "sonia.blake@gmail.com",
         "Two medications have not helped", "prescreen", 265,
         "I have tried two antidepressants and still cannot sleep. Is this "
         "study for people like me?", None),
        ("recruit", "NCT06559306", "Jamal Price", "jamal.price@outlook.com",
         "Confirming Friday screening", "screening", 640,
         "Friday afternoon works for me. Should I bring my medication bottles "
         "or just a written list?", 46),

        # Azetukalner placebo-controlled trial.
        ("ads", "NCT07076407", "Olivia Grant", "olivia.grant@gmail.com",
         "Saw the depression study ad", "new", 23,
         "I saw your ad today. How do I find out whether this study is a fit?", None),
        ("recruit", "NCT07076407", "Khalil Brown", "khalil.brown@gmail.com",
         "My doctor keeps changing my medication", "prescreen", 176,
         "My doctor has changed my antidepressant twice this year. Does that "
         "matter for the study?", None),
        ("instagram", "NCT07076407", "Mei Lin", "@mei.moves.forward",
         "Screening appointment details", "screening", 520,
         "I can make the Wednesday screening. Can you send the address and how "
         "long I should plan to be there?", 39),

        # Azetukalner open-label extension.
        ("recruit", "NCT06922110", "Victor Hale", "victor.hale@gmail.com",
         "When does the follow-on start?", "new", 33,
         "I am finishing the main study next month. When does the follow-on "
         "study normally begin?", None),
        ("instagram", "NCT06922110", "Renee Cole", "@renee.recovery",
         "Will visits stay at the same clinic?", "outreach", 150,
         "for the follow-on study do i keep the same coordinator and clinic?", None),
        ("recruit", "NCT06922110", "Nina Patel", "nina.patel@outlook.com",
         "Another checkup before the follow-on?", "prescreen", 420,
         "I completed every visit in the main study. Is there another checkup "
         "before joining the follow-on?", 48),

        # Seltorexant monotherapy trial.
        ("ads", "NCT07573176", "Luis Ortega", "luis.ortega@gmail.com",
         "Not taking an antidepressant now", "new", 16,
         "I am not currently taking an antidepressant. Can I still ask about "
         "the depression study?", None),
        ("instagram", "NCT07573176", "Farah Khan", "@farah.finds.rest",
         "Remote visit options", "outreach", 110,
         "are any of the follow-up visits by video or are they all in person?", None),
        ("recruit", "NCT07573176", "Brian Wells", "brian.wells@outlook.com",
         "How long would I take the medication?", "prescreen", 310,
         "Before I continue, how many weeks would I take the study medication?", None),
        ("recruit", "NCT07573176", "Monique Taylor", "monique.t@gmail.com",
         "Screening forms completed", "screening", 810,
         "I finished the forms you sent. Please confirm whether my screening is "
         "still scheduled for Monday morning.", 44),

        # Elismetrep long-term migraine safety trial.
        ("recruit", "NCT07674654", "Hailey Brooks", "hailey.b@gmail.com",
         "Do I need to track my migraines first?", "outreach", 72,
         "Do I need to start writing down my migraines before the first phone "
         "call?", None),
        ("ads", "NCT07674654", "Andre Silva", "andre.silva@gmail.com",
         "I take two things for migraines", "prescreen", 280,
         "I get a monthly injection and I have a pill for when a migraine hits. "
         "Is that a problem for the study?", 37),

        # Ubrogepant menstrual-migraine trial.
        ("instagram", "NCT06417775", "Lucia Moretti", "@lucia.migraine",
         "I've been tracking my period migraines", "new", 20,
         "i have tracked migraines around my cycle for six months. how do i "
         "share that with you?", None),
        ("recruit", "NCT06417775", "Erin Shaw", "erin.shaw@gmail.com",
         "Can the first call be by video?", "outreach", 125,
         "Could the first conversation happen by video before I travel to the "
         "clinic?", None),
        ("ads", "NCT06417775", "Zara Ahmed", "zara.ahmed@gmail.com",
         "Is a regular migraine diagnosis enough?", "prescreen", 360,
         "I have a migraine diagnosis and I track my cycle. Is that enough to "
         "get started?", None),
        ("recruit", "NCT06417775", "Chloe Martin", "chloe.martin@gmail.com",
         "Screening scheduled next week", "screening", 900,
         "Next Thursday works. Do I need to stop any migraine medicine before "
         "the appointment?", 35),
    ]
    replies = {
        "outreach": (
            "Thanks for reaching out. I can work through scheduling and the "
            "study-team-approved details with you here."),
        "prescreen": (
            "Thanks, I have added this to your pre-screen notes so the "
            "coordinator can review it with you directly."),
        "screening": (
            "Your screening step is noted. I will confirm the visit details and "
            "anything you need to prepare before you arrive."),
    }

    added = 0
    for (source_key, nct, contact, handle, subject, stage, minutes, body,
         applicant_age) in specs:
        key = (handle.lower(), nct)
        thread = existing.get(key)
        if thread:
            thread_id = thread["id"]
        else:
            reply = replies.get(stage)
            updated_minutes = max(1, minutes - 9) if reply else minutes
            created_at = (dt.datetime.now() - dt.timedelta(minutes=minutes)).strftime(
                "%Y-%m-%d %H:%M")
            updated_at = (
                dt.datetime.now() - dt.timedelta(minutes=updated_minutes)
            ).strftime("%Y-%m-%d %H:%M")
            cur = conn.execute(
                "INSERT INTO marketing_threads "
                "(org_id, source_id, contact_name, contact_handle, subject, "
                "status, assigned_to, unread, nct, study_label, pipeline_stage, "
                "created_at, updated_at) VALUES (?,?,?,?,?,'open',?,?,?,?,?,?,?)",
                (oid, source_ids[source_key], contact, handle, subject, user_id,
                 1 if stage == "new" else 0, nct,
                 _MARKETING_DEMO_STUDIES[nct], stage, created_at, updated_at))
            thread_id = cur.lastrowid
            conn.execute(
                "INSERT INTO marketing_messages "
                "(org_id, thread_id, kind, body, author_name, delivery_status, "
                "created_at) VALUES (?,?,?,?,?,?,?)",
                (oid, thread_id, "inbound", body, contact, "received", created_at))
            if reply:
                conn.execute(
                    "INSERT INTO marketing_messages "
                    "(org_id, thread_id, kind, body, author_user_id, author_name, "
                    "delivery_status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (oid, thread_id, "outbound", reply, user_id,
                     "BridgeMD team", "saved", updated_at))
            added += 1
            thread = {"id": thread_id, "linked_lead_id": None}
            existing[key] = thread

        if applicant_age and not thread["linked_lead_id"]:
            applicant_token = f"demo-inbox-{thread_id}"
            lead = conn.execute(
                "SELECT * FROM leads WHERE applicant_token = ? LIMIT 1",
                (applicant_token,)).fetchone()
            if not lead:
                token = create_lead({
                    "applicant_token": applicant_token,
                    "nct": nct,
                    "title": _MARKETING_DEMO_STUDIES[nct],
                    "condition": (
                        "Migraine" if nct in ("NCT07674654", "NCT06417775")
                        else "Major Depressive Disorder"),
                    "name": contact,
                    "email": handle if "@" in handle and not handle.startswith("@") else "",
                    "age": str(applicant_age),
                    "consent": 1,
                    "source": "inbox",
                    "owner_user_id": user_id,
                    "eligibility": json.dumps({
                        "verdict": "possible",
                        "met": [],
                        "unknown": [
                            "Age requirement",
                            "Current diagnosis and treatment history",
                            "Study-specific safety exclusions",
                        ],
                        "not_met": [],
                    }),
                })
                lead = get_lead_by_token(token)
            if lead:
                ts = now()
                # revealed: an applicant created from a conversation the
                # site is already having is never blinded (see
                # reveal_lead_from_inbox) - seeding them as candidate codes made
                # the demo queue look broken and un-messageable.
                conn.execute(
                    "UPDATE leads SET status = ?, records_authorized_at = ?, "
                    "revealed = 1, updated_at = ? WHERE id = ?",
                    (stage, ts, ts, lead["id"]))
                conn.execute(
                    "UPDATE marketing_threads SET linked_lead_id = ?, "
                    "pipeline_stage = ? WHERE id = ? AND org_id = ?",
                    (lead["id"], stage, thread_id, oid))
                if stage == "screening" and not list_tasks(lead["id"]):
                    add_task(lead["id"], "Confirm screening appointment",
                             assigned_to="site", created_by="site")
                    add_task(lead["id"], "Review patient-authorized records",
                             assigned_to="site", created_by="site")
                existing[key] = {"id": thread_id, "linked_lead_id": lead["id"]}

    # High-volume layer for recording demos: every scoped trial should feel like
    # a real working inbox, not four hand-picked cards. Eight assigned threads and
    # two unassigned inquiries are added per trial, with deterministic identities
    # and message histories so repeated seeding remains a no-op.
    # People describe a study the way the ad did ("the migraine study"), not
    # by its code name, and they name their own medication the way they would
    # to a friend.
    study_context = {
        "NCT05711940": ("the psilocybin study", "an antidepressant",
                        "depression"),
        "NCT07645924": ("the migraine study", "a migraine pill", "migraines"),
        "NCT06559306": ("the depression and insomnia study",
                        "an antidepressant", "depression"),
        "NCT07076407": ("the depression study", "sertraline", "depression"),
        "NCT06922110": ("the follow-on depression study",
                        "the study medication", "depression"),
        "NCT07573176": ("the depression study", "a sleep medication",
                        "sleep"),
        "NCT07674654": ("the long-term migraine study", "a daily preventive",
                        "migraines"),
        "NCT06417775": ("the period migraine study", "a migraine pill",
                        "migraines"),
    }
    question_templates = [
        ("Would my medication be a problem?",
         "I take {medication} for {condition}. Is that a problem for {study}?"),
        ("How many visits are there?",
         "How many visits are there for {study}, and roughly how long is each "
         "one?"),
        ("Do I get paid?",
         "Do you pay people for taking part, and when do you pay?"),
        ("Help with travel or parking?",
         "I live about 45 minutes away. Is there any help with gas, transit, "
         "or parking for {study}?"),
        ("Can the first call be by phone?",
         "Can the first conversation happen by phone or video before I travel "
         "in?"),
        ("Who can see my information?",
         "Who sees the information I share, and is it kept private?"),
        ("Do I need a referral from my doctor?",
         "Do I need a referral or records from my doctor before asking about "
         "{study}, or can I just contact you?"),
        ("Evening or weekend appointments?",
         "I work weekdays until 5. Are there any evening or weekend "
         "appointments?"),
        ("Can a support person join?",
         "Can a family member or friend come with me to the first call and "
         "to visits?"),
        ("What happens after I apply?",
         "I just sent in my information for {study}. What is the next step, "
         "and when will I hear from someone?"),
    ]
    first_names = [
        "Amara", "Theo", "Jasmine", "Caleb", "Elena", "Miles", "Naomi",
        "Owen", "Imani", "Lucas", "Maya", "Adrian", "Talia", "Jonah",
        "Serena", "Felix", "Kiara", "Nolan", "Layla", "Marcus",
    ]
    last_names = [
        "Bennett", "Okafor", "Chen", "Alvarez", "Morgan", "Singh",
        "Thompson", "Rivera", "Brooks", "Patel", "Nguyen", "Carter",
        "Williams", "Foster", "Kim", "Robinson", "Shah", "Martinez",
        "Cooper", "Davis",
    ]
    volume_stages = [
        "new", "new", "outreach", "outreach", "prescreen", "prescreen",
        "screening", "screening", "new", "new",
    ]
    for study_index, (nct, title) in enumerate(_MARKETING_DEMO_STUDIES.items()):
        study_name, medication, condition = study_context[nct]
        for item_index, (subject, body_template) in enumerate(question_templates):
            person_index = study_index * len(question_templates) + item_index
            first = first_names[person_index % len(first_names)]
            last_offset = ((person_index // len(first_names)) * 5
                           + person_index % 5) % len(last_names)
            last = last_names[last_offset]
            contact = f"{first} {last}"
            source_key = ("recruit", "instagram", "ads")[item_index % 3]
            slug = f"{first}.{last}".lower()
            handle = (f"@demo.{slug}.{study_index + 1}"
                      if source_key == "instagram"
                      else f"{slug}.{study_index + 1}@example.com")
            key = (handle.lower(), nct)
            if key in existing:
                continue

            stage = volume_stages[item_index]
            assigned_to = user_id if item_index < 8 else None
            minutes = 50 + study_index * 240 + item_index * 19
            body = body_template.format(
                medication=medication, condition=condition, study=study_name)
            if source_key == "ads":
                body = "I found this through your online ad. " + body
            elif source_key == "instagram":
                body = "Hi, I found your study page on Instagram. " + body

            created_at = (
                dt.datetime.now() - dt.timedelta(minutes=minutes)
            ).strftime("%Y-%m-%d %H:%M")
            message_rows = [
                ("inbound", body, None, contact, "received", created_at),
            ]
            if assigned_to and stage != "new":
                reply_at = (
                    dt.datetime.now() - dt.timedelta(minutes=minutes - 7)
                ).strftime("%Y-%m-%d %H:%M")
                message_rows.append((
                    "outbound", replies[stage], user_id, "BridgeMD team",
                    "saved", reply_at))
            if assigned_to and stage in ("prescreen", "screening"):
                followup_at = (
                    dt.datetime.now() - dt.timedelta(minutes=minutes - 13)
                ).strftime("%Y-%m-%d %H:%M")
                followup = (
                    "That works for me. I can take a coordinator call tomorrow "
                    "afternoon." if stage == "prescreen" else
                    "Thank you, I have the details and will be ready for the "
                    "screening appointment.")
                message_rows.append((
                    "inbound", followup, None, contact, "received", followup_at))
            if assigned_to and stage == "screening":
                note_at = (
                    dt.datetime.now() - dt.timedelta(minutes=minutes - 16)
                ).strftime("%Y-%m-%d %H:%M")
                message_rows.append((
                    "note", "Screening logistics confirmed; verify the current "
                    "medication list at the visit.", user_id, "BridgeMD team", "",
                    note_at))

            updated_at = message_rows[-1][5]
            cur = conn.execute(
                "INSERT INTO marketing_threads "
                "(org_id, source_id, contact_name, contact_handle, subject, "
                "status, assigned_to, unread, nct, study_label, pipeline_stage, "
                "created_at, updated_at) VALUES (?,?,?,?,?,'open',?,?,?,?,?,?,?)",
                (oid, source_ids[source_key], contact, handle, subject,
                 assigned_to, 1 if item_index in (0, 8, 9) else 0, nct, title,
                 stage, created_at, updated_at))
            thread_id = cur.lastrowid
            for (kind, message_body, author_user_id, author_name,
                 delivery_status, created) in message_rows:
                conn.execute(
                    "INSERT INTO marketing_messages "
                    "(org_id, thread_id, kind, body, author_user_id, author_name, "
                    "delivery_status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (oid, thread_id, kind, message_body, author_user_id,
                     author_name, delivery_status, created))
            existing[key] = {"id": thread_id, "linked_lead_id": None}
            added += 1
    conn.commit()
    return added


def _purge_demo_leads(conn, lead_ids):
    """Delete demo applicants and everything that hangs off them, in foreign
    key order. The refresh used to delete tasks and events by hand and then the
    leads, which failed with a FOREIGN KEY error the moment a demo applicant had
    been clicked around (attachments, a portal, a visit). Walking the schema's
    own foreign keys means a new table can never reintroduce that. Inbox threads
    that point at a purged lead are unlinked rather than deleted, since some sit
    on live sources the refresh must not touch."""
    if not lead_ids:
        return
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()]
    children = {}
    for t in tables:
        for fk in conn.execute(f"PRAGMA foreign_key_list({t})").fetchall():
            children.setdefault(fk[2], []).append((t, fk[3]))

    def purge(table, ids, depth=0):
        if not ids or depth > 6:
            return
        for i in range(0, len(ids), 400):
            chunk = list(ids[i:i + 400])
            ph = ",".join("?" * len(chunk))
            for child, col in children.get(table, []):
                if child == "marketing_threads":
                    conn.execute(f"UPDATE marketing_threads SET {col} = NULL "
                                 f"WHERE {col} IN ({ph})", chunk)
                    continue
                cols = [c[1] for c in conn.execute(
                    f"PRAGMA table_info({child})").fetchall()]
                if "id" in cols and child != table:
                    sub = [r[0] for r in conn.execute(
                        f"SELECT id FROM {child} WHERE {col} IN ({ph})",
                        chunk).fetchall()]
                    purge(child, sub, depth + 1)
                conn.execute(f"DELETE FROM {child} WHERE {col} IN ({ph})", chunk)
            conn.execute(f"DELETE FROM {table} WHERE id IN ({ph})", chunk)

    purge("leads", list(lead_ids))


def seed_demo_marketing_hub(user_id):
    """Populate the demo account with a realistic, multi-account marketing inbox.

    Idempotent: seeds once (detected via a sentinel source) so a coordinator can
    click around, reply, and reassign during a live clinic demo without the data
    resetting under them. Missing workflow examples are topped up individually.

    Deliberately gated to the throwaway demo account ONLY: the seeder assumes the
    demo org and refreshes only demo-mode sources, so it is NOT safe to point at
    an arbitrary account. Live integrations in the shared org are never touched.
    A fresh real account correctly gets an empty inbox to connect its own channels."""
    if not user_id:
        return
    conn = get_db()
    demo_user = conn.execute(
        "SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    if not demo_user or (demo_user["email"] or "").lower() != _MARKETING_DEMO_EMAIL:
        return
    oid = user_org_id(user_id)
    seeded = conn.execute(
        "SELECT 1 FROM marketing_sources WHERE org_id = ? "
        "AND lower(identifier) = lower(?) LIMIT 1",
        (oid, _MARKETING_DEMO_SENTINEL)).fetchone()
    # Re-seed once for orgs seeded before conversations were tagged to trials,
    # so switching the top study scoper shows a different set of people.
    tagged = conn.execute(
        "SELECT 1 FROM marketing_threads WHERE org_id = ? AND nct != '' LIMIT 1",
        (oid,)).fetchone()
    linked = conn.execute(
        "SELECT 1 FROM marketing_threads WHERE org_id = ? "
        "AND linked_lead_id IS NOT NULL LIMIT 1", (oid,)).fetchone()
    ad_lead = conn.execute(
        "SELECT 1 FROM marketing_threads t "
        "JOIN marketing_sources s ON s.id = t.source_id "
        "WHERE t.org_id = ? AND s.channel = 'google_ads' "
        "AND t.contact_handle != 'Automated alert' LIMIT 1", (oid,)).fetchone()
    # Re-seed until every demo NCT has conversations the coordinator owns.
    # Older seeds tagged a couple of trials (or assigned them to teammates), so
    # switching the study scoper landed on an empty "My inbox".
    _DEMO_NCTS = (
        "NCT05711940", "NCT07645924", "NCT06559306", "NCT07076407",
        "NCT06922110", "NCT07573176", "NCT07674654", "NCT06417775",
    )
    # "Mine" has to include whoever is covering for this user. Handing your queue
    # to a teammate legitimately empties your own inbox, and without this the
    # health check below would read that as a broken seed and refresh the demo -
    # silently pulling every thread back and undoing the handoff on next load.
    owners = [user_id]
    _away = active_away_for(user_id)
    if _away:
        owners.append(_away["cover_user_id"])
    mine_covered = conn.execute(
        "SELECT COUNT(DISTINCT nct) FROM marketing_threads "
        "WHERE org_id = ? AND assigned_to IN ({}) AND nct IN ({})".format(
            ",".join("?" * len(owners)), ",".join("?" * len(_DEMO_NCTS))),
        (oid, *owners, *_DEMO_NCTS)).fetchone()[0]
    # Older seeds put protocol jargon in applicants' mouths (MADRS cutoffs,
    # washouts, open-label extensions). Nobody who clicked an ad writes that,
    # so a database still carrying those threads is refreshed once.
    jargon = conn.execute(
        "SELECT 1 FROM marketing_threads WHERE org_id = ? AND ("
        "subject LIKE '%MADRS%' OR subject LIKE '%washout%' OR "
        "subject LIKE '%monotherapy%' OR subject LIKE '%OLE%' OR "
        "subject LIKE '%Azetukalner%' OR subject LIKE '%K-304%') LIMIT 1",
        (oid,)).fetchone()
    if (seeded and tagged and linked and ad_lead and not jargon
            and mine_covered >= len(_DEMO_NCTS)):
        _top_up_demo_marketing_workflows(conn, user_id, oid)
        get_marketing_handoff(user_id)
        return

    # Refresh only synthetic sources. The public demo user can share an org with
    # signed-in teammates, so a demo refresh must never touch their live accounts.
    demo_source_ids = [row["id"] for row in conn.execute(
        "SELECT id FROM marketing_sources WHERE org_id = ? "
        "AND connection_mode != 'live'", (oid,)).fetchall()]
    if demo_source_ids:
        source_placeholders = ",".join("?" for _ in demo_source_ids)
        conn.execute(
            "DELETE FROM marketing_messages WHERE thread_id IN "
            f"(SELECT id FROM marketing_threads WHERE source_id IN "
            f"({source_placeholders}))", demo_source_ids)
        conn.execute(
            f"DELETE FROM marketing_threads WHERE source_id IN "
            f"({source_placeholders})", demo_source_ids)
    demo_lead_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM leads WHERE applicant_token LIKE 'demo-inbox-%' "
            "AND owner_user_id IN (SELECT user_id FROM memberships "
            "WHERE org_id = ?)", (oid,)).fetchall()
    ]
    if demo_lead_ids:
        conn.execute("DELETE FROM records_profiles WHERE applicant_token "
                     "LIKE 'demo-inbox-%'")
        _purge_demo_leads(conn, demo_lead_ids)
    # Delete demo connection shells before their sources to preserve FK ordering.
    if demo_source_ids:
        source_placeholders = ",".join("?" for _ in demo_source_ids)
        conn.execute(
            f"DELETE FROM marketing_connections WHERE source_id IN "
            f"({source_placeholders})", demo_source_ids)
        conn.execute(
            f"DELETE FROM marketing_sources WHERE id IN ({source_placeholders})",
            demo_source_ids)

    ts = now()

    def _demo_member(email, name):
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            uid = row["id"]
            conn.execute("UPDATE users SET org_id = ? WHERE id = ?", (oid, uid))
        else:
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, name, verified, "
                "verified_at, created_at, org_id) VALUES (?,?,?,?,?,?,?)",
                (email, "demo-login-disabled", name, 1, ts, ts, oid))
            uid = cur.lastrowid
        conn.execute("DELETE FROM memberships WHERE user_id = ? AND org_id != ?",
                     (uid, oid))
        conn.execute(
            "INSERT OR IGNORE INTO memberships "
            "(org_id, user_id, role, role_label, created_at) VALUES (?,?,?,?,?)",
            (oid, uid, "student", "Marketing teammate", ts))
        return uid

    jordan_id = _demo_member("jordan.lee@northwindclinical.com", "Jordan Lee")
    casey_id = _demo_member("casey.morgan@northwindclinical.com", "Casey Morgan")

    def _source(channel, label, identifier, status="connected"):
        cur = conn.execute(
            "INSERT INTO marketing_sources "
            "(org_id, channel, label, identifier, connection_mode, status, "
            "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (oid, channel, label, identifier, "demo", status, user_id, ts, ts))
        return cur.lastrowid

    # Multiple accounts across channels - what a real recruiting team juggles.
    # Study-specific inboxes/ad accounts so switching trials also changes the
    # "via" line, not just the people.
    recruit_id = _source("email", "Recruitment inbox", _MARKETING_DEMO_SENTINEL)
    migraine_id = _source("email", "Migraine study inbox",
                          "migraine@northwindclinical.com")
    depression_id = _source("email", "Depression study inbox",
                            "depression@northwindclinical.com")
    ole_id = _source("email", "Azetukalner OLE inbox",
                     "xnova-ole@northwindclinical.com")
    menstrual_id = _source("email", "Menstrual migraine inbox",
                           "cycle@northwindclinical.com")
    instagram_id = _source("instagram", "Instagram DMs", "@northwindclinical")
    ads_id = _source("google_ads", "Google Ads: Migraine Search",
                     "Northwind Migraine Search")
    dep_ads_id = _source("google_ads", "Google Ads: Depression Search",
                         "Northwind Depression Search")
    # One disconnected account so the connect/reconnect state is visible.
    _source("email", "Newsletter replies", "news@northwindclinical.com",
            status="disconnected")

    def _ago(minutes):
        return (dt.datetime.now() - dt.timedelta(minutes=minutes)).strftime(
            "%Y-%m-%d %H:%M")

    def _seq(source_id, contact, handle, subject, assignee, status, unread,
             messages, study=("", ""), stage=None):
        """messages: list of (kind, by, body, minutes_ago).
        kind: inbound|outbound|note ; by: 'contact'|'system'|<user_id>.
        study: (nct, label) so switching the top trial scopes the inbox.
        stage: recruitment pipeline stage for the thread's stage tab/pill
        (defaults to the schema default 'new' when omitted)."""
        nct, study_label = study
        mins = [m[3] for m in messages]
        created, updated = _ago(max(mins)), _ago(min(mins))
        if stage:
            cur = conn.execute(
                "INSERT INTO marketing_threads "
                "(org_id, source_id, contact_name, contact_handle, subject, status, "
                "assigned_to, unread, nct, study_label, pipeline_stage, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, source_id, contact, handle, subject, status, assignee,
                 1 if unread else 0, nct, study_label, stage, created, updated))
        else:
            cur = conn.execute(
                "INSERT INTO marketing_threads "
                "(org_id, source_id, contact_name, contact_handle, subject, status, "
                "assigned_to, unread, nct, study_label, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, source_id, contact, handle, subject, status, assignee,
                 1 if unread else 0, nct, study_label, created, updated))
        tid = cur.lastrowid
        for kind, by, body, minutes in messages:
            if kind == "inbound":
                author_uid, author_name, delivery = None, contact, "received"
            elif kind == "note":
                author_uid = by if isinstance(by, int) else None
                author_name, delivery = "BridgeMD team", ""
            else:  # outbound
                author_uid = by if isinstance(by, int) else None
                author_name, delivery = "BridgeMD team", "saved"
            conn.execute(
                "INSERT INTO marketing_messages "
                "(org_id, thread_id, kind, body, author_user_id, author_name, "
                "delivery_status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (oid, tid, kind, body, author_uid, author_name, delivery,
                 _ago(minutes)))
        return tid

    me = user_id

    # Tie conversations to real claimed trials so the top switcher scopes the
    # inbox - each trial reads as its own set of people. Ensure demo studies
    # exist first, then match by theme (with round-robin fallbacks).
    try:
        seed_demo_leads()
    except Exception:
        pass
    seed_demo_claims(user_id)
    claims = list_team_studies(user_id)
    _by_nct = {c["nct"]: (c["nct"], c["title"] or c["nct"]) for c in claims}
    # Canonical titles (same as seed_demo_leads' _S). Always tag threads with
    # these NCTs even if claims/leads haven't landed yet, otherwise the
    # study switcher scopes to an empty inbox.
    _TITLES = {
        "NCT05711940": "COMP360 Psilocybin in Treatment-Resistant Depression",
        "NCT07645924": "Elismetrep (K-304) for the Acute Treatment of Migraine",
        "NCT06559306": "Adjunctive Seltorexant in MDD With Insomnia Symptoms",
        "NCT07076407": "Azetukalner vs Placebo in Major Depressive Disorder (X-NOVA3)",
        "NCT06922110": "Azetukalner Open-Label Extension in Major Depressive Disorder (X-NOVA-OLE)",
        "NCT07573176": "Seltorexant Monotherapy in Major Depressive Disorder",
        "NCT07674654": "Elismetrep (K-304) Long-Term Safety in Acute Migraine",
        "NCT06417775": "Ubrogepant for Menstrual Migraine",
    }

    # The demo site's 8 active trials (see seed_demo_leads' _S map) are looked up by
    # exact NCT rather than fuzzy title keywords - several titles share words
    # like "MDD" and "Azetukalner", and a fuzzy match silently left some trials
    # with zero conversations whenever the top switcher scoped to them.
    def _study(nct):
        return _by_nct.get(nct) or (nct, _MARKETING_DEMO_STUDIES[nct])

    psi = _study("NCT05711940")  # COMP360 Psilocybin in TRD
    mig = _study("NCT07645924")  # Elismetrep (K-304) acute migraine
    mdd = _study("NCT06559306")  # Adjunctive Seltorexant in MDD w/ insomnia
    aze = _study("NCT07076407")  # Azetukalner vs Placebo in MDD (X-NOVA3)
    azeo = _study("NCT06922110")  # Azetukalner Open-Label Extension
    selm = _study("NCT07573176")  # Seltorexant Monotherapy in MDD
    migl = _study("NCT07674654")  # Elismetrep long-term safety
    umm = _study("NCT06417775")  # Ubrogepant for Menstrual Migraine

    # Every thread below reads like what actually lands in a recruiting inbox:
    # a person who saw the ad on Instagram, Google or Facebook and has an
    # ordinary question (is it still open, do I get paid, how many visits, can
    # I keep taking my medication, is my information private). No protocol
    # jargon in their mouths: nobody who clicked an ad asks about MADRS
    # cutoffs, washouts or open-label extensions. Every claimed trial gets its
    # own people, channels and questions so the study switcher never lands on
    # a cloned or empty "My inbox".
    who = {"me": me, "jordan": jordan_id, "casey": casey_id}

    def _t(src, contact, handle, subject, owner, status, unread, messages,
           study, stage=None):
        return _seq(src, contact, handle, subject, who.get(owner), status,
                    unread, messages, study=study, stage=stage)

    # ── COMP360 psilocybin in treatment-resistant depression ─────────────
    _t(recruit_id, "Emily Carter", _MARKETING_DEMO_WORKFLOW_SENTINEL,
       "Can I stay on my antidepressant?", "me", "open", True,
       [("inbound", "contact",
         "I saw your ad for the psilocybin depression study. I take an "
         "antidepressant right now. Do I have to stop it to take part, or is "
         "that something the study doctor looks at?", 4)], psi, "new")
    daniel_thread = _t(recruit_id, "Daniel Cho", "daniel.cho@outlook.com",
       "Screening visit confirmation", "me", "open", False,
       [("inbound", "contact",
         "Tuesday at 10 works for my screening visit. Is there anything I "
         "should bring with me?", 214),
        ("outbound", me,
         "Tuesday at 10 is confirmed. Please bring photo ID and a list of what "
         "you take. I will send the clinic directions separately.", 198)],
       psi, "screening")
    rob_thread = _t(instagram_id, "Rob Torres", "@rob.torres.tx",
       "Do I need a referral?", "me", "open", False,
       [("inbound", "contact",
         "do i need a referral from my psychiatrist to join the psilocybin "
         "study or can i just apply?", 330),
        ("outbound", me,
         "You can apply directly, no referral needed. I'll send a quick link "
         "to check the basics. 👍", 322)], psi, "outreach")
    aisha_thread = _t(ads_id, "Aisha Rahman", "aisha.rahman@gmail.com",
       "Interested in the depression study", "me", "open", False,
       [("inbound", "contact",
         "I found the study through Google. I am 32 and I have tried two "
         "antidepressants that did not help much. What happens next?", 82),
        ("outbound", me,
         "Thanks for reaching out, Aisha. The next step is a short, private "
         "pre-screen so the coordinator can go over the basics with you.", 68),
        ("inbound", "contact",
         "That sounds good. I am free after 3 tomorrow if someone can call.",
         49)], psi, "prescreen")
    _t(depression_id, "Mei Lin", "mei.lin@gmail.com",
       "How much time does it take?", "me", "open", True,
       [("inbound", "contact",
         "I saw the psilocybin study ad. How many visits are there and how "
         "long is each one? I would need to take time off work.", 22)], psi)
    _t(recruit_id, "Dr. Aaron Cole", "acole@lenoxhillpsych.com",
       "Referring a patient", "me", "open", True,
       [("inbound", "contact",
         "I have a patient with depression who has not done well on several "
         "medications and is interested in your psilocybin study. What is "
         "the best way to refer her?", 110)], psi)
    _t(depression_id, "Jonah Hale", "jonah.hale@outlook.com",
       "Time off work for the treatment day", "me", "open", False,
       [("inbound", "contact",
         "How long is the day you take the medication, and do I need someone "
         "to drive me home?", 200),
        ("outbound", me,
         "Plan on a full supervised day, and yes, someone needs to pick you "
         "up. I can send the day-of schedule.", 188)], psi, "prescreen")
    _t(recruit_id, "Priya Anand", "priya.anand@gmail.com",
       "Do I get paid?", "casey", "resolved", False,
       [("inbound", "contact",
         "Do you pay people for taking part, and when?", 1520),
        ("outbound", casey_id,
         "Hi Priya, participants receive up to $1,200 across the study, paid "
         "per completed visit. I emailed the full schedule. Let me know if "
         "you'd like to book screening!", 1505)], psi)
    _t(instagram_id, "Kit R.", "@kit.asks",
       "Is this actual psilocybin or a placebo?", None, "open", True,
       [("inbound", "contact",
         "is this real psilocybin? i'm nervous about a trip and want to know "
         "who is in the room with me.", 125)], psi)
    _t(instagram_id, "Noah Williams", "@noah.wellness",
       "How private is the first call?", None, "open", True,
       [("inbound", "contact",
         "i want to learn more but do not want my employer or family "
         "contacted. is the first call confidential?", 18)], psi, "new")

    # ── Elismetrep (K-304) acute migraine ────────────────────────────────
    _t(recruit_id, "Nadia Brooks", "nadia.brooks@gmail.com",
       "Do you have evening appointments?", "me", "open", True,
       [("inbound", "contact",
         "Hi, I saw your migraine study ad. I work until 5 most days, so are "
         "evening appointments possible? Also, is parking covered when I come "
         "in?", 6)], mig)
    _t(recruit_id, "Dr. Sam Patel", "spatel@riversidefamilymed.com",
       "Referring a patient with frequent migraines", "me", "open", True,
       [("inbound", "contact",
         "I have a patient with 8 to 10 migraines a month who asked about "
         "your study. What's the best way to refer, and can you send a "
         "one-pager my front desk can hand out?", 96),
        ("note", me,
         "@Casey can you send the approved clinic one-pager and set up a warm "
         "handoff?", 88)], mig)
    lauren_thread = _t(migraine_id, "Lauren Fitzgerald", "lauren.f@yahoo.com",
       "I get migraines most weeks, can I join?", "me", "open", True,
       [("inbound", "contact",
         "I get migraines about 16 days a month and I'm 34. Would I qualify "
         "for the migraine study?", 19)], mig)
    _t(ads_id, "Google Ads", "Automated alert",
       "Ad disapproved: landing page policy", "me", "open", True,
       [("inbound", "system",
         "One ad in 'Migraine Search' was disapproved for a landing page "
         "policy issue. Affected ad is not serving. Review and resubmit to "
         "resume delivery.", 9)], mig)
    _t(ads_id, "Ben Walsh", "ben.walsh@outlook.com",
       "How many visits, and is travel covered?", "me", "open", False,
       [("inbound", "contact",
         "I saw the migraine ad. How many clinic visits are involved, and is "
         "there any help with travel costs?", 180),
        ("outbound", me,
         "Hi Ben, I can send the study-team-approved visit schedule and "
         "reimbursement details. What is the best email for you?", 168)],
       mig, "outreach")
    tomas_thread = _t(migraine_id, "Tomás Rivera", "trivera@gmail.com",
       "Need to move my screening visit", "jordan", "open", False,
       [("inbound", "contact",
         "Something came up at work, can I move my Thursday screening visit "
         "to next week?", 215),
        ("outbound", jordan_id,
         "No problem, Tomás. Tuesday 10:00am or Wednesday 2:00pm, which "
         "works better?", 205),
        ("inbound", "contact", "Tuesday 10am is perfect, thank you!", 150)],
       mig, "screening")
    _t(instagram_id, "Alicia Vance", "@mig_warrior", "New DM", "casey",
       "open", True,
       [("inbound", "contact",
         "saw your ad on my feed 🙌 how do i sign up for the migraine study??",
         13)], mig)
    _t(ads_id, "Maya Chen", "maya.chen@gmail.com",
       "I take a daily preventive, can I still join?", None, "open", True,
       [("inbound", "contact",
         "I clicked your Google ad for the migraine study. I get 10 to 12 "
         "migraine days a month and take a daily preventive pill. Could I "
         "still be eligible?", 58)], mig)

    # ── Adjunctive Seltorexant in MDD with insomnia ──────────────────────
    helen_thread = _t(depression_id, "Helen Park", "helen.park@gmail.com",
       "Still can't sleep on my antidepressant", "me", "open", True,
       [("inbound", "contact",
         "I'm on an antidepressant and my mood is a bit better, but I still "
         "lie awake most nights. Is this study for people like me?", 12)],
       mdd)
    _t(depression_id, "Omar Diallo", "omar.diallo@outlook.com",
       "My psychiatrist said to ask about this", "me", "open", True,
       [("inbound", "contact",
         "My psychiatrist said I might fit the depression and insomnia "
         "study. Do I need anything from him before I call you, or can I "
         "just book?", 34)], mdd, "prescreen")
    _t(recruit_id, "Dr. Lila Shah", "lshah@hudsonpsych.com",
       "Referring a patient", "me", "open", True,
       [("inbound", "contact",
         "I have a 47-year-old patient with depression who still sleeps "
         "badly on her current medication. Can you take a referral this "
         "week?", 88)], mdd)
    _t(dep_ads_id, "Keisha Boone", "keisha.boone@icloud.com",
       "Do I stay on my antidepressant?", "me", "open", False,
       [("inbound", "contact",
         "Clicked the insomnia and depression ad. If I join, do I keep "
         "taking what I take now?", 140),
        ("outbound", me,
         "Good question, Keisha. This study is for people who stay on their "
         "current antidepressant. Screening confirms the details. I can send "
         "the pre-screen if you want to continue.", 128)], mdd, "outreach")
    _t(recruit_id, "Marcus Reed", "marcus.reed@outlook.com",
       "Is travel reimbursed?", "jordan", "open", False,
       [("inbound", "contact",
         "I'd have to drive about 45 minutes each way. Is mileage or travel "
         "reimbursed for the visits?", 52),
        ("outbound", jordan_id,
         "Hi Marcus, yes, we reimburse travel for every completed visit, and "
         "it's paid the same week. Want me to hold a screening slot for you?",
         41),
        ("note", casey_id,
         "He's a strong fit, flagging so we prioritize the callback.", 39)],
       mdd)
    _t(instagram_id, "Sam W.", "@skeptical_sam", "Is this real?", None,
       "open", True,
       [("inbound", "contact",
         "is this legit or a scam lol. how do i know my info is safe?", 145)],
       mdd)

    # ── Azetukalner vs Placebo (X-NOVA3) ─────────────────────────────────
    aze_thread = _t(depression_id, "Denise Okafor", "denise.okafor@gmail.com",
       "On an antidepressant, can I still apply?", "me", "open", True,
       [("inbound", "contact",
         "I have depression and I'm on an antidepressant but it's not "
         "helping much. Can I still apply, or does being on medication rule "
         "me out?", 24)], aze)
    _t(depression_id, "Victor Lang", "victor.lang@gmail.com",
       "How bad does it have to be?", "me", "open", True,
       [("inbound", "contact",
         "I'm having a rough month but I don't know if it counts as bad "
         "enough for a study. Can I still come in and talk to someone?",
         47)], aze, "prescreen")
    _t(dep_ads_id, "Amira Soltani", "amira.soltani@yahoo.com",
       "Two medications have not helped", "me", "open", False,
       [("inbound", "contact",
         "I have tried two different antidepressants this year and neither "
         "helped much. Is this study for people like me?", 170),
        ("outbound", me,
         "That is exactly the kind of history we go over at screening, "
         "Amira. I can send the secure pre-screen link.", 158)], aze,
       "outreach")
    _t(depression_id, "Robert Klein", "rklein@protonmail.com",
       "Follow-up after my phone call", "jordan", "open", False,
       [("inbound", "contact",
         "Following up on last week's phone call, any update on an in-person "
         "visit date?", 260),
        ("outbound", jordan_id,
         "You're through the phone screen. Monday 9am or Wednesday 1pm?",
         248)], aze, "outreach")
    _t(instagram_id, "Priya N.", "@priya.n.writes",
       "Do I need my psychiatrist to refer me?", "casey", "open", False,
       [("inbound", "contact",
         "saw the depression study ad, do i need a referral from my "
         "psychiatrist or can i sign up myself?", 410)], aze)
    _t(dep_ads_id, "Chris Nguyen", "chris.nguyen@gmail.com",
       "Saw the ad, still enrolling?", None, "open", True,
       [("inbound", "contact",
         "Clicked your depression study ad. Are you still taking people in "
         "New York?", 70)], aze)

    # ── Azetukalner open-label extension ─────────────────────────────────
    grace_ole = _t(ole_id, "Grace Kim", "grace.kim@icloud.com",
       "Can I keep going after the study ends?", "me", "open", True,
       [("inbound", "contact",
         "I just finished the 12 weeks. My coordinator mentioned there is a "
         "follow-on study. Can I join it, and when would it start?", 40),
        ("outbound", me,
         "Hi Grace, everyone who completes the study can continue into the "
         "follow-on. I'll confirm your dates and send the visit schedule "
         "today.", 31)], azeo, "screening")
    _t(ole_id, "James Whitaker", "j.whitaker@gmail.com",
       "When does the next part start?", "me", "open", True,
       [("inbound", "contact",
         "My last visit was Friday. Is there a break before the follow-on "
         "study, or do I come straight back?", 55)], azeo)
    _t(ole_id, "Elena Cruz", "elena.cruz@outlook.com",
       "I think I was on the placebo", "me", "open", False,
       [("inbound", "contact",
         "I think I was on placebo in the first study. Would I get the real "
         "medication in the follow-on?", 210),
        ("outbound", me,
         "Everyone who completed the first study gets the active medication "
         "in the follow-on, including people who were on placebo. I'll send "
         "the consent form so you can read the details.", 198)],
       azeo, "prescreen")
    _t(ole_id, "David Okonkwo", "d.okonkwo@yahoo.com",
       "Questions before I sign", "casey", "resolved", False,
       [("inbound", "contact",
         "Got the consent form. Two questions before I sign, can someone "
         "call me?", 1980),
        ("outbound", casey_id,
         "Of course. I'll call this afternoon and walk through it with you.",
         1965)], azeo, "enrolled")
    _t(instagram_id, "Leah P.", "@leah.p.notes",
       "Is this only for people already in the study?", None, "open", True,
       [("inbound", "contact",
         "can new patients join the follow-on study or is it only if you "
         "already did the first one?", 160)], azeo)

    # ── Seltorexant monotherapy in MDD ───────────────────────────────────
    wanda_thread = _t(depression_id, "Wanda Price", "wanda.price@yahoo.com",
       "Not on any medication right now", "me", "open", True,
       [("inbound", "contact",
         "I stopped my antidepressant a few months ago and haven't "
         "restarted. Is the depression study still an option for me, or do "
         "I need to be on something already?", 18)], selm)
    _t(depression_id, "Theo March", "theo.march@gmail.com",
       "Which depression study is for me?", "me", "open", True,
       [("inbound", "contact",
         "I saw two depression studies on your page. I'm not taking "
         "anything right now. Which one should I ask about?", 42)],
       selm, "prescreen")
    _t(dep_ads_id, "Anika Bose", "anika.bose@icloud.com",
       "Off medication for a few months", "me", "open", False,
       [("inbound", "contact",
         "Clicked the depression ad. I've been off antidepressants for "
         "about three months. Does that matter?", 190),
        ("outbound", me,
         "Thanks, Anika. Timing like that is part of what screening looks "
         "at. I can send the pre-screen link.", 176)], selm, "outreach")
    _t(instagram_id, "@calm_and_c", "@calm_and_c",
       "How many weeks is the study?", "jordan", "open", False,
       [("inbound", "contact",
         "how many weeks total is the depression study and how many visits "
         "are in person vs phone?", 320),
        ("outbound", jordan_id,
         "It's 8 weeks: 2 in-person (screening and a first visit), the rest "
         "are short phone or video check-ins.", 305)], selm, "outreach")
    _t(dep_ads_id, "Riley Cho", "riley.cho@gmail.com",
       "Saw your ad, still taking people?", None, "open", True,
       [("inbound", "contact",
         "Saw your ad for the depression study. Are you still taking people "
         "who aren't on medication?", 80)], selm)

    # ── Elismetrep long-term safety ──────────────────────────────────────
    isla_thread = _t(migraine_id, "Isla Thompson", "isla.thompson@gmail.com",
       "Can I join the longer study?", "me", "open", True,
       [("inbound", "contact",
         "I was in the short migraine study in the spring. Is the longer "
         "one something I can join, or is it only for new patients?", 95)],
       migl, "screening")
    _t(ads_id, "Colin Deb", "colin.deb@icloud.com",
       "Bad reaction to a migraine pill before", "me", "open", False,
       [("inbound", "contact",
         "Saw the ad for the long-term migraine study. I had a bad reaction "
         "to a migraine pill a few years back. Does that matter?", 500)],
       migl)
    _t(migraine_id, "Hannah Ruiz", "hannah.ruiz@gmail.com",
       "How long does it go on?", "me", "open", True,
       [("inbound", "contact",
         "If I join the long-term migraine study, how many months is it and "
         "how often do I come in?", 28)], migl, "prescreen")
    _t(migraine_id, "Peter Lang", "peter.lang@yahoo.com",
       "Can I still take my usual migraine pills?", "jordan", "open",
       False,
       [("inbound", "contact",
         "During the study, am I allowed to take my usual pill if a "
         "migraine doesn't go away?", 240)], migl, "outreach")
    _t(ads_id, "Nora Ellis", "nora.ellis@gmail.com",
       "Long-term migraine ad, still open?", None, "open", True,
       [("inbound", "contact",
         "I wasn't in the first migraine study. Can new patients join the "
         "long-term one?", 90)], migl)

    # ── Ubrogepant for menstrual migraine ────────────────────────────────
    nadia_umm = _t(menstrual_id, "Nadia Haddad", "nadia.haddad@gmail.com",
       "My migraines come with my period", "me", "open", True,
       [("inbound", "contact",
         "My migraines are tied to my period. Does it matter which day of "
         "my cycle I come in for the first visit?", 20),
        ("outbound", me,
         "Good question. We time the first visit around your expected next "
         "cycle. What's your usual cycle length?", 14)], umm, "prescreen")
    _t(menstrual_id, "Camille Roy", "camille.roy@icloud.com",
       "Can I stay on my birth control?", "me", "open", True,
       [("inbound", "contact",
         "I'm on the pill. Does that rule me out of the period migraine "
         "study?", 36)], umm)
    _t(instagram_id, "Tara O.", "@tara.okonkwo",
       "Do I need a diagnosis first?", "me", "open", False,
       [("inbound", "contact",
         "do i need a doctor to say my migraines are from my period, or is "
         "a regular migraine diagnosis plus tracking my cycle enough?", 48)],
       umm, "outreach")
    _t(menstrual_id, "Sofia Grant", "sofia.grant@gmail.com",
       "How long does it last?", "casey", "open", False,
       [("inbound", "contact",
         "Is this one month or several? I travel for work every other "
         "month.", 300)], umm)
    _t(instagram_id, "Jen K.", "@jen.tracks.cycles",
       "Period migraine study, still enrolling?", None, "open", True,
       [("inbound", "contact",
         "saw your ad. i get attacks the day before my period. is that "
         "enough to qualify?", 100)], umm)

    # A raw conversation is not automatically an applicant. Seed three explicit,
    # consented links so the demo shows both sides of that boundary: general
    # inquiries stay inbox-only, while linked applicants can use records and
    # screening workflow tools.
    def _demo_applicant(thread_id, name, email, age, study, stage, criteria):
        nct, title = study
        applicant_token = f"demo-inbox-{thread_id}"
        token = create_lead({
            "applicant_token": applicant_token,
            "nct": nct,
            "title": title,
            "name": name,
            "email": email if "@" in email else "",
            "age": str(age),
            "consent": 1,
            "source": "inbox",
            "owner_user_id": user_id,
            "eligibility": json.dumps({
                "verdict": "possible",
                "met": [],
                "unknown": criteria,
                "not_met": [],
            }),
        })
        lead = get_lead_by_token(token)
        if not lead:
            return
        conn.execute(
            "UPDATE leads SET status = ?, records_authorized_at = ?, "
            "revealed = 1, updated_at = ? WHERE id = ?",
            (stage, ts, ts, lead["id"]))
        conn.execute(
            "UPDATE marketing_threads SET linked_lead_id = ?, "
            "pipeline_stage = ? WHERE id = ? AND org_id = ?",
            (lead["id"], stage, thread_id, oid))
        if stage in ("screening", "enrolled"):
            add_task(lead["id"], "Confirm screening appointment",
                     assigned_to="site", created_by="site")
            add_task(lead["id"], "Review patient-authorized records",
                     assigned_to="site", created_by="site")

    _demo_applicant(
        lauren_thread, "Lauren Fitzgerald", "lauren.f@yahoo.com", 34, mig,
        "prescreen",
        ["Migraine diagnosis and monthly migraine-day count",
         "Current preventive medications", "No conflicting neurologic condition"])
    _demo_applicant(
        tomas_thread, "Tomás Rivera", "trivera@gmail.com", 41, mig,
        "screening",
        ["Migraine diagnosis confirmed", "Screening visit completed",
         "Medication washout requirements reviewed"])
    _demo_applicant(
        rob_thread, "Rob Torres", "@rob.torres.tx", 38, psi, "outreach",
        ["Two to four adequate antidepressant failures this episode",
         "No excluded psychotic or bipolar history",
         "Support person available on dosing day"])
    _demo_applicant(
        aisha_thread, "Aisha Rahman", "aisha.rahman@gmail.com", 32, psi,
        "prescreen",
        ["Current depressive episode duration", "Prior antidepressant trials",
         "Availability for study visits"])
    _demo_applicant(
        daniel_thread, "Daniel Cho", "daniel.cho@outlook.com", 51, psi,
        "screening",
        ["Medication history reviewed", "Screening visit confirmed",
         "Support person availability for dosing day"])
    _demo_applicant(
        aze_thread, "Denise Okafor", "denise.okafor@gmail.com", 45, aze,
        "outreach",
        ["Current MDE severity (MADRS)", "Antidepressant washout timeline",
         "No bipolar or psychotic history"])
    _demo_applicant(
        helen_thread, "Helen Park", "helen.park@gmail.com", 44, mdd,
        "prescreen",
        ["Stable SSRI dose for adjunctive seltorexant",
         "Clinically significant insomnia (ISI)",
         "No narcolepsy or severe sleep apnea"])
    _demo_applicant(
        wanda_thread, "Wanda Price", "wanda.price@yahoo.com", 51, selm,
        "prescreen",
        ["Not currently on an antidepressant",
         "Washout timing since last SSRI",
         "Current MDE without psychotic features"])
    _demo_applicant(
        grace_ole, "Grace Kim", "grace.kim@icloud.com", 39, azeo,
        "screening",
        ["Completed X-NOVA3 double-blind period",
         "OLE start window after last visit",
         "Willing to continue Azetukalner open-label"])
    _demo_applicant(
        isla_thread, "Isla Thompson", "isla.thompson@gmail.com", 46, migl,
        "screening",
        ["Completed acute Elismetrep treatment period",
         "Willing to continue long-term safety follow-up",
         "Rescue-medication history reviewed"])
    _demo_applicant(
        nadia_umm, "Nadia Haddad", "nadia.haddad@gmail.com", 29, umm,
        "prescreen",
        ["Attacks temporally related to menses",
         "Cycle-length diary for screening window",
         "Current contraceptive method reviewed"])

    _top_up_demo_marketing_workflows(conn, user_id, oid)
    conn.execute(
        "INSERT INTO marketing_handoffs "
        "(org_id, primary_user_id, cover_user_id, vacation_mode, away_until, "
        "note, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(org_id) DO NOTHING",
        (oid, user_id, casey_id, 0, "", "", user_id, ts))
    conn.commit()


def get_user_by_email(email):
    return get_db().execute(
        "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)).fetchone()


def get_user_by_oauth(provider, oauth_sub):
    if not provider or not oauth_sub:
        return None
    return get_db().execute(
        "SELECT * FROM users WHERE oauth_provider = ? AND oauth_sub = ?",
        ((provider or "").strip(), (oauth_sub or "").strip())).fetchone()


def get_user(user_id):
    return get_db().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def list_study_team_users():
    """All study-team users (for scheduled jobs like the daily copilot digest)."""
    return get_db().execute("SELECT * FROM users ORDER BY id").fetchall()


def mark_user_verified(user_id):
    db = get_db()
    db.execute("UPDATE users SET verified = 1, verified_at = ? WHERE id = ?",
               (now(), user_id))
    db.commit()


def link_user_oauth(user_id, provider, oauth_sub, name="", picture=""):
    user = get_user(user_id)
    if not user or not provider or not oauth_sub:
        return False
    db = get_db()
    db.execute(
        "UPDATE users SET oauth_provider = ?, oauth_sub = ?, oauth_picture = ?, "
        "name = CASE WHEN name = '' THEN ? ELSE name END, "
        "verified = 1, verified_at = CASE WHEN verified_at = '' THEN ? ELSE verified_at END "
        "WHERE id = ?",
        ((provider or "").strip(), (oauth_sub or "").strip(),
         (picture or "").strip(), (name or "").strip(), now(), user_id))
    db.commit()
    return True


def set_ehr_connection(user_id, connected, provider=""):
    db = get_db()
    db.execute(
        "UPDATE users SET ehr_connected = ?, ehr_provider = ?, "
        "ehr_connected_at = ? WHERE id = ?",
        (1 if connected else 0, provider if connected else "",
         now() if connected else "", user_id))
    db.commit()


def create_patient_user(email, password_hash, full_name="", oauth_provider="",
                        oauth_sub=None, oauth_picture="", verified=False):
    db = get_db()
    cur = db.execute(
        "INSERT INTO patient_users (email, password_hash, full_name, applicant_token, "
        "oauth_provider, oauth_sub, oauth_picture, verified, verified_at, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (email.lower().strip(), password_hash, (full_name or "").strip(),
         gen_token(), (oauth_provider or "").strip(), oauth_sub,
         (oauth_picture or "").strip(), 1 if verified else 0,
         now() if verified else "", now()))
    db.commit()
    return cur.lastrowid


def get_patient_user(user_id):
    return get_db().execute(
        "SELECT * FROM patient_users WHERE id = ?", (user_id,)).fetchone()


def get_patient_by_email(email):
    return get_db().execute(
        "SELECT * FROM patient_users WHERE email = ?",
        (email.lower().strip(),)).fetchone()


def get_patient_by_oauth(provider, oauth_sub):
    if not provider or not oauth_sub:
        return None
    return get_db().execute(
        "SELECT * FROM patient_users WHERE oauth_provider = ? AND oauth_sub = ?",
        ((provider or "").strip(), (oauth_sub or "").strip())).fetchone()


def link_patient_oauth(patient_id, provider, oauth_sub, full_name="", picture=""):
    patient = get_patient_user(patient_id)
    if not patient or not provider or not oauth_sub:
        return False
    db = get_db()
    db.execute(
        "UPDATE patient_users SET oauth_provider = ?, oauth_sub = ?, oauth_picture = ?, "
        "full_name = CASE WHEN full_name = '' THEN ? ELSE full_name END, "
        "verified = 1, verified_at = CASE WHEN verified_at = '' THEN ? ELSE verified_at END "
        "WHERE id = ?",
        ((provider or "").strip(), (oauth_sub or "").strip(),
         (picture or "").strip(), (full_name or "").strip(), now(), patient_id))
    db.commit()
    return True


def mark_patient_verified(patient_id):
    db = get_db()
    db.execute(
        "UPDATE patient_users SET verified = 1, verified_at = ? WHERE id = ?",
        (now(), patient_id))
    db.commit()


def set_patient_password(patient_id, password_hash):
    """Set (or replace) the password on an existing account. Used to 'claim' an
    apply-first passwordless account so the visitor's earlier applications stay
    attached to the same account when they later create a password."""
    db = get_db()
    db.execute(
        "UPDATE patient_users SET password_hash = ? WHERE id = ?",
        (password_hash, patient_id))
    db.commit()


def set_patient_onboarding(patient_id, primary_interest="", notify_email="",
                           email_alerts=True):
    db = get_db()
    db.execute(
        "UPDATE patient_users SET onboarding_done = 1, primary_interest = ?, "
        "notify_email = ?, email_alerts = ? WHERE id = ?",
        ((primary_interest or "").strip(), (notify_email or "").strip(),
         1 if email_alerts else 0, patient_id))
    db.commit()


def update_patient_account(patient_id, full_name=None, notify_email=None,
                           email_alerts=None, primary_interest=None):
    """Update the basic, patient-editable account fields from the settings page.
    Only the fields passed (not None) are changed."""
    patient = get_patient_user(patient_id)
    if not patient:
        return False
    sets, vals = [], []
    if full_name is not None:
        sets.append("full_name = ?"); vals.append((full_name or "").strip())
    if notify_email is not None:
        sets.append("notify_email = ?"); vals.append((notify_email or "").strip())
    if email_alerts is not None:
        sets.append("email_alerts = ?"); vals.append(1 if email_alerts else 0)
    if primary_interest is not None:
        sets.append("primary_interest = ?"); vals.append((primary_interest or "").strip())
    if not sets:
        return True
    vals.append(patient_id)
    db = get_db()
    db.execute("UPDATE patient_users SET " + ", ".join(sets) + " WHERE id = ?", vals)
    db.commit()
    return True


def create_patient_code(patient_id, purpose, code, expires_ts):
    db = get_db()
    db.execute(
        "INSERT INTO patient_auth_codes (patient_id, purpose, code, expires_ts, "
        "created_at) VALUES (?,?,?,?,?)",
        (patient_id, purpose, code, int(expires_ts), now()))
    db.commit()


def create_user_code(user_id, purpose, code, expires_ts):
    db = get_db()
    db.execute(
        "INSERT INTO user_auth_codes (user_id, purpose, code, expires_ts, created_at) "
        "VALUES (?,?,?,?,?)",
        (user_id, purpose, code, int(expires_ts), now()))
    db.commit()


def verify_patient_code(patient_id, purpose, code, now_ts):
    """True if a live unused code exists; marks it used atomically."""
    db = get_db()
    row = db.execute(
        "SELECT id, code, expires_ts FROM patient_auth_codes WHERE patient_id = ? "
        "AND purpose = ? AND used_at = '' ORDER BY id DESC LIMIT 1",
        (patient_id, purpose)).fetchone()
    if not row:
        return False
    if int(row["expires_ts"]) < int(now_ts):
        return False
    if (code or "").strip() != (row["code"] or "").strip():
        return False
    db.execute("UPDATE patient_auth_codes SET used_at = ? WHERE id = ?",
               (now(), row["id"]))
    db.commit()
    return True


def verify_user_code(user_id, purpose, code, now_ts):
    """True if a live unused clinician code exists; marks it used atomically."""
    db = get_db()
    row = db.execute(
        "SELECT id, code, expires_ts FROM user_auth_codes WHERE user_id = ? "
        "AND purpose = ? AND used_at = '' ORDER BY id DESC LIMIT 1",
        (user_id, purpose)).fetchone()
    if not row:
        return False
    if int(row["expires_ts"]) < int(now_ts):
        return False
    if (code or "").strip() != (row["code"] or "").strip():
        return False
    db.execute("UPDATE user_auth_codes SET used_at = ? WHERE id = ?",
               (now(), row["id"]))
    db.commit()
    return True


def invalidate_patient_codes(patient_id, purpose):
    db = get_db()
    db.execute("UPDATE patient_auth_codes SET used_at = ? WHERE patient_id = ? "
               "AND purpose = ? AND used_at = ''", (now(), patient_id, purpose))
    db.commit()


def invalidate_user_codes(user_id, purpose):
    db = get_db()
    db.execute("UPDATE user_auth_codes SET used_at = ? WHERE user_id = ? "
               "AND purpose = ? AND used_at = ''", (now(), user_id, purpose))
    db.commit()


def check_and_bump_ip_limit(route_key, ip, max_hits, window_seconds, now_ts=None):
    """Return (allowed: bool, retry_after_seconds: int) for an IP/route window.

    This is intentionally lightweight: one row per (route_key, ip), reset when the
    window expires. It blocks when max_hits are already consumed in the window.
    """
    route_key = (route_key or "").strip()
    ip = (ip or "").strip()
    if not route_key or not ip:
        return False, max(1, int(window_seconds or 1))
    try:
        max_hits = int(max_hits)
        window_seconds = int(window_seconds)
    except (TypeError, ValueError):
        return False, 60
    if max_hits <= 0 or window_seconds <= 0:
        return False, 60
    ts = int(now_ts or time.time())
    window_floor = ts - window_seconds

    db = get_db()
    # Keep this tiny table bounded; stale rows don't affect active checks.
    db.execute("DELETE FROM ip_rate_limits WHERE window_start < ?", (window_floor,))
    row = db.execute(
        "SELECT id, window_start, hit_count FROM ip_rate_limits "
        "WHERE route_key = ? AND ip = ?",
        (route_key, ip)).fetchone()
    if not row:
        db.execute(
            "INSERT INTO ip_rate_limits (route_key, ip, window_start, hit_count, "
            "updated_at) VALUES (?,?,?,?,?)",
            (route_key, ip, ts, 1, now()))
        db.commit()
        return True, 0

    start = int(row["window_start"] or 0)
    hits = int(row["hit_count"] or 0)
    if start <= window_floor:
        db.execute(
            "UPDATE ip_rate_limits SET window_start = ?, hit_count = 1, "
            "updated_at = ? WHERE id = ?",
            (ts, now(), row["id"]))
        db.commit()
        return True, 0

    if hits >= max_hits:
        retry_after = max(1, window_seconds - (ts - start))
        return False, retry_after

    db.execute(
        "UPDATE ip_rate_limits SET hit_count = ?, updated_at = ? WHERE id = ?",
        (hits + 1, now(), row["id"]))
    db.commit()
    return True, 0


def _norm_nct(nct):
    nct = (nct or "").strip().upper()
    if not nct:
        return ""
    if nct.startswith("SITE-"):
        return nct
    if not nct.startswith("NCT"):
        nct = "NCT" + nct
    return nct


# --------------------------------------------------------------------------- #
# Study-team setup (profile + claimed studies)
# --------------------------------------------------------------------------- #
def get_site_profile(user_id):
    return get_db().execute(
        "SELECT * FROM site_profiles WHERE user_id = ?", (user_id,)).fetchone()


def upsert_site_profile(user_id, org_name, contact_name, contact_email,
                        contact_phone, intake_sla_hours="", escalation_email="",
                        ctms_endpoint="", redcap_endpoint="",
                        redcap_project_label=""):
    ts = now()
    db = get_db()
    db.execute(
        "INSERT INTO site_profiles (user_id, org_name, contact_name, contact_email, "
        "contact_phone, intake_sla_hours, escalation_email, ctms_endpoint, "
        "redcap_endpoint, redcap_project_label, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET org_name=excluded.org_name, "
        "contact_name=excluded.contact_name, contact_email=excluded.contact_email, "
        "contact_phone=excluded.contact_phone, "
        "intake_sla_hours=excluded.intake_sla_hours, "
        "escalation_email=excluded.escalation_email, "
        "ctms_endpoint=excluded.ctms_endpoint, "
        "redcap_endpoint=excluded.redcap_endpoint, "
        "redcap_project_label=excluded.redcap_project_label, "
        "updated_at=excluded.updated_at",
        (user_id, (org_name or "").strip(), (contact_name or "").strip(),
         (contact_email or "").strip(), (contact_phone or "").strip(),
         (intake_sla_hours or "").strip(), (escalation_email or "").strip(),
         (ctms_endpoint or "").strip(), (redcap_endpoint or "").strip(),
         (redcap_project_label or "").strip(), ts, ts))
    db.commit()


def update_site_redcap(user_id, endpoint=None, api_token=None, field_map=None,
                       intake_instrument=None, intake_enabled=None,
                       project_label=None):
    """Update only the REDCap connection fields on a site's profile.

    Every argument is optional; None means "leave unchanged" (so re-saving the
    form without re-typing the API token does NOT wipe the stored secret). An
    empty string explicitly clears a field. The API token is a secret - it is
    stored here but never logged or rendered back to the page.
    """
    prof = get_site_profile(user_id)
    if not prof:
        # Create a bare profile row so REDCap can be configured before the org
        # profile is filled in.
        upsert_site_profile(user_id, "", "", "", "")
        prof = get_site_profile(user_id)
    sets, vals = [], []
    fields = {
        "redcap_endpoint": endpoint,
        "redcap_api_token": api_token,
        "redcap_field_map": field_map,
        "redcap_intake_instrument": intake_instrument,
        "redcap_project_label": project_label,
    }
    for col, val in fields.items():
        if val is not None:
            sets.append(f"{col} = ?")
            vals.append(val.strip() if isinstance(val, str) else val)
    if intake_enabled is not None:
        sets.append("redcap_intake_enabled = ?")
        vals.append(1 if intake_enabled else 0)
    if not sets:
        return
    sets.append("updated_at = ?")
    vals.append(now())
    vals.append(user_id)
    db = get_db()
    db.execute(f"UPDATE site_profiles SET {', '.join(sets)} WHERE user_id = ?",
               vals)
    db.commit()


def set_site_calendar_url(user_id, url):
    """Save the coordinator's own booking calendar link (Calendly/Cal.com/Google).
    Creates a bare profile row if none exists yet so it can be set standalone."""
    if not get_site_profile(user_id):
        upsert_site_profile(user_id, "", "", "", "")
    db = get_db()
    db.execute("UPDATE site_profiles SET calendar_url = ?, updated_at = ? "
               "WHERE user_id = ?", ((url or "").strip(), now(), user_id))
    db.commit()
    return True


def get_site_calendar_url(user_id):
    prof = get_site_profile(user_id)
    try:
        return (prof["calendar_url"] if prof else "") or ""
    except (KeyError, IndexError, TypeError):
        return ""


def set_site_connector(user_id, url, secret=None):
    """Save the outbound reply connector. `secret=None` leaves the stored secret
    untouched (so a coordinator can edit the URL without re-typing it); pass ""
    to clear it. The secret is write-only - never rendered back to the page."""
    if not get_site_profile(user_id):
        upsert_site_profile(user_id, "", "", "", "")
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    db = get_db()
    if secret is None:
        db.execute("UPDATE site_profiles SET connector_webhook_url = ?, "
                   "updated_at = ? WHERE user_id = ?", (url, now(), user_id))
    else:
        db.execute("UPDATE site_profiles SET connector_webhook_url = ?, "
                   "connector_secret = ?, updated_at = ? WHERE user_id = ?",
                   (url, (secret or "").strip(), now(), user_id))
    db.commit()
    return True


def get_site_connector(user_id):
    """(url, secret) for the team's outbound reply connector - resolved via the
    user's org so any teammate's replies use the same connector."""
    prof = get_site_profile(user_id)
    if not prof:
        return "", ""

    def _v(row, key):
        try:
            return (row[key] or "") if row else ""
        except (KeyError, IndexError, TypeError):
            return ""
    return _v(prof, "connector_webhook_url"), _v(prof, "connector_secret")


def get_connector_for_nct(nct):
    """The outbound connector (url, secret) of the team that owns this NCT, so a
    reply to a social lead can be delivered even when sent from a shared inbox."""
    prof = get_site_profile_for_nct(nct)
    if not prof:
        return "", ""
    try:
        return (prof["connector_webhook_url"] or "",
                prof["connector_secret"] or "")
    except (KeyError, IndexError, TypeError):
        return "", ""


def get_site_profile_for_nct(nct):
    """The site profile of the team that claimed this NCT (first claim wins).

    Used to resolve which REDCap project a patient's screening form belongs to.
    """
    nct = _norm_nct(nct)
    if not nct:
        return None
    row = get_db().execute(
        "SELECT p.* FROM study_claims c "
        "JOIN site_profiles p ON p.user_id = c.user_id "
        "WHERE c.nct = ? ORDER BY c.id ASC LIMIT 1", (nct,)).fetchone()
    return row


def set_lead_redcap(lead_id, record_id=None, survey_status=None):
    """Track a lead's REDCap screening handoff (record id + survey status)."""
    lead = get_lead(lead_id)
    if not lead:
        return False
    sets, vals = [], []
    if record_id is not None:
        sets.append("redcap_record_id = ?")
        vals.append(str(record_id))
    if survey_status is not None:
        sets.append("redcap_survey_status = ?")
        vals.append(survey_status)
    if not sets:
        return False
    sets.append("updated_at = ?")
    vals.append(now())
    vals.append(lead_id)
    db = get_db()
    db.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id = ?", vals)
    db.commit()
    return True


def find_lead_by_redcap_record(record_id):
    """Locate the lead a REDCap webhook/record id belongs to.

    Matches ONLY the stored handoff record id (set when the survey link is
    created). We deliberately do NOT fall back to the lead's primary key: that
    let a caller advance any patient by guessing sequential ids."""
    rid = str(record_id or "").strip()
    if not rid:
        return None
    return get_db().execute(
        "SELECT * FROM leads WHERE redcap_record_id = ? "
        "ORDER BY id DESC LIMIT 1", (rid,)).fetchone()


def list_study_claims(user_id):
    return get_db().execute(
        "SELECT * FROM study_claims WHERE user_id = ? ORDER BY created_at DESC, id DESC",
        (user_id,)).fetchall()


def list_team_studies(user_id):
    """Distinct studies visible to this user's whole team (any member's verified
    claim). Used by the study switcher + the applicants view so every teammate
    sees the same set. Returns rows with nct + title (latest title wins)."""
    members = org_member_ids(user_id)
    qs = ",".join("?" * len(members))
    return get_db().execute(
        f"SELECT nct, MAX(title) AS title, MIN(created_at) AS created_at "
        f"FROM study_claims WHERE user_id IN ({qs}) AND verified = 1 "
        "GROUP BY nct ORDER BY created_at DESC, nct", members).fetchall()


def _gen_site_nct():
    return "SITE-" + secrets.token_hex(5).upper()


def create_site_posted_study(user_id, data):
    """Create a direct site-posted study and auto-claim it for lead routing."""
    title = (data.get("title") or "").strip()
    if not title:
        return None
    ts = now()
    nct = _gen_site_nct()
    db = get_db()
    for _ in range(5):
        try:
            db.execute(
                "INSERT INTO site_posted_studies "
                "(user_id, nct, title, condition, brief_summary, eligibility, "
                "location, site_name, contact_email, contact_phone, phase, status, "
                "lat, lon, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, nct, title, (data.get("condition") or "").strip(),
                 (data.get("brief_summary") or "").strip(),
                 (data.get("eligibility") or "").strip(),
                 (data.get("location") or "").strip(),
                 (data.get("site_name") or "").strip(),
                 (data.get("contact_email") or "").strip(),
                 (data.get("contact_phone") or "").strip(),
                 (data.get("phase") or "").strip(),
                 (data.get("status") or "recruiting").strip().lower(),
                 data.get("lat"), data.get("lon"), ts, ts))
            db.execute(
                "INSERT OR IGNORE INTO study_claims "
                "(user_id, nct, title, notify_email, created_at) "
                "VALUES (?,?,?,?,?)",
                (user_id, nct, title, (data.get("contact_email") or "").strip(), ts))
            db.commit()
            return get_site_posted_study_by_nct(nct)
        except sqlite3.IntegrityError:
            nct = _gen_site_nct()
    return None


def list_site_posted_studies(user_id=None, status=""):
    q = ("SELECT s.*, COALESCE(p.org_name, '') org_name, "
         "COALESCE(p.contact_name, '') profile_contact_name "
         "FROM site_posted_studies s "
         "LEFT JOIN site_profiles p ON p.user_id = s.user_id")
    vals = []
    where = []
    if user_id is not None:
        where.append("s.user_id = ?")
        vals.append(user_id)
    if status:
        where.append("s.status = ?")
        vals.append((status or "").strip().lower())
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY s.updated_at DESC, s.id DESC"
    return get_db().execute(q, vals).fetchall()


def get_site_posted_study_by_nct(nct):
    nct = _norm_nct(nct)
    if not nct:
        return None
    return get_db().execute(
        "SELECT * FROM site_posted_studies WHERE nct = ?", (nct,)).fetchone()


def remove_site_posted_study(user_id, study_id):
    db = get_db()
    row = db.execute(
        "SELECT nct FROM site_posted_studies WHERE id = ? AND user_id = ?",
        (study_id, user_id)).fetchone()
    if not row:
        return False
    nct = row["nct"]
    db.execute("DELETE FROM site_posted_studies WHERE id = ? AND user_id = ?",
               (study_id, user_id))
    keep = db.execute(
        "SELECT 1 FROM leads WHERE nct = ? LIMIT 1", (nct,)).fetchone()
    if not keep:
        db.execute("DELETE FROM study_claims WHERE user_id = ? AND nct = ?",
                   (user_id, nct))
    db.commit()
    return True


def user_claimed_ncts(user_id):
    """NCTs this account may access applicants for. Scoped to the user's TEAM:
    any study claimed by any member of their org is visible to the whole team
    (shared workspace). Only VERIFIED claims count, so an unapproved claim never
    exposes a trial's patient PHI."""
    members = org_member_ids(user_id)
    qs = ",".join("?" * len(members))
    rows = get_db().execute(
        f"SELECT DISTINCT nct FROM study_claims WHERE user_id IN ({qs}) "
        "AND verified = 1", members).fetchall()
    return {r["nct"] for r in rows if r["nct"]}


def lead_counts_for_ncts(ncts):
    ncts = sorted({x for x in (ncts or []) if x})
    if not ncts:
        return {}
    qs = ",".join("?" * len(ncts))
    rows = get_db().execute(
        f"SELECT status, COUNT(*) n FROM leads WHERE nct IN ({qs}) GROUP BY status",
        ncts).fetchall()
    return {r["status"]: r["n"] for r in rows}


# --------------------------------------------------------------------------- #
# Internal patient -> trial matching. A connected clinic's own patients surfaced
# as candidates for a trial. De-identified rows; team-scoped like study claims so
# the whole org shares one review queue. `kind`: internal (clinic runs the study
# -> becomes an applicant on approve) vs external (partner-site trial -> a secure
# referral after consent).
# --------------------------------------------------------------------------- #
PATIENT_MATCH_STATUSES = ("new", "approved", "referred", "dismissed")


def _decode_match(row):
    """Return a patient_matches row as a plain dict with JSON lists parsed."""
    if row is None:
        return None
    d = dict(row)
    for k in ("met", "unknown", "not_met"):
        try:
            d[k] = json.loads(row[k]) if row[k] else []
        except (ValueError, TypeError):
            d[k] = []
    return d


def create_patient_match(user_id, data):
    db = get_db()
    ts = now()
    cur = db.execute(
        """INSERT INTO patient_matches
           (user_id, patient_ref, full_name, age, sex, summary, source_label,
            nct, trial_title, condition, kind, site_name, site_location,
            verdict, score, met, unknown, not_met, rationale, status,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, data.get("patient_ref", ""), data.get("full_name", ""),
         str(data.get("age", "")), data.get("sex", ""), data.get("summary", ""),
         data.get("source_label", ""), _norm_nct(data.get("nct", "")),
         data.get("trial_title", ""), data.get("condition", ""),
         (data.get("kind") or "internal"), data.get("site_name", ""),
         data.get("site_location", ""), data.get("verdict", ""),
         int(data.get("score") or 0),
         json.dumps(data.get("met") or []), json.dumps(data.get("unknown") or []),
         json.dumps(data.get("not_met") or []), data.get("rationale", ""),
         (data.get("status") or "new"), ts, ts))
    db.commit()
    return cur.lastrowid


def list_patient_matches(user_id, kind=None, status=None):
    members = org_member_ids(user_id)
    qs = ",".join("?" * len(members))
    q = f"SELECT * FROM patient_matches WHERE user_id IN ({qs})"
    vals = list(members)
    if kind:
        q += " AND kind = ?"
        vals.append(kind)
    if status:
        q += " AND status = ?"
        vals.append(status)
    q += " ORDER BY (status = 'new') DESC, score DESC, id DESC"
    return [_decode_match(r) for r in get_db().execute(q, vals).fetchall()]


def get_patient_match(user_id, match_id):
    members = org_member_ids(user_id)
    qs = ",".join("?" * len(members))
    row = get_db().execute(
        f"SELECT * FROM patient_matches WHERE id = ? AND user_id IN ({qs})",
        [match_id] + list(members)).fetchone()
    return _decode_match(row)


def set_patient_match_status(user_id, match_id, status, lead_id=None):
    if status not in PATIENT_MATCH_STATUSES:
        return False
    if not get_patient_match(user_id, match_id):
        return False
    db = get_db()
    db.execute(
        "UPDATE patient_matches SET status = ?, "
        "lead_id = COALESCE(?, lead_id), updated_at = ? WHERE id = ?",
        (status, lead_id, now(), match_id))
    db.commit()
    return True


def patient_match_counts(user_id):
    members = org_member_ids(user_id)
    qs = ",".join("?" * len(members))
    rows = get_db().execute(
        f"SELECT kind, status, COUNT(*) n FROM patient_matches "
        f"WHERE user_id IN ({qs}) GROUP BY kind, status", members).fetchall()
    out = {"new": 0, "new_internal": 0, "new_external": 0, "approved": 0,
           "referred": 0, "dismissed": 0, "total": 0}
    for r in rows:
        out["total"] += r["n"]
        if r["status"] == "new":
            out["new"] += r["n"]
            key = f"new_{r['kind']}"
            out[key] = out.get(key, 0) + r["n"]
        else:
            out[r["status"]] = out.get(r["status"], 0) + r["n"]
    return out


def site_contact_for_nct(nct):
    """Notification email for a claimed study (profile contact first, then login)."""
    nct = _norm_nct(nct)
    if not nct:
        return ""
    row = get_db().execute(
        "SELECT c.notify_email, p.contact_email, u.email FROM study_claims c "
        "JOIN users u ON u.id = c.user_id "
        "LEFT JOIN site_profiles p ON p.user_id = c.user_id "
        "WHERE c.nct = ? AND c.verified = 1 ORDER BY c.id DESC LIMIT 1",
        (nct,)).fetchone()
    if not row:
        return ""
    return (row["notify_email"] or row["contact_email"] or row["email"] or "").strip()


def add_recruitment_spend(user_id, nct, source, campaign, amount_usd, spend_date,
                          note=""):
    db = get_db()
    db.execute(
        "INSERT INTO recruitment_spend (user_id, nct, source, campaign, amount_usd, "
        "spend_date, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (user_id, _norm_nct(nct), (source or "").strip().lower(),
         (campaign or "").strip(), float(amount_usd or 0),
         (spend_date or "").strip(), (note or "").strip(), now()))
    db.commit()


def list_recruitment_spend_for_user(user_id, ncts=None):
    ncts = sorted({x for x in (ncts or []) if x})
    if ncts:
        qs = ",".join("?" * len(ncts))
        return get_db().execute(
            f"SELECT * FROM recruitment_spend WHERE user_id = ? AND nct IN ({qs}) "
            "ORDER BY created_at DESC, id DESC",
            [user_id] + ncts).fetchall()
    return get_db().execute(
        "SELECT * FROM recruitment_spend WHERE user_id = ? "
        "ORDER BY created_at DESC, id DESC", (user_id,)).fetchall()


def spend_summary_for_user(user_id, ncts=None):
    rows = list_recruitment_spend_for_user(user_id, ncts=ncts)
    total = sum(float(r["amount_usd"] or 0) for r in rows)
    by_source = {}
    for r in rows:
        src = (r["source"] or "unknown").strip().lower() or "unknown"
        by_source[src] = by_source.get(src, 0.0) + float(r["amount_usd"] or 0)
    src_rows = [{"source": k, "amount_usd": round(v, 2)}
                for k, v in sorted(by_source.items(), key=lambda kv: -kv[1])]
    return {"total_usd": round(total, 2), "by_source": src_rows, "rows": rows}


# --------------------------------------------------------------------------- #
# Layer 2 - Part 2: recruitment campaigns (the AI "agency", wired to the funnel)
#
# Campaigns feed applicants into the SAME lead pipeline, so we can measure cost
# per *enrolled* patient - not vanity clicks. AI-drafted creative always starts
# irb_approved=0; set_campaign_status refuses to activate an unapproved campaign
# (compliance hard gate: recruitment ads must be IRB/REB-approved + truthful).
# --------------------------------------------------------------------------- #
CAMPAIGN_CHANNELS = ("meta", "google", "reddit", "campus", "email", "other")
# Lead statuses that count as having reached each funnel stage, for rollups.
_SCREENED_STATUSES = {"screening", "screened", "eligible", "enrolled",
                      "randomized", "active"}
_ENROLLED_STATUSES = {"enrolled", "randomized", "active", "retained"}


def create_campaign(user_id, nct, name, channel="other", budget_usd=0):
    db = get_db()
    ts = now()
    track = "cmp-" + secrets.token_hex(6)
    ch = (channel or "other").strip().lower()
    if ch not in CAMPAIGN_CHANNELS:
        ch = "other"
    cur = db.execute(
        "INSERT INTO campaigns (user_id, nct, name, channel, status, budget_usd, "
        "track_token, created_at, updated_at) VALUES (?,?,?,?,'draft',?,?,?,?)",
        (user_id, _norm_nct(nct), (name or "").strip(), ch,
         float(budget_usd or 0), track, ts, ts))
    db.commit()
    return cur.lastrowid


def get_campaign(campaign_id):
    return get_db().execute(
        "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()


def get_campaign_by_token(track_token):
    """Resolve a landing/tracking token to its campaign so an inbound applicant
    can be attributed. Only active campaigns attribute (paused/ended don't)."""
    t = (track_token or "").strip()
    if not t:
        return None
    return get_db().execute(
        "SELECT * FROM campaigns WHERE track_token = ?", (t,)).fetchone()


def list_campaigns_for_user(user_id, ncts=None):
    # Shared across the team (any org member's campaigns are visible to all).
    members = org_member_ids(user_id)
    mqs = ",".join("?" * len(members))
    ncts = sorted({_norm_nct(x) for x in (ncts or []) if x})
    if ncts:
        qs = ",".join("?" * len(ncts))
        return get_db().execute(
            f"SELECT * FROM campaigns WHERE user_id IN ({mqs}) AND nct IN ({qs}) "
            "ORDER BY created_at DESC, id DESC", members + ncts).fetchall()
    return get_db().execute(
        f"SELECT * FROM campaigns WHERE user_id IN ({mqs}) ORDER BY created_at DESC, "
        "id DESC", members).fetchall()


def set_campaign_creative(campaign_id, headline, body, landing_copy):
    """Store campaign copy. ANY creative edit resets IRB approval to 0 - changed
    ad copy must be re-reviewed before it can run again (compliance)."""
    db = get_db()
    db.execute(
        "UPDATE campaigns SET headline = ?, body = ?, landing_copy = ?, "
        "irb_approved = 0, updated_at = ? WHERE id = ?",
        ((headline or "").strip(), (body or "").strip(),
         (landing_copy or "").strip(), now(), campaign_id))
    db.commit()


def approve_campaign(campaign_id, approved=True):
    """Mark a campaign's creative as IRB/REB-approved (a human attests to this).
    Only after this can it be activated."""
    db = get_db()
    db.execute("UPDATE campaigns SET irb_approved = ?, updated_at = ? WHERE id = ?",
               (1 if approved else 0, now(), campaign_id))
    db.commit()


def set_campaign_status(campaign_id, status):
    """Move a campaign through draft | active | paused | ended. Returns
    (ok: bool, reason: str). Refuses to activate creative that isn't
    IRB-approved - the compliance gate lives here so no route can bypass it."""
    status = (status or "").strip().lower()
    if status not in ("draft", "active", "paused", "ended"):
        return False, "invalid status"
    camp = get_campaign(campaign_id)
    if not camp:
        return False, "not found"
    if status == "active" and not int(camp["irb_approved"] or 0):
        return False, "creative must be IRB/REB-approved before going live"
    db = get_db()
    db.execute("UPDATE campaigns SET status = ?, updated_at = ? WHERE id = ?",
               (status, now(), campaign_id))
    db.commit()
    return True, ""


def campaign_performance(user_id, ncts=None):
    """Per-campaign funnel + cost, computed from the REAL pipeline: applicants
    attributed to the campaign, how many reached screened/enrolled, matched spend,
    and derived cost-per-applicant / cost-per-enrolled. This is the number that
    tells a site which channel actually produces enrolled patients."""
    camps = list_campaigns_for_user(user_id, ncts=ncts)
    if not camps:
        return []
    db = get_db()
    ids = [c["id"] for c in camps]
    qs = ",".join("?" * len(ids))
    counts = {}
    for r in db.execute(
            f"SELECT campaign_id, status, COUNT(*) n FROM leads "
            f"WHERE campaign_id IN ({qs}) GROUP BY campaign_id, status",
            ids).fetchall():
        counts.setdefault(r["campaign_id"], {})[r["status"] or ""] = r["n"]
    out = []
    for c in camps:
        by_status = counts.get(c["id"], {})
        applicants = sum(by_status.values())
        screened = sum(n for s, n in by_status.items()
                       if s in _SCREENED_STATUSES)
        enrolled = sum(n for s, n in by_status.items()
                       if s in _ENROLLED_STATUSES)
        # Spend attributed by matching recruitment_spend on (nct, campaign name);
        # fall back to the campaign's own budget if no spend rows were logged.
        spend_row = db.execute(
            "SELECT COALESCE(SUM(amount_usd),0) s FROM recruitment_spend "
            "WHERE user_id = ? AND nct = ? AND campaign = ?",
            (user_id, c["nct"], c["name"])).fetchone()
        spend = float(spend_row["s"] or 0) or float(c["budget_usd"] or 0)
        out.append({
            "id": c["id"], "name": c["name"], "channel": c["channel"],
            "status": c["status"], "nct": c["nct"],
            "irb_approved": int(c["irb_approved"] or 0),
            "track_token": c["track_token"],
            "applicants": applicants, "screened": screened,
            "enrolled": enrolled, "spend_usd": round(spend, 2),
            "cost_per_applicant": round(spend / applicants, 2) if applicants else None,
            "cost_per_enrolled": round(spend / enrolled, 2) if enrolled else None,
        })
    return out


# --- Placements: the places a campaign is posted, each with its own track link ---
def create_placement(campaign_id, label="", channel="", posted_url=""):
    db = get_db()
    ts = now()
    track = "pl-" + secrets.token_hex(6)
    db.execute(
        "INSERT INTO campaign_placements (campaign_id, label, channel, "
        "track_token, posted_url, posted_at, created_at) VALUES (?,?,?,?,?,?,?)",
        (campaign_id, (label or "").strip(), (channel or "").strip().lower(),
         track, (posted_url or "").strip(),
         ts if (posted_url or "").strip() else "", ts))
    db.commit()
    return db.execute(
        "SELECT * FROM campaign_placements WHERE track_token = ?",
        (track,)).fetchone()


def list_placements(campaign_id):
    return get_db().execute(
        "SELECT * FROM campaign_placements WHERE campaign_id = ? "
        "ORDER BY created_at DESC, id DESC", (campaign_id,)).fetchall()


def get_placement(placement_id):
    return get_db().execute(
        "SELECT * FROM campaign_placements WHERE id = ?",
        (placement_id,)).fetchone()


def get_placement_by_token(token):
    t = (token or "").strip()
    if not t:
        return None
    return get_db().execute(
        "SELECT * FROM campaign_placements WHERE track_token = ?", (t,)).fetchone()


def mark_placement_posted(placement_id, posted_url=""):
    db = get_db()
    db.execute(
        "UPDATE campaign_placements SET posted_url = ?, posted_at = ? WHERE id = ?",
        ((posted_url or "").strip(), now(), placement_id))
    db.commit()


def bump_placement_clicks(token):
    db = get_db()
    db.execute(
        "UPDATE campaign_placements SET clicks = clicks + 1 WHERE track_token = ?",
        ((token or "").strip(),))
    db.commit()


def resolve_tracking_token(token):
    """A tracking token on a posted link is either a placement token (pl-...) or a
    campaign token (cmp-...). Resolve either to {campaign_id, placement_id, nct}
    so a click/apply can be attributed to the right campaign (and exact place)."""
    t = (token or "").strip()
    if not t:
        return None
    pl = get_placement_by_token(t)
    if pl:
        camp = get_campaign(pl["campaign_id"])
        return {"campaign_id": pl["campaign_id"], "placement_id": pl["id"],
                "nct": camp["nct"] if camp else ""}
    camp = get_campaign_by_token(t)
    if camp:
        return {"campaign_id": camp["id"], "placement_id": None,
                "nct": camp["nct"]}
    return None


def attribute_lead(lead_id, campaign_id=None, placement_id=None, only_if_nct=None):
    """Credit an applicant to a campaign/placement. If only_if_nct is given, we
    attribute ONLY when the lead is for that study - so a tracked click that then
    applies to an UNRELATED trial never inflates a campaign's numbers (net-
    throughput guardrail). Doesn't overwrite an existing attribution."""
    lead = get_lead(lead_id)
    if not lead:
        return False
    if only_if_nct and _norm_nct(lead["nct"]) != _norm_nct(only_if_nct):
        return False
    if lead["campaign_id"]:  # already attributed - first touch wins
        return False
    db = get_db()
    db.execute(
        "UPDATE leads SET campaign_id = ?, placement_id = ?, updated_at = ? "
        "WHERE id = ?", (campaign_id, placement_id, now(), lead_id))
    db.commit()
    return True


def placement_performance(user_id, ncts=None):
    """Per-placement funnel: for every placement under this user's campaigns, how
    many applicants/screened/enrolled it produced, plus clicks. This is the "which
    place actually works" view. Cost stays at the campaign level (spend isn't
    tracked per placement)."""
    camps = list_campaigns_for_user(user_id, ncts=ncts)
    if not camps:
        return []
    by_camp = {c["id"]: c for c in camps}
    db = get_db()
    cids = list(by_camp.keys())
    qs = ",".join("?" * len(cids))
    placements = db.execute(
        f"SELECT * FROM campaign_placements WHERE campaign_id IN ({qs}) "
        f"ORDER BY created_at DESC", cids).fetchall()
    if not placements:
        return []
    pids = [p["id"] for p in placements]
    pqs = ",".join("?" * len(pids))
    counts = {}
    for r in db.execute(
            f"SELECT placement_id, status, COUNT(*) n FROM leads "
            f"WHERE placement_id IN ({pqs}) GROUP BY placement_id, status",
            pids).fetchall():
        counts.setdefault(r["placement_id"], {})[r["status"] or ""] = r["n"]
    out = []
    for p in placements:
        camp = by_camp.get(p["campaign_id"], {})
        bs = counts.get(p["id"], {})
        out.append({
            "id": p["id"], "campaign_id": p["campaign_id"],
            "campaign_name": camp["name"] if camp else "",
            "label": p["label"],
            "channel": p["channel"] or (camp["channel"] if camp else ""),
            "track_token": p["track_token"], "posted_url": p["posted_url"],
            "clicks": int(p["clicks"] or 0),
            "applicants": sum(bs.values()),
            "screened": sum(n for s, n in bs.items() if s in _SCREENED_STATUSES),
            "enrolled": sum(n for s, n in bs.items() if s in _ENROLLED_STATUSES),
        })
    return out


def add_study_claim(user_id, nct, title="", notify_email="", verified=False):
    """Record a study-team claim on an NCT.

    `verified` defaults to False: a new claim is PENDING and grants no access to
    that trial's applicants until approved (see verify_study_claim). Callers that
    are trusted (demo builds, admin onboarding) pass verified=True."""
    nct = _norm_nct(nct)
    if not nct:
        return False
    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO study_claims "
        "(user_id, nct, title, notify_email, verified, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (user_id, nct, (title or "").strip(), (notify_email or "").strip(),
         1 if verified else 0, now()))
    db.commit()
    return True


def set_claim_verified(user_id, nct, verified=True):
    """Approve (or revoke) a study claim so it grants/stops granting access."""
    nct = _norm_nct(nct)
    if not nct:
        return False
    db = get_db()
    db.execute("UPDATE study_claims SET verified = ? WHERE user_id = ? AND nct = ?",
               (1 if verified else 0, user_id, nct))
    db.commit()
    return True


def set_claim_schedule_url(user_id, nct, url):
    """Save the reusable per-study booking link so it prefills for every applicant
    in that study. Scoped to the owner so one team can't set another's link."""
    nct = _norm_nct(nct)
    if not nct:
        return False
    db = get_db()
    db.execute("UPDATE study_claims SET schedule_url = ? WHERE user_id = ? AND nct = ?",
               ((url or "").strip(), user_id, nct))
    db.commit()
    return True


def get_claim_schedule_url(user_id, nct):
    nct = _norm_nct(nct)
    if not nct:
        return ""
    row = get_db().execute(
        "SELECT schedule_url FROM study_claims WHERE user_id = ? AND nct = ?",
        (user_id, nct)).fetchone()
    return (row["schedule_url"] if row else "") or ""


# Connectable channels a study can recruit through (canonical, order = UI order).
# Mirrors ROUTING_CHANNELS (minus the "any" wildcard) plus the direct-message
# channels that carry inbound but aren't routing targets, so "connected sources"
# and routing stay aligned on one vocabulary.
CONNECTABLE_CHANNELS = ("email_intake", "instagram", "messenger", "facebook",
                        "whatsapp", "meta", "google", "reddit", "ctgov",
                        "referral")
# Channels that email/SMS can't reach - a reply to these must go out through the
# connector webhook, not the mailer. Used by the outbound reply router.
CONNECTOR_CHANNELS = frozenset(("instagram", "messenger", "facebook", "whatsapp",
                                "meta", "reddit"))


def _norm_sources(sources):
    """Coerce a list/CSV of channel keys to an ordered, de-duped tuple of valid
    connectable channels (drops anything unknown so bad input can't poison a
    filter)."""
    if isinstance(sources, str):
        sources = sources.split(",")
    seen, out = set(), []
    for s in (sources or []):
        k = (s or "").strip().lower()
        if k in CONNECTABLE_CHANNELS and k not in seen:
            seen.add(k)
            out.append(k)
    return tuple(out)


def set_claim_connected_sources(user_id, nct, sources):
    """Save which channels a study recruits through (empty = every source)."""
    nct = _norm_nct(nct)
    if not nct:
        return False
    csv = ",".join(_norm_sources(sources))
    db = get_db()
    db.execute("UPDATE study_claims SET connected_sources = ? "
               "WHERE user_id = ? AND nct = ?", (csv, user_id, nct))
    db.commit()
    return True


def get_claim_connected_sources(user_id, nct):
    """The study's connected channels as a list of keys ([] = every source).
    Falls back to any same-org claim on this NCT, so a teammate viewing a shared
    trial's inbox sees the same connected sources the owner configured."""
    nct = _norm_nct(nct)
    if not nct:
        return []
    row = get_db().execute(
        "SELECT connected_sources FROM study_claims WHERE user_id = ? AND nct = ?",
        (user_id, nct)).fetchone()
    csv = (row["connected_sources"] if row else "") or ""
    if not csv:
        oid = user_org_id(user_id)
        if oid:
            row = get_db().execute(
                "SELECT sc.connected_sources FROM study_claims sc "
                "JOIN users u ON u.id = sc.user_id "
                "WHERE u.org_id = ? AND sc.nct = ? AND sc.connected_sources != '' "
                "LIMIT 1", (oid, nct)).fetchone()
            csv = (row["connected_sources"] if row else "") or ""
    return list(_norm_sources(csv))


def list_pending_claims():
    """All unverified claims awaiting approval (for an admin/ops review)."""
    return get_db().execute(
        "SELECT c.*, u.email AS user_email FROM study_claims c "
        "JOIN users u ON u.id = c.user_id "
        "WHERE c.verified = 0 ORDER BY c.created_at DESC, c.id DESC").fetchall()


def remove_study_claim(user_id, nct):
    nct = _norm_nct(nct)
    if not nct:
        return False
    db = get_db()
    db.execute("DELETE FROM study_claims WHERE user_id = ? AND nct = ?",
               (user_id, nct))
    db.commit()
    return True


def update_study_claim_notify_email(user_id, nct, notify_email=""):
    nct = _norm_nct(nct)
    if not nct:
        return False
    db = get_db()
    db.execute(
        "UPDATE study_claims SET notify_email = ? WHERE user_id = ? AND nct = ?",
        ((notify_email or "").strip(), user_id, nct))
    db.commit()
    return True


def lead_belongs_to_user(lead_id, user_id):
    lead = get_lead(lead_id)
    if not lead:
        return False
    claims = user_claimed_ncts(user_id)
    return bool(claims and lead["nct"] in claims)


def lead_token_belongs_to_user(token, user_id):
    lead = get_lead_by_token(token)
    if not lead:
        return False
    claims = user_claimed_ncts(user_id)
    return bool(claims and lead["nct"] in claims)


# --------------------------------------------------------------------------- #
# Referrals
# --------------------------------------------------------------------------- #
def create_referral(user_id, data):
    db = get_db()
    ts = now()
    token = gen_token()
    consent = 1 if data.get("consent") else 0
    cur = db.execute(
        """INSERT INTO referrals
           (user_id, token, nct, title, patient_label, patient_summary, condition,
            country, site, coordinator, coordinator_email, patient_name,
            patient_contact, consent, consent_at, verdict, score, rationale,
            status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, token, data.get("nct", ""), data.get("title", ""),
         data.get("patient_label", ""), data.get("patient_summary", ""),
         data.get("condition", ""), data.get("country", ""),
         data.get("site", ""), data.get("coordinator", ""),
         data.get("coordinator_email", ""), data.get("patient_name", ""),
         data.get("patient_contact", ""), consent, ts if consent else "",
         data.get("verdict", ""), int(data.get("score") or 0),
         data.get("rationale", ""), "referred", ts, ts))
    ref_id = cur.lastrowid
    db.execute(
        "INSERT INTO referral_events (referral_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)", (ref_id, "referred", "referral created", "you", ts))
    db.commit()
    return ref_id


def get_referral_by_token(token):
    if not token:
        return None
    return get_db().execute(
        "SELECT * FROM referrals WHERE token = ?", (token,)).fetchone()


def mark_notified(ref_id, user_id, email=""):
    db = get_db()
    ref = get_referral(ref_id, user_id)
    if not ref:
        return False
    ts = now()
    db.execute("UPDATE referrals SET notified_at = ?, coordinator_email = ? "
               "WHERE id = ?",
               (ts, email or ref["coordinator_email"], ref_id))
    db.execute(
        "INSERT INTO referral_events (referral_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)",
        (ref_id, ref["status"], f"Referral sent to coordinator"
         f"{(' (' + email + ')') if email else ''}", "you", ts))
    db.commit()
    return True


def update_status_by_token(token, status, note="", actor="site"):
    db = get_db()
    ref = get_referral_by_token(token)
    if not ref or status not in STATUSES:
        return False
    ts = now()
    db.execute("UPDATE referrals SET status = ?, updated_at = ? WHERE id = ?",
               (status, ts, ref["id"]))
    db.execute(
        "INSERT INTO referral_events (referral_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)", (ref["id"], status, note, actor, ts))
    db.commit()
    return True


def list_referrals(user_id):
    return get_db().execute(
        "SELECT * FROM referrals WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
        (user_id,)).fetchall()


def get_referral(ref_id, user_id):
    return get_db().execute(
        "SELECT * FROM referrals WHERE id = ? AND user_id = ?",
        (ref_id, user_id)).fetchone()


def get_events(ref_id):
    return get_db().execute(
        "SELECT * FROM referral_events WHERE referral_id = ? ORDER BY id ASC",
        (ref_id,)).fetchall()


def update_status(ref_id, user_id, status, note=""):
    db = get_db()
    ref = get_referral(ref_id, user_id)
    if not ref:
        return False
    ts = now()
    db.execute("UPDATE referrals SET status = ?, updated_at = ? WHERE id = ?",
               (status, ts, ref_id))
    db.execute(
        "INSERT INTO referral_events (referral_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)", (ref_id, status, note, "you", ts))
    db.commit()
    return True


# --------------------------------------------------------------------------- #
# Patient leads (consumer funnel)
# --------------------------------------------------------------------------- #
def create_lead(data):
    db = get_db()
    token = gen_token()
    site_token = gen_token()
    ts = now()
    opt_in = 1 if data.get("registry_opt_in") else 0
    cur = db.execute(
        """INSERT INTO leads
           (token, site_token, site_token_expires_at, site_token_revoked,
            applicant_token, nct, title, condition, location, site, name,
            email, phone, age, sex, notes, consent, source, status, screener,
            eligibility, prescreen_readiness, records_connected, record_summary,
            referred_by, invite_token, registry_opt_in, registry_consent_at,
            registry_consent_version, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (token, site_token, _site_token_expiry(), 0,
         data.get("applicant_token", ""), data.get("nct", ""),
         data.get("title", ""), data.get("condition", ""),
         data.get("location", ""), data.get("site", ""), data.get("name", ""),
         data.get("email", ""), data.get("phone", ""), data.get("age", ""),
         data.get("sex", ""), data.get("notes", ""),
         1 if data.get("consent") else 0, data.get("source", "web"),
         "prescreen", data.get("screener", ""), data.get("eligibility", ""),
         data.get("prescreen_readiness", ""),
         1 if data.get("records_connected") else 0,
         data.get("record_summary", ""), data.get("referred_by", ""),
         data.get("invite_token", ""), opt_in,
         (ts if opt_in else ""), (data.get("registry_consent_version", "") if opt_in else ""),
         ts, ts))
    lead_id = cur.lastrowid
    # Workspace ownership (inbox product): tie the lead to the receiving team so
    # it shows with no study/claim needed. Set separately to avoid reshaping the
    # big INSERT above.
    if data.get("owner_user_id"):
        db.execute("UPDATE leads SET owner_user_id = ? WHERE id = ?",
                   (data.get("owner_user_id"), lead_id))
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)",
        (lead_id, "submitted", "application received", "you", ts))
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)",
        (lead_id, "prescreen", "ready for study-team review", "you", ts))
    db.commit()
    return token


def list_registry_leads():
    """All applications whose applicant opted into the consented matching pool
    (registry_opt_in = 1), newest first. Callers dedupe by person to build the
    pool view. Consent here is the SEPARATE future-matching opt-in, not the
    per-trial contact consent."""
    return get_db().execute(
        "SELECT * FROM leads WHERE registry_opt_in = 1 ORDER BY created_at DESC"
    ).fetchall()


def set_registry_opt_out(applicant_token=None, email=None):
    """Revoke the consented-pool opt-in for a person (by applicant token or
    email). Honours the right to withdraw; leaves the underlying application
    untouched. Returns the number of rows updated."""
    db = get_db()
    if applicant_token:
        cur = db.execute(
            "UPDATE leads SET registry_opt_in = 0, updated_at = ? "
            "WHERE applicant_token = ?", (now(), applicant_token))
    elif email:
        cur = db.execute(
            "UPDATE leads SET registry_opt_in = 0, updated_at = ? "
            "WHERE lower(email) = lower(?)", (now(), email))
    else:
        return 0
    db.commit()
    return cur.rowcount


# --------------------------------------------------------------------------- #
# Layer 2 - Part 1: multi-source ATS intake (capture)
#
# A site's applicants arrive from many channels (their inbox, ClinicalTrials.gov,
# ad lead forms, referrals, walk-ins). These helpers let all of them land in ONE
# per-trial queue: a unique inbound email address per study, a match-or-create so
# forwarded mail dedupes onto an existing applicant, and a CSV import to load a
# site's current spreadsheet on day one. Everything reuses the existing `leads`
# pipeline + `messages` inbox. Owner scoping (user_id+nct) keeps PHI contained.
# --------------------------------------------------------------------------- #
def get_or_create_intake_address(user_id, nct, label=""):
    """Return the study's single inbound intake address, minting one if needed."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM intake_addresses WHERE user_id = ? AND nct = ?",
        (user_id, nct)).fetchone()
    if row:
        return row
    addr = "in-" + secrets.token_hex(6)
    db.execute(
        "INSERT INTO intake_addresses (user_id, nct, address, label, active, "
        "created_at) VALUES (?,?,?,?,1,?)",
        (user_id, (nct or "").strip(), addr, (label or "").strip(), now()))
    db.commit()
    return db.execute(
        "SELECT * FROM intake_addresses WHERE address = ?", (addr,)).fetchone()


def get_or_create_catchall_address(user_id, label="All inquiries"):
    """The site's single catch-all intake address (nct = ''). This is what a new
    site forwards its whole recruitment mailbox to on day one - no study needed.
    Inbound mail here creates workspace-owned leads that show in the inbox
    immediately; the coordinator can attach a study later."""
    return get_or_create_intake_address(user_id, "", label=label)


def resolve_intake_address(address):
    """Map an inbound address (full email or bare local-part) to its active
    intake row, or None. Used by the inbound-email webhook to route mail to the
    right trial's queue - and to reject mail to unknown/disabled addresses."""
    a = (address or "").strip().lower()
    if "@" in a:
        a = a.split("@", 1)[0]
    if not a:
        return None
    return get_db().execute(
        "SELECT * FROM intake_addresses WHERE address = ? AND active = 1",
        (a,)).fetchone()


def list_intake_addresses(user_id):
    return get_db().execute(
        "SELECT * FROM intake_addresses WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,)).fetchall()


def set_intake_address_active(address, active):
    db = get_db()
    db.execute("UPDATE intake_addresses SET active = ? WHERE address = ?",
               (1 if active else 0, (address or "").strip().lower()))
    db.commit()


def find_lead_by_nct_email(nct, email):
    """Newest existing applicant for this study with this email, or None.
    Dedupe key for intake so the same person emailing twice is one thread."""
    e = (email or "").strip().lower()
    if not e:
        return None
    return get_db().execute(
        "SELECT * FROM leads WHERE nct = ? AND lower(email) = ? "
        "ORDER BY id DESC LIMIT 1", ((nct or "").strip(), e)).fetchone()


def set_lead_campaign(lead_id, campaign_id):
    db = get_db()
    db.execute("UPDATE leads SET campaign_id = ?, updated_at = ? WHERE id = ?",
               (campaign_id, now(), lead_id))
    db.commit()


def match_or_create_lead(nct, email="", name="", title="", condition="",
                         phone="", notes="", source="intake", consent=0,
                         campaign_id=None, owner_user_id=None):
    """Find an existing applicant for (nct, email) or create one. Returns
    (lead_id, token, created: bool). This is the single entrypoint every capture
    channel (inbound email, CSV import, campaign landing) routes through, so
    dedupe and attribution stay consistent. owner_user_id ties a lead to the
    workspace that received it (inbox product; may have no study yet)."""
    existing = find_lead_by_nct_email(nct, email) if email else None
    if existing:
        # Backfill attribution if we now know the campaign and didn't before.
        if campaign_id and not existing["campaign_id"]:
            set_lead_campaign(existing["id"], campaign_id)
        # Backfill ownership if this lead predates the inbox product.
        if owner_user_id and not (
                "owner_user_id" in existing.keys() and existing["owner_user_id"]):
            get_db().execute(
                "UPDATE leads SET owner_user_id = ? WHERE id = ?",
                (owner_user_id, existing["id"]))
            get_db().commit()
        return existing["id"], existing["token"], False
    token = create_lead({
        "nct": (nct or "").strip(),
        "title": title,
        "condition": condition,
        "name": name,
        "email": email,
        "phone": phone,
        "notes": notes,
        "consent": 1 if consent else 0,
        "source": source or "intake",
        "owner_user_id": owner_user_id,
    })
    lead = get_lead_by_token(token)
    if campaign_id:
        set_lead_campaign(lead["id"], campaign_id)
    return lead["id"], token, True


def import_leads_csv(user_id, nct, rows, source="csv_import", title="",
                     condition=""):
    """Bulk-load a site's existing applicant list. `rows` is an iterable of dicts
    with any of: name, email, phone, notes. Deduplicates via match_or_create_lead
    so re-importing the same sheet is safe. Returns a small summary."""
    created = matched = skipped = 0
    ids = []
    for r in rows or []:
        email = (r.get("email") or "").strip()
        name = (r.get("name") or "").strip()
        if not email and not name:
            skipped += 1
            continue
        lid, _tok, is_new = match_or_create_lead(
            nct, email=email, name=name, phone=(r.get("phone") or "").strip(),
            notes=(r.get("notes") or "").strip(), title=title,
            condition=condition, source=source)
        ids.append(lid)
        created += 1 if is_new else 0
        matched += 0 if is_new else 1
    return {"created": created, "matched": matched, "skipped": skipped,
            "ids": ids}


def accept_candidate(lead_id, note=""):
    """Study team accepts a blinded candidate: mark likely eligible and reveal
    contact + record (mutual consent - the patient already opted in on apply)."""
    lead = get_lead(lead_id)
    if not lead:
        return False
    db = get_db()
    ts = now()
    db.execute("UPDATE leads SET status = 'eligible', decision = 'accepted', "
               "decided_at = ?, revealed = 1, updated_at = ? WHERE id = ?",
               (ts, ts, lead_id))
    msg = "accepted - likely eligible" + (f": {note}" if note else "")
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)", (lead_id, "eligible", msg, "you", ts))
    db.commit()
    return True


def reveal_lead_from_inbox(lead_id, note=""):
    """Un-blind an applicant the site created from a conversation it is already
    having.

    `revealed` gates whether the study team may see contact details and message
    someone. That gate exists for the CTMS path, where a patient applies through
    BridgeMD and stays a candidate code until the site accepts them. It makes no
    sense on the inbox path: the person emailed or DM'd the site, so the site
    already holds their name, their handle and the whole thread. Leaving those
    records blinded hid nothing and broke every downstream surface (no name in
    the queue, no contact column, no blast selection, no messaging).

    Deliberately narrower than accept_candidate: no `decision` is recorded and
    the status is left alone, so the coordinator still reviews the person. Only
    the blinding comes off, and the permission basis is written to the event log
    so the reveal stays auditable.
    """
    lead = get_lead(lead_id)
    if not lead:
        return False
    db = get_db()
    ts = now()
    db.execute("UPDATE leads SET revealed = 1, consent = 1, updated_at = ? "
               "WHERE id = ?", (ts, lead_id))
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)",
        (lead_id, lead["status"] or "prescreen",
         note or "added as an applicant from a conversation", "site", ts))
    db.commit()
    return True


def decline_candidate(lead_id, reason=""):
    lead = get_lead(lead_id)
    if not lead:
        return False
    db = get_db()
    ts = now()
    db.execute("UPDATE leads SET status = 'closed', decision = 'declined', "
               "decision_reason = ?, decided_at = ?, updated_at = ? WHERE id = ?",
               (reason, ts, ts, lead_id))
    msg = "not a match" + (f": {reason}" if reason else "")
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)", (lead_id, "closed", msg, "you", ts))
    db.commit()
    return True


def accept_candidate_by_token(token, note=""):
    lead = get_lead_by_site_token(token)
    if not site_token_active(lead):
        return False
    return accept_candidate(lead["id"], note) if lead else False


def decline_candidate_by_token(token, reason=""):
    lead = get_lead_by_site_token(token)
    if not site_token_active(lead):
        return False
    return decline_candidate(lead["id"], reason) if lead else False


def advance_by_token(token, status, note=""):
    """Coordinator moves an already-accepted candidate forward (screening,
    enrolled) via their secure link."""
    lead = get_lead_by_site_token(token)
    if not site_token_active(lead):
        return False
    if not lead:
        return False
    return update_lead_status(lead["id"], status, note, actor="site")


def list_leads():
    return get_db().execute(
        "SELECT * FROM leads ORDER BY updated_at DESC, id DESC").fetchall()


def list_leads_for_user(user_id):
    """Every lead this team can see: those on a study the team has VERIFIED-
    claimed (the CTMS path) PLUS those directly OWNED by a team member's
    workspace (the inbox path - forwarded/imported mail that may have no study
    yet). Union means a self-serve site sees its own inbound with zero claim
    setup, while cross-org leakage stays impossible (both filters are scoped to
    this user's org members)."""
    members = org_member_ids(user_id)
    claims = sorted(user_claimed_ncts(user_id))
    where = []
    args = []
    if claims:
        where.append(f"nct IN ({','.join('?' * len(claims))})")
        args.extend(claims)
    if members:
        where.append(f"owner_user_id IN ({','.join('?' * len(members))})")
        args.extend(members)
    if not where:
        return []
    return get_db().execute(
        f"SELECT * FROM leads WHERE {' OR '.join(where)} "
        "ORDER BY updated_at DESC, id DESC", args).fetchall()


def list_leads_by_applicant(applicant_token):
    if not applicant_token:
        return []
    return get_db().execute(
        "SELECT * FROM leads WHERE applicant_token = ? "
        "ORDER BY updated_at DESC, id DESC", (applicant_token,)).fetchall()


def applied_ncts(applicant_token):
    """NCT ids this visitor has active (non-withdrawn) applications for."""
    if not applicant_token:
        return set()
    rows = get_db().execute(
        "SELECT nct FROM leads WHERE applicant_token = ? AND status != 'withdrawn'",
        (applicant_token,)).fetchall()
    return {r["nct"] for r in rows if r["nct"]}


def count_applications(applicant_token):
    if not applicant_token:
        return 0
    r = get_db().execute(
        "SELECT COUNT(*) n FROM leads WHERE applicant_token = ? "
        "AND status != 'withdrawn'", (applicant_token,)).fetchone()
    return r["n"] if r else 0


def get_lead(lead_id):
    return get_db().execute(
        "SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()


def get_lead_by_token(token):
    if not token:
        return None
    return get_db().execute(
        "SELECT * FROM leads WHERE token = ?", (token,)).fetchone()


def get_lead_by_site_token(token):
    if not token:
        return None
    return get_db().execute(
        "SELECT * FROM leads WHERE site_token = ?", (token,)).fetchone()


def revoke_site_token(lead_id, revoked=True):
    lead = get_lead(lead_id)
    if not lead:
        return False
    db = get_db()
    db.execute("UPDATE leads SET site_token_revoked = ?, updated_at = ? WHERE id = ?",
               (1 if revoked else 0, now(), lead_id))
    db.commit()
    return True


def extend_site_token(lead_id, days=None):
    lead = get_lead(lead_id)
    if not lead:
        return False
    db = get_db()
    db.execute("UPDATE leads SET site_token_expires_at = ?, site_token_revoked = 0, "
               "updated_at = ? WHERE id = ?",
               (_site_token_expiry(days), now(), lead_id))
    db.commit()
    return True


def get_lead_events(lead_id):
    return get_db().execute(
        "SELECT * FROM lead_events WHERE lead_id = ? ORDER BY id ASC",
        (lead_id,)).fetchall()


# Stamped on each clinic_notify_json row so we can resend when the outbound
# copy changes (e.g. sponsor-only -> local clinic lookup).
CLINIC_NOTIFY_COPY = "clinic_lookup_v5"


def record_clinic_notify(lead_id, recipients):
    """Persist which clinics were selected for the auto-notify on apply.

    `recipients` is a list of dicts with at least email + facility + source.
    Always stored, even when SMTP is off, so the operator can see the intended
    clinic handoff and the secure link still works if they send it by hand.
    """
    db = get_db()
    if not get_lead(lead_id):
        return False
    payload = []
    for rec in recipients or []:
        email = (rec.get("email") or "").strip()
        if not email:
            continue
        payload.append({
            "email": email,
            "facility": (rec.get("facility") or "").strip(),
            "city": (rec.get("city") or "").strip(),
            "source": (rec.get("source") or "").strip(),
            "name": (rec.get("name") or "").strip(),
            "copy": CLINIC_NOTIFY_COPY,
        })
    db.execute(
        "UPDATE leads SET clinic_notify_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False), now(), lead_id))
    if payload:
        note = "clinic notified: " + ", ".join(
            (p["facility"] or p["email"]) for p in payload[:4])
        db.execute(
            "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
            "VALUES (?,?,?,?,?)",
            (lead_id, "prescreen", note, "system", now()))
    db.commit()
    return True


def leads_missing_clinic_notify(copy=CLINIC_NOTIFY_COPY):
    """Real inbound applies that have not had the current clinic send.

    Used to email existing applicants once when we change the study-team
    message. Demo/seed rows are excluded by source.
    """
    out = []
    rows = get_db().execute(
        "SELECT * FROM leads WHERE source IN ('web', 'referral') "
        "ORDER BY id ASC").fetchall()
    for row in rows:
        saved = lead_clinic_notify(row)
        if not saved or not any((r.get("copy") or "") == copy for r in saved):
            out.append(row)
    return out


def lead_clinic_notify(lead):
    """Parse clinic_notify_json from a lead row. Returns a list of dicts."""
    raw = ""
    try:
        raw = lead["clinic_notify_json"] or ""
    except (KeyError, IndexError, TypeError):
        raw = ""
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return rows if isinstance(rows, list) else []


def mark_connect_emailed(lead_id):
    """Stamp that we emailed this applicant their application thread."""
    if not get_lead(lead_id):
        return False
    db = get_db()
    db.execute(
        "UPDATE leads SET connect_emailed_at = ?, updated_at = ? WHERE id = ?",
        (now(), now(), lead_id))
    db.commit()
    return True


def leads_missing_connect_email():
    """Inbound applies that still need the applicant thread email."""
    rows = get_db().execute(
        "SELECT * FROM leads WHERE COALESCE(connect_emailed_at, '') = '' "
        "AND COALESCE(email, '') != '' "
        "AND COALESCE(source, '') NOT IN ('demo', 'emr') "
        "ORDER BY id ASC").fetchall()
    return rows


def set_lead_prescreen(lead_id, eligibility_json="", readiness_json=""):
    """Persist an AI pre-screen result on a lead. Stores the structured
    eligibility read (verdict + met/unknown/not_met, so the queue can show the
    reason a verdict was reached) and the readiness estimate. Decision-support
    only - never auto-accepts or auto-rejects; a human still decides."""
    db = get_db()
    if not get_lead(lead_id):
        return False
    db.execute(
        "UPDATE leads SET eligibility = ?, prescreen_readiness = ?, "
        "updated_at = ? WHERE id = ?",
        (eligibility_json or "", readiness_json or "", now(), lead_id))
    db.commit()
    return True


def update_lead_status(lead_id, status, note="", actor="you"):
    if status not in LEAD_STATUSES:
        return False
    db = get_db()
    if not get_lead(lead_id):
        return False
    ts = now()
    db.execute("UPDATE leads SET status = ?, updated_at = ? WHERE id = ?",
               (status, ts, lead_id))
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)", (lead_id, status, note, actor, ts))
    db.commit()
    return True


def add_reconciliation(lead_id, outcome, source_system="", source_ref="",
                       note="", actor="site"):
    """Append an auditable outcome event for a lead.

    If enrollment is verified here, auto-advance the lead to enrolled so the
    operational funnel and commercial truth stay in sync.
    """
    if outcome not in RECON_OUTCOMES:
        return False
    lead = get_lead(lead_id)
    if not lead:
        return False
    db = get_db()
    ts = now()
    db.execute(
        "INSERT INTO lead_reconciliations (lead_id, outcome, source_system, "
        "source_ref, note, actor, created_at) VALUES (?,?,?,?,?,?,?)",
        (lead_id, outcome, (source_system or "").strip(), (source_ref or "").strip(),
         (note or "").strip(), actor, ts))
    if outcome == "enrolled_verified" and lead["status"] != "enrolled":
        db.execute("UPDATE leads SET status = 'enrolled', updated_at = ? WHERE id = ?",
                   (ts, lead_id))
        db.execute(
            "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
            "VALUES (?,?,?,?,?)",
            (lead_id, "enrolled", "enrollment verified from source of truth",
             actor, ts))
    db.commit()
    return True


def get_reconciliations(lead_id):
    return get_db().execute(
        "SELECT * FROM lead_reconciliations WHERE lead_id = ? ORDER BY id DESC",
        (lead_id,)).fetchall()


def latest_reconciliation(lead_id):
    return get_db().execute(
        "SELECT * FROM lead_reconciliations WHERE lead_id = ? "
        "ORDER BY id DESC LIMIT 1", (lead_id,)).fetchone()


def latest_reconciliation_for_leads(lead_ids):
    """Return {lead_id: latest reconciliation row} for a list of ids."""
    lead_ids = [int(x) for x in (lead_ids or [])]
    if not lead_ids:
        return {}
    qs = ",".join("?" * len(lead_ids))
    rows = get_db().execute(
        "SELECT r.* FROM lead_reconciliations r "
        "JOIN (SELECT lead_id, MAX(id) max_id FROM lead_reconciliations "
        f"WHERE lead_id IN ({qs}) GROUP BY lead_id) z ON z.max_id = r.id",
        lead_ids).fetchall()
    return {r["lead_id"]: r for r in rows}


def set_lead_schedule(lead_id, url, actor="site"):
    """Attach the study team's booking link to an accepted candidate so the
    patient can self-schedule their screening call. Moves the app to 'screening'
    and logs it on the timeline. Returns the lead row (or None)."""
    lead = get_lead(lead_id)
    if not lead:
        return None
    db = get_db()
    ts = now()
    db.execute("UPDATE leads SET schedule_url = ?, updated_at = ? WHERE id = ?",
               (url, ts, lead_id))
    if url and lead["status"] in ("eligible", "prescreen", "submitted"):
        db.execute("UPDATE leads SET status = 'screening' WHERE id = ?", (lead_id,))
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)",
        (lead_id, "screening" if url else lead["status"],
         "sent booking link" if url else "removed booking link", actor, ts))
    db.commit()
    return get_lead(lead_id)


def lead_contactable(lead_id):
    """True if this applicant can be messaged: exists, revealed, not opted out."""
    lead = get_lead(lead_id)
    if not lead or not lead["revealed"]:
        return False
    try:
        return not int(lead["contact_opt_out"] or 0)
    except (KeyError, IndexError, TypeError):
        return True


def set_contact_opt_out(lead_id, opted_out=True, actor="patient"):
    """Record a messaging opt-out (or clear it). Honored by every send path.
    Logged on the timeline so the opt-out is auditable."""
    lead = get_lead(lead_id)
    if not lead:
        return None
    db = get_db()
    ts = now()
    db.execute(
        "UPDATE leads SET contact_opt_out = ?, contact_opt_out_at = ?, "
        "updated_at = ? WHERE id = ?",
        (1 if opted_out else 0, ts if opted_out else "", ts, lead_id))
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)",
        (lead_id, lead["status"],
         "opted out of messages" if opted_out else "opted back into messages",
         actor, ts))
    db.commit()
    return get_lead(lead_id)


def set_lead_video(lead_id, url, actor="site"):
    """Attach a video-call link (Zoom/Google Meet) to a candidate's screening
    visit and log it on the timeline. Returns the lead row (or None)."""
    lead = get_lead(lead_id)
    if not lead:
        return None
    db = get_db()
    ts = now()
    db.execute("UPDATE leads SET video_url = ?, updated_at = ? WHERE id = ?",
               (url, ts, lead_id))
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)",
        (lead_id, lead["status"],
         "added video call link" if url else "removed video call link", actor, ts))
    db.commit()
    return get_lead(lead_id)


# --------------------------------------------------------------------------- #
# Messaging (two-way patient <-> study team, + system nudges)
# --------------------------------------------------------------------------- #
def add_message(lead_id, sender, body):
    """Append a message to an application thread. sender in
    {'patient','site','system'}. The sender's own side is marked read."""
    body = (body or "").strip()
    if not body or sender not in ("patient", "site", "system"):
        return None
    db = get_db()
    db.execute(
        "INSERT INTO messages (lead_id, sender, body, read_patient, read_site, "
        "created_at) VALUES (?,?,?,?,?,?)",
        (lead_id, sender, body, 1 if sender == "patient" else 0,
         1 if sender == "site" else 0, now()))
    db.execute("UPDATE leads SET updated_at = ? WHERE id = ?", (now(), lead_id))
    db.commit()
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def get_messages(lead_id):
    return get_db().execute(
        "SELECT * FROM messages WHERE lead_id = ? ORDER BY id ASC",
        (lead_id,)).fetchall()


def mark_thread_read(lead_id, side):
    """side in {'patient','site'} - clears that reader's unread flags."""
    col = "read_patient" if side == "patient" else "read_site"
    db = get_db()
    db.execute(f"UPDATE messages SET {col} = 1 WHERE lead_id = ?", (lead_id,))
    db.commit()


def unread_for_patient(applicant_token):
    """Messages the patient hasn't read (from site/system) across their apps."""
    if not applicant_token:
        return 0
    r = get_db().execute(
        "SELECT COUNT(*) n FROM messages m JOIN leads l ON l.id = m.lead_id "
        "WHERE l.applicant_token = ? AND m.sender != 'patient' "
        "AND m.read_patient = 0", (applicant_token,)).fetchone()
    return r["n"] if r else 0


def unread_for_site(user_id=None):
    """Patient messages the study team hasn't read (badge on the board)."""
    if user_id:
        claims = sorted(user_claimed_ncts(user_id))
        if not claims:
            return 0
        qs = ",".join("?" * len(claims))
        q = ("SELECT COUNT(*) n FROM messages m JOIN leads l ON l.id = m.lead_id "
             f"WHERE m.sender = 'patient' AND m.read_site = 0 AND l.nct IN ({qs})")
        r = get_db().execute(q, claims).fetchone()
        return r["n"] if r else 0
    r = get_db().execute(
        "SELECT COUNT(*) n FROM messages WHERE sender = 'patient' "
        "AND read_site = 0").fetchone()
    return r["n"] if r else 0


def lead_unread_for_site(lead_id):
    r = get_db().execute(
        "SELECT COUNT(*) n FROM messages WHERE lead_id = ? AND sender = 'patient' "
        "AND read_site = 0", (lead_id,)).fetchone()
    return r["n"] if r else 0


def last_message(lead_id):
    """Most recent message on a thread (any sender), or None. Used by the inbox
    to show a one-line preview without loading the whole thread."""
    return get_db().execute(
        "SELECT * FROM messages WHERE lead_id = ? ORDER BY id DESC LIMIT 1",
        (lead_id,)).fetchone()


# --------------------------------------------------------------------------- #
# Inbox triage + shared-workspace assignment
#
# Triage is DECISION SUPPORT only: it labels what a thread is about and how
# urgent it is so a coordinator can clear the inbox top-down. A human always
# acts - we never auto-reply or auto-close from a triage label. Assignment lets
# a team share one inbox without stepping on each other.
# --------------------------------------------------------------------------- #
def set_lead_triage(lead_id, intent="", priority=""):
    db = get_db()
    db.execute(
        "UPDATE leads SET triage_intent = ?, triage_priority = ?, triage_at = ? "
        "WHERE id = ?",
        ((intent or "").strip(), (priority or "").strip(), now(), lead_id))
    db.commit()


def set_lead_assignee(lead_id, user_id):
    """Assign (user_id) or unassign (None) a thread to a teammate."""
    db = get_db()
    db.execute("UPDATE leads SET assigned_user_id = ?, updated_at = ? WHERE id = ?",
               (user_id, now(), lead_id))
    db.commit()


# --------------------------------------------------------------------------- #
# Auto-routing rules (channel/study -> teammate)
# --------------------------------------------------------------------------- #
# Canonical channels a rule can match, in the order shown in the UI.
ROUTING_CHANNELS = ("any", "email_intake", "instagram", "messenger", "facebook",
                    "meta", "google", "ctgov", "reddit", "referral")


def list_routing_rules(user_id):
    """All routing rules for this user's org, in evaluation order. Joins the
    assignee's display name so the UI/inbox can show who a channel routes to."""
    oid = user_org_id(user_id)
    return get_db().execute(
        "SELECT r.*, u.name AS assignee_name, u.email AS assignee_email "
        "FROM routing_rules r JOIN users u ON u.id = r.assignee_id "
        "WHERE r.org_id = ? ORDER BY r.position, r.id", (oid,)).fetchall()


def add_routing_rule(actor_user_id, channel, nct, assignee_id):
    """Create a rule routing (channel, nct) -> assignee. Scoped to the actor's
    org; the assignee must be a same-org member. Returns the new rule id or None."""
    oid = user_org_id(actor_user_id)
    if assignee_id not in set(org_member_ids(actor_user_id)):
        return None
    channel = channel if channel in ROUTING_CHANNELS else "any"
    nct = (nct or "").strip()
    db = get_db()
    nextpos = db.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 AS p FROM routing_rules "
        "WHERE org_id = ?", (oid,)).fetchone()["p"]
    cur = db.execute(
        "INSERT INTO routing_rules (org_id, channel, nct, assignee_id, position, "
        "active, created_by, created_at) VALUES (?,?,?,?,?,1,?,?)",
        (oid, channel, nct, assignee_id, nextpos, actor_user_id, now()))
    db.commit()
    return cur.lastrowid


def delete_routing_rule(actor_user_id, rule_id):
    """Remove a rule (org-scoped so no cross-team deletes)."""
    oid = user_org_id(actor_user_id)
    db = get_db()
    cur = db.execute("DELETE FROM routing_rules WHERE id = ? AND org_id = ?",
                     (rule_id, oid))
    db.commit()
    return cur.rowcount > 0


def match_routing_rule(org_id, channel, nct):
    """Return the assignee_id for the first active rule matching (channel, nct),
    or None. A rule with channel/nct 'any'/'' is a wildcard; more specific rules
    should be ordered before wildcards (lower position). First match wins."""
    channel = (channel or "").strip()
    nct = (nct or "").strip()
    rows = get_db().execute(
        "SELECT channel, nct, assignee_id FROM routing_rules "
        "WHERE org_id = ? AND active = 1 ORDER BY position, id", (org_id,)
    ).fetchall()
    for r in rows:
        if r["channel"] not in ("any", channel):
            continue
        if r["nct"] and r["nct"] != nct:
            continue
        return r["assignee_id"]
    return None


def apply_routing_rules(lead_id, channel="", nct=""):
    """Auto-assign a freshly captured lead per its owner org's routing rules.
    No-op if the lead is already assigned (never steal a human's claim) or no
    rule matches. Returns the assigned user_id, or None. Never raises."""
    try:
        lead = get_lead(lead_id)
        if not lead:
            return None
        if "assigned_user_id" in lead.keys() and lead["assigned_user_id"]:
            return None
        owner = lead["owner_user_id"] if "owner_user_id" in lead.keys() else None
        if not owner:
            return None
        oid = user_org_id(owner)
        assignee = match_routing_rule(
            oid, channel or (lead["source"] if "source" in lead.keys() else ""),
            nct or lead["nct"])
        if not assignee:
            return None
        # Coverage applies to NEW work too: if the teammate a rule points at is
        # away, the lead lands with whoever is covering. Doing the swap here is
        # what makes every intake path (email, Instagram, Meta, referral,
        # CT.gov) respect an away period without knowing it exists.
        assignee = effective_assignee(assignee)
        set_lead_assignee(lead_id, assignee)
        return assignee
    except Exception:
        return None


def reassign_open_leads(actor_user_id, from_user_id, to_user_id):
    """Coverage / vacation handoff: move every OPEN thread currently assigned to
    `from_user_id` over to `to_user_id` (or None to just unassign), in one call.
    Scoped to the actor's own workspace (owned leads OR verified-claim studies)
    and to same-org members, so no one can reassign another team's threads.
    Returns the number of threads moved."""
    members = set(org_member_ids(actor_user_id))
    if from_user_id not in members:
        return 0
    if to_user_id is not None and to_user_id not in members:
        return 0
    claims = sorted(user_claimed_ncts(actor_user_id))
    scope = ["owner_user_id IN (%s)" % ",".join("?" * len(members))]
    args = list(members)
    if claims:
        scope.append("nct IN (%s)" % ",".join("?" * len(claims)))
        args.extend(claims)
    closed = tuple(LEAD_CLOSED)
    q = (f"UPDATE leads SET assigned_user_id = ?, updated_at = ? "
         f"WHERE assigned_user_id = ? AND ({' OR '.join(scope)}) "
         f"AND status NOT IN ({','.join('?' * len(closed))})")
    db = get_db()
    cur = db.execute(q, [to_user_id, now(), from_user_id, *args, *closed])
    db.commit()
    return cur.rowcount




# --------------------------------------------------------------------------- #
# @mentions - looping a teammate in without CC-ing them
# --------------------------------------------------------------------------- #
# A handle is what someone types after "@". We accept letters, digits and the
# separators that show up in real names and emails, so "@ana", "@ana.diaz" and
# "@ana-diaz" all resolve. Sentence punctuation is excluded, so "@ana, can you
# look?" still matches "ana".
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9._-]{0,40})")

# Where a mention may be recorded. Both are staff-only surfaces. The `messages`
# table is deliberately absent: it is patient-visible.
MENTION_NOTE_TYPES = ("lead_note", "marketing_message")
MENTION_OBJECT_TYPES = ("lead", "marketing_thread")


def _mention_handles(user_row):
    """Every handle a member can be addressed by, lowercased: first name, the
    full name with and without dots, and the email local-part."""
    out = set()
    name = (_row_val(user_row, "name", "") or "").strip()
    email = (_row_val(user_row, "email", "") or "").strip()
    if name:
        parts = [x for x in re.split(r"\s+", name) if x]
        if parts:
            out.add(parts[0].lower())
            out.add("".join(parts).lower())
            out.add(".".join(parts).lower())
    if "@" in email:
        out.add(email.split("@", 1)[0].strip().lower())
    return {h for h in out if h}


def mentionable_members(user_id):
    """The roster the @-picker offers: every teammate with a display name and
    the handle that will actually resolve back to them."""
    out = []
    for m in list_org_members(user_id):
        name = (m["name"] or "").strip() or (m["email"] or "").split("@")[0]
        handles = sorted(_mention_handles(m))
        out.append({
            "user_id": m["user_id"],
            "name": name,
            "email": m["email"] or "",
            "role": ORG_ROLE_LABELS.get(m["role"], m["role"] or "Team"),
            "handle": (handles[0] if handles else str(m["user_id"])),
            "handles": handles,
        })
    return out


def parse_mentions(actor_user_id, body):
    """Map @handles in a note body to teammate user ids.

    Ambiguity is resolved by NOT guessing: if two teammates answer to the same
    handle the text stays plain rather than notifying the wrong person (the
    picker inserts an unambiguous handle, which is the happy path). Returns a
    list of {user_id, name, handle}."""
    body = body or ""
    found = [m.group(1).lower() for m in MENTION_RE.finditer(body)]
    if not found:
        return []
    index = {}
    for m in mentionable_members(actor_user_id):
        for h in m["handles"]:
            index.setdefault(h, []).append(m)
    out, seen = [], set()
    for h in found:
        cands = index.get(h) or []
        if len(cands) != 1:
            continue
        m = cands[0]
        if m["user_id"] in seen:
            continue
        seen.add(m["user_id"])
        out.append({"user_id": m["user_id"], "name": m["name"], "handle": h})
    return out


def record_mentions(author_id, object_type, object_id, note_type, note_id, body):
    """Persist every resolvable @mention in `body`. Self-mentions are skipped -
    you don't need to be notified about your own note. Returns the mentioned
    members so the caller can tell the author who was pulled in."""
    if object_type not in MENTION_OBJECT_TYPES:
        return []
    if note_type not in MENTION_NOTE_TYPES:
        return []
    people = [m for m in parse_mentions(author_id, body)
              if m["user_id"] != author_id]
    if not people:
        return []
    oid = user_org_id(author_id)
    excerpt = (body or "").strip()
    if len(excerpt) > 280:
        excerpt = excerpt[:277].rstrip() + "..."
    db = get_db()
    ts = now()
    for m in people:
        db.execute(
            "INSERT INTO mentions (org_id, object_type, object_id, note_type, "
            "note_id, mentioned_id, author_id, excerpt, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (oid, object_type, object_id, note_type, note_id, m["user_id"],
             author_id, excerpt, ts))
    db.commit()
    return people


def unread_mention_count(user_id):
    r = get_db().execute(
        "SELECT COUNT(*) n FROM mentions WHERE mentioned_id = ? AND read_at = ''",
        (user_id,)).fetchone()
    return r["n"] if r else 0


def list_mentions(user_id, unread_only=False, limit=50):
    """Threads this user was pulled into, newest first, with enough context to
    render a row without a second query per item."""
    q = ("SELECT m.*, u.name AS author_name, u.email AS author_email "
         "FROM mentions m LEFT JOIN users u ON u.id = m.author_id "
         "WHERE m.mentioned_id = ?")
    args = [user_id]
    if unread_only:
        q += " AND m.read_at = ''"
    q += " ORDER BY m.id DESC LIMIT ?"
    args.append(int(limit))
    return get_db().execute(q, args).fetchall()


def mark_mentions_read(user_id, object_type=None, object_id=None):
    """Clear the badge - for one thread when given, otherwise for everything."""
    q = "UPDATE mentions SET read_at = ? WHERE mentioned_id = ? AND read_at = ''"
    args = [now(), user_id]
    if object_type and object_id:
        q += " AND object_type = ? AND object_id = ?"
        args.extend([object_type, int(object_id)])
    db = get_db()
    cur = db.execute(q, args)
    db.commit()
    return cur.rowcount


# --------------------------------------------------------------------------- #
# Away coverage - handing a whole queue to a teammate, and getting it back
# --------------------------------------------------------------------------- #
def _today():
    return dt.date.today().isoformat()


def display_name(user_id):
    """What to call a teammate in the UI: their name, else their email handle."""
    if not user_id:
        return ""
    r = get_db().execute("SELECT name, email FROM users WHERE id = ?",
                         (user_id,)).fetchone()
    if not r:
        return ""
    return (r["name"] or "").strip() or (r["email"] or "").split("@")[0]


def reassign_open_marketing_threads(actor_user_id, from_user_id, to_user_id):
    """Mirror of reassign_open_leads for the omnichannel inbox: move every
    unresolved thread still assigned to `from_user_id`. Org-scoped both ways, so
    no one can move another team's conversations. Returns the ids moved (not a
    count) so the handoff ledger can record each one individually."""
    members = set(org_member_ids(actor_user_id))
    if from_user_id not in members or to_user_id not in members:
        return []
    db = get_db()
    ids = _open_thread_ids_for(actor_user_id, from_user_id)
    if ids:
        db.execute(
            "UPDATE marketing_threads SET assigned_to = ?, updated_at = ? "
            "WHERE id IN (%s)" % ",".join("?" * len(ids)),
            [to_user_id, now(), *ids])
        db.commit()
    return ids


def _open_lead_ids_for(actor_user_id, assignee_id):
    """Open leads currently assigned to `assignee_id`, within the actor's scope.
    Mirrors the scoping in reassign_open_leads exactly, so the ledger and the
    update can never disagree about which rows were in play."""
    members = set(org_member_ids(actor_user_id))
    if assignee_id not in members:
        return []
    claims = sorted(user_claimed_ncts(actor_user_id))
    scope = ["owner_user_id IN (%s)" % ",".join("?" * len(members))]
    args = list(members)
    if claims:
        scope.append("nct IN (%s)" % ",".join("?" * len(claims)))
        args.extend(claims)
    closed = tuple(LEAD_CLOSED)
    rows = get_db().execute(
        "SELECT id FROM leads WHERE assigned_user_id = ? AND (%s) "
        "AND status NOT IN (%s)" % (" OR ".join(scope),
                                    ",".join("?" * len(closed))),
        [assignee_id, *args, *closed]).fetchall()
    return [r["id"] for r in rows]


def _open_thread_ids_for(actor_user_id, assignee_id):
    """Unresolved inbox threads assigned to `assignee_id`. Same scoping as
    reassign_open_marketing_threads, so a preview count and the actual move can
    never disagree."""
    if assignee_id not in set(org_member_ids(actor_user_id)):
        return []
    rows = get_db().execute(
        "SELECT id FROM marketing_threads WHERE org_id = ? AND assigned_to = ? "
        "AND status != 'resolved'",
        (user_org_id(actor_user_id), assignee_id)).fetchall()
    return [r["id"] for r in rows]


def open_queue_count(user_id):
    """How much work would move if this person went away right now. Uses the
    exact same two queries the handoff uses, so the number on the button is the
    number of conversations that actually move."""
    return (len(_open_lead_ids_for(user_id, user_id))
            + len(_open_thread_ids_for(user_id, user_id)))


def _log_moves(away_id, kind, ids, from_user, to_user, direction="out"):
    if not ids:
        return
    db = get_db()
    ts = now()
    db.executemany(
        "INSERT INTO handoff_moves (away_id, kind, object_id, from_user, "
        "to_user, direction, created_at) VALUES (?,?,?,?,?,?,?)",
        [(away_id, kind, i, from_user, to_user, direction, ts) for i in ids])
    db.commit()


def start_away(actor_id, user_id, cover_user_id, until="", note=""):
    """Open an away period and move the person's whole open queue to their cover.

    Returns (away_row, error). Everything moved is written to handoff_moves so
    end_away can reverse exactly this set and nothing else."""
    members = set(org_member_ids(actor_id))
    if user_id not in members or cover_user_id not in members:
        return None, "Pick a teammate on your team."
    if user_id == cover_user_id:
        return None, "Choose someone other than yourself to cover."
    if active_away_for(user_id):
        return None, "That person already has coverage turned on."
    if until:
        try:
            if dt.date.fromisoformat(until) < dt.date.today():
                return None, "The return date can't be in the past."
        except ValueError:
            return None, "Choose a valid return date."

    oid = user_org_id(user_id)
    db = get_db()
    cur = db.execute(
        "INSERT INTO away_periods (org_id, user_id, cover_user_id, starts_at, "
        "until, note, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,'active',?,?)",
        (oid, user_id, cover_user_id, now(), (until or "").strip(),
         (note or "").strip(), actor_id, now()))
    away_id = cur.lastrowid
    db.commit()

    lead_ids = _open_lead_ids_for(actor_id, user_id)
    reassign_open_leads(actor_id, user_id, cover_user_id)
    _log_moves(away_id, "lead", lead_ids, user_id, cover_user_id, "out")

    thread_ids = reassign_open_marketing_threads(actor_id, user_id,
                                                 cover_user_id)
    _log_moves(away_id, "marketing_thread", thread_ids, user_id, cover_user_id,
               "out")

    db.execute("UPDATE away_periods SET moved_leads = ?, moved_threads = ? "
               "WHERE id = ?", (len(lead_ids), len(thread_ids), away_id))
    db.commit()
    return get_away(away_id), None


def end_away(away_id, actor_id=None):
    """Close a period and hand back exactly what was moved out.

    Only items the cover person STILL owns come back: if they deliberately
    passed something to a third teammate, that decision stands. Idempotent."""
    row = get_away(away_id)
    if not row or row["status"] != "active":
        return 0
    db = get_db()
    back_leads, back_threads = [], []
    moves = db.execute(
        "SELECT kind, object_id FROM handoff_moves WHERE away_id = ? AND "
        "direction = 'out'", (away_id,)).fetchall()
    for m in moves:
        if m["kind"] == "lead":
            cur = db.execute(
                "UPDATE leads SET assigned_user_id = ?, updated_at = ? "
                "WHERE id = ? AND assigned_user_id = ?",
                (row["user_id"], now(), m["object_id"], row["cover_user_id"]))
            if cur.rowcount:
                back_leads.append(m["object_id"])
        else:
            cur = db.execute(
                "UPDATE marketing_threads SET assigned_to = ?, updated_at = ? "
                "WHERE id = ? AND assigned_to = ?",
                (row["user_id"], now(), m["object_id"], row["cover_user_id"]))
            if cur.rowcount:
                back_threads.append(m["object_id"])
    db.execute("UPDATE away_periods SET status = 'ended', ended_at = ? "
               "WHERE id = ?", (now(), away_id))
    db.commit()
    _log_moves(away_id, "lead", back_leads, row["cover_user_id"],
               row["user_id"], "back")
    _log_moves(away_id, "marketing_thread", back_threads, row["cover_user_id"],
               row["user_id"], "back")
    return len(back_leads) + len(back_threads)


def get_away(away_id):
    return get_db().execute("SELECT * FROM away_periods WHERE id = ?",
                            (away_id,)).fetchone()


def active_away_for(user_id):
    """The open away period for this user, or None.

    Expiry is LAZY: a period past its return date is ended here, on read, so
    hand-back stays correct even if the cron sweep never runs. The sweep only
    makes it happen on time instead of at next login."""
    if not user_id:
        return None
    row = get_db().execute(
        "SELECT * FROM away_periods WHERE user_id = ? AND status = 'active' "
        "ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    if not row:
        return None
    if row["until"] and row["until"] < _today():
        end_away(row["id"])
        return None
    return row


def covering_for(user_id):
    """Everyone this user is currently covering, with the counts they inherited."""
    rows = get_db().execute(
        "SELECT a.*, u.name AS away_name, u.email AS away_email "
        "FROM away_periods a LEFT JOIN users u ON u.id = a.user_id "
        "WHERE a.cover_user_id = ? AND a.status = 'active' ORDER BY a.id DESC",
        (user_id,)).fetchall()
    return [r for r in rows if not (r["until"] and r["until"] < _today())]


def sweep_expired_aways():
    """End every period past its return date. Safe to call from cron repeatedly."""
    rows = get_db().execute(
        "SELECT id FROM away_periods WHERE status = 'active' AND until != '' "
        "AND until < ?", (_today(),)).fetchall()
    for r in rows:
        end_away(r["id"])
    return len(rows)


def unseen_away_recap(user_id):
    """The most recent finished away period this user hasn't acknowledged, so
    the inbox can show a "what happened while you were out" card exactly once."""
    return get_db().execute(
        "SELECT a.*, u.name AS cover_name FROM away_periods a "
        "LEFT JOIN users u ON u.id = a.cover_user_id "
        "WHERE a.user_id = ? AND a.status = 'ended' AND a.seen_at = '' "
        "ORDER BY a.id DESC LIMIT 1", (user_id,)).fetchone()


def mark_away_seen(user_id, away_id):
    db = get_db()
    cur = db.execute(
        "UPDATE away_periods SET seen_at = ? WHERE id = ? AND user_id = ?",
        (now(), away_id, user_id))
    db.commit()
    return cur.rowcount > 0


def away_recap(user_id, away_id=None):
    """What actually changed on the threads that were covered.

    Grounded entirely in the ledger plus current row state: it counts what came
    back, what is still waiting on a reply, and what reached enrolled. It never
    speculates about why."""
    row = get_away(away_id) if away_id else unseen_away_recap(user_id)
    if not row or row["user_id"] != user_id:
        return None
    db = get_db()
    moved = db.execute(
        "SELECT kind, object_id FROM handoff_moves WHERE away_id = ? AND "
        "direction = 'out'", (row["id"],)).fetchall()
    lead_ids = [m["object_id"] for m in moved if m["kind"] == "lead"]
    thread_ids = [m["object_id"] for m in moved
                  if m["kind"] == "marketing_thread"]
    waiting = 0
    if thread_ids:
        r = db.execute(
            "SELECT COUNT(*) n FROM marketing_threads WHERE id IN (%s) "
            "AND status != 'resolved' AND unread > 0"
            % ",".join("?" * len(thread_ids)), thread_ids).fetchone()
        waiting = r["n"] if r else 0
    enrolled = 0
    if lead_ids:
        r = db.execute(
            "SELECT COUNT(*) n FROM leads WHERE id IN (%s) AND status = "
            "'enrolled'" % ",".join("?" * len(lead_ids)), lead_ids).fetchone()
        enrolled = r["n"] if r else 0
    return {
        "away": row,
        "cover_name": display_name(row["cover_user_id"]) or "your cover",
        "leads": len(lead_ids),
        "threads": len(thread_ids),
        "waiting": waiting,
        "enrolled": enrolled,
    }


def effective_assignee(user_id):
    """Swap in the cover when the intended assignee is away.

    Every intake path (email, Instagram, Meta, referral, CT.gov) routes through
    this, so coverage applies to new work without each caller knowing about it.
    Follows one hop only - if the cover is also away, the work still lands with
    them rather than bouncing around the team."""
    if not user_id:
        return user_id
    away = active_away_for(user_id)
    return away["cover_user_id"] if away else user_id


# --------------------------------------------------------------------------- #
# Blasts - messaging a filtered audience inside ONE study
# --------------------------------------------------------------------------- #
# The audience filters a coordinator can combine. Every mode narrows within a
# single study; nothing here can widen a send across studies.
BLAST_MODES = ("everyone", "stage", "tag", "owner", "idle", "selected")


def _lead_idle_days(lead):
    ts = _parse_ts(_row_val(lead, "updated_at", "")
                   or _row_val(lead, "created_at", ""))
    if not ts:
        return 0
    return (dt.datetime.now() - ts).days


def blast_audience(user_id, nct, mode="everyone", value="", lead_ids=None):
    """Resolve a blast audience to lead rows. Returns (leads, error).

    The non-negotiable rails live here and nowhere else, so no code path can
    send around them:
      * `nct` must be a study this team has VERIFIED-claimed - protocol
        materials are IRB-approved per study, so a cross-study send is refused;
      * a recipient must be revealed (accepted), not closed, and not opted out.
    `mode` narrows further. A hand-picked selection INTERSECTS the rails rather
    than replacing them, and is rejected outright if it reaches outside `nct`."""
    nct = (nct or "").strip()
    if not nct:
        return [], "Pick a study to message."
    if nct not in user_claimed_ncts(user_id):
        return [], "You can only message a study your team has claimed."

    pool = [l for l in list_leads_for_user(user_id)
            if l["nct"] == nct and l["revealed"]
            and l["status"] not in LEAD_CLOSED
            and not int(_row_val(l, "contact_opt_out", 0) or 0)]

    if mode == "selected":
        picked = {int(x) for x in (lead_ids or [])}
        if not picked:
            return [], "Select at least one applicant."
        # A selection spanning studies is exactly the wrong-cohort mistake this
        # rule exists to prevent, so it fails loudly rather than silently
        # dropping the strays.
        in_study = {l["id"] for l in pool}
        if picked - in_study:
            return [], ("Some of those applicants aren't accepted members of "
                        "this study. A blast stays inside one study.")
        return [l for l in pool if l["id"] in picked], None

    if mode == "stage":
        want = (value or "").strip()
        if want not in LEAD_PIPELINE:
            return [], "Pick a pipeline stage."
        return [l for l in pool if l["status"] == want], None

    if mode == "tag":
        want = (value or "").strip()
        if want not in _CONV_TAG_KEYS:
            return [], "Pick a tag."
        return [l for l in pool if want in lead_tags(l)], None

    if mode == "owner":
        try:
            owner = int(value)
        except (TypeError, ValueError):
            return [], "Pick a teammate."
        if owner not in set(org_member_ids(user_id)):
            return [], "Pick a teammate on your team."
        return [l for l in pool
                if _row_val(l, "assigned_user_id", None) == owner], None

    if mode == "idle":
        try:
            days = max(1, int(value))
        except (TypeError, ValueError):
            days = 7
        return [l for l in pool if _lead_idle_days(l) >= days], None

    return pool, None


def blast_audience_label(mode, value="", count=0, member_name=""):
    """A short human summary of the filter, stored with the blast and shown in
    the composer so a coordinator can always see who a send went to."""
    if mode == "stage":
        return "Stage: %s" % (value or "").title()
    if mode == "tag":
        return "Tag: " + next((t["label"] for t in CONV_TAGS
                               if t["key"] == value), value or "")
    if mode == "owner":
        return "Owner: %s" % (member_name or "teammate")
    if mode == "idle":
        return "Quiet %s+ days" % (value or 7)
    if mode == "selected":
        return "%d hand-picked" % count
    return "Everyone accepted"


def create_blast(user_id, nct, mode, value, label, body, task_title, lead_ids):
    db = get_db()
    cur = db.execute(
        "INSERT INTO blasts (org_id, sent_by, nct, audience, label, body, "
        "task_title, lead_ids, recipients, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_org_id(user_id), user_id, nct,
         json.dumps({"mode": mode, "value": value}), label, body or "",
         task_title or "", ",".join(str(i) for i in lead_ids), len(lead_ids),
         now()))
    db.commit()
    return cur.lastrowid


def list_blasts(user_id, nct="", limit=10):
    q = ("SELECT b.*, u.name AS sender_name FROM blasts b "
         "LEFT JOIN users u ON u.id = b.sent_by WHERE b.org_id = ?")
    args = [user_org_id(user_id)]
    if nct:
        q += " AND b.nct = ?"
        args.append(nct)
    q += " ORDER BY b.id DESC LIMIT ?"
    args.append(int(limit))
    return get_db().execute(q, args).fetchall()


def leads_blasted_since(user_id, lead_ids, hours=24):
    """How many of these people were already in a blast recently. Powers the
    double-send nudge in the composer - a warning, never a block."""
    ids = {int(i) for i in (lead_ids or [])}
    if not ids:
        return 0
    cutoff = (dt.datetime.now() - dt.timedelta(hours=hours)).strftime(
        "%Y-%m-%d %H:%M")
    rows = get_db().execute(
        "SELECT lead_ids FROM blasts WHERE org_id = ? AND created_at >= ?",
        (user_org_id(user_id), cutoff)).fetchall()
    recent = set()
    for r in rows:
        for chunk in (r["lead_ids"] or "").split(","):
            chunk = chunk.strip()
            if chunk.isdigit():
                recent.add(int(chunk))
    return len(ids & recent)




# --------------------------------------------------------------------------- #
# Patient portal - recruiter-issued access to one applicant's own record
# --------------------------------------------------------------------------- #
# Distinct from the self-serve `patient_users` account behind /applications: the
# recruiter hands a specific person a link plus a one-time password for THEIR
# application only. No signup, no email round-trip - which is what makes it
# usable for someone the team is already messaging.
#
# The temporary password is returned to the recruiter exactly once, at creation,
# and only its hash is ever stored - so it cannot be recovered from the database
# or re-displayed later, only reissued. `must_change` forces rotation on first
# successful sign-in, so the credential the recruiter typed into a chat stops
# working the moment the patient actually uses it.
PORTAL_MAX_ATTEMPTS = 8          # wrong passwords before a timed lockout
PORTAL_LOCKOUT_MINUTES = 15
PORTAL_MIN_PASSWORD = 8

# Ambiguous glyphs are excluded: this password gets read off a screen and typed
# by hand, so 0/O and 1/l/I would turn into support load.
_PORTAL_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_portal_password(words=3, size=4):
    """A readable one-time password, e.g. 'K7MP-3RXQ-9TFA'."""
    return "-".join(
        "".join(secrets.choice(_PORTAL_ALPHABET) for _ in range(size))
        for _ in range(words))


def get_portal_by_lead(lead_id):
    return get_db().execute(
        "SELECT * FROM portal_access WHERE lead_id = ?", (lead_id,)).fetchone()


def get_portal_by_token(token):
    token = (token or "").strip()
    if not token:
        return None
    return get_db().execute(
        "SELECT * FROM portal_access WHERE token = ? AND status = 'active'",
        (token,)).fetchone()


def issue_portal_access(lead_id, created_by, password_hasher):
    """Create or re-issue portal access for one applicant.

    Returns (row, plaintext_password). The plaintext is handed back to the
    caller ONCE so the recruiter can pass it on; it is never stored. Re-issuing
    keeps the same link but rotates the password and re-arms the forced change,
    so a lost credential is recoverable without breaking a link already sent."""
    existing = get_portal_by_lead(lead_id)
    pw = generate_portal_password()
    db = get_db()
    ts = now()
    if existing:
        db.execute(
            "UPDATE portal_access SET password_hash = ?, must_change = 1, "
            "status = 'active', failed_attempts = 0, locked_until = '', "
            "created_by = ?, created_at = ? WHERE lead_id = ?",
            (password_hasher(pw), created_by, ts, lead_id))
        db.commit()
        return get_portal_by_lead(lead_id), pw
    db.execute(
        "INSERT INTO portal_access (lead_id, token, password_hash, must_change, "
        "status, created_by, created_at) VALUES (?,?,?,1,'active',?,?)",
        (lead_id, gen_token(), password_hasher(pw), created_by, ts))
    db.commit()
    return get_portal_by_lead(lead_id), pw


def revoke_portal_access(lead_id):
    db = get_db()
    cur = db.execute(
        "UPDATE portal_access SET status = 'revoked' WHERE lead_id = ?",
        (lead_id,))
    db.commit()
    return cur.rowcount > 0


def portal_locked_for(row):
    """Minutes remaining on a lockout, or 0. Lockout is time-based rather than
    permanent so a patient fat-fingering their own code isn't locked out for
    good with no way to self-serve."""
    if not row:
        return 0
    until = _parse_ts(_row_val(row, "locked_until", "") or "")
    if not until:
        return 0
    left = (until - dt.datetime.now()).total_seconds()
    return max(0, int(left // 60) + (1 if left > 0 else 0))


def portal_check_password(token, password, verifier):
    """Verify a portal password. Returns (row, error).

    Counts failures and locks the link for a while once they pile up, so the
    link plus a short password can't be brute-forced."""
    row = get_portal_by_token(token)
    if not row:
        return None, "That link is no longer active."
    mins = portal_locked_for(row)
    if mins:
        return None, (f"Too many incorrect attempts. Try again in about "
                      f"{mins} minute(s).")
    if not verifier(row["password_hash"], password or ""):
        db = get_db()
        attempts = int(_row_val(row, "failed_attempts", 0) or 0) + 1
        locked = ""
        if attempts >= PORTAL_MAX_ATTEMPTS:
            locked = (dt.datetime.now()
                      + dt.timedelta(minutes=PORTAL_LOCKOUT_MINUTES)).strftime(
                          "%Y-%m-%d %H:%M")
            attempts = 0
        db.execute(
            "UPDATE portal_access SET failed_attempts = ?, locked_until = ? "
            "WHERE id = ?", (attempts, locked, row["id"]))
        db.commit()
        if locked:
            return None, (f"Too many incorrect attempts. Try again in about "
                          f"{PORTAL_LOCKOUT_MINUTES} minutes.")
        return None, "That password doesn't match. Check it and try again."
    db = get_db()
    ts = now()
    db.execute(
        "UPDATE portal_access SET failed_attempts = 0, locked_until = '', "
        "last_seen_at = ?, first_seen_at = CASE WHEN first_seen_at = '' THEN ? "
        "ELSE first_seen_at END WHERE id = ?", (ts, ts, row["id"]))
    db.commit()
    return get_portal_by_token(token), None


def portal_set_password(token, new_password, password_hasher):
    """Rotate the password and clear the forced-change flag."""
    row = get_portal_by_token(token)
    if not row:
        return False, "That link is no longer active."
    new_password = (new_password or "").strip()
    if len(new_password) < PORTAL_MIN_PASSWORD:
        return False, (f"Choose a password of at least {PORTAL_MIN_PASSWORD} "
                       "characters.")
    db = get_db()
    db.execute(
        "UPDATE portal_access SET password_hash = ?, must_change = 0, "
        "password_set_at = ? WHERE id = ?",
        (password_hasher(new_password), now(), row["id"]))
    db.commit()
    return True, None


def portal_touch(token):
    db = get_db()
    db.execute("UPDATE portal_access SET last_seen_at = ? WHERE token = ?",
               (now(), token))
    db.commit()

# --------------------------------------------------------------------------- #
# Thread documents + per-candidate task checklist (collaboration layer)
# --------------------------------------------------------------------------- #
def add_attachment(lead_id, uploaded_by, orig_name, stored_name, mime="",
                   size_bytes=0, note=""):
    if uploaded_by not in ("site", "patient"):
        return None
    db = get_db()
    cur = db.execute(
        "INSERT INTO message_attachments (lead_id, uploaded_by, orig_name, "
        "stored_name, mime, size_bytes, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (lead_id, uploaded_by, orig_name, stored_name, mime, int(size_bytes or 0),
         (note or "").strip(), now()))
    db.execute("UPDATE leads SET updated_at = ? WHERE id = ?", (now(), lead_id))
    db.commit()
    return cur.lastrowid


def list_attachments(lead_id):
    return get_db().execute(
        "SELECT * FROM message_attachments WHERE lead_id = ? ORDER BY id DESC",
        (lead_id,)).fetchall()


def get_attachment(att_id):
    return get_db().execute(
        "SELECT * FROM message_attachments WHERE id = ?", (att_id,)).fetchone()


def add_task(lead_id, title, assigned_to="patient", created_by="site"):
    title = (title or "").strip()
    if not title:
        return None
    if assigned_to not in ("patient", "site"):
        assigned_to = "patient"
    db = get_db()
    cur = db.execute(
        "INSERT INTO lead_tasks (lead_id, title, assigned_to, status, created_by, "
        "created_at) VALUES (?,?,?,?,?,?)",
        (lead_id, title, assigned_to, "open", created_by, now()))
    db.execute("UPDATE leads SET updated_at = ? WHERE id = ?", (now(), lead_id))
    db.commit()
    return cur.lastrowid


def list_tasks(lead_id):
    return get_db().execute(
        "SELECT * FROM lead_tasks WHERE lead_id = ? ORDER BY status ASC, id ASC",
        (lead_id,)).fetchall()


def get_task(task_id):
    return get_db().execute(
        "SELECT * FROM lead_tasks WHERE id = ?", (task_id,)).fetchone()


def set_task_status(task_id, status):
    status = "done" if status == "done" else "open"
    db = get_db()
    db.execute(
        "UPDATE lead_tasks SET status = ?, done_at = ? WHERE id = ?",
        (status, now() if status == "done" else "", task_id))
    db.commit()


def add_note(lead_id, body, author="", author_user_id=None):
    """Internal team-only note on a candidate (not visible to the patient).

    `author` is the display name shown on the note; `author_user_id` is who
    actually wrote it, which is what a mention is attributed to and scoped by."""
    body = (body or "").strip()
    if not body:
        return None
    db = get_db()
    cur = db.execute(
        "INSERT INTO lead_notes (lead_id, body, author, author_user_id, "
        "created_at) VALUES (?,?,?,?,?)",
        (lead_id, body, (author or "").strip(), author_user_id, now()))
    db.execute("UPDATE leads SET updated_at = ? WHERE id = ?", (now(), lead_id))
    db.commit()
    return cur.lastrowid


def list_notes(lead_id):
    return get_db().execute(
        "SELECT * FROM lead_notes WHERE lead_id = ? ORDER BY id DESC",
        (lead_id,)).fetchall()


def open_task_count(lead_id, assigned_to=None):
    q = "SELECT COUNT(*) n FROM lead_tasks WHERE lead_id = ? AND status = 'open'"
    args = [lead_id]
    if assigned_to:
        q += " AND assigned_to = ?"
        args.append(assigned_to)
    r = get_db().execute(q, args).fetchone()
    return r["n"] if r else 0


# --------------------------------------------------------------------------- #
# Document requests (specific records the study team asks a candidate for)
# --------------------------------------------------------------------------- #
DOC_REQUEST_OPEN = ("requested", "rejected")  # patient/coordinator may still upload


def add_doc_request(lead_id, title, note="", created_by="site"):
    title = (title or "").strip()
    if not title:
        return None
    db = get_db()
    cur = db.execute(
        "INSERT INTO doc_requests (lead_id, title, note, status, created_by, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (lead_id, title, (note or "").strip(), "requested", created_by,
         now(), now()))
    db.execute("UPDATE leads SET updated_at = ? WHERE id = ?", (now(), lead_id))
    db.commit()
    return cur.lastrowid


def list_doc_requests(lead_id):
    return get_db().execute(
        "SELECT d.*, a.orig_name AS file_name FROM doc_requests d "
        "LEFT JOIN message_attachments a ON a.id = d.attachment_id "
        "WHERE d.lead_id = ? ORDER BY "
        "CASE d.status WHEN 'requested' THEN 0 WHEN 'rejected' THEN 1 "
        "WHEN 'received' THEN 2 ELSE 3 END, d.id ASC",
        (lead_id,)).fetchall()


def get_doc_request(req_id):
    return get_db().execute(
        "SELECT * FROM doc_requests WHERE id = ?", (req_id,)).fetchone()


def set_doc_request_upload(req_id, attachment_id):
    """A file was uploaded against this request -> awaiting study-team review."""
    db = get_db()
    db.execute(
        "UPDATE doc_requests SET attachment_id = ?, status = 'received', "
        "review_note = '', updated_at = ? WHERE id = ?",
        (attachment_id, now(), req_id))
    db.commit()


def set_doc_request_review(req_id, accepted, review_note=""):
    """Study team accepts (done) or rejects (patient must re-upload)."""
    status = "accepted" if accepted else "rejected"
    db = get_db()
    db.execute(
        "UPDATE doc_requests SET status = ?, review_note = ?, updated_at = ? "
        "WHERE id = ?",
        (status, (review_note or "").strip(), now(), req_id))
    db.commit()


def open_doc_request_count(lead_id):
    r = get_db().execute(
        "SELECT COUNT(*) n FROM doc_requests WHERE lead_id = ? AND status IN "
        "('requested', 'rejected')", (lead_id,)).fetchone()
    return r["n"] if r else 0


# --------------------------------------------------------------------------- #
# Conversation tags (per-lead quick labels a coordinator can filter by).
# Predefined so filtering stays clean; "pinned" floats a thread to the top.
# --------------------------------------------------------------------------- #
CONV_TAGS = [
    {"key": "pinned", "label": "Pinned"},
    {"key": "consent", "label": "Needs consent"},
    {"key": "docs", "label": "Awaiting docs"},
    {"key": "followup", "label": "Follow up"},
]
_CONV_TAG_KEYS = {t["key"] for t in CONV_TAGS}


def _row_val(row, key, default=""):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def lead_tags(row):
    """Return the ordered list of tag keys set on a lead row."""
    raw = _row_val(row, "conv_tags", "") or ""
    return [t for t in (x.strip() for x in raw.split(",")) if t in _CONV_TAG_KEYS]


def toggle_lead_tag(lead_id, tag):
    """Add/remove a predefined tag on a lead. Returns the new tag list."""
    if tag not in _CONV_TAG_KEYS:
        return None
    db = get_db()
    row = db.execute("SELECT conv_tags FROM leads WHERE id = ?",
                     (lead_id,)).fetchone()
    if row is None:
        return None
    cur = [t for t in (x.strip() for x in (row["conv_tags"] or "").split(","))
           if t in _CONV_TAG_KEYS]
    if tag in cur:
        cur.remove(tag)
    else:
        cur.append(tag)
    db.execute("UPDATE leads SET conv_tags = ? WHERE id = ?",
               (",".join(cur), lead_id))
    db.commit()
    return cur


# --------------------------------------------------------------------------- #
# Internal team room (staff-only, keyed by trial NCT). Never patient-visible.
# --------------------------------------------------------------------------- #
def add_team_message(nct, user_id, sender_name, body):
    body = (body or "").strip()
    if not (nct and body):
        return None
    db = get_db()
    cur = db.execute(
        "INSERT INTO team_messages (nct, sender_user_id, sender_name, body, "
        "created_at) VALUES (?,?,?,?,?)",
        (nct, user_id, (sender_name or "").strip(), body, now()))
    db.commit()
    return cur.lastrowid


def list_team_messages(nct, limit=200):
    return get_db().execute(
        "SELECT * FROM team_messages WHERE nct = ? ORDER BY id ASC LIMIT ?",
        (nct, limit)).fetchall()


def add_team_attachment(team_message_id, orig_name, stored_name, mime="",
                        size_bytes=0):
    db = get_db()
    cur = db.execute(
        "INSERT INTO team_message_attachments (team_message_id, orig_name, "
        "stored_name, mime, size_bytes, created_at) VALUES (?,?,?,?,?,?)",
        (team_message_id, orig_name, stored_name, mime, int(size_bytes or 0),
         now()))
    db.commit()
    return cur.lastrowid


def list_team_attachments(team_message_ids):
    ids = [i for i in (team_message_ids or []) if i]
    if not ids:
        return {}
    qs = ",".join("?" * len(ids))
    rows = get_db().execute(
        f"SELECT * FROM team_message_attachments WHERE team_message_id IN ({qs}) "
        "ORDER BY id ASC", ids).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["team_message_id"], []).append(r)
    return out


def get_team_attachment(att_id):
    return get_db().execute(
        "SELECT a.*, m.nct AS nct FROM team_message_attachments a "
        "JOIN team_messages m ON m.id = a.team_message_id WHERE a.id = ?",
        (att_id,)).fetchone()


def engagement_for_ncts(ncts):
    """Message/visit counts scoped to a list/set of NCT ids."""
    ncts = sorted({x for x in (ncts or []) if x})
    if not ncts:
        return {"messages": 0, "patient_messages": 0, "visits": 0}
    qs = ",".join("?" * len(ncts))
    db = get_db()
    msgs = db.execute(
        "SELECT COUNT(*) n FROM messages m JOIN leads l ON l.id = m.lead_id "
        f"WHERE l.nct IN ({qs})", ncts).fetchone()["n"]
    from_patient = db.execute(
        "SELECT COUNT(*) n FROM messages m JOIN leads l ON l.id = m.lead_id "
        f"WHERE l.nct IN ({qs}) AND m.sender='patient'", ncts).fetchone()["n"]
    visits = db.execute(
        "SELECT COUNT(*) n FROM lead_visits v JOIN leads l ON l.id = v.lead_id "
        f"WHERE l.nct IN ({qs})", ncts).fetchone()["n"]
    return {"messages": msgs, "patient_messages": from_patient, "visits": visits}


# --------------------------------------------------------------------------- #
# Visits (site books a screening/follow-up; drives reminders)
# --------------------------------------------------------------------------- #
def add_visit(lead_id, visit_at, kind="screening", location="", note="",
              window_start="", window_end="", duration_min=30, title="",
              prep="", update_id=None, agenda="", series_id="", recurrence=""):
    db = get_db()
    db.execute(
        "INSERT INTO lead_visits (lead_id, kind, visit_at, location, note, "
        "window_start, window_end, duration_min, title, prep, status, "
        "update_id, agenda, series_id, recurrence, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lead_id, kind or "screening", visit_at, location, note,
         window_start or "", window_end or "", int(duration_min or 30),
         (title or "").strip(), (prep or "").strip(), "scheduled",
         update_id, (agenda or "").strip(), series_id or "", recurrence or "",
         now()))
    db.commit()
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


# ---- Schedule of Events (protocol visit template per study) -----------------
def list_soe_visits(user_id, nct):
    db = get_db()
    return db.execute(
        "SELECT * FROM soe_visits WHERE user_id = ? AND nct = ? "
        "ORDER BY seq ASC, day_offset ASC, id ASC", (user_id, nct or "")).fetchall()


def soe_visit_count(user_id, nct):
    db = get_db()
    return db.execute(
        "SELECT COUNT(*) AS n FROM soe_visits WHERE user_id = ? AND nct = ?",
        (user_id, nct or "")).fetchone()["n"]


def replace_soe_visits(user_id, nct, rows):
    """Replace a study's whole Schedule of Events (simplest correct save)."""
    db = get_db()
    ts = now()
    db.execute("DELETE FROM soe_visits WHERE user_id = ? AND nct = ?",
               (user_id, nct or ""))
    for i, r in enumerate(rows):
        db.execute(
            "INSERT INTO soe_visits (user_id, nct, seq, name, day_offset, "
            "window_before, window_after, duration_min, procedures, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, nct or "", i, (r.get("name") or "").strip(),
             int(r.get("day_offset") or 0), int(r.get("window_before") or 0),
             int(r.get("window_after") or 0), int(r.get("duration_min") or 30),
             (r.get("procedures") or "").strip(), ts, ts))
    db.commit()


def materialize_soe_for_lead(lead_id, user_id, nct, anchor_date):
    """Create real lead_visits for one participant from the study SoE, anchored at
    anchor_date (the Day-1 / baseline date). Each visit carries its protocol
    window so the calendar can flag out-of-window (deviation) risk. Returns the
    number of visits booked."""
    import datetime as _dt
    soe = list_soe_visits(user_id, nct)
    if not soe:
        return 0
    try:
        base = _dt.datetime.strptime((anchor_date or "")[:10], "%Y-%m-%d").date()
    except Exception:
        base = _dt.date.today()
    n = 0
    for v in soe:
        target = base + _dt.timedelta(days=int(v["day_offset"] or 0))
        w0 = target - _dt.timedelta(days=int(v["window_before"] or 0))
        w1 = target + _dt.timedelta(days=int(v["window_after"] or 0))
        add_visit(
            lead_id, kind="visit",
            visit_at=f"{target.strftime('%Y-%m-%d')} 09:00",
            window_start=w0.strftime("%Y-%m-%d"),
            window_end=w1.strftime("%Y-%m-%d"),
            duration_min=int(v["duration_min"] or 30),
            title=v["name"] or "Study visit",
            agenda=v["procedures"] or "")
        n += 1
    return n


_RECUR_DAYS = {"weekly": 7, "biweekly": 14, "monthly": 28}


def add_visit_series(lead_id, first_visit_at, count, recurrence="weekly",
                     **visit_kwargs):
    """Create a repeating set of visits sharing one series_id, spaced by the
    recurrence cadence. Returns the list of created visit ids. KPI: Tier-2 -
    booking a whole schedule once cuts coordinator scheduling time per patient."""
    import datetime as _dt
    import uuid as _uuid
    step = _RECUR_DAYS.get(recurrence, 7)
    try:
        base = _dt.datetime.strptime(first_visit_at[:16], "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        base = _dt.datetime.strptime(first_visit_at[:10] + " 09:00",
                                     "%Y-%m-%d %H:%M")
    sid = _uuid.uuid4().hex[:12]
    ids = []
    for i in range(max(1, int(count or 1))):
        when = (base + _dt.timedelta(days=step * i)).strftime("%Y-%m-%d %H:%M")
        ids.append(add_visit(lead_id, when, series_id=sid,
                             recurrence=recurrence, **visit_kwargs))
    return sid, ids


def get_series_visits(series_id):
    if not series_id:
        return []
    return get_db().execute(
        "SELECT * FROM lead_visits WHERE series_id = ? ORDER BY visit_at ASC",
        (series_id,)).fetchall()


def shift_series_after(series_id, pivot_visit_at, delta_seconds, exclude_id=None):
    """Move every not-yet-completed visit in a series that falls on/after the
    pivot time by delta_seconds (used when a recurring visit is rescheduled and
    the coordinator opts to slide the rest of the series). Clears reminded_at so
    patients get a fresh reminder. Returns the count of visits shifted."""
    import datetime as _dt
    if not series_id or not delta_seconds:
        return 0
    q = ("SELECT id, visit_at FROM lead_visits WHERE series_id = ? "
         "AND status NOT IN ('completed','cancelled') AND visit_at >= ?")
    params = [series_id, pivot_visit_at]
    if exclude_id is not None:
        q += " AND id != ?"
        params.append(exclude_id)
    rows = get_db().execute(q, params).fetchall()
    db = get_db()
    n = 0
    for r in rows:
        try:
            va = _dt.datetime.strptime(r["visit_at"][:16], "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            continue
        new_at = (va + _dt.timedelta(seconds=delta_seconds)).strftime(
            "%Y-%m-%d %H:%M")
        db.execute("UPDATE lead_visits SET visit_at = ?, reminded_at = '' "
                   "WHERE id = ?", (new_at, r["id"]))
        n += 1
    db.commit()
    return n


def update_visit(visit_id, **fields):
    """Update a visit. Rescheduling (a new visit_at) clears reminded_at so the
    reminder engine re-notifies the patient about the new time."""
    allowed = ("visit_at", "kind", "location", "note", "window_start",
               "window_end", "duration_min", "title", "prep", "status",
               "agenda", "series_id", "recurrence")
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return False
    if "visit_at" in fields:
        sets.append("reminded_at = ?")
        vals.append("")
    vals.append(visit_id)
    db = get_db()
    db.execute(f"UPDATE lead_visits SET {', '.join(sets)} WHERE id = ?", vals)
    db.commit()
    return True


def set_visit_status(visit_id, status):
    db = get_db()
    db.execute("UPDATE lead_visits SET status = ? WHERE id = ?",
               ((status or "scheduled").strip(), visit_id))
    db.commit()
    return True


def get_visit(visit_id):
    return get_db().execute(
        "SELECT * FROM lead_visits WHERE id = ?",
        (visit_id,)).fetchone()


# Roles a visit guest can hold. Value -> display label. Kept small and clinical
# so the inline "switch role" picker is fast and unambiguous.
GUEST_ROLES = [
    ("sub_i", "Sub-investigator"),
    ("pi", "Principal investigator"),
    ("coordinator", "Study coordinator"),
    ("interpreter", "Interpreter"),
    ("caregiver", "Caregiver / family"),
    ("monitor", "Sponsor monitor (CRA)"),
    ("guest", "Guest"),
]
GUEST_ROLE_LABELS = dict(GUEST_ROLES)


def guest_role_label(role):
    return GUEST_ROLE_LABELS.get(role, (role or "Guest").replace("_", " ").title())


def list_visit_guests(visit_id):
    rows = get_db().execute(
        "SELECT * FROM visit_guests WHERE visit_id = ? ORDER BY id ASC",
        (visit_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["role_label"] = guest_role_label(d.get("role"))
        out.append(d)
    return out


def add_visit_guest(visit_id, name, email="", role="guest"):
    if not (name or "").strip():
        return None
    if role not in GUEST_ROLE_LABELS:
        role = "guest"
    db = get_db()
    db.execute(
        "INSERT INTO visit_guests (visit_id, name, email, role, created_at) "
        "VALUES (?,?,?,?,?)",
        (visit_id, name.strip(), (email or "").strip(), role, now()))
    db.commit()
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def update_visit_guest(guest_id, visit_id, **fields):
    """Edit a guest (name/email) or switch their role. Scoped to visit_id so a
    guest can only be changed on the visit it belongs to."""
    allowed = ("name", "email", "role")
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed or v is None:
            continue
        if k == "role" and v not in GUEST_ROLE_LABELS:
            continue
        sets.append(f"{k} = ?")
        vals.append((v or "").strip() if isinstance(v, str) else v)
    if not sets:
        return False
    vals.extend([guest_id, visit_id])
    db = get_db()
    db.execute(f"UPDATE visit_guests SET {', '.join(sets)} "
               f"WHERE id = ? AND visit_id = ?", vals)
    db.commit()
    return True


def remove_visit_guest(guest_id, visit_id):
    db = get_db()
    db.execute("DELETE FROM visit_guests WHERE id = ? AND visit_id = ?",
               (guest_id, visit_id))
    db.commit()
    return True


def get_visits(lead_id):
    return get_db().execute(
        "SELECT * FROM lead_visits WHERE lead_id = ? ORDER BY visit_at ASC",
        (lead_id,)).fetchall()


def list_calendar_visits(user_id, start_iso, end_iso):
    """All visits in [start, end) across the trials this user's team runs, joined
    with the applicant + trial so the calendar can render across studies. Scoped
    by claimed NCTs (the same access model as list_leads_for_user)."""
    claims = sorted(user_claimed_ncts(user_id))
    if not claims:
        return []
    qs = ",".join("?" * len(claims))
    return get_db().execute(
        f"SELECT v.*, l.name AS lead_name, l.nct, l.title AS trial_title, "
        f"l.token AS lead_token, l.status AS lead_status "
        f"FROM lead_visits v JOIN leads l ON l.id = v.lead_id "
        f"WHERE l.nct IN ({qs}) AND v.visit_at >= ? AND v.visit_at < ? "
        f"ORDER BY v.visit_at ASC",
        (*claims, start_iso, end_iso)).fetchall()


# --------------------------------------------------------------------------- #
# Calendar sync: a per-user, secret iCalendar (.ics) feed the coordinator
# subscribes to from Google/Outlook/Apple. One-way, read-only, auto-refreshing.
# --------------------------------------------------------------------------- #
def get_or_create_calendar_feed(user_id):
    """Return the user's calendar-feed row, creating it (with a fresh secret
    token) on first use. The token is the feed URL's only credential."""
    import secrets as _secrets
    db = get_db()
    row = db.execute("SELECT * FROM calendar_feeds WHERE user_id = ?",
                     (user_id,)).fetchone()
    if row:
        return row
    token = _secrets.token_urlsafe(24)
    db.execute(
        "INSERT INTO calendar_feeds (user_id, token, deidentify, created_at) "
        "VALUES (?,?,1,?)", (user_id, token, now()))
    db.commit()
    return db.execute("SELECT * FROM calendar_feeds WHERE user_id = ?",
                      (user_id,)).fetchone()


def get_calendar_feed_by_token(token):
    if not token:
        return None
    return get_db().execute(
        "SELECT * FROM calendar_feeds WHERE token = ?", (token,)).fetchone()


def set_calendar_feed(user_id, provider=None, deidentify=None):
    """Update connection state (provider connected, de-identify preference)."""
    feed = get_or_create_calendar_feed(user_id)
    db = get_db()
    sets, vals = [], []
    if provider is not None:
        sets.append("provider = ?")
        vals.append(provider or "")
        sets.append("connected_at = ?")
        vals.append(now() if provider else "")
    if deidentify is not None:
        sets.append("deidentify = ?")
        vals.append(1 if deidentify else 0)
    if not sets:
        return feed
    vals.append(user_id)
    db.execute(f"UPDATE calendar_feeds SET {', '.join(sets)} WHERE user_id = ?",
               vals)
    db.commit()
    return db.execute("SELECT * FROM calendar_feeds WHERE user_id = ?",
                      (user_id,)).fetchone()


def touch_calendar_feed_synced(user_id):
    db = get_db()
    db.execute("UPDATE calendar_feeds SET last_synced_at = ? WHERE user_id = ?",
               (now(), user_id))
    db.commit()


def rotate_calendar_feed_token(user_id):
    """Issue a new secret token (revokes any previously shared feed URL)."""
    import secrets as _secrets
    get_or_create_calendar_feed(user_id)
    token = _secrets.token_urlsafe(24)
    db = get_db()
    db.execute("UPDATE calendar_feeds SET token = ? WHERE user_id = ?",
               (token, user_id))
    db.commit()
    return token


# --------------------------------------------------------------------------- #
# Participant payments (stipends / reimbursement). See the schema comment above
# and COMPLIANCE.md §5: subjects only, IRB-approved amounts, auditable ledger.
# --------------------------------------------------------------------------- #
def add_payment_rule(user_id, nct, kind, label, amount_cents, currency="USD",
                     method="gift_card", irb_approved=0, irb_note="",
                     rule_type="visit", prorate=0):
    db = get_db()
    ts = now()
    db.execute(
        "INSERT INTO payment_rules (user_id, nct, kind, label, amount_cents, "
        "currency, method, irb_approved, irb_note, rule_type, prorate, active, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
        (user_id, nct or "", kind or "screening", (label or "").strip(),
         int(amount_cents or 0), currency or "USD", method or "gift_card",
         1 if irb_approved else 0, (irb_note or "").strip(),
         rule_type or "visit", 1 if prorate else 0, ts, ts))
    db.commit()
    return db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def update_payment_rule(rule_id, **fields):
    allowed = ("kind", "label", "amount_cents", "currency", "method",
               "irb_approved", "irb_note", "active", "rule_type", "prorate")
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ?")
        vals.append(int(v) if k in ("amount_cents", "irb_approved", "active",
                                    "prorate") else v)
    if not sets:
        return False
    sets.append("updated_at = ?")
    vals.append(now())
    vals.append(rule_id)
    db = get_db()
    db.execute(f"UPDATE payment_rules SET {', '.join(sets)} WHERE id = ?", vals)
    db.commit()
    return True


def get_payment_rule(rule_id):
    return get_db().execute(
        "SELECT * FROM payment_rules WHERE id = ?", (rule_id,)).fetchone()


def list_payment_rules(user_id, nct=None):
    if nct:
        return get_db().execute(
            "SELECT * FROM payment_rules WHERE user_id = ? AND nct = ? "
            "ORDER BY active DESC, kind ASC", (user_id, nct)).fetchall()
    return get_db().execute(
        "SELECT * FROM payment_rules WHERE user_id = ? "
        "ORDER BY nct ASC, active DESC, kind ASC", (user_id,)).fetchall()


def find_active_payment_rule(user_id, nct, kind):
    """Active per-visit stipend rule for this trial + visit kind (auto-queue)."""
    return get_db().execute(
        "SELECT * FROM payment_rules WHERE user_id = ? AND nct = ? AND kind = ? "
        "AND active = 1 AND rule_type = 'visit' ORDER BY id DESC LIMIT 1",
        (user_id, nct, kind)).fetchone()


def find_active_completion_rule(user_id, nct):
    """Active lump-sum-on-completion rule for a trial, if any."""
    return get_db().execute(
        "SELECT * FROM payment_rules WHERE user_id = ? AND nct = ? "
        "AND active = 1 AND rule_type = 'completion' ORDER BY id DESC LIMIT 1",
        (user_id, nct)).fetchone()


def generate_payment_rules_from_soe(user_id, nct):
    """Draft one per-visit stipend rule for each Schedule of Events visit that
    does not already have a rule. Amounts start at 0 and IRB is unattested, so a
    coordinator sets the approved amount and attests before it can auto-issue.
    Returns the number of draft rules created. Idempotent: skips visit kinds that
    already have a rule for this study."""
    soe = list_soe_visits(user_id, nct)
    if not soe:
        return 0
    existing = {r["kind"] for r in get_db().execute(
        "SELECT kind FROM payment_rules WHERE user_id = ? AND nct = ? "
        "AND rule_type = 'visit'", (user_id, nct)).fetchall()}
    created = 0
    for v in soe:
        name = (v["name"] or "").strip() or "Study visit"
        kind = _soe_name_to_kind(name)
        if kind in existing:
            continue
        existing.add(kind)
        add_payment_rule(
            user_id, nct, kind, f"{name} - time & travel", 0,
            method="gift_card", irb_approved=0,
            irb_note="", rule_type="visit")
        created += 1
    return created


def _soe_name_to_kind(name):
    """Map a Schedule of Events visit name to a coarse visit kind used by rules
    and the calendar (screening|baseline|treatment|followup|reconsent|visit)."""
    n = (name or "").lower()
    if "screen" in n:
        return "screening"
    if "baseline" in n or "randomiz" in n or "day 1" in n or "week 0" in n:
        return "baseline"
    if "treat" in n or "dos" in n or "infus" in n or "inject" in n:
        return "treatment"
    if "re-consent" in n or "reconsent" in n:
        return "reconsent"
    if "follow" in n or "eos" in n or "end of study" in n or "final" in n:
        return "followup"
    return "followup"


def completed_visit_count(lead_id):
    """How many of a participant's booked visits are completed (proration basis)."""
    return get_db().execute(
        "SELECT COUNT(*) AS n FROM lead_visits WHERE lead_id = ? "
        "AND status = 'completed'", (lead_id,)).fetchone()["n"] or 0


def _log_payment_event(db, payment_id, action, actor="system", note=""):
    db.execute(
        "INSERT INTO payment_events (payment_id, action, actor, note, created_at) "
        "VALUES (?,?,?,?,?)", (payment_id, action, actor, note, now()))


def create_payment(lead_id, nct, amount_cents, kind="", label="", visit_id=None,
                   rule_id=None, currency="USD", method="gift_card",
                   status="queued", created_by="system", note="", created_at=None):
    db = get_db()
    ts = created_at or now()
    db.execute(
        "INSERT INTO participant_payments (lead_id, nct, visit_id, rule_id, kind, "
        "label, amount_cents, currency, method, status, created_by, note, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lead_id, nct or "", visit_id, rule_id, kind or "", (label or "").strip(),
         int(amount_cents or 0), currency or "USD", method or "gift_card",
         status, created_by, (note or "").strip(), ts, ts))
    pid = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    _log_payment_event(db, pid, status, created_by, note)
    db.commit()
    return pid


def get_payment(payment_id):
    return get_db().execute(
        "SELECT * FROM participant_payments WHERE id = ?", (payment_id,)).fetchone()


def payment_for_visit(visit_id):
    """Existing non-void payment tied to a visit (dedupe auto-triggers)."""
    if not visit_id:
        return None
    return get_db().execute(
        "SELECT * FROM participant_payments WHERE visit_id = ? AND status != 'void' "
        "ORDER BY id DESC LIMIT 1", (visit_id,)).fetchone()


def list_payments(user_id, nct=None):
    claims = sorted(user_claimed_ncts(user_id))
    if not claims:
        return []
    if nct and nct in claims:
        claims = [nct]
    qs = ",".join("?" * len(claims))
    return get_db().execute(
        f"SELECT p.*, l.name AS lead_name, l.email AS lead_email, "
        f"l.token AS lead_token, l.title AS trial_title "
        f"FROM participant_payments p JOIN leads l ON l.id = p.lead_id "
        f"WHERE p.nct IN ({qs}) ORDER BY p.created_at DESC, p.id DESC",
        claims).fetchall()


def update_payment_status(payment_id, status, actor="you", provider=None,
                          provider_ref=None, note=""):
    db = get_db()
    ts = now()
    sets = ["status = ?", "updated_at = ?"]
    vals = [status, ts]
    if provider is not None:
        sets.append("provider = ?")
        vals.append(provider)
    if provider_ref is not None:
        sets.append("provider_ref = ?")
        vals.append(provider_ref)
    if status == "issued":
        sets.append("issued_at = ?")
        vals.append(ts)
    vals.append(payment_id)
    db.execute(f"UPDATE participant_payments SET {', '.join(sets)} WHERE id = ?",
               vals)
    _log_payment_event(db, payment_id, status, actor, note)
    db.commit()
    return True


def list_payment_events(payment_id):
    return get_db().execute(
        "SELECT * FROM payment_events WHERE payment_id = ? ORDER BY id ASC",
        (payment_id,)).fetchall()


def get_payment_recipient(lead_id):
    row = get_db().execute(
        "SELECT * FROM payment_recipients WHERE lead_id = ?", (lead_id,)).fetchone()
    if row:
        return dict(row)
    return {"lead_id": lead_id, "w9_status": "not_needed",
            "w9_collected_at": "", "payout_email": "", "payout_method": "",
            "updated_at": ""}


def set_payout_method(lead_id, method="", email=None):
    """Record how a participant chose to be paid (informational + a prefill for a
    live disbursement rail). Never moves money by itself."""
    db = get_db()
    ts = now()
    db.execute(
        "INSERT INTO payment_recipients (lead_id, payout_method, payout_email, "
        "updated_at) VALUES (?,?,?,?) ON CONFLICT(lead_id) DO UPDATE SET "
        "payout_method = excluded.payout_method, "
        "payout_email = CASE WHEN excluded.payout_email != '' "
        "THEN excluded.payout_email ELSE payment_recipients.payout_email END, "
        "updated_at = excluded.updated_at",
        (lead_id, method or "", (email or "").strip(), ts))
    db.commit()
    return True


def set_w9_status(lead_id, status):
    db = get_db()
    ts = now()
    collected = ts if status == "collected" else ""
    db.execute(
        "INSERT INTO payment_recipients (lead_id, w9_status, w9_collected_at, "
        "updated_at) VALUES (?,?,?,?) ON CONFLICT(lead_id) DO UPDATE SET "
        "w9_status = excluded.w9_status, "
        "w9_collected_at = CASE WHEN excluded.w9_status = 'collected' "
        "THEN excluded.w9_collected_at ELSE payment_recipients.w9_collected_at END, "
        "updated_at = excluded.updated_at",
        (lead_id, status, collected, ts))
    db.commit()
    return True


def participant_year_total_cents(lead_id, year, exclude_payment_id=None):
    """Sum of issued/paid payments to a participant in a calendar year (tax
    basis for the 1099 threshold). created_at is 'YYYY-MM-DD HH:MM'."""
    q = ("SELECT COALESCE(SUM(amount_cents),0) AS t FROM participant_payments "
         "WHERE lead_id = ? AND status IN ('issued','paid') "
         "AND substr(created_at,1,4) = ?")
    args = [lead_id, str(year)]
    if exclude_payment_id:
        q += " AND id != ?"
        args.append(exclude_payment_id)
    return get_db().execute(q, args).fetchone()["t"] or 0


# --------------------------------------------------------------------------- #
# Sponsor -> site updates (amendments, safety letters, doc requests, bulletins).
# See the schema comment above. Owned by the site (user_id) in this MVP.
# --------------------------------------------------------------------------- #
# Participants who are already consented/active and therefore need re-consent
# when a protocol amendment lands.
RECONSENT_STATUSES = ("screening", "enrolled")


def add_sponsor_update(user_id, nct, type="amendment", version="", title="",
                       summary="", source="", received_at="", due_at="",
                       requires_ack=1, created_by="you"):
    db = get_db()
    ts = now()
    # Amendments need the full checklist; lighter types skip steps up front.
    if type == "amendment":
        irb, icf, recon, retr = "pending", "pending", "pending", "pending"
    elif type == "safety":
        irb, icf, recon, retr = "pending", "not_required", "not_required", "pending"
    else:  # doc_request | bulletin
        irb, icf, recon, retr = "not_required", "not_required", "not_required", "not_required"
    db.execute(
        "INSERT INTO sponsor_updates (user_id, nct, type, version, title, "
        "summary, source, received_at, due_at, requires_ack, ack_status, "
        "irb_status, icf_status, reconsent_status, retrain_status, created_by, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, nct or "", type, (version or "").strip(), (title or "").strip(),
         (summary or "").strip(), (source or "").strip(),
         received_at or ts, (due_at or "").strip(), 1 if requires_ack else 0,
         "pending", irb, icf, recon, retr, created_by, ts, ts))
    uid = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    db.execute("INSERT INTO update_events (update_id, action, actor, note, "
               "created_at) VALUES (?,?,?,?,?)",
               (uid, "created", created_by, "Update logged", ts))
    db.commit()
    return uid


def get_sponsor_update(update_id):
    return get_db().execute(
        "SELECT * FROM sponsor_updates WHERE id = ?", (update_id,)).fetchone()


def list_sponsor_updates(user_id, nct=None):
    if nct:
        return get_db().execute(
            "SELECT * FROM sponsor_updates WHERE user_id = ? AND nct = ? "
            "ORDER BY created_at DESC, id DESC", (user_id, nct)).fetchall()
    return get_db().execute(
        "SELECT * FROM sponsor_updates WHERE user_id = ? "
        "ORDER BY created_at DESC, id DESC", (user_id,)).fetchall()


def update_sponsor_update(update_id, actor="you", event=None, note="", **fields):
    allowed = ("type", "version", "title", "summary", "source", "due_at",
               "ack_status", "ack_at", "irb_status", "irb_submitted_at",
               "irb_approved_at", "icf_status", "icf_note", "reconsent_status",
               "retrain_status")
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return False
    sets.append("updated_at = ?")
    vals.append(now())
    vals.append(update_id)
    db = get_db()
    db.execute(f"UPDATE sponsor_updates SET {', '.join(sets)} WHERE id = ?", vals)
    if event:
        db.execute("INSERT INTO update_events (update_id, action, actor, note, "
                   "created_at) VALUES (?,?,?,?,?)",
                   (update_id, event, actor, note, now()))
    db.commit()
    return True


def list_update_events(update_id):
    return get_db().execute(
        "SELECT * FROM update_events WHERE update_id = ? ORDER BY id ASC",
        (update_id,)).fetchall()


# --------------------------------------------------------------------------- #
# Controlled-document versions (Protocol / ICF / IB) per study. An amendment
# introduces a NEW version (pending) that becomes current on IRB approval and
# supersedes the prior current one. Gives the site an unambiguous "which version
# is in effect + its history" view instead of digging through sponsor email.
# --------------------------------------------------------------------------- #
def add_doc_version(user_id, nct, doc_type="protocol", version="",
                    status="current", effective_at="", update_id=None,
                    label="", note=""):
    db = get_db()
    ts = now()
    db.execute(
        "INSERT INTO study_doc_versions (user_id, nct, doc_type, version, label, "
        "status, effective_at, update_id, note, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, nct or "", doc_type, (version or "").strip(),
         (label or "").strip(), status, (effective_at or "").strip(),
         update_id, (note or "").strip(), ts))
    vid = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    db.commit()
    return vid


def list_doc_versions(user_id, nct, doc_type=None):
    db = get_db()
    if doc_type:
        return db.execute(
            "SELECT * FROM study_doc_versions WHERE user_id=? AND nct=? "
            "AND doc_type=? ORDER BY created_at DESC, id DESC",
            (user_id, nct, doc_type)).fetchall()
    return db.execute(
        "SELECT * FROM study_doc_versions WHERE user_id=? AND nct=? "
        "ORDER BY created_at DESC, id DESC", (user_id, nct)).fetchall()


def doc_versions_for_update(update_id):
    return get_db().execute(
        "SELECT * FROM study_doc_versions WHERE update_id=? ORDER BY id ASC",
        (update_id,)).fetchall()


def promote_doc_versions_for_update(user_id, update_id, effective_at=""):
    """On IRB approval: an amendment's pending doc versions become 'current' and
    the previous current versions of the same doc_type are 'superseded'. Returns
    how many versions were promoted."""
    db = get_db()
    pend = db.execute(
        "SELECT * FROM study_doc_versions WHERE update_id=? AND status='pending'",
        (update_id,)).fetchall()
    eff = (effective_at or now()[:10]).strip()
    for v in pend:
        db.execute(
            "UPDATE study_doc_versions SET status='superseded' WHERE user_id=? "
            "AND nct=? AND doc_type=? AND status='current' AND id<>?",
            (user_id, v["nct"], v["doc_type"], v["id"]))
        db.execute(
            "UPDATE study_doc_versions SET status='current', effective_at=? "
            "WHERE id=?", (eff, v["id"]))
    if pend:
        db.commit()
    return len(pend)


def reconsent_candidates(user_id, nct):
    """Enrolled/active (already-consented) participants on a trial this user
    runs, the people who must re-consent when the protocol changes."""
    if nct not in user_claimed_ncts(user_id):
        return []
    qs = ",".join("?" * len(RECONSENT_STATUSES))
    return get_db().execute(
        f"SELECT * FROM leads WHERE nct = ? AND revealed = 1 "
        f"AND status IN ({qs}) ORDER BY name ASC",
        (nct, *RECONSENT_STATUSES)).fetchall()


def reconsent_visits_for_update(update_id):
    """Re-consent visits booked for an amendment, joined with the participant."""
    return get_db().execute(
        "SELECT v.*, l.name AS lead_name, l.token AS lead_token "
        "FROM lead_visits v JOIN leads l ON l.id = v.lead_id "
        "WHERE v.update_id = ? ORDER BY v.visit_at ASC", (update_id,)).fetchall()


def sponsor_updates_action_count(user_id):
    """Badge count: updates with any open required step."""
    rows = get_db().execute(
        "SELECT ack_status, irb_status, icf_status, reconsent_status, "
        "retrain_status FROM sponsor_updates WHERE user_id = ?", (user_id,)
    ).fetchall()
    n = 0
    for r in rows:
        steps = [r["ack_status"], r["irb_status"], r["icf_status"],
                 r["reconsent_status"], r["retrain_status"]]
        if any(s in ("pending", "submitted", "in_progress") for s in steps):
            n += 1
    return n


def get_lead_support(lead_id):
    row = get_db().execute(
        "SELECT * FROM lead_support_checks WHERE lead_id = ?",
        (lead_id,)).fetchone()
    if not row:
        return {
            "lead_id": lead_id,
            "coverage_status": "",
            "coverage_note": "",
            "coverage_payload": {},
            "coverage_provider": "",
            "coverage_ref": "",
            "coverage_checked_at": "",
            "travel_status": "",
            "travel_note": "",
            "travel_payload": {},
            "travel_provider": "",
            "travel_ref": "",
            "travel_checked_at": "",
            "updated_at": "",
        }
    out = dict(row)
    try:
        out["coverage_payload"] = json.loads(out.get("coverage_payload") or "{}")
    except Exception:
        out["coverage_payload"] = {}
    try:
        out["travel_payload"] = json.loads(out.get("travel_payload") or "{}")
    except Exception:
        out["travel_payload"] = {}
    return out


def set_lead_coverage_check(lead_id, status, note="", payload=None,
                            provider="", ref=""):
    if not get_lead(lead_id):
        return False
    db = get_db()
    ts = now()
    prev = get_lead_support(lead_id)
    db.execute(
        "INSERT INTO lead_support_checks (lead_id, coverage_status, coverage_note, "
        "coverage_payload, coverage_provider, coverage_ref, coverage_checked_at, "
        "travel_status, travel_note, travel_payload, travel_provider, travel_ref, "
        "travel_checked_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(lead_id) DO UPDATE SET "
        "coverage_status = excluded.coverage_status, "
        "coverage_note = excluded.coverage_note, "
        "coverage_payload = excluded.coverage_payload, "
        "coverage_provider = excluded.coverage_provider, "
        "coverage_ref = excluded.coverage_ref, "
        "coverage_checked_at = excluded.coverage_checked_at, "
        "updated_at = excluded.updated_at",
        (lead_id, (status or "").strip(), (note or "").strip(),
         json.dumps(payload or {}), (provider or "").strip(), (ref or "").strip(), ts,
         prev.get("travel_status", ""), prev.get("travel_note", ""),
         json.dumps(prev.get("travel_payload") or {}),
         prev.get("travel_provider", ""), prev.get("travel_ref", ""),
         prev.get("travel_checked_at", ""), ts))
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)",
        (lead_id, get_lead(lead_id)["status"], f"coverage check: {status}", "system", ts))
    db.commit()
    return True


def set_lead_travel_check(lead_id, status, note="", payload=None,
                          provider="", ref=""):
    if not get_lead(lead_id):
        return False
    db = get_db()
    ts = now()
    prev = get_lead_support(lead_id)
    db.execute(
        "INSERT INTO lead_support_checks (lead_id, coverage_status, coverage_note, "
        "coverage_payload, coverage_provider, coverage_ref, coverage_checked_at, "
        "travel_status, travel_note, travel_payload, travel_provider, travel_ref, "
        "travel_checked_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(lead_id) DO UPDATE SET "
        "travel_status = excluded.travel_status, "
        "travel_note = excluded.travel_note, "
        "travel_payload = excluded.travel_payload, "
        "travel_provider = excluded.travel_provider, "
        "travel_ref = excluded.travel_ref, "
        "travel_checked_at = excluded.travel_checked_at, "
        "updated_at = excluded.updated_at",
        (lead_id, prev.get("coverage_status", ""), prev.get("coverage_note", ""),
         json.dumps(prev.get("coverage_payload") or {}),
         prev.get("coverage_provider", ""), prev.get("coverage_ref", ""),
         prev.get("coverage_checked_at", ""),
         (status or "").strip(), (note or "").strip(), json.dumps(payload or {}),
         (provider or "").strip(), (ref or "").strip(), ts, ts))
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)",
        (lead_id, get_lead(lead_id)["status"], f"travel plan: {status}", "system", ts))
    db.commit()
    return True


def upcoming_visits_for_applicant(applicant_token):
    """Future visits across a patient's applications, joined with trial title."""
    if not applicant_token:
        return []
    return get_db().execute(
        "SELECT v.*, l.title, l.nct, l.token AS lead_token FROM lead_visits v "
        "JOIN leads l ON l.id = v.lead_id WHERE l.applicant_token = ? "
        "AND v.visit_at >= ? ORDER BY v.visit_at ASC",
        (applicant_token, now())).fetchall()


def visits_due_for_reminder(within_iso):
    """Future visits happening before `within_iso` that haven't been reminded."""
    return get_db().execute(
        "SELECT v.*, l.email, l.phone, l.name, l.title, l.nct, l.applicant_token "
        "FROM lead_visits v JOIN leads l ON l.id = v.lead_id "
        "WHERE v.reminded_at = '' AND v.visit_at >= ? AND v.visit_at <= ? "
        "ORDER BY v.visit_at ASC", (now(), within_iso)).fetchall()


def mark_visit_reminded(visit_id):
    db = get_db()
    db.execute("UPDATE lead_visits SET reminded_at = ? WHERE id = ?",
               (now(), visit_id))
    db.commit()


# --------------------------------------------------------------------------- #
# Quiet-applicant nudges (retention: re-engage people who've gone silent)
# --------------------------------------------------------------------------- #
_ACTIVE_STAGES = ("submitted", "prescreen", "eligible", "screening")


def list_active_stage_leads():
    """Leads still working through the funnel (not closed/withdrawn/enrolled)."""
    qs = ",".join("?" * len(_ACTIVE_STAGES))
    return get_db().execute(
        f"SELECT * FROM leads WHERE status IN ({qs}) ORDER BY id",
        _ACTIVE_STAGES).fetchall()


def last_activity_at(lead_id, created_at):
    """Most recent timestamp across created/events/messages for a lead."""
    db = get_db()
    e = db.execute("SELECT MAX(created_at) t FROM lead_events WHERE lead_id = ?",
                   (lead_id,)).fetchone()["t"]
    m = db.execute("SELECT MAX(created_at) t FROM messages WHERE lead_id = ?",
                   (lead_id,)).fetchone()["t"]
    return max(x for x in (created_at, e, m) if x)


def set_nudged(lead_id):
    db = get_db()
    db.execute("UPDATE leads SET nudged_at = ?, "
               "nudge_count = COALESCE(nudge_count, 0) + 1 WHERE id = ?",
               (now(), lead_id))
    db.commit()


def withdraw_lead(token, applicant_token, reason=""):
    """Patient withdraws their own application (must own the applicant token).
    The reason is stored on the status event so it flows into the funnel
    drop-off analytics (why patients leave)."""
    lead = get_lead_by_token(token)
    if not lead or lead["applicant_token"] != applicant_token:
        return False
    reason = (reason or "").strip()
    note = f"withdrawn by applicant: {reason}" if reason else "withdrawn by applicant"
    return update_lead_status(lead["id"], "withdrawn", note, actor="you")


def connect_records(token, applicant_token, summary):
    """Patient connects (prototype) health records to speed up pre-screening.
    Attaches a de-identified summary and moves the application into pre-screen."""
    lead = get_lead_by_token(token)
    if not lead or lead["applicant_token"] != applicant_token:
        return False
    db = get_db()
    ts = now()
    db.execute("UPDATE leads SET records_connected = 1, record_summary = ?, "
               "updated_at = ? WHERE id = ?", (summary, ts, lead["id"]))
    db.execute(
        "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
        "VALUES (?,?,?,?,?)",
        (lead["id"], lead["status"], "health records connected (prototype)",
         "you", ts))
    db.commit()
    if lead["status"] == "submitted":
        update_lead_status(lead["id"], "prescreen",
                           "auto-advanced after records connected", actor="you")
    return True


# --------------------------------------------------------------------------- #
# Physician invite links ("your doctor referred you") + attribution
# --------------------------------------------------------------------------- #
def create_invite(clinician_id, clinician_name, nct, title, condition, note=""):
    db = get_db()
    token = gen_token()
    db.execute(
        "INSERT INTO invites (token, clinician_id, clinician_name, nct, title, "
        "condition, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (token, clinician_id, clinician_name, nct, title, condition, note, now()))
    db.commit()
    return token


def get_invite(token):
    return get_db().execute("SELECT * FROM invites WHERE token = ?",
                            (token,)).fetchone()


def bump_invite_clicks(token):
    db = get_db()
    db.execute("UPDATE invites SET clicks = clicks + 1 WHERE token = ?", (token,))
    db.commit()


def list_invites(clinician_id):
    """A clinician's invites, each with how many patients applied + enrolled -
    this is the clinician closing their own loop (they hear what happened)."""
    rows = get_db().execute(
        "SELECT * FROM invites WHERE clinician_id = ? ORDER BY id DESC",
        (clinician_id,)).fetchall()
    out = []
    for r in rows:
        leads = get_db().execute(
            "SELECT status FROM leads WHERE invite_token = ?", (r["token"],)
        ).fetchall()
        applied = len(leads)
        enrolled = sum(1 for l in leads if l["status"] == "enrolled")
        active = sum(1 for l in leads if l["status"] in _ACTIVE_STAGES)
        out.append({"invite": r, "applied": applied, "enrolled": enrolled,
                    "active": active})
    return out


def lead_counts():
    rows = get_db().execute(
        "SELECT status, COUNT(*) n FROM leads GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


# --------------------------------------------------------------------------- #
# Demo data - realistic (but clearly fake) candidates for the study-team board.
# --------------------------------------------------------------------------- #
def _demo_record(cond, age, sex):
    who = " ".join(x for x in [age and f"{age}yo", sex] if x) or "adult"
    c = (cond or "").lower()
    if "migraine" in c:
        meds = "sumatriptan 100mg PRN; propranolol 40mg BID"
        detail = ("- Headache history: ~5 migraine days/month, onset in 20s\n"
                  "- Recent vitals: BP 118/74\n")
    elif "treatment-resistant" in c:
        meds = "venlafaxine XR 225mg daily (current); prior SSRI trials"
        detail = ("- Mental status: persistent low mood, anhedonia, poor sleep\n"
                  "- Screening scores: MADRS 28; MGH-ATRQ shows 2 prior failures\n")
    elif "depress" in c or "mdd" in c:
        meds = "sertraline 100mg daily (partial response)"
        detail = ("- Mental status: depressed mood, impaired sleep/concentration\n"
                  "- Screening scores: PHQ-9 16; MADRS pending at visit\n")
    else:
        meds = "per problem list"
        detail = "- Recent vitals within normal limits\n"
    return (
        f"De-identified record summary for a {who} patient.\n"
        f"- Active problems: {cond}\n"
        f"- Medications: {meds}\n"
        f"{detail}"
        f"- No prior investigational-drug participation on file.\n"
        f"Structured summary used for records-based pre-screening.")


def _demo_lead_specs():
    """A small, staged set covering the whole funnel: several awaiting review,
    plus accepted / declined / screening / enrolled so the board looks real."""
    scr_ok = {"travel": "yes", "other_trial": "no",
              "pregnancy": "no", "consent_capable": "yes"}
    site, loc = "Northwind Clinical Research", "New York, NY"
    specs = [
        # ---- Awaiting review (prescreen, no decision) ----------------------
        {"days": 1, "status": "prescreen", "records": 1,
         "nct": "NCT07076407",
         "title": "Azetukalner vs Placebo in Major Depressive Disorder (X-NOVA3)",
         "condition": "Major Depressive Disorder", "location": loc, "site": site,
         "name": "Danielle R.", "email": "danielle.r@example.com",
         "phone": "+1 212 555 0148", "age": "34", "sex": "female",
         "screener": scr_ok,
         "elig": {"met": ["Meets DSM-5-TR criteria for current MDD (MINI-confirmed)",
                          "Current episode between 6 weeks and 24 months",
                          "Age within 18-74"],
                  "unknown": ["MADRS severity to confirm at screening",
                              "First lifetime episode onset before age 50 (confirm)"],
                  "not_met": [],
                  "rationale": "Meets the core MDD inclusion criteria; two items "
                               "to confirm at the screening visit."}},
        {"days": 2, "status": "prescreen", "records": 0,
         "nct": "NCT06559306",
         "title": "Adjunctive Seltorexant in MDD With Insomnia Symptoms",
         "condition": "Major Depressive Disorder", "location": loc, "site": site,
         "name": "Marcus L.", "email": "marcus.l@example.com",
         "phone": "+1 212 555 0193", "age": "58", "sex": "male",
         "screener": scr_ok,
         "elig": {"met": ["DSM-5 MDD without psychotic features",
                          "Currently on a stable antidepressant"],
                  "unknown": ["Inadequate response to 1-2 antidepressants (MGH-ATRQ)",
                              "Confirm clinically significant insomnia (ISI)"],
                  "not_met": [],
                  "rationale": "Likely fit but no records connected, so prior "
                               "treatment history needs to be verified."}},
        {"days": 3, "status": "prescreen", "records": 0,
         "nct": "NCT07645924",
         "title": "Elismetrep (K-304) for the Acute Treatment of Migraine",
         "condition": "Migraine", "location": loc, "site": site,
         "name": "Dana K.", "email": "dana.k@example.com",
         "phone": "+1 212 555 0170", "age": "36", "sex": "female",
         "screener": {"travel": "yes", "other_trial": "yes",
                      "pregnancy": "no", "consent_capable": "yes"},
         "elig": {"met": ["Over 1-year history of migraine (IHS criteria)",
                          "Age within 18-75"],
                  "unknown": ["Confirm 2-10 moderate/severe attacks per month"],
                  "not_met": ["Currently enrolled in another interventional "
                              "trial (washout may be required)"],
                  "rationale": "Screener flags an active trial - confirm washout "
                               "period before proceeding."}},
        {"days": 4, "status": "prescreen", "records": 1,
         "nct": "NCT05711940",
         "title": "COMP360 Psilocybin in Treatment-Resistant Depression",
         "condition": "Treatment-Resistant Depression", "location": loc, "site": site,
         "name": "Robert P.", "email": "robert.p@example.com",
         "phone": "+1 212 555 0125", "age": "61", "sex": "male",
         "screener": scr_ok,
         "elig": {"met": ["MDD without psychotic features",
                          "MADRS 26 at screening (moderate-to-severe)",
                          "Two prior antidepressant failures on record"],
                  "unknown": ["Confirm 2-4 adequate trials via MGH-ATRQ",
                              "Washout / taper plan for current antidepressant"],
                  "not_met": [],
                  "rationale": "Strong TRD fit from the connected record; confirm "
                               "treatment history meets the definition."}},
        # ---- Reviewed: accepted -> likely eligible (contact revealed) ------
        {"days": 5, "status": "eligible", "decision": "accepted", "revealed": 1,
         "records": 1, "nct": "NCT07076407",
         "title": "Azetukalner vs Placebo in Major Depressive Disorder (X-NOVA3)",
         "condition": "Major Depressive Disorder", "location": loc, "site": site,
         "name": "Sarah M.", "email": "sarah.m@example.com",
         "phone": "+1 212 555 0132", "age": "49", "sex": "female",
         "screener": scr_ok, "accepted_days": 2,
         "elig": {"met": ["DSM-5-TR MDD confirmed (MINI)", "Episode duration in range",
                          "No bipolar or psychotic history on record"],
                  "unknown": [], "not_met": [],
                  "rationale": "Clear fit - moved to likely eligible."}},
        # ---- Reviewed: declined (no reveal) --------------------------------
        {"days": 6, "status": "closed", "decision": "declined", "revealed": 0,
         "records": 0, "nct": "NCT07076407",
         "title": "Azetukalner vs Placebo in Major Depressive Disorder (X-NOVA3)",
         "condition": "Major Depressive Disorder", "location": loc, "site": site,
         "name": "Emily R.", "email": "emily.r@example.com",
         "phone": "+1 212 555 0188", "age": "29", "sex": "female",
         "screener": scr_ok,
         "reason": "History of Bipolar II disorder - protocol exclusion",
         "declined_days": 3,
         "elig": {"met": ["Meets criteria for a current depressive episode"],
                  "unknown": [],
                  "not_met": ["History of Bipolar I/II disorder is an exclusion"],
                  "rationale": "Not eligible due to protocol exclusion."}},
        # ---- Reviewed: at screening visit ----------------------------------
        {"days": 7, "status": "screening", "decision": "accepted", "revealed": 1,
         "records": 1, "nct": "NCT07645924",
         "title": "Elismetrep (K-304) for the Acute Treatment of Migraine",
         "condition": "Migraine", "location": loc, "site": site,
         "name": "James T.", "email": "james.t@example.com",
         "phone": "+1 212 555 0161", "age": "47", "sex": "male",
         "screener": scr_ok, "accepted_days": 4, "screening_days": 1,
         "elig": {"met": ["Over 1-year migraine history (IHS)",
                          "About 4 moderate/severe attacks per month"],
                  "unknown": [], "not_met": [],
                  "rationale": "Invited to screening visit."}},
        # ---- Reviewed: enrolled --------------------------------------------
        {"days": 9, "status": "enrolled", "decision": "accepted", "revealed": 1,
         "records": 1, "nct": "NCT06559306",
         "title": "Adjunctive Seltorexant in MDD With Insomnia Symptoms",
         "condition": "Major Depressive Disorder", "location": loc, "site": site,
         "name": "Linda C.", "email": "linda.c@example.com",
         "phone": "+1 212 555 0117", "age": "55", "sex": "female",
         "screener": scr_ok, "accepted_days": 6, "screening_days": 3,
         "enrolled_days": 1,
         "elig": {"met": ["DSM-5 MDD with insomnia symptoms",
                          "Inadequate response to one antidepressant this episode",
                          "Age within 18-74"],
                  "unknown": [], "not_met": [],
                  "rationale": "Completed screening and enrolled."}},
    ]

    # Make the demo queue feel realistic for ATS walkthroughs: many candidates
    # across review/active/done instead of a tiny sample.
    # Study lookup (nct, title, condition) keyed by a short code, so the volume
    # roster below stays readable. All applicants are at the one demo site (NYC).
    # Real, currently-active trials (verified on ClinicalTrials.gov by NCT),
    # attributed to the fictional demo site. Titles are shortened for the UI but
    # the NCTs are exact, so each study reads as a real protocol.
    _S = {
        "aze": ("NCT07076407",
                "Azetukalner vs Placebo in Major Depressive Disorder (X-NOVA3)",
                "Major Depressive Disorder"),
        "azeo": ("NCT06922110",
                 "Azetukalner Open-Label Extension in Major Depressive Disorder (X-NOVA-OLE)",
                 "Major Depressive Disorder"),
        "sel": ("NCT06559306",
                "Adjunctive Seltorexant in MDD With Insomnia Symptoms",
                "Major Depressive Disorder"),
        "selm": ("NCT07573176",
                 "Seltorexant Monotherapy in Major Depressive Disorder",
                 "Major Depressive Disorder"),
        "trd": ("NCT05711940",
                "COMP360 Psilocybin in Treatment-Resistant Depression",
                "Treatment-Resistant Depression"),
        "mig": ("NCT07645924",
                "Elismetrep (K-304) for the Acute Treatment of Migraine",
                "Migraine"),
        "migl": ("NCT07674654",
                 "Elismetrep (K-304) Long-Term Safety in Acute Migraine",
                 "Acute Migraine"),
        "umm": ("NCT06417775",
                "Ubrogepant for Menstrual Migraine",
                "Menstrual Migraine"),
    }
    # (first, last, sex, age, status, records, study-code). Spread across all 8
    # active demo trials. Menstrual-migraine (umm) applicants are female by design.
    _rows = [
        ("Ava", "Chen", "female", "41", "prescreen", 1, "aze"),
        ("Noah", "Bernstein", "male", "49", "prescreen", 0, "sel"),
        ("Mia", "Alvarez", "female", "33", "prescreen", 1, "trd"),
        ("Liam", "Okafor", "male", "43", "prescreen", 0, "mig"),
        ("Sofia", "Ramos", "female", "27", "eligible", 1, "aze"),
        ("Ethan", "Weiss", "male", "62", "screening", 1, "sel"),
        ("Isla", "Thompson", "female", "46", "screening", 1, "mig"),
        ("Leo", "Kaplan", "male", "54", "enrolled", 1, "trd"),
        ("Chloe", "Nguyen", "female", "31", "closed", 0, "mig"),
        ("Mason", "Rivera", "male", "58", "eligible", 1, "trd"),
        ("Ella", "Brooks", "female", "39", "prescreen", 0, "azeo"),
        ("James", "Sullivan", "male", "50", "prescreen", 1, "sel"),
        ("Zoe", "Feldman", "female", "45", "eligible", 1, "aze"),
        ("Lucas", "Park", "male", "37", "screening", 0, "mig"),
        ("Grace", "Adams", "female", "64", "closed", 0, "trd"),
        ("Olivia", "Bennett", "female", "38", "eligible", 1, "sel"),
        ("Henry", "Cohen", "male", "63", "screening", 1, "trd"),
        ("Amelia", "Rosen", "female", "56", "enrolled", 1, "aze"),
        ("Jack", "Donovan", "male", "60", "eligible", 1, "selm"),
        ("Charlotte", "Diaz", "female", "26", "eligible", 1, "mig"),
        ("Benjamin", "Foster", "male", "48", "screening", 1, "trd"),
        ("Harper", "Reed", "female", "35", "enrolled", 1, "migl"),
        ("Daniel", "Goldberg", "male", "52", "eligible", 1, "sel"),
        ("Aria", "Morgan", "female", "40", "eligible", 1, "aze"),
        ("William", "Perry", "male", "51", "screening", 1, "azeo"),
        ("Scarlett", "Klein", "female", "53", "eligible", 1, "selm"),
        ("Michael", "Torres", "male", "59", "enrolled", 1, "trd"),
        ("Rosa", "Delacruz", "female", "34", "eligible", 1, "umm"),
        ("Nadia", "Haddad", "female", "29", "screening", 1, "umm"),
        ("Tara", "Okonkwo", "female", "44", "prescreen", 1, "umm"),
        ("Bianca", "Lozano", "female", "37", "enrolled", 1, "umm"),
        ("Gina", "Petrov", "female", "31", "prescreen", 0, "umm"),
        # More fresh inbound requests (prescreen) so the "New" queue reads busy
        # across every trial, not a couple of stragglers.
        ("Nora", "Whitfield", "female", "34", "prescreen", 1, "aze"),
        ("Elijah", "Barnes", "male", "45", "prescreen", 0, "sel"),
        ("Priya", "Nair", "female", "29", "prescreen", 1, "mig"),
        ("Caleb", "Fisher", "male", "57", "prescreen", 0, "trd"),
        ("Maya", "Stein", "female", "42", "prescreen", 1, "azeo"),
        ("Oscar", "Delgado", "male", "39", "prescreen", 1, "migl"),
        ("Ruth", "Abramson", "female", "61", "prescreen", 0, "selm"),
        ("Simon", "Yang", "male", "47", "prescreen", 1, "trd"),
        ("Talia", "Rosenthal", "female", "31", "prescreen", 1, "aze"),
        ("Devon", "Pierce", "male", "53", "prescreen", 0, "mig"),
        ("Hannah", "Blum", "female", "38", "prescreen", 1, "sel"),
        ("Andre", "Costa", "male", "44", "prescreen", 1, "azeo"),
        ("Vera", "Lindqvist", "female", "50", "prescreen", 0, "trd"),
        ("Marco", "Santos", "male", "36", "prescreen", 1, "migl"),
    ]
    extra = [(f, l, sx, ag, st, rc, _S[k][0], _S[k][1], _S[k][2], loc, site)
             for (f, l, sx, ag, st, rc, k) in _rows]

    for i, (first, last, sex, age, status, rec, nct, title, cond, loc, site) in enumerate(extra):
        idx = i + 1
        screener = {"travel": "yes", "other_trial": "no",
                    "pregnancy": "no", "consent_capable": "yes"}
        not_met = []
        reason = ""
        decision = "accepted" if status in ("eligible", "screening", "enrolled") else ""
        revealed = 1 if decision else 0
        accepted_days = 0
        screening_days = 0
        enrolled_days = 0
        declined_days = 0
        if status == "closed":
            decision = "declined"
            revealed = 0
            reason = "Protocol mismatch after coordinator review"
            not_met = ["Coordinator marked protocol mismatch"]
            declined_days = max(1, 3 + (idx % 3))
        if decision == "accepted":
            accepted_days = max(1, 2 + (idx % 4))
        if status == "screening":
            screening_days = max(1, accepted_days - 1)
        if status == "enrolled":
            screening_days = max(1, accepted_days - 1)
            enrolled_days = max(1, screening_days - 1)
        specs.append({
            "days": 10 + idx,
            "status": status,
            "decision": decision,
            "revealed": revealed,
            "records": rec,
            "nct": nct,
            "title": title,
            "condition": cond,
            "location": loc,
            "site": site,
            "name": f"{first} {last[0]}.",
            "email": f"candidate.{first.lower()}.{last.lower()}@example.com",
            "phone": f"+1 212 555 {1200 + idx:04d}",
            "age": age,
            "sex": sex,
            "screener": screener,
            "reason": reason,
            "accepted_days": accepted_days,
            "declined_days": declined_days,
            "screening_days": screening_days,
            "enrolled_days": enrolled_days,
            "elig": {
                "met": ["Age within the protocol range",
                        f"Reported diagnosis of {cond} aligns with inclusion",
                        "No exclusionary condition reported on the screener"],
                "unknown": _demo_elig_unknowns(cond, idx),
                "not_met": not_met,
                "rationale": (
                    "Coordinator marked a protocol mismatch after review."
                    if not_met else
                    "Meets the core inclusion criteria; a couple of items to "
                    "confirm at the screening visit."),
            },
        })
    return specs


def _demo_elig_unknowns(condition, idx):
    """A couple of plausible, condition-appropriate 'confirm at screening' items
    so the demo queue reads like real pre-screens instead of filler."""
    cond = (condition or "").lower()
    if "migraine" in cond:
        pool = ["Confirm 2-10 moderate/severe attacks per month (headache diary)",
                "Confirm migraine onset before age 50",
                "Rule out medication-overuse headache"]
    elif "resistant" in cond:
        pool = ["Confirm 2-4 adequate antidepressant trials (MGH-ATRQ)",
                "Confirm MADRS severity at screening",
                "Washout / taper plan for current antidepressant"]
    else:  # MDD and general
        pool = ["Confirm MADRS severity at screening",
                "Confirm current episode duration is in range",
                "Verify prior antidepressant response history"]
    return [pool[idx % len(pool)], pool[(idx + 1) % len(pool)]]


# --------------------------------------------------------------------------- #
# PI / coordinator document workspace: review & approve + append-only audit.
# Approve-only by design - we record internal approvals with a name, timestamp
# and meaning (the "manifestation of signature"), NOT a binding 21 CFR Part 11
# e-signature. Binding signatures + patient eConsent are deferred to a validated
# vendor (see COMPLIANCE.md). The trail lives in document_events (INSERT-only).
# --------------------------------------------------------------------------- #
DOC_CATEGORIES = ["consent", "patient", "regulatory", "site"]
DOC_STATUSES = ["pending", "in_review", "approved", "returned", "signed"]
DOC_STATUS_LABELS = {
    "pending": "Awaiting review", "in_review": "In review",
    "approved": "Approved", "returned": "Sent back", "signed": "Signed",
}
DOC_CATEGORY_LABELS = {
    "consent": "Patient consent", "patient": "Patient forms",
    "regulatory": "Regulatory / FDA", "site": "Site & training",
}


def create_document(user_id, title, nct="", category="regulatory", doc_type="",
                    party="investigator", party_name="", version="v1.0",
                    status="pending", lead_id=None, due_at="", summary=""):
    db = get_db()
    ts = now()
    cur = db.execute(
        "INSERT INTO trial_documents (user_id, nct, lead_id, category, doc_type, "
        "title, party, party_name, version, status, due_at, summary, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, _norm_nct(nct), lead_id, category, doc_type, title, party,
         party_name, version, status, due_at, summary, ts, ts))
    db.commit()
    add_document_event(cur.lastrowid, "created", actor="System",
                       note="Document added to the workspace")
    return cur.lastrowid


def add_document_event(document_id, action, meaning="", actor="", actor_role="",
                       note=""):
    """Append (never mutate) one audit row for a document action."""
    db = get_db()
    db.execute(
        "INSERT INTO document_events (document_id, action, meaning, actor, "
        "actor_role, note, created_at) VALUES (?,?,?,?,?,?,?)",
        (document_id, action, meaning, actor, actor_role, note, now()))
    db.commit()
    return True


def get_document(document_id):
    return get_db().execute("SELECT * FROM trial_documents WHERE id = ?",
                            (document_id,)).fetchone()


def list_documents(user_id, nct=None, category=None):
    members = org_member_ids(user_id)
    qs = ",".join("?" * len(members))
    q = f"SELECT * FROM trial_documents WHERE user_id IN ({qs})"
    args = list(members)
    if nct:
        q += " AND nct = ?"
        args.append(_norm_nct(nct))
    if category:
        q += " AND category = ?"
        args.append(category)
    q += " ORDER BY (status = 'pending') DESC, updated_at DESC"
    return get_db().execute(q, args).fetchall()


def list_document_events(document_id):
    return get_db().execute(
        "SELECT * FROM document_events WHERE document_id = ? ORDER BY id DESC",
        (document_id,)).fetchall()


def set_document_status(document_id, status, actor="", actor_role="", meaning="",
                        note=""):
    """Update a document's status AND append an audit event (review/approve/send
    back). Approve-only: 'approved' records an internal approval, not a binding
    Part 11 signature."""
    if status not in DOC_STATUSES:
        return False
    if not get_document(document_id):
        return False
    db = get_db()
    db.execute("UPDATE trial_documents SET status = ?, updated_at = ? WHERE id = ?",
               (status, now(), document_id))
    db.commit()
    action = {"approved": "approved", "returned": "returned",
              "in_review": "reviewed", "signed": "signed"}.get(status, status)
    add_document_event(document_id, action, meaning=meaning, actor=actor,
                       actor_role=actor_role, note=note)
    return True


def document_counts(user_id):
    members = org_member_ids(user_id)
    qs = ",".join("?" * len(members))
    rows = get_db().execute(
        f"SELECT status, COUNT(*) n FROM trial_documents WHERE user_id IN ({qs}) "
        "GROUP BY status", members).fetchall()
    return {r["status"]: r["n"] for r in rows}


# --------------------------------------------------------------------------- #
# IRB / REB recruitment-material submissions
# --------------------------------------------------------------------------- #
# The review workflow, in the order a real submission moves. `revisions` = the IRB
# returned "modifications required" (you edit and resubmit). `expired` = the annual
# continuing-review lapsed (materials can no longer be used until renewed).
IRB_STATUSES = ("draft", "submitted", "in_review", "revisions", "approved", "expired")
IRB_STATUS_LABELS = {
    "draft": "Draft", "submitted": "Submitted", "in_review": "Under review",
    "revisions": "Revisions requested", "approved": "Approved", "expired": "Expired",
}
IRB_STATUS_TONE = {
    "draft": "neutral", "submitted": "info", "in_review": "info",
    "revisions": "danger", "approved": "ok", "expired": "warn",
}
IRB_SUBMISSION_TYPES = {
    "initial": "Initial review",
    "modification": "Modification / amendment",
    "continuing": "Continuing review",
}
# The big central boards most US sites use, plus a local/academic escape hatch.
KNOWN_IRBS = ("WCG IRB", "Advarra IRB", "Advarra CIRBI", "Sterling IRB",
              "Local / academic IRB")
IRB_ITEM_KINDS = {
    "flyer": "Flyer / poster", "social": "Social media ad", "script": "Phone screening script",
    "letter": "Recruitment letter / email", "consent": "Consent form", "protocol": "Protocol",
    "campaign": "Campaign creative", "material": "Recruitment material",
}


def create_irb_submission(user_id, nct, title, irb_name="", irb_kind="central",
                          submission_type="initial", pi_name="",
                          protocol_version="", notes=""):
    db = get_db()
    ts = now()
    cur = db.execute(
        "INSERT INTO irb_submissions (user_id, nct, title, irb_name, irb_kind, "
        "submission_type, status, pi_name, protocol_version, notes, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,'draft',?,?,?,?,?)",
        (user_id, _norm_nct(nct), (title or "").strip(), (irb_name or "").strip(),
         (irb_kind or "central"), (submission_type or "initial"),
         (pi_name or "").strip(), (protocol_version or "").strip(),
         (notes or "").strip(), ts, ts))
    db.commit()
    sid = cur.lastrowid
    add_irb_event(sid, "created", meaning="Package created")
    return sid


def get_irb_submission(sub_id):
    return get_db().execute("SELECT * FROM irb_submissions WHERE id = ?",
                            (sub_id,)).fetchone()


def list_irb_submissions_for_user(user_id, ncts=None):
    members = org_member_ids(user_id)
    mqs = ",".join("?" * len(members))
    ncts = sorted({_norm_nct(x) for x in (ncts or []) if x})
    if ncts:
        qs = ",".join("?" * len(ncts))
        return get_db().execute(
            f"SELECT * FROM irb_submissions WHERE user_id IN ({mqs}) AND nct IN ({qs}) "
            "ORDER BY updated_at DESC, id DESC", members + ncts).fetchall()
    return get_db().execute(
        f"SELECT * FROM irb_submissions WHERE user_id IN ({mqs}) "
        "ORDER BY updated_at DESC, id DESC", members).fetchall()


def add_irb_item(submission_id, kind="material", label="", version="v1.0",
                 detail="", campaign_id=None):
    db = get_db()
    db.execute(
        "INSERT INTO irb_submission_items (submission_id, kind, campaign_id, label, "
        "version, detail, created_at) VALUES (?,?,?,?,?,?,?)",
        (submission_id, kind, campaign_id, (label or "").strip(),
         (version or "v1.0").strip(), (detail or "").strip(), now()))
    db.execute("UPDATE irb_submissions SET updated_at = ? WHERE id = ?",
               (now(), submission_id))
    db.commit()
    return True


def list_irb_items(submission_id):
    return get_db().execute(
        "SELECT * FROM irb_submission_items WHERE submission_id = ? ORDER BY id",
        (submission_id,)).fetchall()


def remove_irb_item(item_id, submission_id):
    db = get_db()
    db.execute("DELETE FROM irb_submission_items WHERE id = ? AND submission_id = ?",
               (item_id, submission_id))
    db.execute("UPDATE irb_submissions SET updated_at = ? WHERE id = ?",
               (now(), submission_id))
    db.commit()
    return True


def add_irb_event(submission_id, action, meaning="", actor="", actor_role="",
                  note=""):
    """Append (never mutate) one audit row for a submission action."""
    db = get_db()
    db.execute(
        "INSERT INTO irb_submission_events (submission_id, action, meaning, actor, "
        "actor_role, note, created_at) VALUES (?,?,?,?,?,?,?)",
        (submission_id, action, meaning, actor, actor_role, note, now()))
    db.commit()
    return True


def list_irb_events(submission_id):
    return get_db().execute(
        "SELECT * FROM irb_submission_events WHERE submission_id = ? ORDER BY id DESC",
        (submission_id,)).fetchall()


def _irb_linked_campaign_ids(submission_id):
    rows = get_db().execute(
        "SELECT DISTINCT campaign_id FROM irb_submission_items "
        "WHERE submission_id = ? AND campaign_id IS NOT NULL", (submission_id,)
    ).fetchall()
    return [r["campaign_id"] for r in rows if r["campaign_id"]]


def advance_irb_status(submission_id, status, actor="", actor_role="", note="",
                       submission_ref="", approved_version="", expires_at=""):
    """Move a submission through its review states AND append an audit event. The
    compliance side-effects live here so no route can bypass them:
      - `approved`  -> flip every linked campaign's irb_approved gate ON (they can
        now go live), stamping the cleared version + expiry.
      - `revisions`/`expired` -> revoke the gate on linked campaigns and PAUSE any
        that were live, so unapproved/lapsed creative can't keep running.
    Returns (ok, reason)."""
    status = (status or "").strip().lower()
    if status not in IRB_STATUSES:
        return False, "invalid status"
    sub = get_irb_submission(submission_id)
    if not sub:
        return False, "not found"
    db = get_db()
    ts = now()
    fields = ["status = ?", "updated_at = ?"]
    args = [status, ts]
    if submission_ref:
        fields.append("submission_ref = ?"); args.append(submission_ref.strip())
    if status == "approved":
        fields.append("approved_at = ?"); args.append(ts)
        fields.append("approved_version = ?")
        args.append((approved_version or "").strip() or "v1.0")
        if expires_at:
            fields.append("expires_at = ?"); args.append(expires_at.strip())
    args.append(submission_id)
    db.execute(f"UPDATE irb_submissions SET {', '.join(fields)} WHERE id = ?", args)
    db.commit()

    meaning = IRB_STATUS_LABELS.get(status, status)
    add_irb_event(submission_id, status, meaning=meaning, actor=actor,
                  actor_role=actor_role, note=note)

    # Propagate to the campaign gate.
    cids = _irb_linked_campaign_ids(submission_id)
    if status == "approved":
        for cid in cids:
            approve_campaign(cid, approved=True)
    elif status in ("revisions", "expired"):
        for cid in cids:
            approve_campaign(cid, approved=False)
            c = get_campaign(cid)
            if c and (c["status"] or "") == "active":
                db.execute("UPDATE campaigns SET status = 'paused', updated_at = ? "
                           "WHERE id = ?", (now(), cid))
        db.commit()
    return True, ""


def irb_counts(user_id):
    members = org_member_ids(user_id)
    qs = ",".join("?" * len(members))
    rows = get_db().execute(
        f"SELECT status, COUNT(*) n FROM irb_submissions WHERE user_id IN ({qs}) "
        "GROUP BY status", members).fetchall()
    return {r["status"]: r["n"] for r in rows}


def _demo_doc_specs():
    """The standard ICH-GCP essential-documents set + patient consent forms a PI
    and coordinator actually route and sign, staged across statuses so the demo
    workspace looks live. (patient=True attaches it to a revealed applicant.)"""
    return [
        # cat, type, title, party, name, version, status, due_days, summary, patient
        ("consent", "icf", "Informed Consent Form", "patient", None, "v2.1",
         "signed", None,
         "Main IRB-approved study consent (v2.1), signed via the eConsent vendor - "
         "the approval is recorded here for the binder.", True),
        ("patient", "hipaa", "HIPAA Authorization", "patient", None, "v1.0",
         "signed", None,
         "Authorizes use and disclosure of the participant's health information "
         "for this study only.", True),
        ("patient", "records_release", "Medical Records Release", "patient", None,
         "v1.0", "in_review", 2,
         "Release to obtain outside records confirming the diagnosis before "
         "screening.", True),
        ("regulatory", "form_1572", "FDA Form 1572 - Statement of Investigator",
         "investigator", "David Chen, MD", "v1.0", "pending", 1,
         "The PI's commitment to conduct the trial per the protocol and 21 CFR 312.",
         False),
        ("regulatory", "doa_log", "Delegation of Authority Log", "investigator",
         "David Chen, MD", "v3", "in_review", 2,
         "Which team members are authorized for which trial tasks - the PI must "
         "review and sign.", False),
        ("regulatory", "fin_disclosure", "Financial Disclosure (FDA 3455)",
         "investigator", "David Chen, MD", "v1.0", "pending", 3,
         "Investigator conflict-of-interest disclosure required by the sponsor.",
         False),
        ("regulatory", "irb_approval", "IRB Approval Letter + Approved ICF",
         "coordinator", "WCG IRB", "2026-A", "approved", None,
         "Ethics board approval of the protocol and the current consent version.",
         False),
        ("regulatory", "protocol_amend", "Protocol Amendment 3 - Signature Page",
         "investigator", "David Chen, MD", "Amd 3", "pending", 1,
         "PI acknowledgement and sign-off on the latest protocol amendment.", False),
        ("site", "ib_ack", "Investigator's Brochure - Acknowledgement",
         "investigator", "David Chen, MD", "Ed 7", "in_review", 4,
         "Confirms the PI reviewed the current IB safety information.", False),
        ("site", "gcp_cert", "GCP Training Certificate", "coordinator",
         "Avery Kim, MPH", "2026", "approved", None,
         "Good Clinical Practice training on file for the coordinator.", False),
        ("site", "lab_cert", "Lab Certification (CLIA/CAP) + Normal Ranges",
         "coordinator", "Central Lab", "2026", "approved", None,
         "Local lab accreditation and reference ranges for the regulatory binder.",
         False),
    ]


def seed_demo_trial_documents(user_id):
    """Populate a realistic document workspace on the demo account. No-op once any
    document exists for the user, so real data is never mixed with demo."""
    if not user_id:
        return
    db = get_db()
    if db.execute("SELECT COUNT(*) n FROM trial_documents WHERE user_id = ?",
                  (user_id,)).fetchone()["n"]:
        return
    ncts = sorted(user_claimed_ncts(user_id) or [])
    default_nct = ncts[0] if ncts else ""
    patient_leads = db.execute(
        "SELECT id, name, nct FROM leads WHERE revealed = 1 ORDER BY id LIMIT 4"
    ).fetchall()
    pi = 0
    for (cat, dtype, title, party, pname, ver, status, due_days, summary,
         is_patient) in _demo_doc_specs():
        lead_id, nct = None, default_nct
        if is_patient and patient_leads:
            ld = patient_leads[pi % len(patient_leads)]
            pi += 1
            lead_id, nct, pname = ld["id"], (ld["nct"] or default_nct), ld["name"]
        due_at = ""
        if due_days is not None:
            due_at = (dt.datetime.now() + dt.timedelta(days=due_days)).strftime(
                "%Y-%m-%d %H:%M")
        doc_id = create_document(user_id, title, nct=nct, category=cat,
                                 doc_type=dtype, party=party,
                                 party_name=pname or "", version=ver,
                                 status=status, lead_id=lead_id, due_at=due_at,
                                 summary=summary)
        # Backfill a believable audit trail for non-pending docs.
        if status in ("in_review", "approved", "returned", "signed"):
            add_document_event(doc_id, "sent", actor="Avery Kim, MPH",
                               actor_role="Coordinator", note="Routed for review")
        if is_patient:
            add_document_event(doc_id, "received", actor=(pname or "Applicant"),
                               actor_role="Patient", note="Returned by participant")
        if status == "approved":
            add_document_event(
                doc_id, "approved", meaning="Approval", actor="David Chen, MD",
                actor_role="Principal Investigator",
                note="Reviewed and approved for the regulatory binder.")
        if status == "signed":
            add_document_event(
                doc_id, "approved", meaning="Approval", actor="David Chen, MD",
                actor_role="Principal Investigator",
                note="Signed via the validated e-signature vendor; approval "
                     "recorded here.")


def seed_demo_claims(user_id):
    """Claim the demo studies for this account so every study-team surface
    (dashboard, applicants, campaigns, documents) has studies to hang data off.
    Titles are derived from the seeded demo leads so they always match. No-op if
    the account already claims anything (never touches a real, non-empty account)."""
    if not user_id:
        return
    if list_study_claims(user_id):
        return
    db = get_db()
    rows = db.execute(
        "SELECT nct, MAX(title) title FROM leads WHERE nct != '' "
        "GROUP BY nct ORDER BY nct").fetchall()
    for r in rows[:8]:
        add_study_claim(user_id, r["nct"], r["title"] or "", verified=True)
        # Reusable per-study booking link + attach it to already-accepted demo
        # applicants so the scheduling flow shows as live (link already sent).
        link = "https://calendly.com/bridgemd-demo/screening"
        set_claim_schedule_url(user_id, r["nct"], link)
        db.execute(
            "UPDATE leads SET schedule_url = ? WHERE nct = ? AND schedule_url = '' "
            "AND status IN ('eligible','screening','enrolled')", (link, r["nct"]))
        # Connected sources = the channels this study's demo leads actually came
        # in on, so each trial's inbox filters reflect a real, differing source
        # mix (one trial runs IG+email, another adds Facebook/CT.gov, etc.).
        srcs = [row["source"] for row in db.execute(
            "SELECT DISTINCT source FROM leads WHERE nct = ?", (r["nct"],)).fetchall()]
        connected = _norm_sources(srcs)
        if connected:
            set_claim_connected_sources(user_id, r["nct"], connected)
    # The coordinator's own account calendar (the app-wide default) + a video
    # link on applicants already in screening, so the calls flow shows as live.
    if not get_site_calendar_url(user_id):
        set_site_calendar_url(user_id, "https://calendly.com/bridgemd-demo/screening")
    db.execute(
        "UPDATE leads SET video_url = 'https://meet.google.com/bmd-demo-visit' "
        "WHERE video_url = '' AND status IN ('screening','enrolled') "
        "AND nct IN (SELECT nct FROM study_claims WHERE user_id = ?)", (user_id,))
    db.commit()


def _demo_internal_match_specs():
    """De-identified internal patients (CNS/psychiatry, to fit the demo's
    claimed studies) surfaced from the clinic's own records. Assigned round-robin
    across whatever the account has claimed."""
    return [
        {"patient_ref": "R.M.", "full_name": "Rebecca M.", "age": "54", "sex": "Female",
         "summary": "Major depressive disorder, partial response to sertraline. Coming in Thu for a routine follow-up.",
         "source_label": "Upcoming visit · Thu 10:30", "verdict": "likely_eligible", "score": 91,
         "met": ["DSM-5-TR MDD in a current episode",
                 "Current episode duration within the 6-week to 24-month window",
                 "Age within 18-74"],
         "unknown": ["Confirm MADRS severity at screening",
                     "First lifetime episode onset before age 50?"],
         "rationale": "Diagnosis and episode history line up with the MDD inclusion criteria."},
        {"patient_ref": "D.O.", "full_name": "David O.", "age": "47", "sex": "Male",
         "summary": "Recurrent MDD with persistent insomnia, on escitalopram with partial benefit. Seen last month.",
         "source_label": "Problem list · seen 3w ago", "verdict": "likely_eligible", "score": 88,
         "met": ["MDD without psychotic features",
                 "Reports clinically significant insomnia",
                 "On a stable antidepressant"],
         "unknown": ["Inadequate response confirmed via MGH-ATRQ?",
                     "ISI insomnia threshold at screening?"],
         "rationale": "Strong fit for the adjunctive-insomnia study; two items to confirm at screening."},
        {"patient_ref": "S.K.", "full_name": "Sara K.", "age": "61", "sex": "Female",
         "summary": "Episodic migraine, roughly 5 moderate/severe attacks per month, no aura.",
         "source_label": "Upcoming visit · next Tue", "verdict": "possible", "score": 66,
         "met": ["Over 1-year migraine history (IHS)", "Attack frequency in range"],
         "unknown": ["Confirm 2-10 attacks/month via a prospective diary",
                     "Rule out medication-overuse headache"],
         "rationale": "Likely a fit but attack frequency must be confirmed in a diary."},
        {"patient_ref": "J.T.", "full_name": "James T.", "age": "58", "sex": "Male",
         "summary": "Treatment-resistant depression; failed 3 adequate antidepressant trials this episode.",
         "source_label": "Problem list", "verdict": "possible", "score": 61,
         "met": ["MDD without psychotic features", "Multiple prior treatment failures"],
         "unknown": ["Confirm 2-4 adequate trials via MGH-ATRQ",
                     "Washout / taper plan for current antidepressant"],
         "rationale": "Meets the TRD picture; confirm the treatment history against the definition."},
        {"patient_ref": "A.L.", "full_name": "Aisha L.", "age": "43", "sex": "Female",
         "summary": "Moderate MDD, MADRS ~24, no bipolar history. New patient this week.",
         "source_label": "New this week", "verdict": "likely_eligible", "score": 84,
         "met": ["Current MDE, moderate severity", "No bipolar/psychotic history"],
         "unknown": ["Confirm episode duration window",
                     "MINI diagnostic confirmation at screening"],
         "rationale": "Clear MDD fit; standard screening items remain."},
        {"patient_ref": "M.P.", "full_name": "Marcus P.", "age": "66", "sex": "Male",
         "summary": "MDD, prior MI (2019), coming in for a medication review.",
         "source_label": "Upcoming visit · Fri", "verdict": "possible", "score": 54,
         "met": ["Meets criteria for a current depressive episode"],
         "unknown": ["Prior MI: confirm it's outside the exclusion window",
                     "Current cardiac status stable?"],
         "rationale": "Meets depression criteria; cardiac history needs review against exclusions."},
        {"patient_ref": "P.R.", "full_name": "Priya R.", "age": "50", "sex": "Female",
         "summary": "Migraine with aura, ~6 attacks/month. Flagged at a recent visit.",
         "source_label": "Recent visit", "verdict": "likely_eligible", "score": 79,
         "met": ["Over 1-year migraine history (IHS)", "6 attacks/month is in range"],
         "unknown": ["Confirm attack severity in a diary", "Any preventive medication use"],
         "rationale": "Clean episodic-migraine fit; only routine screening items remain."},
        {"patient_ref": "C.N.", "full_name": "Carlos N.", "age": "63", "sex": "Male",
         "summary": "Depressive episode, but a note of possible past hypomania in the chart.",
         "source_label": "Upcoming visit · Mon", "verdict": "possible", "score": 57,
         "met": ["Current depressive episode documented"],
         "unknown": ["Rule out Bipolar I/II (possible past hypomania), an exclusion",
                     "Confirm with MINI at screening"],
         "rationale": "Meets depression criteria; bipolarity must be ruled out before enrolling."},
        {"patient_ref": "H.B.", "full_name": "Hannah B.", "age": "41", "sex": "Female",
         "summary": "Episodic migraine, ~4 attacks/month, on no preventive therapy. New patient.",
         "source_label": "New patient this week", "verdict": "likely_eligible", "score": 83,
         "met": ["Over 1-year migraine history", "Attack frequency within 2-10/month"],
         "unknown": ["Confirm non-childbearing status or contraception per protocol"],
         "rationale": "Strong migraine fit; standard screening only."},
        {"patient_ref": "W.G.", "full_name": "Wei G.", "age": "56", "sex": "Male",
         "summary": "Treatment-resistant depression, MADRS 28, failed two adequate antidepressant trials.",
         "source_label": "Psychiatry referral", "verdict": "likely_eligible", "score": 89,
         "met": ["MADRS 28 (moderate-to-severe)", "Two documented antidepressant failures",
                 "MDD without psychotic features"],
         "unknown": ["Confirm trials meet MGH-ATRQ adequacy", "Taper/washout plan"],
         "rationale": "Clean TRD fit: severity and treatment history already documented."},
    ]


def _demo_external_match_specs():
    """Partner-site trials (studies the clinic does NOT run). A match here becomes
    a secure referral after the patient consents - the clinic gives better care,
    BridgeMD connects the network. Fake but realistic sites."""
    return [
        {"patient_ref": "E.C.", "full_name": "Eleanor C.", "age": "72", "sex": "Female",
         "summary": "Mild cognitive impairment, MMSE 25, family history of Alzheimer's. Followed in clinic.",
         "source_label": "Problem list", "nct": "NCT05310071",
         "trial_title": "Anti-Amyloid Infusion in Early Alzheimer's Disease",
         "condition": "Early Alzheimer's disease",
         "site_name": "Sunnybrook Health Sciences Centre", "site_location": "Toronto, ON",
         "verdict": "likely_eligible", "score": 86,
         "met": ["MCI with documented memory decline", "Age within 55-80 window",
                 "Study partner available"],
         "unknown": ["Amyloid PET / CSF confirmation needed", "MRI to rule out microhemorrhages"],
         "rationale": "Clinical picture fits; biomarker confirmation happens at the trial site."},
        {"patient_ref": "T.W.", "full_name": "Tobias W.", "age": "39", "sex": "Male",
         "summary": "Moderate-to-severe plaque psoriasis, ~12% BSA, failed two topicals.",
         "source_label": "Upcoming visit · next week", "nct": "NCT05590297",
         "trial_title": "Once-Daily Oral Therapy for Moderate-to-Severe Plaque Psoriasis",
         "condition": "Plaque psoriasis",
         "site_name": "Toronto Dermatology Research", "site_location": "Toronto, ON",
         "verdict": "likely_eligible", "score": 82,
         "met": ["BSA \u226510% meets severity threshold", "Failed prior topical therapy",
                 "Adult 18-70"],
         "unknown": ["Washout from prior systemics", "Latent TB screen on file?"],
         "rationale": "Meets the severity bar for a systemic-therapy trial nearby."},
        {"patient_ref": "G.R.", "full_name": "Grace R.", "age": "45", "sex": "Female",
         "summary": "Crohn's disease, moderate activity despite azathioprine. Recent flare.",
         "source_label": "Problem list · flare 2w ago", "nct": "NCT05625048",
         "trial_title": "Investigational Biologic for Moderate-to-Severe Crohn's Disease",
         "condition": "Crohn's disease",
         "site_name": "Mount Sinai IBD Centre", "site_location": "Toronto, ON",
         "verdict": "possible", "score": 63,
         "met": ["Confirmed Crohn's, moderate activity", "Inadequate response to immunomodulator"],
         "unknown": ["Recent colonoscopy for endoscopic score", "Prior biologic exposure count"],
         "rationale": "Likely eligible but endoscopic scoring is needed to confirm."},
    ]


def seed_demo_patient_matches(user_id):
    """Populate the internal-matching queue: the clinic's own de-identified
    patients surfaced as candidates for studies it runs (internal) and partner
    trials (external). Idempotent per patient (keyed on patient_ref), so it tops
    up newly-added demo patients without duplicating or wiping the DB. Only the
    first seed sets the approved/referred/passed lifecycle examples."""
    if not user_id:
        return
    db = get_db()
    existing = {r["patient_ref"] for r in db.execute(
        "SELECT patient_ref FROM patient_matches WHERE user_id = ?",
        (user_id,)).fetchall()}
    first_seed = not existing
    claims = list_study_claims(user_id)
    studies = [(c["nct"], c["title"] or c["nct"]) for c in claims]
    if not studies:
        rows = db.execute("SELECT nct, MAX(title) title FROM leads "
                          "WHERE nct != '' GROUP BY nct ORDER BY nct").fetchall()
        studies = [(r["nct"], r["title"] or r["nct"]) for r in rows]
    for i, spec in enumerate(_demo_internal_match_specs()):
        if spec["patient_ref"] in existing:
            continue
        if studies:
            nct, title = studies[i % len(studies)]
        else:
            nct, title = "", "Your claimed study"
        spec = dict(spec)
        spec.update({"kind": "internal", "nct": nct, "trial_title": title,
                     "condition": spec.get("condition", "")})
        create_patient_match(user_id, spec)
    for spec in _demo_external_match_specs():
        if spec["patient_ref"] in existing:
            continue
        spec = dict(spec)
        spec["kind"] = "external"
        create_patient_match(user_id, spec)
    # On the very first seed, show the whole lifecycle in the filters: one
    # already added to a study, one referred out, one passed on.
    if first_seed:
        seeded = db.execute(
            "SELECT id, kind FROM patient_matches WHERE user_id = ? ORDER BY id",
            (user_id,)).fetchall()
        internal_ids = [r["id"] for r in seeded if r["kind"] == "internal"]
        external_ids = [r["id"] for r in seeded if r["kind"] == "external"]
        if len(internal_ids) >= 2:
            set_patient_match_status(user_id, internal_ids[-1], "approved")
            set_patient_match_status(user_id, internal_ids[-2], "dismissed")
        if external_ids:
            set_patient_match_status(user_id, external_ids[-1], "referred")


def seed_demo_campaigns(user_id):
    """Populate the campaigns surface so the marketing side isn't empty in the
    demo: a live/paused/draft mix across claimed studies, each with a placement.
    No-op once the account has any campaign."""
    if not user_id:
        return
    if list_campaigns_for_user(user_id):
        return
    ncts = sorted(user_claimed_ncts(user_id) or [])
    if not ncts:
        return

    def pick(i):
        return ncts[i % len(ncts)]

    specs = [
        (pick(0), "Meta - depression study awareness", "meta", 1200, "active", True,
         "Depression interest - NYC metro", "Instagram / Facebook feed"),
        (pick(1), "Google Search - depression trial", "google", 800, "active", True,
         "Search - branded + condition", "Google Ads"),
        (pick(2), "Reddit - r/migraine recruitment", "reddit", 400, "paused", True,
         "r/migraine weekly thread", "Reddit post"),
        (pick(3), "Campus flyers - NYU / Columbia", "campus", 150, "draft", False,
         "NYU & Columbia community boards", "Printed flyer"),
    ]
    for nct, name, channel, budget, status, approved, place, place_ch in specs:
        cid = create_campaign(user_id, nct, name, channel=channel,
                              budget_usd=budget)
        set_campaign_creative(
            cid, "Volunteers needed for a research study",
            "A local research team is enrolling participants for a study. See if "
            "you may be eligible - takes a couple of minutes.",
            "You may qualify for a clinical research study in your area. Check your "
            "eligibility and apply in minutes.")
        if approved:
            approve_campaign(cid, approved=True)
            set_campaign_status(cid, status)
        create_placement(cid, label=place, channel=place_ch)


def seed_demo_irb_submissions(user_id):
    """Populate the IRB & approvals surface so the recruitment-compliance workflow
    reads as live: submissions across every review state (approved w/ expiry, under
    review, revisions requested, draft), each with a materials package, an audit
    trail attributed to the fictional demo staff, and linked to the matching campaign
    so approval visibly unlocks that campaign's gate. No-op once any submission
    exists. Demo-only (see COMPLIANCE.md)."""
    if not user_id:
        return
    db = get_db()
    if db.execute("SELECT COUNT(*) n FROM irb_submissions WHERE user_id = ?",
                  (user_id,)).fetchone()["n"]:
        return
    studies = list_team_studies(user_id)
    if not studies:
        return
    title_by_nct = {s["nct"]: (s["title"] or s["nct"]) for s in studies}
    ncts = [s["nct"] for s in studies]
    camps = db.execute(
        "SELECT id, nct FROM campaigns WHERE user_id = ?", (user_id,)).fetchall()
    camp_by_nct = {}
    for c in camps:
        camp_by_nct.setdefault(c["nct"], c["id"])

    base = dt.datetime.now()

    def ds(days=0):
        return (base + dt.timedelta(days=days)).strftime("%Y-%m-%d")

    def dts(days=0, hours=0):
        return (base + dt.timedelta(days=days, hours=hours)).strftime("%Y-%m-%d %H:%M")

    def mk(nct, title, irb_name, irb_kind, sub_type, status, pi, proto,
           ref, approved_ver, approved_days, expires_days, items, events):
        cid = camp_by_nct.get(nct)
        cur = db.execute(
            "INSERT INTO irb_submissions (user_id, nct, title, irb_name, irb_kind, "
            "submission_type, status, pi_name, protocol_version, submission_ref, "
            "approved_version, approved_at, expires_at, notes, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, _norm_nct(nct), title, irb_name, irb_kind, sub_type, status,
             pi, proto, ref,
             approved_ver, (dts(approved_days) if approved_days is not None else ""),
             (ds(expires_days) if expires_days is not None else ""), "",
             dts(-40), dts(0)))
        sid = cur.lastrowid
        for kind, label, ver, detail, link in items:
            db.execute(
                "INSERT INTO irb_submission_items (submission_id, kind, campaign_id, "
                "label, version, detail, created_at) VALUES (?,?,?,?,?,?,?)",
                (sid, kind, (cid if link else None), label, ver, detail, dts(-39)))
        for edays, action, meaning, actor, role, note in events:
            db.execute(
                "INSERT INTO irb_submission_events (submission_id, action, meaning, "
                "actor, actor_role, note, created_at) VALUES (?,?,?,?,?,?,?)",
                (sid, action, meaning, actor, role, note, dts(edays)))
        # An APPROVED package clears its linked campaign's gate. We deliberately do
        # NOT revoke on other states here: a modification/continuing review runs
        # against materials that are already approved and still live, so the seed
        # leaves those campaigns as they are (the live status engine still revokes
        # when a user actively marks revisions/expired on a real submission).
        if cid and status == "approved":
            approve_campaign(cid, approved=True)
        db.commit()
        return sid

    KARA = ("Avery Kim, MPH", "Clinical Research Coordinator")
    DANNY = ("Riley Patel", "Clinical Research Coordinator")
    MARG = ("Priya Shah, MD", "Director of Clinical Operations")
    PAUL = ("David Chen, MD", "Principal Investigator")

    # 1) COMP360 - APPROVED, with a live expiry (the happy path).
    n0 = ncts[0]
    mk(n0, "Recruitment materials: initial review", "WCG IRB", "central",
       "initial", "approved", PAUL[0], "v3.0", "WCG #20260142", "v2.0", -58,
       305,
       [("flyer", "Waiting-room flyer / poster", "v2.0",
         "One-page flyer for the clinic waiting room and community boards.", False),
        ("social", "Instagram / Facebook ad", "v2.0",
         "Neutral awareness ad - no benefit or payment claims.", True),
        ("script", "Phone pre-screening script", "v2.0",
         "What the coordinator reads when a respondent calls in.", False),
        ("letter", "Patient-facing study brochure", "v2.0",
         "Plain-language overview of the study, risks and time commitment.", False)],
       [(-40, "created", "Package created", *KARA, ""),
        (-38, "submitted", "Submitted to WCG IRB", *DANNY,
         "Initial recruitment package for review."),
        (-37, "in_review", "Under review", "WCG IRB", "Central IRB", ""),
        (-31, "revisions", "Revisions requested", "WCG IRB", "Central IRB",
         "Reviewer: remove the word 'free' and add the time commitment to the flyer."),
        (-29, "submitted", "Resubmitted with revisions", *KARA,
         "Flyer + brochure updated per the reviewer's comments."),
        (-58 + 30, "approved", "Approved (v2.0)", "WCG IRB", "Central IRB",
         "Stamped and cleared for use. Continuing review due before the expiry.")])

    # 2) Ubrogepant / migraine - UNDER REVIEW (a modification of existing materials).
    if len(ncts) > 1:
        n1 = ncts[1]
        mk(n1, "Social media ad set: modification", "Advarra IRB", "central",
           "modification", "in_review", PAUL[0], "v2.0", "Advarra #PRO000451",
           "", None, None,
           [("social", "TikTok / Reels short-form ad", "v1.1",
             "15-second video ad; adds a new channel to the approved set.", True),
            ("social", "Reddit r/migraine post", "v1.1",
             "Text post for the weekly recruitment thread.", False)],
           [(-9, "created", "Package created", *DANNY, ""),
            (-7, "submitted", "Submitted to Advarra", *DANNY,
             "Modification: adds two social placements to the approved ad set."),
            (-6, "in_review", "Under review", "Advarra IRB", "Central IRB",
             "Assigned to expedited review.")])

    # 3) MDD study - REVISIONS REQUESTED (the board sent modifications back).
    if len(ncts) > 2:
        n2 = ncts[2]
        mk(n2, "Continuing review: recruitment package", "WCG IRB", "central",
           "continuing", "revisions", PAUL[0], "v4.0", "WCG #20260233", "",
           None, None,
           [("flyer", "Updated waiting-room flyer", "v3.0",
             "Refreshed flyer for the annual continuing review.", False),
            ("script", "Phone pre-screening script", "v3.0",
             "Adds the new insomnia sub-study screen questions.", False)],
           [(-14, "created", "Package created", *KARA, ""),
            (-12, "submitted", "Submitted for continuing review", *MARG,
             "Annual continuing review of the recruitment materials."),
            (-11, "in_review", "Under review", "WCG IRB", "Central IRB", ""),
            (-4, "revisions", "Revisions requested", "WCG IRB", "Central IRB",
             "Soften the 'compensation' language and restore fair-balance of risks "
             "on the flyer, then resubmit.")])

    # 4) A DRAFT still being assembled - shows the starting state.
    if len(ncts) > 3:
        n3 = ncts[3]
        mk(n3, "Campus flyer: initial review", "Local / academic IRB", "local",
           "initial", "draft", PAUL[0], "v1.0", "", None, None, None,
           [("flyer", "NYU / Columbia community-board flyer", "v1.0",
             "Printed flyer for campus community boards - drafting.", False)],
           [(-2, "created", "Package created", *DANNY,
             "Assembling materials before submitting to the local board.")])


def seed_demo_lead_attribution(user_id):
    """Tie demo applicants that arrived from a paid channel (meta/google/reddit)
    back to the matching campaign, so the recruitment tracker shows real
    campaign attribution instead of '0 campaign-tracked'. Honest by construction:
    only leads whose source IS that channel get attributed. No-op once any lead
    is already attributed."""
    if not user_id:
        return
    db = get_db()
    if db.execute(
            "SELECT 1 FROM leads WHERE campaign_id IS NOT NULL LIMIT 1").fetchone():
        return
    camps = db.execute(
        "SELECT id, nct, channel FROM campaigns WHERE user_id = ?",
        (user_id,)).fetchall()
    if not camps:
        return
    by_channel = {}
    for c in camps:
        by_channel.setdefault((c["channel"] or "").lower(), []).append(c)
    paid = {"meta", "google", "reddit"}
    n = 0
    for l in db.execute("SELECT id, nct, source FROM leads").fetchall():
        ch = (l["source"] or "").strip().lower()
        if ch not in paid or ch not in by_channel:
            continue
        opts = by_channel[ch]
        cid = next((c["id"] for c in opts if c["nct"] == l["nct"]), opts[0]["id"])
        db.execute("UPDATE leads SET campaign_id = ? WHERE id = ?", (cid, l["id"]))
        n += 1
    if n:
        db.commit()


def seed_demo_leads():
    """Populate clearly-labelled DEMO candidates so the study-team review board
    shows a full end-to-end picture before any real applicants arrive.
    No-op if any leads already exist (so real data is never mixed with demo)."""
    con = sqlite3.connect(DB_PATH)
    try:
        if con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]:
            return
        base = dt.datetime.now()

        def at(days_ago):
            return (base - dt.timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M")

        # Spread applicants across the real acquisition channels a site actually
        # juggles: email inquiries about the trial, Instagram DMs and Facebook
        # Messenger chats off social ads, Meta lead-ad forms, plus CT.gov,
        # physician referrals and web forms. This is the whole point of the
        # inbox - every source landing in one triage list - so the demo has to
        # show that mix, not a single bucket.
        sources = ["email_intake", "instagram", "ctgov", "messenger", "referral",
                   "meta", "instagram", "email_intake", "facebook", "ctgov",
                   "web", "instagram", "email_intake", "google", "messenger",
                   "referral", "facebook", "ctgov"]

        for i, s in enumerate(_demo_lead_specs()):
            created = at(s["days"])
            source = s.get("source") or sources[i % len(sources)]
            decided = at(s.get("accepted_days") or s.get("declined_days") or 0) \
                if (s.get("accepted_days") or s.get("declined_days")) else ""
            rec = _demo_record(s["condition"], s["age"], s["sex"]) \
                if s.get("records") else ""
            cur = con.execute(
                """INSERT INTO leads
                   (token, site_token, site_token_expires_at, site_token_revoked,
                    applicant_token, nct, title, condition, location, site,
                    name, email, phone, age, sex, notes, consent, source, status,
                    records_connected, record_summary, screener, eligibility,
                    decision, decision_reason, decided_at, revealed,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (gen_token(), gen_token(), _site_token_expiry(), 0,
                 "demo-" + gen_token(), s["nct"], s["title"],
                 s["condition"], s["location"], s["site"], s["name"], s["email"],
                 s["phone"], s["age"], s["sex"], "", 1, source, s["status"],
                 s.get("records", 0), rec, json.dumps(s["screener"]),
                 json.dumps(s["elig"]), s.get("decision", ""),
                 s.get("reason", ""), decided, s.get("revealed", 0),
                 created, decided or created))
            lid = cur.lastrowid
            # Build a plausible event trail for the timeline.
            evs = [("submitted", "application received", "you", s["days"]),
                   ("prescreen", "ready for study-team review", "you", s["days"])]
            if s.get("accepted_days"):
                evs.append(("eligible", "accepted - likely eligible", "you",
                            s["accepted_days"]))
            if s.get("declined_days"):
                evs.append(("closed", "not a match: " + s.get("reason", ""),
                            "you", s["declined_days"]))
            if s.get("screening_days"):
                evs.append(("screening", "invited to screening visit", "site",
                            s["screening_days"]))
            if s.get("enrolled_days"):
                evs.append(("enrolled", "enrolled in study", "site",
                            s["enrolled_days"]))
            for status, note, actor, days in evs:
                con.execute(
                    "INSERT INTO lead_events (lead_id, status, note, actor, "
                    "created_at) VALUES (?,?,?,?,?)",
                    (lid, status, note, actor, at(days)))
        con.commit()
    finally:
        con.close()


# A varied pool of realistic applicant replies, mixed across triage intents
# (scheduling / question / document) so the inbox reads like a real one and the
# triage labels show genuine variety instead of a single repeated line. Indexed
# by lead id at seed time so no two adjacent threads are identical.
_DEMO_PATIENT_REPLIES = [
    "Can we schedule my screening for Tuesday or Thursday afternoon?",
    "Roughly how many visits are involved, and is there any compensation?",
    "I've attached my insurance card and the signed consent form.",
    "What should I bring on my first day, and how long will it take?",
    "Will taking part affect the care I get from my own doctor?",
    "Mornings are easier for me. Is next week open to book?",
    "Is travel to the site reimbursed?",
    "Thanks. I'll upload the completed forms you sent tonight.",
    "Is the study still enrolling? I'd like to move forward.",
    "I'm free Wednesday at 10am if that works to book a visit.",
]

# The old seed strings this pool replaces (used to de-clone existing demo DBs).
_DEMO_OLD_REPLIES = (
    "Thank you! Roughly how many visits are involved, and is parking available?",
    "Great, thanks. What should I bring to the first visit?",
    "Sounds good - mornings work best for me. Is Thursday possible?",
    "Appreciate it! Will taking part affect my regular care?",
)


def diversify_demo_replies():
    """Fix an already-seeded demo DB in place: the old seeder gave the first
    applicant in every study the same canned reply, so the inbox read as a wall
    of clones. Swap those known strings for the varied pool (keyed by lead id,
    deterministic) and clear triage so labels/priority recompute with the current
    classifier. Idempotent - once the old strings are gone it no-ops, and it only
    ever touches the known seed strings, never a real conversation."""
    db = get_db()
    rows = db.execute(
        "SELECT id, lead_id FROM messages WHERE sender = 'patient' AND body IN "
        "(?,?,?,?) ORDER BY id", _DEMO_OLD_REPLIES).fetchall()
    if not rows:
        return
    # Round-robin over the pool so the copy is spread evenly across the inbox
    # (keying by lead id clustered several threads onto the same line).
    for i, r in enumerate(rows):
        body = _DEMO_PATIENT_REPLIES[i % len(_DEMO_PATIENT_REPLIES)]
        db.execute("UPDATE messages SET body = ? WHERE id = ?", (body, r["id"]))
    # Re-triage everything so the labels reflect the new bodies + fixed rules.
    db.execute("UPDATE leads SET triage_intent = '', triage_priority = '', "
               "triage_at = ''")
    db.commit()


def seed_demo_engagement(clinician_id):
    """Populate the retention/engagement surfaces (messages, visits, one physician
    referral) on top of the demo leads so a fresh demo shows the whole loop alive.
    Idempotent: no-op once any message exists."""
    db = get_db()
    base = dt.datetime.now()

    def ts(days=0, hours=0):
        return (base + dt.timedelta(days=days, hours=hours)).strftime("%Y-%m-%d %H:%M")

    # Always ensure the demo study-team setup exists (claimed NCTs + profile),
    # even if message seeding already ran in a previous session.
    if clinician_id:
        study_rows = db.execute(
            "SELECT DISTINCT nct, title FROM leads WHERE nct != '' ORDER BY nct"
        ).fetchall()
        for s in study_rows[:4]:
            db.execute(
                "INSERT OR IGNORE INTO study_claims (user_id, nct, title, created_at) "
                "VALUES (?,?,?,?)",
                (clinician_id, s["nct"], s["title"] or "", ts(days=-10)))
        # Fill the org profile with the demo site's details. An earlier step
        # (set_site_calendar_url) may have created a blank profile row, so an
        # INSERT OR IGNORE would be skipped - upsert instead, but only when the
        # org name is still empty so we never clobber a real edited profile.
        _prof = db.execute(
            "SELECT org_name FROM site_profiles WHERE user_id = ?",
            (clinician_id,)).fetchone()
        if not _prof or not ((_prof["org_name"] or "").strip()):
            upsert_site_profile(
                clinician_id, "Northwind Clinical Research", "Elena Vargas",
                "info@northwindclinical.com", "+1 212 555 0148")

    # Ensure at least one visible booking link exists in demo so "calendar invite"
    # UX can be tested immediately on both study-team and patient surfaces.
    sched = db.execute(
        "SELECT id, schedule_url FROM leads WHERE status IN ('screening', 'enrolled') "
        "ORDER BY id LIMIT 1").fetchone()
    if sched and not (sched["schedule_url"] or "").strip():
        db.execute(
            "UPDATE leads SET schedule_url = ?, updated_at = ? WHERE id = ?",
            ("https://calendly.com/site/screening", ts(), sched["id"]))
        db.execute(
            "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
            "VALUES (?,?,?,?,?)",
            (sched["id"], "screening", "booking link shared with applicant", "site", ts()))
    if db.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]:
        db.commit()
        return

    # Threads on the accepted/revealed candidates so neither side looks empty.
    # Vary the conversation per person so the inbox reads like a real one: some
    # are awaiting our reply (unread), some are waiting on the patient.
    openers = [
        "Hi {first}, thanks for applying - you look like a strong fit. Any "
        "questions before we book your screening visit?",
        "Hi {first}, welcome! I'm your study coordinator. I'll share a couple of "
        "forms to complete before your first visit - happy to help with anything.",
        "Hi {first}, good news - you've cleared our initial review. When works for "
        "a quick screening call this week?",
        "Hi {first}, thanks for your interest. I've noted what happens next; let "
        "me know if anything is unclear.",
    ]
    replies = _DEMO_PATIENT_REPLIES
    follow = ("Absolutely - I'll send those details over now. Talk soon!")
    revealed = db.execute(
        "SELECT * FROM leads WHERE revealed = 1 ORDER BY id").fetchall()

    # A visit on the screening/enrolled candidates (drives reminders + patient
    # view). Seed this BEFORE the chat so the real conversation keeps the last
    # (highest-id) slot and drives the inbox preview / unread / awaiting cues.
    # Place on clean, spread working-day slots (never now-relative minutes) so the
    # schedule reads like a real clinic day, not a wall of identical timestamps.
    def at_hour(days, hour):
        return (base + dt.timedelta(days=days)).replace(
            hour=hour, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")

    _slot_hours = [9, 10, 11, 13, 14, 15, 16]
    _sched_days = [1, 2, 3, 4, 7]  # upcoming screening visits, spread out
    _si = 0
    for ld in revealed:
        if ld["status"] in ("screening", "enrolled"):
            if ld["status"] == "screening":
                when = at_hour(_sched_days[_si % len(_sched_days)],
                               _slot_hours[_si % len(_slot_hours)])
            else:
                when = at_hour(-2, _slot_hours[_si % len(_slot_hours)])
            _si += 1
            db.execute("INSERT INTO lead_visits (lead_id, kind, visit_at, location, "
                       "note, reminded_at, created_at) VALUES (?,?,?,?,?,?,?)",
                       (ld["id"], "screening", when, ld["site"] or "Study site",
                        "Please bring a photo ID. Allow about 90 minutes.",
                        "", ts(hours=-34)))
            db.execute("INSERT INTO messages (lead_id, sender, body, read_patient, "
                       "read_site, created_at) VALUES (?,?,?,?,?,?)",
                       (ld["id"], "system",
                        f"Your screening visit is booked for {when} at "
                        f"{ld['site'] or 'the study site'}. We'll remind you beforehand.",
                        1, 1, ts(hours=-34)))

    # Spread the conversation "kind" WITHIN each trial (not by global id) so every
    # trial inbox has a realistic mix: some awaiting our reply (unread), some
    # waiting on the patient, some just opened. Pin one patient per trial so the
    # Starred filter and gold stars have something to show.
    by_trial = {}
    for ld in revealed:
        by_trial.setdefault(ld["nct"], []).append(ld)
    ri = oi = 0  # running counters so copy spreads evenly, never per-trial clones
    for _nct, leads in by_trial.items():
        for idx, ld in enumerate(leads):
            first = ld["name"].split()[0] if ld["name"] else "there"
            kind = idx % 3
            # Running counters (not per-trial idx) so the first applicant in
            # every study doesn't get the identical line - that cloned wall was
            # the thing that read as fake.
            db.execute("INSERT INTO messages (lead_id, sender, body, read_patient, "
                       "read_site, created_at) VALUES (?,?,?,?,?,?)",
                       (ld["id"], "site",
                        openers[oi % len(openers)].format(first=first),
                        1, 1, ts(hours=-30)))
            oi += 1
            if kind != 1:  # patient wrote back
                db.execute("INSERT INTO messages (lead_id, sender, body, read_patient, "
                           "read_site, created_at) VALUES (?,?,?,?,?,?)",
                           (ld["id"], "patient", replies[ri % len(replies)],
                            1, 0 if kind == 0 else 1, ts(hours=-26)))
                ri += 1
            if kind == 2:  # we already replied -> waiting on the patient
                db.execute("INSERT INTO messages (lead_id, sender, body, read_patient, "
                           "read_site, created_at) VALUES (?,?,?,?,?,?)",
                           (ld["id"], "site", follow, 1, 1, ts(hours=-24)))
            if idx == 1:  # pin the second person in each trial
                db.execute("UPDATE leads SET conv_tags = 'pinned' WHERE id = ?",
                           (ld["id"],))

    # At least one source-verified enrollment for revenue/reconciliation demos.
    enrolled = db.execute(
        "SELECT id FROM leads WHERE status = 'enrolled' ORDER BY id LIMIT 1"
    ).fetchone()
    if enrolled:
        db.execute(
            "INSERT INTO lead_reconciliations (lead_id, outcome, source_system, "
            "source_ref, note, actor, created_at) VALUES (?,?,?,?,?,?,?)",
            (enrolled["id"], "enrolled_verified", "REDCap", "record-001",
             "Verified by coordinator after baseline visit", "site", ts(days=-1)))

    # One physician referral with attribution, so the invite view + funnel show the
    # trusted-messenger channel producing a real applicant.
    if clinician_id:
        tok = gen_token()
        db.execute(
            "INSERT INTO invites (token, clinician_id, clinician_name, nct, title, "
            "condition, note, clicks, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (tok, clinician_id, "Referring Clinician", "NCT07076407",
             "Azetukalner vs Placebo in Major Depressive Disorder (X-NOVA3)",
             "Major Depressive Disorder",
             "I think this could be a good fit for you - worth a look.", 3,
             ts(days=-6)))
        target = db.execute(
            "SELECT id FROM leads WHERE nct = 'NCT07076407' AND status = 'eligible' "
            "ORDER BY id LIMIT 1").fetchone()
        if target:
            db.execute("UPDATE leads SET referred_by = 'Referring Clinician', "
                       "invite_token = ?, source = 'referral' WHERE id = ?",
                       (tok, target["id"]))
    db.commit()


def seed_demo_team(clinician_id):
    """Seed the shared workspace's Team with a fictional demo roster so it
    reads like a real site team is already set up. Idempotent by email, and it
    points each teammate at the shared org so signing in lands them here too.
    Demo-only (see COMPLIANCE.md); turn SITE_DEMO off before onboarding real
    sites."""
    if not clinician_id:
        return
    migrate_demo_staff_identities()
    db = get_db()
    oid = user_org_id(clinician_id)
    # Name the shared workspace after the site.
    db.execute("UPDATE organizations SET name = ? WHERE id = ?",
               ("Northwind Clinical Research", oid))
    # Fictional demo staff. Base role gates permissions:
    # pi = signs/approves docs; coordinator = admin (manages team + approves);
    # student = full day-to-day visibility, no sign-off/team management (fits the
    # CRCs). role_label carries their title for display.
    for email, name, role, label, _old in _DEMO_TEAM:
        email = email.lower().strip()
        row = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            uid = row["id"]
            db.execute("UPDATE users SET name = ? WHERE id = ?", (name, uid))
        else:
            cur = db.execute(
                "INSERT INTO users (email, password_hash, name, verified, "
                "verified_at, created_at) VALUES (?,?,?,?,?,?)",
                (email, "", name, 1, now(), now()))
            uid = cur.lastrowid
        db.execute("UPDATE users SET org_id = ? WHERE id = ?", (oid, uid))
        db.execute("INSERT OR IGNORE INTO memberships (org_id, user_id, role, "
                   "role_label, created_at) VALUES (?,?,?,?,?)",
                   (oid, uid, role, label, now()))
        db.execute("UPDATE memberships SET role = ?, role_label = ? "
                   "WHERE org_id = ? AND user_id = ?", (role, label, oid, uid))
    db.commit()


def seed_demo_case_notes(clinician_id=None):
    """Seed internal team notes on the first few candidates, attributed across the
    site's fictional staff (CRCs, PI, Director of Ops) so each candidate chart reads
    like the whole team is working the case together - not one anonymous
    "Coordinator". Runs unconditionally (unlike the calendar seeder, which bails
    once future visits exist), so this collaboration surface is never empty.
    Idempotent per-lead. Demo-only (see COMPLIANCE.md)."""
    db = get_db()
    base = dt.datetime.now()

    def ts(days=0, hours=0):
        return (base + dt.timedelta(days=days, hours=hours)).strftime("%Y-%m-%d %H:%M")

    # A small rotation of realistic, role-appropriate notes. Each candidate gets a
    # short multi-author thread: logistics from a CRC, a clinical note from the
    # PI, and an ops/compliance reminder - so the chart shows people collaborating.
    threads = [
        [("Avery Kim, MPH", "Prefers morning visits - works afternoons. Booked "
          "screening for Thu 10:30 and sent visit-prep."),
         ("David Chen, MD", "Reviewed pre-screen - looks eligible. Confirm washout "
          "on the SSRI before baseline; I'll sign consent at the visit."),
         ("Riley Patel", "Daughter (caregiver) usually attends - added her to the "
          "reminder list and shared parking info.")],
        [("Riley Patel", "Reached out twice; prefers text. Confirmed for Friday "
          "and sent the e-diary link."),
         ("Avery Kim, MPH", "Mild nausea reported week 1, resolved on its own. "
          "Flagged to monitor at next dose."),
         ("Priya Shah, MD", "Source is current through last visit. Keep "
          "only IRB-approved materials in the patient thread, please.")],
        [("Avery Kim, MPH", "Insurance card on file, transport not needed - lives "
          "10 min from site."),
         ("David Chen, MD", "No exclusionary meds. Cleared to proceed to baseline."),
         ("Riley Patel", "Rebooked the missed follow-up; confirmed adherence back "
          "on track.")],
    ]

    revealed = db.execute(
        "SELECT id FROM leads WHERE revealed = 1 AND name IS NOT NULL "
        "AND name != '' ORDER BY id LIMIT 3").fetchall()
    for idx, ld in enumerate(revealed):
        if list_notes(ld["id"]):
            continue  # keep idempotent; don't stack duplicate notes
        thread = threads[idx % len(threads)]
        # Oldest note first so the chart reads top-to-bottom chronologically.
        for j, (author, body) in enumerate(thread):
            db.execute(
                "INSERT INTO lead_notes (lead_id, body, author, created_at) "
                "VALUES (?,?,?,?)",
                (ld["id"], body, author, ts(days=-6, hours=j * 5)))
    db.commit()


def seed_demo_collaboration(clinician_id=None):
    """Seed the collaboration layer (per-candidate to-do checklists + a starter
    internal team channel per trial) so a fresh demo shows conversations working.
    Idempotent per-surface."""
    db = get_db()
    base = dt.datetime.now()

    def ts(days=0, hours=0):
        return (base + dt.timedelta(days=days, hours=hours)).strftime("%Y-%m-%d %H:%M")

    # Per-candidate checklists on the first couple of accepted candidates.
    if not db.execute("SELECT COUNT(*) n FROM lead_tasks").fetchone()["n"]:
        revealed = db.execute(
            "SELECT id FROM leads WHERE revealed = 1 ORDER BY id LIMIT 3").fetchall()
        seed_tasks = [
            ("Sign the consent form we shared", "patient", "done"),
            ("Upload a photo of your insurance card", "patient", "open"),
            ("Confirm you can travel to the study site", "patient", "open"),
        ]
        for ld in revealed:
            for title, who, status in seed_tasks:
                db.execute(
                    "INSERT INTO lead_tasks (lead_id, title, assigned_to, status, "
                    "created_by, created_at, done_at) VALUES (?,?,?,?,?,?,?)",
                    (ld["id"], title, who, status, "site", ts(hours=-24),
                     ts(hours=-6) if status == "done" else ""))

    # Internal (staff-only) team channel per claimed trial. Seed a realistic
    # multi-person thread attributed to the fictional demo staff so the channel
    # reads like the team actually working the study together - recruiter triage,
    # CRC scheduling, Director-of-Ops monitoring/compliance, and PI sign-off.
    if clinician_id:
        claims = db.execute(
            "SELECT nct, title FROM study_claims WHERE user_id = ?",
            (clinician_id,)).fetchall()
        # Map the seeded staff to (user_id, chat display name) by email so each
        # message links to the matching member. Falls back to a name string if a
        # teammate isn't present.
        oid = user_org_id(clinician_id)
        who = {}
        for email, disp in (
            ("evargas@northwindclinical.com", "Elena Vargas"),
            ("rpatel@northwindclinical.com", "Riley Patel"),
            ("akim@northwindclinical.com", "Avery Kim"),
            ("pshah@northwindclinical.com", "Priya Shah"),
            ("dchen@northwindclinical.com", "Dr. Chen"),
        ):
            r = db.execute(
                "SELECT u.id FROM users u JOIN memberships m ON m.user_id = u.id "
                "WHERE m.org_id = ? AND u.email = ?", (oid, email)).fetchone()
            who[disp] = (r["id"] if r else None, disp)

        def _rater(title):
            t = (title or "").lower()
            if "migraine" in t:
                return "the e-diary / attack-frequency check"
            if "treatment-resistant" in t or "trd" in t:
                return "the MGH-ATRQ treatment-history review"
            return "the MADRS + C-SSRS ratings"

        for c in claims:
            has = db.execute(
                "SELECT COUNT(*) n FROM team_messages WHERE nct = ?",
                (c["nct"],)).fetchone()["n"]
            if has:
                continue
            rater = _rater(c["title"])
            thread = [
                ("Elena Vargas",
                 "Kicking this cohort off. Sponsor wants steady screening this "
                 "month - let's keep screen-fail tight and source current.",
                 -30),
                ("Riley Patel",
                 "Three new pre-screens came in overnight. Two look strong - moved "
                 "them to review. Third has a washout question I flagged.", -27),
                ("Avery Kim",
                 f"Booked the two strong ones for screening Thu-Fri. Sent visit-prep "
                 f"+ consent and confirmed {rater} is set up.", -24),
                ("Priya Shah",
                 "Monitor visit next Wed - please have source current by Tue EOD. "
                 "Reminder: only IRB-approved materials in patient threads; drafts "
                 "stay here.", -22),
                ("Dr. Chen",
                 "Reviewed the two flagged charts - both eligible, cleared to "
                 "screen. I'll sign consent at the visit.", -6),
                ("Avery Kim",
                 "Thanks Dr. Chen - updating their status and prepping the rooms.",
                 -5),
            ]
            for disp, body, hrs in thread:
                uid, nm = who.get(disp, (None, disp))
                db.execute(
                    "INSERT INTO team_messages (nct, sender_user_id, sender_name, "
                    "body, created_at) VALUES (?,?,?,?,?)",
                    (c["nct"], uid, nm, body, ts(hours=hrs)))
    db.commit()


def ensure_demo_claim_volume(user_id, minimum_rows=18):
    """Ensure demo study-team queues have enough rows for ATS walkthroughs.

    Existing projects can already contain older/smaller demo datasets; this tops
    up only the claimed studies for the current user so the board feels realistic.
    """
    try:
        minimum_rows = max(0, int(minimum_rows))
    except Exception:
        minimum_rows = 18
    if minimum_rows <= 0:
        return 0
    claims = list_study_claims(user_id)
    ncts = [c["nct"] for c in claims if c["nct"]]
    if not ncts:
        return 0
    db = get_db()
    qs = ",".join("?" * len(ncts))
    row = db.execute(
        f"SELECT COUNT(*) n FROM leads WHERE nct IN ({qs})", ncts).fetchone()
    have = int((row["n"] if row else 0) or 0)
    if have >= minimum_rows:
        return 0
    need = minimum_rows - have
    title_for = {c["nct"]: (c["title"] or f"Claimed study {c['nct']}") for c in claims}
    # Pull each study's real context (condition/location/site) from an existing lead
    # so filler applicants match the actual trial instead of a generic placeholder.
    meta_for = {}
    for _nct in ncts:
        r = db.execute(
            "SELECT condition, location, site FROM leads WHERE nct = ? AND "
            "TRIM(COALESCE(condition,'')) != '' ORDER BY id LIMIT 1", (_nct,)).fetchone()
        meta_for[_nct] = {
            "condition": (r["condition"] if r else "") or "Major Depressive Disorder",
            "location": (r["location"] if r else "") or "New York, NY",
            "site": (r["site"] if r else "") or "Northwind Clinical Research",
        }
    # Real-sounding identities (first + last pools). Applicants are de-identified on
    # the board until revealed, but real names make the ones you accept read right.
    _firsts = ["Aiden", "Amara", "Andre", "Bianca", "Caleb", "Carmen", "Damon",
               "Elena", "Felix", "Grace", "Hugo", "Imani", "Jonah", "Kayla",
               "Liam", "Maya", "Nadia", "Omar", "Priya", "Quinn", "Rosa", "Sean",
               "Tara", "Uma", "Victor", "Wendy", "Xavier", "Yara", "Zane", "Cole",
               "Nina", "Reza", "Tessa", "Devon", "Gina", "Hassan"]
    _lasts = ["Alvarez", "Bennett", "Chen", "Diaz", "Ellis", "Foster", "Gomez",
              "Harris", "Ibrahim", "Jensen", "Khan", "Lopez", "Mensah", "Novak",
              "Owens", "Patel", "Quinn", "Reyes", "Silva", "Tran", "Ueda",
              "Vasquez", "Walsh", "Yousef", "Zimmer", "Brooks", "Nguyen", "Park",
              "Cohen", "Adams", "Ford", "Rivera", "Hale", "Ortiz"]

    def _elig_for(cond, status, idx):
        c = (cond or "").lower()
        if "migraine" in c:
            met = ["Over 1-year migraine history (IHS criteria)", "Age within 18-75"]
            unknown = ["Confirm 2-10 moderate/severe attacks per month"]
            excl = "Currently in another interventional trial (washout needed)"
        elif "insomnia" in c:
            met = ["DSM-5 MDD with insomnia symptoms", "On a stable antidepressant"]
            unknown = ["Confirm clinically significant insomnia (ISI)"]
            excl = "Untreated obstructive sleep apnea on record"
        elif "resistant" in c or "trd" in c:
            met = ["MDD without psychotic features",
                   "Two prior antidepressant failures on record"]
            unknown = ["Confirm adequate trials via MGH-ATRQ"]
            excl = "History of psychosis - protocol exclusion"
        else:
            met = ["Meets DSM-5-TR criteria for current MDD", "Age within 18-74"]
            unknown = ["MADRS severity to confirm at screening"]
            excl = "History of bipolar disorder - protocol exclusion"
        # Vary the read so the board shows a real spread (strong fits, maybes, and a
        # few clear no's) instead of every applicant reading the same.
        if status == "closed":
            return {"verdict": "unlikely", "met": met[:1], "unknown": [],
                    "not_met": [excl],
                    "rationale": "Not eligible - " + excl.lower() + "."}
        strong = {"verdict": "likely_eligible", "met": met, "unknown": [],
                  "not_met": [],
                  "rationale": "Meets the core inclusion criteria on record."}
        possible = {"verdict": "possible", "met": met, "unknown": unknown,
                    "not_met": [],
                    "rationale": "Meets core inclusion criteria; a few items to "
                                 "confirm at the screening visit."}
        # Accepted applicants should never read as a clear no; only the awaiting-
        # review pool carries the borderline/unlikely reads.
        if status in ("eligible", "screening", "enrolled"):
            return strong if (idx % 2 == 0) else possible
        bucket = idx % 5
        if bucket in (0, 1):
            return strong
        if bucket == 4:
            return {"verdict": "unlikely", "met": met[:1], "unknown": unknown,
                    "not_met": [excl],
                    "rationale": "Possible exclusion flagged - confirm before review."}
        return possible
    # Weighted funnel: most applicants still await review, fewer reach enrolled - a
    # realistic recruitment shape rather than an even split across stages.
    statuses = ["prescreen", "prescreen", "prescreen", "prescreen", "eligible",
                "eligible", "screening", "enrolled", "closed", "prescreen"]
    ts = now()
    added = 0
    for i in range(need):
        nct = ncts[i % len(ncts)]
        status = statuses[i % len(statuses)]
        idx = have + i + 1
        token = gen_token()
        site_token = gen_token()
        decision = ""
        decision_reason = ""
        revealed = 0
        if status in ("eligible", "screening", "enrolled"):
            decision = "accepted"
            revealed = 1
        elif status == "closed":
            decision = "declined"
            decision_reason = "Protocol mismatch after coordinator review"
        meta = meta_for.get(nct, {})
        cond = meta.get("condition") or "Major Depressive Disorder"
        city = meta.get("location") or "New York, NY"
        site_name = meta.get("site") or "Northwind Clinical Research"
        sex = "female" if idx % 2 else "male"
        # Sex-restricted studies (e.g. menstrual migraine) only enroll women.
        if "menstrual" in (title_for.get(nct, "") or "").lower():
            sex = "female"
        age = str(24 + (idx * 7) % 50)
        first = _firsts[(idx * 5) % len(_firsts)]
        last = _lasts[(idx * 3) % len(_lasts)]
        full = f"{first} {last[0]}."
        records_connected = 1 if (idx % 2 == 0) else 0
        elig = _elig_for(cond, status, idx)
        rec = _demo_record(cond, age, sex) if records_connected else ""
        vol_source = ["ctgov", "web", "referral", "google", "web", "meta",
                      "ctgov", "reddit", "referral", "web"][idx % 10]
        # Spread creation across the past ~6 weeks so "last activity" reads naturally
        # instead of every filler applicant landing at the same instant.
        created = (dt.datetime.now()
                   - dt.timedelta(days=(idx * 3) % 42, hours=(idx * 5) % 12)
                   ).strftime("%Y-%m-%d %H:%M")
        db.execute(
            """INSERT INTO leads
               (token, site_token, site_token_expires_at, site_token_revoked,
                applicant_token, nct, title, condition, location, site, name,
                email, phone, age, sex, notes, consent, source, status, screener,
                eligibility, records_connected, record_summary, decision,
                decision_reason, decided_at, revealed, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (token, site_token, _site_token_expiry(), 0,
             f"seeded-volume-{idx}", nct, title_for.get(nct, nct), cond, city,
             site_name, full,
             f"{first.lower()}.{last.lower()}{idx}@example.com",
             f"+1 212 555 {2000 + idx:04d}", age, sex, "", 1, vol_source, status,
             json.dumps({"travel": "yes", "other_trial": "no",
                         "pregnancy": "no", "consent_capable": "yes"}),
             json.dumps(elig), records_connected, rec, decision,
             decision_reason, created if decision else "", revealed, created, created))
        lid = db.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        db.execute(
            "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
            "VALUES (?,?,?,?,?)", (lid, "submitted", "application received", "you", created))
        db.execute(
            "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
            "VALUES (?,?,?,?,?)", (lid, "prescreen", "ready for study-team review", "you", created))
        if decision == "accepted":
            db.execute(
                "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
                "VALUES (?,?,?,?,?)",
                (lid, "eligible", "accepted - likely eligible", "you", created))
        if status == "screening":
            db.execute(
                "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
                "VALUES (?,?,?,?,?)",
                (lid, "screening", "invited to screening visit", "site", created))
        if status == "enrolled":
            db.execute(
                "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
                "VALUES (?,?,?,?,?)",
                (lid, "enrolled", "enrolled in study", "site", created))
        if status == "closed":
            db.execute(
                "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
                "VALUES (?,?,?,?,?)",
                (lid, "closed", decision_reason, "you", created))
        added += 1
    if added:
        db.commit()
    return added


def seed_demo_patient_apps(applicant_token):
    """Seed realistic patient-facing applications for demo mode.

    Idempotent for the given applicant token.
    """
    applicant_token = (applicant_token or "").strip()
    if not applicant_token:
        return
    db = get_db()
    has_any = db.execute(
        "SELECT COUNT(*) n FROM leads WHERE applicant_token = ?",
        (applicant_token,)).fetchone()
    if has_any and has_any["n"]:
        return
    ts = now()
    # Clean upcoming clinic slot for the seeded screening visit (never now-relative
    # minutes, which read as a fake wall of identical odd times on the schedule).
    visit_when = (dt.datetime.now() + dt.timedelta(days=2)).replace(
        hour=10, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
    demo_specs = [
        {
            "nct": "NCT07645924",
            "title": "Elismetrep (K-304) for the Acute Treatment of Migraine",
            "condition": "Migraine",
            "location": "New York, NY",
            "site": "Northwind Clinical Research",
            "status": "prescreen",
            "decision": "",
            "revealed": 0,
            "schedule_url": "",
        },
        {
            "nct": "NCT06559306",
            "title": "Adjunctive Seltorexant in MDD With Insomnia Symptoms",
            "condition": "Major Depressive Disorder",
            "location": "New York, NY",
            "site": "Northwind Clinical Research",
            "status": "screening",
            "decision": "accepted",
            "revealed": 1,
            "schedule_url": "https://calendly.com/site/screening",
        },
    ]
    for s in demo_specs:
        cur = db.execute(
            """INSERT INTO leads
               (token, site_token, site_token_expires_at, site_token_revoked,
                applicant_token, nct, title, condition, location, site, name,
                email, phone, age, sex, notes, consent, source, status,
                screener, eligibility, records_connected, record_summary, decision,
                decision_reason, decided_at, revealed, schedule_url, created_at,
                updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (gen_token(), gen_token(), _site_token_expiry(), 0, applicant_token,
             s["nct"], s["title"], s["condition"], s["location"], s["site"],
             "Jordan Blake", "jordan.blake@bridgemd.local", "+1 416 555 0110",
             "31", "female", "", 1, "web", s["status"],
             json.dumps({"travel": "yes", "other_trial": "no",
                         "pregnancy": "na", "consent_capable": "yes"}),
             json.dumps({
                 "verdict": "possible",
                 "met": ["Age within range", "Condition appears aligned"],
                 "unknown": ["Labs to confirm"],
                 "not_met": [],
                 "rationale": "Good initial fit pending site review."
             }),
             1, _demo_record(s["condition"], "31", "female"), s["decision"], "",
             ts if s["decision"] else "", s["revealed"], s["schedule_url"], ts, ts))
        lead_id = cur.lastrowid
        db.execute(
            "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
            "VALUES (?,?,?,?,?)",
            (lead_id, "submitted", "application received", "you", ts))
        db.execute(
            "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
            "VALUES (?,?,?,?,?)",
            (lead_id, "prescreen", "ready for study-team review", "you", ts))
        if s["status"] in ("screening", "enrolled"):
            db.execute(
                "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
                "VALUES (?,?,?,?,?)",
                (lead_id, "eligible", "accepted - likely eligible", "you", ts))
            db.execute(
                "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
                "VALUES (?,?,?,?,?)",
                (lead_id, "screening", "screening visit invited", "site", ts))
            db.execute(
                "INSERT INTO messages (lead_id, sender, body, read_patient, "
                "read_site, created_at) VALUES (?,?,?,?,?,?)",
                (lead_id, "site",
                 "Great news - you look like a fit. Please book your screening call.",
                 0, 1, ts))
            db.execute(
                "INSERT INTO lead_visits (lead_id, kind, visit_at, location, note, "
                "reminded_at, created_at) VALUES (?,?,?,?,?,?,?)",
                (lead_id, "screening", visit_when, s["site"],
                 "Bring a photo ID and medication list.", "", ts))
    db.commit()


def cleanup_demo_threads():
    """Demo hygiene: keep seeded message threads reading like a real inbox.

    A persistent demo DB accumulates cruft over calendar time: the background
    reminder sweep stacks repeated quiet-applicant nudges (a wall of identical
    "still active" check-ins) and re-seeders can re-append the same patient
    reply. This strips that machine-generated repetition so the demo shows how
    the product is actually used. Only touches system-generated repeats and
    exact duplicates - never a genuine back-and-forth. Safe to call repeatedly.
    """
    db = get_db()
    # 1) Drop the automated quiet-applicant nudges. The curated seed already
    #    shows the engagement loop; the live sweep just repeats copy.
    db.execute(
        "DELETE FROM messages WHERE sender = 'system' AND ("
        "body LIKE '%application is still active%' OR "
        "body LIKE '%last note got buried%' OR "
        "body LIKE '%your spot is still open%')")
    # 2) Collapse exact-duplicate messages within a thread (same sender+body),
    #    keeping the earliest - removes re-seeded patient replies / repeats.
    db.execute(
        "DELETE FROM messages WHERE id NOT IN ("
        "SELECT MIN(id) FROM messages GROUP BY lead_id, sender, body)")
    # 3) Keep only the most recent visit reminder per thread. Their dates differ
    #    so they aren't exact dupes, but a stack of them reads like spam.
    db.execute(
        "DELETE FROM messages WHERE sender = 'system' "
        "AND body LIKE 'Reminder: your%visit is on%' "
        "AND id NOT IN ("
        "SELECT MAX(id) FROM messages WHERE sender = 'system' "
        "AND body LIKE 'Reminder: your%visit is on%' GROUP BY lead_id)")
    db.commit()


def seed_demo_referrals(clinician_id):
    """Seed clinician-facing referral history for demo mode (idempotent)."""
    if not clinician_id:
        return
    db = get_db()
    has_any = db.execute(
        "SELECT COUNT(*) n FROM referrals WHERE user_id = ?",
        (clinician_id,)).fetchone()
    if has_any and has_any["n"]:
        return
    ts = now()
    rows = [
        ("NCT05869903", "Once-Weekly Semaglutide in Adults With Obesity",
         "Patient A", "Obesity", "Toronto site", "coordinator@site.example",
         "contacted"),
        ("NCT06034262", "Tirzepatide vs Placebo for Type 2 Diabetes and Weight",
         "Patient B", "Type 2 diabetes", "Mississauga site",
         "coordinator@site.example", "enrolled"),
    ]
    for nct, title, label, condition, site, coord_email, status in rows:
        token = gen_token()
        db.execute(
            """INSERT INTO referrals
               (user_id, token, nct, title, patient_label, patient_summary, condition,
                country, site, coordinator, coordinator_email, patient_name,
                patient_contact, consent, consent_at, verdict, score, rationale,
                status, created_at, updated_at, notified_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (clinician_id, token, nct, title, label,
             "De-identified summary on file.", condition, "CA", site,
             "Site Coordinator", coord_email, "", "", 1, ts, "possible", 78,
             "Good fit for follow-up.", status, ts, ts, ts))
        ref_id = db.execute(
            "SELECT id FROM referrals WHERE token = ?", (token,)).fetchone()["id"]
        db.execute(
            "INSERT INTO referral_events (referral_id, status, note, actor, created_at) "
            "VALUES (?,?,?,?,?)",
            (ref_id, "referred", "referral created", "you", ts))
        db.execute(
            "INSERT INTO referral_events (referral_id, status, note, actor, created_at) "
            "VALUES (?,?,?,?,?)",
            (ref_id, status, "seeded progression", "site", ts))
    db.commit()


# --------------------------------------------------------------------------- #
# Search traffic - powers the live "trending" chips on the landing page.
# --------------------------------------------------------------------------- #
def log_search_term(term, kind):
    """Record one search/visit for a term so trending reflects real traffic.
    kind is 'condition' or 'drug'. Safe to call on every request (best-effort)."""
    term = (term or "").strip()
    if not term or kind not in ("condition", "drug"):
        return
    key = kind + ":" + " ".join(term.lower().split())
    db = get_db()
    ts = now()
    db.execute(
        "INSERT INTO search_stats (term_key, term, kind, hits, last_at) "
        "VALUES (?,?,?,1,?) "
        "ON CONFLICT(term_key) DO UPDATE SET hits = hits + 1, "
        "term = excluded.term, last_at = excluded.last_at",
        (key, term, kind, ts))
    db.commit()


def top_terms(kind, limit=8):
    """Most-searched terms for a kind, busiest first."""
    rows = get_db().execute(
        "SELECT term FROM search_stats WHERE kind = ? "
        "ORDER BY hits DESC, last_at DESC LIMIT ?", (kind, limit)).fetchall()
    return [r["term"] for r in rows]


# --------------------------------------------------------------------------- #
# Web analytics - privacy-safe on-site behavior + traffic attribution.
# --------------------------------------------------------------------------- #
_WEB_FUNNEL = ("visit", "search", "trial_view", "apply")
_WEB_FUNNEL_LABELS = {
    "visit": "Visited site",
    "search": "Ran a search",
    "trial_view": "Viewed a trial",
    "apply": "Applied",
}


def log_web_event(name, visitor="", path="", source="", medium="",
                  campaign="", referrer="", detail=None, ua="",
                  city="", region="", country=""):
    """Record one on-site action. Best-effort; never raises. `detail` is a small
    dict of non-identifying facts (e.g. term, result count, nct). `ua` is the
    browser user-agent, stored only for bot auditing. `city`/`region`/`country`
    are coarse geo (analytics only) - the raw IP is never stored."""
    name = (name or "").strip()
    if not name:
        return
    try:
        payload = json.dumps(detail, separators=(",", ":")) if detail else ""
    except (TypeError, ValueError):
        payload = ""
    try:
        d = get_db()
        d.execute(
            "INSERT INTO web_events (ts, visitor, name, path, source, medium, "
            "campaign, referrer, detail, ua, city, region, country) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now(), (visitor or "")[:64], name[:40], (path or "")[:200],
             (source or "")[:80], (medium or "")[:40], (campaign or "")[:80],
             (referrer or "")[:200], payload[:500], (ua or "")[:200],
             (city or "")[:80], (region or "")[:80], (country or "")[:80]))
        d.commit()
    except Exception:
        pass


def _event_detail(raw):
    try:
        v = json.loads(raw or "")
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def found_via_label(source="", referrer=""):
    """Friendly name for the engine or site that sent someone here.
    Google does not pass the search phrase; empty source is a direct open."""
    raw = (source or referrer or "").strip().lower()
    host = raw.split("/")[0]
    if host.startswith("www."):
        host = host[4:]
    if "duckduckgo" in host:
        return "DuckDuckGo"
    if host in ("bing.com", "bing") or host.endswith(".bing.com"):
        return "Bing"
    if "google" in host:
        return "Google"
    if host:
        return host
    return "Direct"


def live_apply_index():
    """Browser apply events keyed by (NCT, timestamp-to-the-minute).

    Seeded leads never write a web_events apply row, so this is the live set.
    Each value includes where they arrived from and what they typed on /find.
    """
    d = get_db()
    applies = d.execute(
        "SELECT ts, visitor, source, medium, campaign, referrer, city, detail "
        "FROM web_events WHERE name = 'apply'").fetchall()
    visitors = {a["visitor"] for a in applies if a["visitor"]}
    searches = {}
    if visitors:
        marks = ",".join("?" * len(visitors))
        for row in d.execute(
            f"SELECT visitor, detail FROM web_events WHERE name = 'search' "
            f"AND visitor IN ({marks}) ORDER BY ts", tuple(visitors)):
            q = (_event_detail(row["detail"]).get("q") or "").strip()
            if q and row["visitor"] not in searches:
                searches[row["visitor"]] = q
    out = {}
    for a in applies:
        nct = (_event_detail(a["detail"]).get("nct") or "").strip().upper()
        ts = (a["ts"] or "")[:16]
        if not nct or not ts:
            continue
        out[(nct, ts)] = {
            "found_via": found_via_label(a["source"], a["referrer"]),
            "search_q": searches.get(a["visitor"] or "", ""),
            "city": a["city"] or "",
            "source": a["source"] or "",
            "referrer": a["referrer"] or "",
        }
    return out


def apply_attribution_for(nct, created_at):
    """Attribution for one lead, or empty if it was not a live browser apply."""
    key = ((nct or "").strip().upper(), (created_at or "")[:16])
    if not key[0] or not key[1]:
        return {}
    return live_apply_index().get(key) or {}


def _web_since(days):
    return (dt.datetime.now() - dt.timedelta(days=max(1, days))).strftime(
        "%Y-%m-%d %H:%M")


def web_funnel_stats(days=30, limit_terms=10, limit_sources=10):
    """Traffic + on-site funnel + attribution over the last `days`, all computed
    from web_events. Returns counts, unique-visitor funnel, source breakdown, and
    top searches. Safe on an empty table."""
    d = get_db()
    since = _web_since(days)

    # Raw + unique-visitor counts per funnel stage.
    funnel = []
    for stage in _WEB_FUNNEL:
        row = d.execute(
            "SELECT COUNT(*) c, COUNT(DISTINCT visitor) u FROM web_events "
            "WHERE name = ? AND ts >= ?", (stage, since)).fetchone()
        funnel.append({
            "key": stage, "label": _WEB_FUNNEL_LABELS.get(stage, stage),
            "unique": (row["u"] or 0), "count": (row["c"] or 0),
            "conv_from_prev": 100.0,
        })
    # Conversion from the previous stage (first stage anchors at 100%).
    for i in range(1, len(funnel)):
        base = funnel[i - 1]["unique"]
        funnel[i]["conv_from_prev"] = round(
            funnel[i]["unique"] / base * 100.0, 0) if base else 0.0
    top_u = funnel[0]["unique"] if funnel else 0

    # Biggest on-site leak.
    dropoff = None
    for i in range(1, len(funnel)):
        base = funnel[i - 1]["unique"]
        if base >= 1:
            lost = base - funnel[i]["unique"]
            pct = lost / base * 100.0
            if dropoff is None or pct > dropoff["pct"]:
                dropoff = {"from": funnel[i - 1]["label"], "to": funnel[i]["label"],
                           "pct": round(pct, 0), "lost": lost}

    # Where clicks come from: attribution on the 'visit' event.
    src_rows = d.execute(
        "SELECT CASE WHEN source != '' THEN source "
        "WHEN referrer != '' THEN referrer ELSE 'direct' END AS src, "
        "COUNT(DISTINCT visitor) u, COUNT(*) c FROM web_events "
        "WHERE name = 'visit' AND ts >= ? GROUP BY src "
        "ORDER BY u DESC, c DESC LIMIT ?", (since, limit_sources)).fetchall()
    # Applies attributed by source (join visitor's first source is complex in raw
    # SQL; approximate by the source stamped on the apply event itself).
    apply_by_src = {}
    for r in d.execute(
        "SELECT CASE WHEN source != '' THEN source "
        "WHEN referrer != '' THEN referrer ELSE 'direct' END AS src, "
        "COUNT(DISTINCT visitor) u FROM web_events "
        "WHERE name = 'apply' AND ts >= ? GROUP BY src", (since,)).fetchall():
        apply_by_src[r["src"]] = r["u"]
    sources = [{
        "source": r["src"], "visitors": r["u"], "hits": r["c"],
        "applies": apply_by_src.get(r["src"], 0),
    } for r in src_rows]

    # Top searches (term + how many results they saw on average).
    terms = {}
    for r in d.execute(
        "SELECT detail FROM web_events WHERE name = 'search' AND ts >= ? "
        "AND detail != ''", (since,)).fetchall():
        try:
            det = json.loads(r["detail"])
        except (TypeError, ValueError):
            continue
        term = (det.get("q") or "").strip().lower()
        if not term:
            continue
        t = terms.setdefault(term, {"term": term, "count": 0, "results": 0,
                                    "zero": 0})
        t["count"] += 1
        t["results"] += int(det.get("results") or 0)
        if int(det.get("results") or 0) == 0:
            t["zero"] += 1
    top_searches = sorted(terms.values(), key=lambda x: -x["count"])[:limit_terms]
    for t in top_searches:
        t["avg_results"] = round(t["results"] / t["count"], 1) if t["count"] else 0

    return {
        "days": days,
        "funnel": funnel,
        "dropoff": dropoff,
        "sources": sources,
        "top_searches": top_searches,
        "totals": {
            "visits": funnel[0]["unique"] if funnel else 0,
            "visit_hits": funnel[0]["count"] if funnel else 0,
            "searches": funnel[1]["count"] if len(funnel) > 1 else 0,
            "trial_views": next(
                (f["unique"] for f in funnel if f["key"] == "trial_view"), 0),
            "applies": funnel[-1]["unique"] if funnel else 0,
            "visit_to_apply": round(
                (funnel[-1]["unique"] / top_u * 100.0), 1) if top_u else 0.0,
        },
    }


def unmet_demand(days=30, limit=10):
    """Searches that returned zero results, aggregated by term. This is the
    highest-signal card for the operator: each is real demand with no trial to
    send them to - a candidate to seed a study or recruit a site for."""
    d = get_db()
    since = _web_since(days)
    terms = {}
    for r in d.execute(
        "SELECT detail FROM web_events WHERE name = 'search' AND ts >= ? "
        "AND detail != ''", (since,)).fetchall():
        try:
            det = json.loads(r["detail"])
        except (TypeError, ValueError):
            continue
        term = (det.get("q") or "").strip()
        if not term:
            continue
        t = terms.setdefault(term.lower(), {"term": term, "searches": 0, "zero": 0})
        t["searches"] += 1
        if int(det.get("results") or 0) == 0:
            t["zero"] += 1
    out = [t for t in terms.values() if t["zero"] > 0]
    out.sort(key=lambda x: (-x["zero"], -x["searches"]))
    return out[:limit]


def top_trials(days=30, limit=10):
    """Most-viewed trials over the window, with how many went on to apply. This
    localizes the view->apply leak: a trial with lots of views and no applies is
    where interest is dying (bad fit, weak listing, or unclaimed/no follow-up)."""
    d = get_db()
    since = _web_since(days)
    views = {}
    for r in d.execute(
        "SELECT detail, visitor FROM web_events WHERE name = 'trial_view' "
        "AND ts >= ? AND detail != ''", (since,)).fetchall():
        try:
            det = json.loads(r["detail"])
        except (TypeError, ValueError):
            continue
        nct = (det.get("nct") or "").strip()
        if not nct:
            continue
        t = views.setdefault(nct, {"nct": nct, "views": 0,
                                   "visitors": set(), "applies": 0})
        t["views"] += 1
        if r["visitor"]:
            t["visitors"].add(r["visitor"])
    for r in d.execute(
        "SELECT detail FROM web_events WHERE name = 'apply' AND ts >= ? "
        "AND detail != ''", (since,)).fetchall():
        try:
            det = json.loads(r["detail"])
        except (TypeError, ValueError):
            continue
        nct = (det.get("nct") or "").strip()
        if nct in views:
            views[nct]["applies"] += 1
    out = []
    for nct, t in views.items():
        row = d.execute(
            "SELECT title FROM leads WHERE nct = ? AND title != '' "
            "ORDER BY id DESC LIMIT 1", (nct,)).fetchone()
        uv = len(t["visitors"])
        out.append({
            "nct": nct, "title": (row["title"] if row else ""),
            "views": t["views"], "visitors": uv, "applies": t["applies"],
            "view_to_apply": round(t["applies"] / uv * 100.0, 0) if uv else 0.0,
        })
    out.sort(key=lambda x: (-x["views"], -x["visitors"]))
    return out[:limit]


def device_breakdown(days=30):
    """Rough device split (mobile/tablet/desktop) of real visitors, from the
    user-agent. Tells the operator whether to prioritize the mobile experience."""
    d = get_db()
    since = _web_since(days)
    buckets = {"Mobile": set(), "Tablet": set(), "Desktop": set()}
    for r in d.execute(
        "SELECT ua, visitor FROM web_events WHERE name = 'visit' AND ts >= ?",
        (since,)).fetchall():
        vis = r["visitor"] or ""
        if not vis:
            continue
        ua = (r["ua"] or "").lower()
        if "ipad" in ua or "tablet" in ua:
            buckets["Tablet"].add(vis)
        elif "mobi" in ua or "iphone" in ua or "android" in ua:
            buckets["Mobile"].add(vis)
        else:
            buckets["Desktop"].add(vis)
    total = sum(len(s) for s in buckets.values()) or 1
    return [{"device": k, "visitors": len(v),
             "pct": round(len(v) / total * 100.0, 0)}
            for k, v in buckets.items() if v]


def web_areas(days=30, limit=12):
    """Coarse geographic breakdown of visitors over the last `days`, grouped by
    country/region/city (derived from IP at log time; the raw IP is never
    stored). Returns the top areas by unique visitors, plus how many visitors
    couldn't be located (private IP, lookup miss, or geo disabled)."""
    d = get_db()
    since = _web_since(days)
    rows = d.execute(
        "SELECT country, region, city, COUNT(DISTINCT visitor) u, COUNT(*) c "
        "FROM web_events WHERE ts >= ? "
        "AND (city != '' OR region != '' OR country != '') "
        "GROUP BY country, region, city ORDER BY u DESC, c DESC LIMIT ?",
        (since, limit)).fetchall()
    areas = []
    for r in rows:
        label = ", ".join(p for p in (r["city"], r["region"], r["country"]) if p)
        areas.append({"area": label or "Unknown",
                      "visitors": (r["u"] or 0), "hits": (r["c"] or 0)})
    unknown = d.execute(
        "SELECT COUNT(DISTINCT visitor) u FROM web_events WHERE ts >= ? "
        "AND city = '' AND region = '' AND country = ''", (since,)).fetchone()
    return {"areas": areas, "unknown_visitors": (unknown["u"] or 0)}


def clear_web_events():
    """Wipe all visitor-analytics events. Used by the owner to reset to a clean
    baseline (e.g. after removing bot-inflated history). Returns rows deleted."""
    d = get_db()
    n = d.execute("SELECT COUNT(*) c FROM web_events").fetchone()["c"]
    d.execute("DELETE FROM web_events")
    d.commit()
    return int(n or 0)


def web_timeseries(days=30):
    """Per-day traffic for the visitor chart: unique visitors, searches, and
    applies for each of the last `days` days. Zero-filled so the line is
    continuous even on days with no traffic. Newest last (left-to-right)."""
    d = get_db()
    since = _web_since(days)
    rows = d.execute(
        "SELECT substr(ts, 1, 10) AS day, "
        "COUNT(DISTINCT CASE WHEN name = 'visit' THEN visitor END) AS visitors, "
        "SUM(CASE WHEN name = 'search' THEN 1 ELSE 0 END) AS searches, "
        "SUM(CASE WHEN name = 'apply' THEN 1 ELSE 0 END) AS applies "
        "FROM web_events WHERE ts >= ? GROUP BY day", (since,)).fetchall()
    by_day = {r["day"]: r for r in rows}
    span = max(1, days)
    start = dt.date.today() - dt.timedelta(days=span - 1)
    out = []
    for i in range(span):
        day = (start + dt.timedelta(days=i)).strftime("%Y-%m-%d")
        r = by_day.get(day)
        out.append({
            "day": day,
            "visitors": int((r["visitors"] if r else 0) or 0),
            "searches": int((r["searches"] if r else 0) or 0),
            "applies": int((r["applies"] if r else 0) or 0),
        })
    return out


def recent_searches(days=30, limit=40):
    """Most recent search events (term, result count, source, when) - a live feed
    of what real visitors are looking for. Newest first. No PHI."""
    d = get_db()
    since = _web_since(days)
    out = []
    for r in d.execute(
        "SELECT ts, detail, city, region, country, "
        "CASE WHEN source != '' THEN source WHEN referrer != '' THEN referrer "
        "ELSE 'direct' END AS src "
        "FROM web_events WHERE name = 'search' AND ts >= ? "
        "ORDER BY ts DESC LIMIT ?", (since, limit)).fetchall():
        try:
            det = json.loads(r["detail"]) if r["detail"] else {}
        except (TypeError, ValueError):
            det = {}
        term = (det.get("q") or "").strip()
        if not term:
            continue
        # "place" = where the search was aimed. Prefer the location the searcher
        # typed (their intent), and fall back to the coarse IP-derived geo (where
        # they physically are) so the feed is rarely blank. No raw IP/PHI.
        typed = (det.get("loc") or "").strip()
        geo = ", ".join(p for p in (r["city"], r["region"] or r["country"]) if p)
        out.append({
            "ts": r["ts"],
            "term": term,
            "results": int(det.get("results") or 0),
            "source": r["src"],
            "place": typed or geo,
            "place_kind": "typed" if typed else ("geo" if geo else ""),
        })
    return out


# --------------------------------------------------------------------------- #
# Search result cache - shared across gunicorn workers (see web/app.py). Kept in
# SQLite (not process memory) so a trial detail request served by a different
# worker than the one that ran the search can still find the ranked results.
# --------------------------------------------------------------------------- #
SEARCH_CACHE_TTL = 24 * 3600   # results are good for a day
SEARCH_CACHE_MAX = 500         # hard cap on rows kept


def save_search(sid, payload):
    """Persist a search result blob (JSON string) under a short id, then prune
    anything older than the TTL and trim back to SEARCH_CACHE_MAX rows."""
    con = get_db()
    ts = time.time()
    con.execute(
        "INSERT OR REPLACE INTO search_cache (sid, payload, created_at) "
        "VALUES (?,?,?)", (sid, payload, ts))
    con.execute("DELETE FROM search_cache WHERE created_at < ?",
                (ts - SEARCH_CACHE_TTL,))
    con.execute(
        "DELETE FROM search_cache WHERE sid NOT IN "
        "(SELECT sid FROM search_cache ORDER BY created_at DESC LIMIT ?)",
        (SEARCH_CACHE_MAX,))
    con.commit()


def get_search(sid):
    """Return the stored payload (JSON string) for a search id, or None."""
    row = get_db().execute(
        "SELECT payload FROM search_cache WHERE sid = ?", (sid,)).fetchone()
    return row["payload"] if row else None


# --------------------------------------------------------------------------- #
# Trending cache - externally-sourced trending terms (see web/trends.py).
# --------------------------------------------------------------------------- #
def get_trend_cache(kind):
    """Return (terms:list, updated_at:str|None) for a trending kind."""
    row = get_db().execute(
        "SELECT terms, updated_at FROM trend_cache WHERE kind = ?",
        (kind,)).fetchone()
    if not row:
        return [], None
    try:
        terms = json.loads(row["terms"])
    except (ValueError, TypeError):
        terms = []
    return terms, row["updated_at"]


def set_trend_cache(kind, terms):
    db = get_db()
    db.execute(
        "INSERT INTO trend_cache (kind, terms, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(kind) DO UPDATE SET terms = excluded.terms, "
        "updated_at = excluded.updated_at",
        (kind, json.dumps(list(terms)), now()))
    db.commit()


# --------------------------------------------------------------------------- #
# Plain-English trial summaries (see web/summarize.py)
# --------------------------------------------------------------------------- #
def get_trial_summary(nct):
    if not nct:
        return None
    row = get_db().execute(
        "SELECT data FROM trial_summaries WHERE nct = ?", (nct,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["data"])
    except (ValueError, TypeError):
        return None


def set_trial_summary(nct, data):
    if not nct:
        return
    db = get_db()
    db.execute(
        "INSERT INTO trial_summaries (nct, data, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(nct) DO UPDATE SET data = excluded.data, "
        "updated_at = excluded.updated_at",
        (nct, json.dumps(data), now()))
    db.commit()


# --------------------------------------------------------------------------- #
# Patient-mediated records profile (connected once, see records.py)
# --------------------------------------------------------------------------- #
def get_records_profile(applicant_token):
    if not applicant_token:
        return None
    row = get_db().execute(
        "SELECT * FROM records_profiles WHERE applicant_token = ?",
        (applicant_token,)).fetchone()
    if not row:
        return None
    try:
        prof = json.loads(row["data"])
    except (ValueError, TypeError):
        prof = {}
    prof.setdefault("provider", row["provider"])
    prof.setdefault("summary", row["summary"])
    prof["age"] = row["age"] or prof.get("age")
    prof["sex"] = row["sex"] or prof.get("sex")
    prof["sync_status"] = row["sync_status"] or prof.get("sync_status") or ""
    prof["source_status"] = row["source_status"] or prof.get("source_status") or ""
    prof["external_patient_id"] = row["external_patient_id"] or \
        prof.get("external_patient_id") or ""
    prof["external_query_id"] = row["external_query_id"] or \
        prof.get("external_query_id") or ""
    prof["completeness_score"] = int(
        row["completeness_score"] or prof.get("completeness_score") or 0)
    prof["last_sync_error"] = row["last_sync_error"] or \
        prof.get("last_sync_error") or ""
    prof["last_sync_at"] = row["last_sync_at"] or prof.get("last_sync_at") or ""
    prof["connected_at"] = row["connected_at"] or prof.get("connected_at") or ""
    return prof


def set_records_profile(applicant_token, prof):
    if not applicant_token or not prof:
        return
    db = get_db()
    age = "" if prof.get("age") in (None, "") else str(prof.get("age"))
    db.execute(
        "INSERT INTO records_profiles "
        "(applicant_token, provider, age, sex, sync_status, source_status, "
        "external_patient_id, external_query_id, completeness_score, "
        "last_sync_error, last_sync_at, data, summary, connected_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(applicant_token) DO UPDATE SET provider = excluded.provider, "
        "age = excluded.age, sex = excluded.sex, sync_status = excluded.sync_status, "
        "source_status = excluded.source_status, "
        "external_patient_id = excluded.external_patient_id, "
        "external_query_id = excluded.external_query_id, "
        "completeness_score = excluded.completeness_score, "
        "last_sync_error = excluded.last_sync_error, "
        "last_sync_at = excluded.last_sync_at, data = excluded.data, "
        "summary = excluded.summary, connected_at = excluded.connected_at",
        (applicant_token, prof.get("provider", ""), age, prof.get("sex", ""),
         prof.get("sync_status", ""), prof.get("source_status", ""),
         prof.get("external_patient_id", ""), prof.get("external_query_id", ""),
         int(prof.get("completeness_score") or 0),
         prof.get("last_sync_error", ""), prof.get("last_sync_at", ""),
         json.dumps(prof), prof.get("summary", ""), now()))
    db.commit()


def _norm_sync(v):
    return (v or "").strip().lower()


def _pick_sync_status(prev_status, next_status, allow_regress=False):
    prev = _norm_sync(prev_status)
    nxt = _norm_sync(next_status)
    if not nxt:
        return prev
    if prev == nxt or not prev:
        return nxt
    if allow_regress:
        return nxt
    if nxt in ("connected", "error"):
        return nxt
    if nxt == "syncing" and prev in ("connected", "error"):
        # Ignore stale in-flight events after we already have a terminal state.
        return prev
    return nxt


def set_records_sync_state(applicant_token, provider="", sync_status="",
                           source_status="", external_patient_id="",
                           external_query_id="", error_msg="",
                           allow_regress=False):
    """Upsert sync metadata for a profile before/without full record payload."""
    if not applicant_token:
        return
    prev = get_records_profile(applicant_token) or {}
    target_sync = _pick_sync_status(
        prev.get("sync_status", ""), sync_status or prev.get("sync_status", ""),
        allow_regress=allow_regress)
    prof = {
        "provider": provider or prev.get("provider", ""),
        "age": prev.get("age", ""),
        "sex": prev.get("sex", ""),
        "summary": prev.get("summary", ""),
        "sync_status": target_sync,
        "source_status": source_status or prev.get("source_status", ""),
        "external_patient_id": external_patient_id or prev.get("external_patient_id", ""),
        "external_query_id": external_query_id or prev.get("external_query_id", ""),
        "completeness_score": prev.get("completeness_score", 0),
        "last_sync_error": error_msg if error_msg is not None else prev.get("last_sync_error", ""),
        "last_sync_at": now(),
        "conditions": prev.get("conditions", []),
        "meds": prev.get("meds", []),
        "labs": prev.get("labs", []),
        "data_source": prev.get("data_source", ""),
    }
    set_records_profile(applicant_token, prof)


def record_records_webhook_event(event_key, applicant_token="", provider="",
                                 event_type="", source_status=""):
    """Returns True for first-seen event key, False for duplicate delivery."""
    event_key = (event_key or "").strip()
    if not event_key:
        return False
    db = get_db()
    ts = now()
    cur = db.execute(
        "INSERT OR IGNORE INTO records_webhook_events "
        "(event_key, applicant_token, provider, event_type, source_status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (event_key, (applicant_token or "").strip(), (provider or "").strip(),
         (event_type or "").strip(), (source_status or "").strip(), ts))
    # Keep the dedup table bounded without separate jobs.
    try:
        keep_days = max(1, int(os.environ.get("RECORDS_WEBHOOK_EVENT_RETENTION_DAYS", "30")))
    except Exception:
        keep_days = 30
    cutoff = (dt.datetime.now() - dt.timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M")
    db.execute("DELETE FROM records_webhook_events WHERE created_at < ?", (cutoff,))
    db.commit()
    return bool((cur.rowcount or 0) > 0)


def find_applicant_by_external_patient(external_patient_id):
    if not external_patient_id:
        return ""
    row = get_db().execute(
        "SELECT applicant_token FROM records_profiles WHERE external_patient_id = ?",
        ((external_patient_id or "").strip(),)).fetchone()
    return row["applicant_token"] if row else ""


def find_applicant_by_external_query(external_query_id):
    if not external_query_id:
        return ""
    row = get_db().execute(
        "SELECT applicant_token FROM records_profiles WHERE external_query_id = ?",
        ((external_query_id or "").strip(),)).fetchone()
    return row["applicant_token"] if row else ""


def clear_records_profile(applicant_token):
    if not applicant_token:
        return
    db = get_db()
    db.execute("DELETE FROM records_profiles WHERE applicant_token = ?",
               (applicant_token,))
    db.commit()


# --------------------------------------------------------------------------- #
# Trial alerts (saved searches, see alerts.py)
# --------------------------------------------------------------------------- #
def create_alert(data):
    db = get_db()
    cur = db.execute(
        """INSERT INTO alerts
           (applicant_token, label, condition, intervention, location, lat, lon,
            cc, radius, unit, email, active, strong_only, created_at,
            notify_min_days)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)""",
        (data.get("applicant_token", ""), data.get("label", ""),
         data.get("condition", ""), data.get("intervention", ""),
         data.get("location", ""), data.get("lat"), data.get("lon"),
         data.get("cc", ""), int(data.get("radius") or 50),
         data.get("unit", "km"), data.get("email", ""),
         1 if data.get("strong_only", True) else 0, now(),
         int(data.get("notify_min_days") or 7)))
    db.commit()
    return cur.lastrowid


def update_alert_prefs(alert_id, applicant_token, *, radius=None, unit=None,
                       strong_only=None, location=None, lat=None, lon=None,
                       cc=None):
    """Let the owner tune an alert's quality controls (distance radius, unit,
    strong-fit-only) and location. Only touches provided fields; ownership is
    enforced by applicant_token so one patient can't edit another's alert."""
    a = get_alert(alert_id)
    if not a or a["applicant_token"] != applicant_token:
        return False
    sets, vals = [], []
    if radius is not None:
        sets.append("radius = ?"); vals.append(max(1, int(radius)))
    if unit is not None:
        sets.append("unit = ?"); vals.append(unit if unit in ("km", "mi") else "km")
    if strong_only is not None:
        sets.append("strong_only = ?"); vals.append(1 if strong_only else 0)
    if location is not None:
        sets.append("location = ?"); vals.append(location.strip())
    if lat is not None:
        sets.append("lat = ?"); vals.append(lat)
    if lon is not None:
        sets.append("lon = ?"); vals.append(lon)
    if cc is not None:
        sets.append("cc = ?"); vals.append((cc or "").strip().upper())
    if not sets:
        return False
    vals.extend([alert_id, applicant_token])
    db = get_db()
    db.execute("UPDATE alerts SET " + ", ".join(sets)
               + " WHERE id = ? AND applicant_token = ?", vals)
    db.commit()
    return True


def list_alerts(applicant_token):
    if not applicant_token:
        return []
    return get_db().execute(
        "SELECT * FROM alerts WHERE applicant_token = ? ORDER BY id DESC",
        (applicant_token,)).fetchall()


def list_active_alerts():
    return get_db().execute(
        "SELECT * FROM alerts WHERE active = 1 ORDER BY id").fetchall()


def upsert_seo_study(nct, title="", condition=""):
    """Record a trial as part of our indexable SEO surface (idempotent)."""
    nct = (nct or "").strip().upper()
    if not nct:
        return
    db = get_db()
    db.execute(
        "INSERT INTO seo_study_index (nct, title, condition, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(nct) DO UPDATE SET title=excluded.title, "
        "condition=excluded.condition, updated_at=excluded.updated_at",
        (nct, (title or "").strip(), (condition or "").strip(), now()))
    db.commit()


def is_seo_study(nct):
    nct = (nct or "").strip().upper()
    if not nct:
        return False
    return get_db().execute(
        "SELECT 1 FROM seo_study_index WHERE nct = ?", (nct,)).fetchone() is not None


def list_seo_studies(limit=5000):
    """NCTs in our indexable SEO surface, most-recently-seen first."""
    return get_db().execute(
        "SELECT nct, title, condition, updated_at FROM seo_study_index "
        "ORDER BY updated_at DESC, nct LIMIT ?", (limit,)).fetchall()


def record_seo_city_page(slug, city_slug, is_local, trials):
    """Remember whether a condition/city page has LOCAL recruiting trials, so the
    sitemap can list only the indexable ones (idempotent per slug pair)."""
    slug = (slug or "").strip()
    city_slug = (city_slug or "").strip()
    if not slug or not city_slug:
        return
    db = get_db()
    db.execute(
        "INSERT INTO seo_city_page (slug, city_slug, is_local, trials, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(slug, city_slug) DO UPDATE SET is_local=excluded.is_local, "
        "trials=excluded.trials, updated_at=excluded.updated_at",
        (slug, city_slug, 1 if is_local else 0, int(trials or 0), now()))
    db.commit()


def list_local_seo_city_pages():
    """Condition/city (slug, city_slug) pairs with confirmed local trials - the
    only city pages worth putting in the sitemap."""
    return get_db().execute(
        "SELECT slug, city_slug, updated_at FROM seo_city_page "
        "WHERE is_local = 1 AND trials > 0 ORDER BY updated_at DESC").fetchall()


def get_kv(key, default=None):
    """Read a small persistent app-state value (see app_kv). Returns default if
    unset."""
    row = get_db().execute("SELECT v FROM app_kv WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else default


def set_kv(key, val):
    """Write a small persistent app-state value (idempotent upsert)."""
    db = get_db()
    db.execute(
        "INSERT INTO app_kv (k, v, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
        (str(key), str(val), now()))
    db.commit()


def list_notifiable_alerts():
    """Active alerts the cron sweep should actually process: only those owned by
    an existing account that has email alerts switched on. This is what makes the
    sweep a no-op until someone signs up and opts into alerts - we never do CT.gov
    work (or email) for guests or patients who turned alerts off."""
    return get_db().execute(
        "SELECT a.* FROM alerts a "
        "JOIN patient_users p ON p.applicant_token = a.applicant_token "
        "WHERE a.active = 1 AND p.email_alerts = 1 "
        "ORDER BY a.id").fetchall()


def get_alert(alert_id):
    return get_db().execute(
        "SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()


def delete_alert(alert_id, applicant_token):
    a = get_alert(alert_id)
    if not a or a["applicant_token"] != applicant_token:
        return False
    db = get_db()
    db.execute("DELETE FROM alert_matches WHERE alert_id = ?", (alert_id,))
    db.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    db.commit()
    return True


def alert_seen_ncts(alert_id):
    rows = get_db().execute(
        "SELECT nct FROM alert_matches WHERE alert_id = ?", (alert_id,)).fetchall()
    return {r["nct"] for r in rows}


def add_alert_matches(alert_id, matches, is_new):
    """matches: iterable of (nct, title). Skips NCTs already recorded."""
    db = get_db()
    seen = alert_seen_ncts(alert_id)
    ts = now()
    added = 0
    for nct, title in matches:
        if not nct or nct in seen:
            continue
        db.execute(
            "INSERT INTO alert_matches (alert_id, nct, title, is_new, created_at) "
            "VALUES (?,?,?,?,?)",
            (alert_id, nct, title or "", 1 if is_new else 0, ts))
        seen.add(nct)
        added += 1
    db.commit()
    return added


def mark_alert_checked(alert_id):
    db = get_db()
    db.execute("UPDATE alerts SET last_checked_at = ? WHERE id = ?",
               (now(), alert_id))
    db.commit()


def get_alert_matches(alert_id, limit=20):
    return get_db().execute(
        "SELECT * FROM alert_matches WHERE alert_id = ? "
        "ORDER BY is_new DESC, id DESC LIMIT ?", (alert_id, limit)).fetchall()


def get_new_alert_matches(alert_id, limit=100):
    return get_db().execute(
        "SELECT * FROM alert_matches WHERE alert_id = ? AND is_new = 1 "
        "ORDER BY id DESC LIMIT ?", (alert_id, limit)).fetchall()


def alert_notify_due(alert, min_days=7):
    """Whether enough time has passed since the last alert email."""
    a = alert
    if isinstance(alert, int):
        a = get_alert(alert)
    if not a:
        return False
    try:
        min_days = max(1, int(a["notify_min_days"] or min_days))
    except Exception:
        min_days = max(1, int(min_days or 7))
    try:
        last = a["last_notified_at"]  # sqlite3.Row has no .get()
    except (KeyError, IndexError, TypeError):
        last = ""
    ts = _parse_ts(last or "")
    if not ts:
        return True
    return (dt.datetime.now() - ts).total_seconds() >= (min_days * 86400)


def mark_alert_notified(alert_id):
    db = get_db()
    db.execute("UPDATE alerts SET last_notified_at = ? WHERE id = ?",
               (now(), alert_id))
    db.commit()


def new_matches_count(applicant_token):
    if not applicant_token:
        return 0
    r = get_db().execute(
        "SELECT COUNT(*) n FROM alert_matches m JOIN alerts a ON a.id = m.alert_id "
        "WHERE a.applicant_token = ? AND m.is_new = 1",
        (applicant_token,)).fetchone()
    return r["n"] if r else 0


def clear_new_flags(applicant_token):
    """Called when the patient views their alerts, so the nav badge resets."""
    if not applicant_token:
        return
    db = get_db()
    db.execute(
        "UPDATE alert_matches SET is_new = 0 WHERE alert_id IN "
        "(SELECT id FROM alerts WHERE applicant_token = ?)", (applicant_token,))
    db.commit()


def attach_records_to_open_leads(applicant_token, summary):
    """When a patient connects records, backfill their pending (not-yet-decided)
    applications so the study team sees the record on those too."""
    if not applicant_token:
        return 0
    db = get_db()
    ts = now()
    rows = db.execute(
        "SELECT id FROM leads WHERE applicant_token = ? AND decision = '' "
        "AND records_connected = 0", (applicant_token,)).fetchall()
    for r in rows:
        db.execute("UPDATE leads SET records_connected = 1, record_summary = ?, "
                   "updated_at = ? WHERE id = ?", (summary, ts, r["id"]))
        db.execute(
            "INSERT INTO lead_events (lead_id, status, note, actor, created_at) "
            "VALUES (?,?,?,?,?)",
            (r["id"], "prescreen", "health records connected", "you", ts))
    db.commit()
    return len(rows)


def status_counts(user_id):
    rows = get_db().execute(
        "SELECT status, COUNT(*) n FROM referrals WHERE user_id = ? GROUP BY status",
        (user_id,)).fetchall()
    return {r["status"]: r["n"] for r in rows}


def enrolled_count(user_id):
    """Number of this user's referrals that reached the 'enrolled' state. Pure
    outcome tracking - BridgeMD never pays clinicians per referral/enrollment
    (anti-kickback / fee-splitting), so there is no money attached."""
    row = get_db().execute(
        "SELECT COUNT(*) AS n FROM referrals WHERE user_id = ? AND status = ?",
        (user_id, "enrolled")).fetchone()
    return row["n"] if row else 0
