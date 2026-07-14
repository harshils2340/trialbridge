# BridgeMD — Test Log

A running log of changes and the local tests that verify them. Every entry lists
what changed, how to reproduce the test, and the result. Tests are hermetic
(temp DB, no LLM key, no network) and run from `web/`.

## How to run

```bash
cd matcher
python3 -m venv .venv
.venv/bin/pip install -r web/requirements.txt
cd web
../.venv/bin/python test_search_cache.py
```

---

## 2026-07-08 — Fix "That trial result expired" on trial detail pages

**Symptom:** Clicking "Proceed / see more details" on a trial sometimes bounced
back with *"That trial result expired - please run your search again."*

**Root cause:** Production runs `gunicorn --workers 2` (see `render.yaml`). The
ranked search results were held in a per-process in-memory dict (`_SEARCH_CACHE`
in `web/app.py`). A search cached by worker A was invisible to worker B, so a
detail request routed to the other worker (~50% of the time) found nothing and
reported the results as expired.

**Fix:** Persist searches in SQLite (shared by both workers) via
`db.save_search` / `db.get_search`, keeping the in-process dict only as an L1
fast path. Entries are pruned after 24h and capped at 500 rows. Table is created
automatically on startup (`CREATE TABLE IF NOT EXISTS`), so no manual migration.

- `web/db.py`: `search_cache` table + index, `save_search` / `get_search`.
- `web/app.py`: `_cache_search` also writes to SQLite; new `_load_search`
  reads L1 then falls back to the shared store; `_get_cached_trial` uses it.

**Tests:** `web/test_search_cache.py`

| Test | What it proves |
|------|----------------|
| `test_cross_worker_function_level` | After clearing the in-process cache (simulating a *different* worker), the trial is still found via SQLite. |
| `test_missing_sid_returns_none` | Unknown search id returns nothing (no false hits). |
| `test_detail_route_survives_worker_switch` | `/trial/<sid>/<nct>` renders **200** after the L1 cache is cleared — the exact bug scenario, which previously produced the "expired" redirect. |
| `test_expired_search_redirects` | A genuinely unknown search id still shows the friendly redirect. |

**Result:** `All 4 tests passed`.

---

## 2026-07-10 — Auto-hide "Probably not a fit" + real location on mobile

**Two patient-facing fixes.**

### 1. Auto-deselect "Probably not a match" trials
Results now start with the "Probably not a fit" match-quality filter **unchecked**,
so those studies are hidden until the patient opts in.

- `web/templates/patient_results.html`: the `no` fit checkbox renders unchecked;
  `apply()` runs on load to hide those cards; `reset()` restores this default
  (good/maybe shown, "no" hidden). The filter group only appears when there's a
  mix of qualities, so an all-"no" result set still shows everything.

### 2. Location field showed "Current location" instead of the real place
On the live (non-demo) site, `/geo/reverse` was gated by `@login_required`.
Logged-out patients' browser calls got redirected to the login page, `r.json()`
threw, and the search box fell back to the literal string "Current location".

- `web/app.py`: removed `@login_required` from `geo_reverse` (it's a stateless
  reverse-geocode helper, no user data). Cleaned the label to read
  "City, Region" and append the ZIP for US (e.g. "New York, New York 10014",
  "Toronto, Ontario").

**Tests:** `web/test_ui_fixes.py`

| Test | What it proves |
|------|----------------|
| `test_geo_reverse_public_no_login` | `/geo/reverse` returns 200 JSON when logged out (`NO_LOGIN=0`) — no login redirect. |
| `test_label_us_includes_zip` | US coordinates format as `City, State ZIP`. |
| `test_label_canada_city_region` | Non-US coordinates format as `City, Region` (no ZIP). |
| `test_results_hide_probably_not_by_default` | Results template renders the "no" fit checkbox unchecked, "good" checked, and applies the filter on load. |

**Result:** `All 4 tests passed`; search-cache regression suite still `4/4`.

---

## 2026-07-10 — Mobile responsiveness, dark mode & accessibility

**Feedback addressed:** the app was "whacky" on phones; red/green fit indicators
are invisible to colorblind users; and white backgrounds with grey text are hard
on the eyes (needed a clean dark mode).

### 1. Mobile responsiveness (the "whacky" layouts)
Measured every page at a 390px (iPhone) viewport with headless Chromium and
checked `document.documentElement.scrollWidth > innerWidth` (horizontal
overflow = the tell-tale sign of a broken mobile layout).

- **Root cause:** the clinician/study **sidebar** (`base.html`) collapsed into a
  non-wrapping horizontal row on mobile, running **240–320px off-screen**. Wide
  tables (`.inv-table`) and the candidate-review tab bar (`.rev-tabs`) also
  overflowed.
- **Fix (`static/style.css`):** the mobile top bar now `flex-wrap`s (brand + sign
  out on row 1, POV switch + nav links below); the theme toggle stays a compact
  circle; wide tables and tab bars scroll instead of pushing the page.
- **Before:** 10 pages overflowed by 66–320px. **After:** every tested page = **0px
  overflow** in both light and dark.

### 2. Clean dark mode
- Set before first paint by an inline script in both base templates (honors a
  saved choice, else the OS `prefers-color-scheme`) — no white flash on load.
- A round **sun/moon toggle** (nav on public pages, sidebar on the app, floating
  on auth pages) persists the choice in `localStorage`.
- The theme is token-driven: `[data-theme="dark"]` redefines the CSS variables and
  patches the ~80 places that hard-coded light colors (inputs, cards, tinted
  pills, POV switcher, chat bubbles, etc.). Verified visually across landing,
  results, applications, clinician dashboard and study-team board.

