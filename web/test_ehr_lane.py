"""Guards the product pivot away from the physician surface.

BridgeMD is now hyperfocused on the research-site inbox (see the product-identity
rule). The old physician-facing surface - the search dashboard AND the demo
"lane 4" EHR background-matching view, both served from the /app (dashboard)
endpoint - has been intentionally HIDDEN: `_hide_physician_surface` redirects every
hidden physician endpoint so "the physician side isn't reachable through any path"
(see _HIDDEN_PHYSICIAN_ENDPOINTS in app.py).

What this file now verifies:
1. The EHR-matching demo payload builder still produces a well-formed structure
   (kept as a pure helper; harmless if the lane is ever re-surfaced).
2. The physician surface STAYS hidden: /app and /app?wf=proactive redirect a
   signed-in user to the inbox instead of rendering a physician view.

(Earlier revisions asserted that /app rendered the physician search dashboard and
the EHR matching workspace with de-identified PT-#### patients. Those views are no
longer reachable by design, so those assertions were replaced with this
hidden-surface guard.)

Run: python test_ehr_lane.py   (offline; NO_LOGIN gives a demo user)
"""
import os
import sys
import tempfile

os.environ["DB_PATH"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["NO_LOGIN"] = "1"
os.environ.setdefault("ALERTS_BACKGROUND", "0")
os.environ.setdefault("REMINDERS_BACKGROUND", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")

import app  # noqa: E402


def test_demo_payload_shape():
    d = app._ehr_matching_demo()
    assert d["connected"] is True
    assert d["trials"], "demo should include active trials"
    total = sum(len(t["patients"]) for t in d["trials"])
    assert d["new_matches"] == total, "new_matches must equal the patients listed"
    for t in d["trials"]:
        assert t["nct"] and t["title"] and t["condition"]
        for p in t["patients"]:
            assert p["reason"], "each match needs a why-matched reason"
    print("PASS: EHR demo payload is well-formed")


def test_physician_surface_is_hidden():
    """The physician surface is deprecated and must not be reachable: both the plain
    search dashboard (/app) and the EHR-matching deep link (/app?wf=proactive) are
    served by the hidden `dashboard` endpoint, so a signed-in user is redirected to
    the inbox instead of seeing any physician view. Guards the pivot so the physician
    side can't silently come back."""
    c = app.app.test_client()
    for path in ("/app", "/app?wf=proactive"):
        r = c.get(path)
        assert r.status_code == 302, (path, r.status_code)
        assert r.headers.get("Location", "").endswith("/app/inbox"), \
            (path, r.headers.get("Location"))
    # Following the redirect lands on the inbox (the actual product home), not a
    # physician EHR workspace.
    html = c.get("/app", follow_redirects=True).get_data(as_text=True)
    assert "New matches from your patients" not in html, \
        "physician EHR view leaked through the redirect"
    print("PASS: physician search + EHR lane stay hidden (redirect to the inbox)")


def main():
    tests = [
        test_demo_payload_shape,
        test_physician_surface_is_hidden,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL: {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR: {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed")


if __name__ == "__main__":
    main()
