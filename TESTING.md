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
