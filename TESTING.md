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