### 3. Colorblind-safe status + other a11y
- Fit badges now carry **distinct icons per verdict** (good = check, maybe =
  sparkle, not-a-fit = ✕) — not hue alone — plus a defining border.
- Match-quality filter dots use distinct **shapes** (circle / square / diamond).
- Eligibility chips/criteria get a leading glyph (✓ / ? / ✕).
- Added: visible `:focus-visible` rings on all controls, a **skip-to-content**
  link, a `#main` landmark and `role="main"`, accessible name + `aria-pressed`
  on the theme toggle, and `<meta name="color-scheme">`.

**Tests:** `web/test_a11y_theme.py` (6 checks) — dark-mode wiring in both bases,
skip link + `#main`, dark theme + focus/skip/toggle CSS, colorblind dot shapes,
mobile sidebar-wrap + table/tab scroll rules, and distinct fit-badge icons.
Visual verification (headless Chromium, 390px + 1280px, light + dark) confirmed
**0px** horizontal overflow on all pages.

**Result:** `All 6 tests passed`; prior suites still green
(`test_ui_fixes.py` 4/4, `test_search_cache.py` 4/4).

---

## 2026-07-13 — Demo lane 4: EHR background matching

**Goal:** the 4th demo lane ("EHR background matching") deep-linked to the plain
physician search view, so there was nothing showing the clinic-wide EHR story.
Build a clear, self-explanatory demo of it.

**The workflow it now shows (simple, one screen):**
1. The clinic's EHR is **connected clinic-wide** (green banner: system + clinic +
   records screened + last sync).
2. The platform **continuously screens the clinic's own patients** against the
   trials that clinic runs (3-step "how it works" strip + summary stats).
3. **New matches surface grouped by trial** — each patient shown de-identified
   (`PT-####` + initials, age/sex, last seen) with a plain-language "why matched"
   reason and a match-strength flag.
4. Staff **moves forward in one click**: *Email patient* or *Send to physician*.
   In the demo those actions mark the row done and clear it from the "new
   matches" count + the trial's badge.

- `web/app.py`: `dashboard()` branches on `wf=proactive` to render a dedicated
  view; `_ehr_matching_demo()` supplies the demo payload (gated on preview mode).
- `web/templates/ehr_matching.html`: the new view.
- `web/static/style.css`: `.ehr-*` styles — token-driven so dark mode themes it
  automatically; stacks on mobile.

**Compliance:** this is clinical decision support for the treating clinic (their
physicians, their patients). Patients are de-identified; the UI states a
physician reviews before anyone is contacted; no referral is bought/sold
(see `matcher/COMPLIANCE.md`).

**Tests:** `web/test_ehr_lane.py`

| Test | What it proves |
|------|----------------|
| `test_demo_payload_shape` | Demo payload is well-formed; `new_matches` equals the patients listed; every match has a reason. |
| `test_proactive_renders_ehr_view` | `/app?wf=proactive` renders the EHR view: connection state, match queue, per-trial NCTs, and both actions. |
| `test_plain_dashboard_is_search` | Plain `/app` still renders the physician search dashboard (not the EHR view). |
| `test_compliance_deidentified_and_review_note` | Patients use de-identified `PT-####` refs and the "physician reviews before anyone is contacted" note is present. |

Visual + interaction check (headless Chromium): **0px** horizontal overflow at
390px and 1280px in light and dark; clicking *Email patient* marks the row
"Email invite sent" and decrements the new-match count (6→5) and trial badge
(3 new → 2 new).

**Result:** `All 4 tests passed`; prior suites still green
(`test_a11y_theme.py` 6/6, `test_ui_fixes.py` 4/4, `test_search_cache.py` 4/4).

## 2026-07-13 — Lane 4 de-slop: user-POV work queue

**Feedback:** the lane opened with a marketing "Epic EHR connected — clinic-wide"
banner and a "Connect once → we screen → matches appear" 3-step explainer. That
reads like an investor/landing pitch, not the tool a clinic coordinator actually
uses ("bare AI slop … this isn't an investor website, it's a user POV").

**Change:** stripped the pitch and made it a real workspace.
- Removed the `.ehr-banner` promo card, the `.ehr-how` 3-step strip, and the
  "Demo data…" footer explainer.
- Header now mirrors the physician dashboard: an `<h1>` + inline `stat-strip`
  (active trials · records scanned · new to review).
- Connection state is a single compact, muted line (`.ehr-status`): green dot +
  `Epic · Riverside Family Medicine · synced 12 min ago` + the review-before-
  contact note. No promo pill, no "Matching in the background".
- Dropped the redundant "New patient matches" H2 (the H1 already labels it).
- `web/templates/ehr_matching.html`, `web/static/style.css` (removed the now-
  unused banner/how/stats/foot-note rules, added `.ehr-status`).

**Tests:** `web/test_ehr_lane.py` (updated to the new UI; added
`test_no_marketing_slop` asserting the banner/how-strip/promo copy are gone).

**Result:** `All 5 tests passed`. Also fixed `web/test_ui_fixes.py` (stale: it
still unpacked `_nominatim_reverse` as a 2-tuple after it grew a `country` field →
now 3-tuple). Full suite green: `test_ehr_lane` 5/5, `test_a11y_theme` 6/6,
`test_ui_fixes` 4/4, `test_search_cache` 4/4. Headless Chromium: **0px** overflow
at 390px and 1200px in light and dark; Details expander shows the de-identified
chart snapshot (vitals/labs/meds/eligibility).
