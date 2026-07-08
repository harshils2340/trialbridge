# UI/UX Sprint Checklist (BridgeMD)

Use this before shipping any visual changes. The goal is not "prettier UI"; the goal is
**Enrollment Velocity**: move users from found -> contacted -> screened -> enrolled -> retained.

## Design principles (high-credibility healthcare UX)
- Keep one primary action per screen.
- Use plain-English copy ("what happens next" always visible).
- Use consistent spacing, typography, and component styles (avoid page-level hardcoding).
- Prefer calm, neutral surfaces with strong contrast for trust.
- Remove decorative clutter that does not move a funnel stage.

## System choices implemented
- Font stack: `Manrope + Inter` (modern, clear, readable).
- Rounded but restrained component geometry.
- Elevated cards, subtle borders, and low-saturation accents.
- Reusable status/timeline/message patterns across patient and operator views.

## Page-by-page checklist

### `landing.html` (patient acquisition)
**Keep**
- 2-field hero search with optional enrichment.
- "Use current location" and chip-assisted prefill.

**Improve**
- Trust rail under search (`Free`, `Private`, `Live CT.gov`).
- Stronger hierarchy in hero title and search shell.
- Cleaner chips + card-style steps.

**Expected funnel impact**
- Improves **found -> contacted** by reducing trust friction at first touch.

### `applications.html` (patient retention + progression)
**Keep**
- Status stepper and message thread.
- Booking CTA when schedule link exists.

**Improve**
- Add explicit "Next best step" callout when awaiting movement.
- Tighten visual grouping for visits/messages/actions.

**Expected funnel impact**
- Improves **contacted -> screened** and **retained**.

### `leads.html` (study-team triage speed)
**Keep**
- Awaiting review vs reviewed tabs.
- Horizontal candidate rows and action controls.

**Improve**
- Claimed-study strip at top for routing clarity.
- Triage note with ideal operator behavior (review -> decision -> first message).

**Expected funnel impact**
- Improves **contacted -> screened** and **screened -> enrolled** by reducing coordinator delay.

### `recruitment.html` (operator optimization loop)
**Keep**
- Funnel conversion, time-in-stage, drop-off, per-trial breakdown.

**Improve**
- Add weekly execution cue: biggest drop-off, longest stage, unanswered messages.

**Expected funnel impact**
- Improves all stages by guiding weekly interventions from data.

## What to avoid ("hardcoding bs")
- One-off colors/radii/shadows in random templates.
- New button styles per page.
- New copy tone per page.
- New layout patterns when an existing one already fits.

If a new style is needed, add it to `style.css` once and reuse.

## Weekly UX ops ritual (30 min)
1. Pull 5 recordings or live sessions (patient + coordinator).
2. Mark every hesitation > 3 seconds.
3. Convert top 3 hesitations into UI copy/layout fixes.
4. Ship within 24h.
5. Check funnel deltas in `/app/dashboard`.
