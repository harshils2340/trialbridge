"""Regression tests for the cross-worker search-result cache.

Background: production runs `gunicorn --workers 2`, so the old in-process
`_SEARCH_CACHE` dict was invisible to the other worker. A trial detail request
routed to the worker that did NOT run the search failed with "that trial result
expired". The fix persists searches in SQLite (shared by both workers) with an
in-process L1 cache in front. These tests simulate the two-worker case by
clearing the in-process cache and asserting the result is still found.

Run: python test_search_cache.py   (offline, no LLM key needed)
"""
import json
import os
import sys
import tempfile

# Hermetic: temp DB, no LLM calls, no login wall.
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ.pop("LLM_API_KEY", None)
os.environ.setdefault("SECRET_KEY", "test-secret")

import app  # noqa: E402  (imports after env is set)


def _fake_result(nct="NCT12345678"):
    """A result shaped like one entry from run_search()."""
    return {
        "trial": {"nctId": nct, "title": "A Study of Widgets in Adults",
                  "phase": "PHASE2", "studyType": "INTERVENTIONAL",
                  "leadSponsor": "Acme Research", "briefSummary": "",
                  "conditions": "Widgetitis", "locations": []},
        "match": {"verdict": "possible", "score": 60,
                  "rationale": "Matches your condition and location.",
                  "met": ["Adult"], "not_met": [], "unknown": ["Lab values"]},
        "site": None, "site_str": "Acme Clinic, Toronto",
        "coordinator": None, "distance": 12.0, "unit": "km",
        "nearby": [], "nearby_total": 0,
        "other_count": 2, "other_regions": ["Ottawa, ON", "Montreal, QC"],
    }


def _fake_ctx():
    return {"condition": "Widgetitis", "location": "Toronto", "unit": "km",
            "q_condition": "Widgetitis", "q_intervention": "", "q_age": "45",
            "q_sex": "female", "q_about": "", "q_radius": 50,
            "q_lat": "", "q_lon": "", "q_cc": ""}


def test_cross_worker_function_level():
    """Cache a search, wipe the in-process cache (as if a *different* gunicorn
    worker handled the follow-up request), and confirm the trial is still found
    via the shared SQLite store."""
    nct = "NCT12345678"
    with app.app.test_request_context("/"):
        sid = app._cache_search([_fake_result(nct)], _fake_ctx())

    # Simulate a second worker: it has never seen this sid in memory.
    app._SEARCH_CACHE.clear()
    assert sid not in app._SEARCH_CACHE, "L1 cache should be empty here"

    with app.app.test_request_context("/"):
        r, ctx = app._get_cached_trial(sid, nct)
    assert r is not None, "trial not found after clearing in-process cache"
    assert r["trial"]["nctId"] == nct
    assert ctx["condition"] == "Widgetitis"
    print("PASS: cross-worker function-level lookup")


def test_missing_sid_returns_none():
    app._SEARCH_CACHE.clear()
    with app.app.test_request_context("/"):
        r, ctx = app._get_cached_trial("does-not-exist", "NCT00000000")
    assert r is None and ctx is None
    print("PASS: unknown search id returns nothing")


def test_detail_route_survives_worker_switch():
    """End-to-end: the /trial page must render (200) even when the in-process
    cache is empty - i.e. no 'that trial result expired' redirect."""
    nct = "NCT87654321"
    with app.app.test_request_context("/"):
        sid = app._cache_search([_fake_result(nct)], _fake_ctx())

    app._SEARCH_CACHE.clear()  # different worker

    client = app.app.test_client()
    resp = client.get(f"/trial/{sid}/{nct}", follow_redirects=False)
    assert resp.status_code == 200, (
        f"expected 200, got {resp.status_code} (expired-redirect bug?)")
    body = resp.get_data(as_text=True)
    assert "trial result expired" not in body
    assert nct in body
    print("PASS: /trial detail route renders after worker switch")


def test_expired_search_redirects():
    """A genuinely unknown search id should still show the friendly redirect."""
    app._SEARCH_CACHE.clear()
    client = app.app.test_client()
    resp = client.get("/trial/bogus-sid/NCT00000000", follow_redirects=False)
    assert resp.status_code in (301, 302), "unknown search should redirect"
    print("PASS: unknown search id redirects (graceful expiry)")


def main():
    tests = [
        test_cross_worker_function_level,
        test_missing_sid_returns_none,
        test_detail_route_survives_worker_switch,
        test_expired_search_redirects,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {t.__name__}: {e}")
        except Exception as e:  # unexpected error
            failed += 1
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}")
    os.unlink(_TMP_DB) if os.path.exists(_TMP_DB) else None
    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed")


if __name__ == "__main__":
    main()
