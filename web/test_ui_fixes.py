"""Tests for two patient-facing fixes:

1. /geo/reverse must work for logged-OUT patients (it was gated behind
   @login_required, so the public search form's fetch got redirected to the
   login page, r.json() threw, and the field fell back to "Current location").
   It should also format a clean label ("City, Region", + ZIP for US).

2. The results "Match quality" filter must start with "Probably not a fit"
   UNCHECKED so those studies are auto-hidden until the patient opts in.

Run: python test_ui_fixes.py   (offline; nominatim is stubbed)
"""
import io
import json
import os
import sys
import tempfile
from unittest import mock

# Hermetic + realistic auth: NO_LOGIN off so g.user is truly None (as in prod).
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ["NO_LOGIN"] = "0"
os.environ.setdefault("ALERTS_BACKGROUND", "0")
os.environ.setdefault("REMINDERS_BACKGROUND", "0")
os.environ.pop("LLM_API_KEY", None)
os.environ.setdefault("SECRET_KEY", "test-secret")

import app  # noqa: E402


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def _stub_nominatim(address):
    """Return a fake urlopen that yields one Nominatim reverse-geocode result."""
    payload = {"address": address, "display_name": "fallback name"}

    def _fake_urlopen(req, timeout=None):
        return _FakeResp(json.dumps(payload).encode())
    return _fake_urlopen


def test_geo_reverse_public_no_login():
    """A logged-out patient must get a 200 JSON response, not a login redirect."""
    with mock.patch.object(app, "_nominatim_reverse",
                           return_value=("Toronto, Ontario", "CA", "Canada")):
        client = app.app.test_client()
        resp = client.get("/geo/reverse?lat=43.65&lon=-79.38",
                          follow_redirects=False)
    assert resp.status_code == 200, (
        f"expected 200, got {resp.status_code} (login gate still on?)")
    data = resp.get_json()
    assert data["ok"] is True
    assert data["label"] == "Toronto, Ontario"
    assert data["unit"] == "km"
    print("PASS: /geo/reverse works without login")


def test_label_us_includes_zip():
    addr = {"city": "New York", "state": "New York", "country": "United States",
            "country_code": "us", "postcode": "10014"}
    with mock.patch("urllib.request.urlopen", _stub_nominatim(addr)):
        label, cc, country = app._nominatim_reverse(40.73, -74.00)
    assert label == "New York, New York 10014", label
    assert cc == "US"
    assert country == "United States"
    print("PASS: US label reads 'City, State ZIP'")


def test_label_canada_city_region():
    addr = {"city": "Toronto", "state": "Ontario", "country": "Canada",
            "country_code": "ca", "postcode": "M5G 2C4"}
    with mock.patch("urllib.request.urlopen", _stub_nominatim(addr)):
        label, cc, country = app._nominatim_reverse(43.65, -79.38)
    assert label == "Toronto, Ontario", label   # no ZIP appended outside US
    assert cc == "CA"
    assert country == "Canada"
    print("PASS: non-US label reads 'City, Region'")


def _fake_result(nct, verdict):
    return {
        "trial": {"nctId": nct, "title": f"Study {nct}", "phase": "PHASE2",
                  "leadSponsor": "Acme", "source": "ctgov",
                  "briefSummary": "A study of things in adults."},
        "match": {"verdict": verdict, "score": 50, "rationale": "",
                  "met": [], "not_met": [], "unknown": []},
        "site": None, "site_str": "Acme Clinic, Toronto", "coordinator": None,
        "distance": 10.0, "unit": "km", "nearby": [], "nearby_total": 0,
        "other_count": 0, "other_regions": [], "pay": None, "support": None,
    }


def test_results_hide_probably_not_by_default():
    """The 'Probably not a fit' checkbox must render UNCHECKED, while a good
    match renders checked, and the init script applies the filter on load."""
    ctx = {
        "results": [_fake_result("NCT1", "likely_eligible"),
                    _fake_result("NCT2", "ineligible")],
        "search_id": "sid123", "applied": [], "condition": "Widgetitis",
        "location": "Toronto", "unit": "km", "q_condition": "Widgetitis",
        "q_intervention": "", "q_age": "", "q_sex": "", "q_about": "",
        "q_radius": 50, "q_lat": "", "q_lon": "", "q_cc": "",
    }
    with app.app.test_request_context("/"):
        app.app.preprocess_request()  # run before_request so g.user etc. are set
        html = app.render_template("patient_results.html", **ctx)

    assert 'class="f-fit" value="good" checked' in html, "good should be checked"
    assert 'class="f-fit" value="no" checked' not in html, \
        "'Probably not a fit' must NOT be checked by default"
    assert 'class="f-fit" value="no"' in html, "the 'no' checkbox should exist"
    assert 'id="prCondInput"' in html
    assert 'value="Widgetitis"' in html, "results should prefill the current condition"
    assert "\n  apply();" in html or "apply();" in html, \
        "init should apply the default filter on load"
    print("PASS: results auto-hide 'Probably not a fit' by default")


def test_find_trial_is_split_carousel():
    """/find-trial must match production: two-panel split, not a stacked
    marketing page. Regression for when the carousel CSS/JS was retired."""
    client = app.app.test_client()
    r = client.get("/find-trial")
    assert r.status_code == 200, r.status_code
    html = r.get_data(as_text=True)
    assert 'id="fyCarousel"' in html
    assert 'class="fy-grip"' in html
    assert "For research sites" in html
    assert "A real trial finder, directly on your website." in html
    assert "Find a trial that fits." in html
    assert "bmd-embed-frame" in html
    assert "Find a clinical trial at" in html
    assert "One snippet to embed" not in html
    assert "Horizontal focus carousel" in html
    assert "Start split." in html
    assert "Split carousel retired" not in html
    assert "Find your next trial match." not in html
    assert "We also have a trial finder" not in html
    assert "home-screen" in html
    assert "is-fy-split" in html
    print("PASS: /find-trial is the production split carousel")


def test_find_trial_book_a_call_uses_cal_link():
    """The sites-side Book a call pill must open Cal.com. The header lives in
    a Jinja macro imported without context, so cal_link has to be passed in."""
    client = app.app.test_client()
    html = client.get("/find-trial").get_data(as_text=True)
    assert 'class="pill-nav-cta"' in html
    assert 'href="' + app.CAL_LINK + '"' in html
    assert 'Book a call' in html
    print("PASS: /find-trial Book a call points at Cal.com")


def test_for_sites_legacy_url_redirects_home():
    """The footer used to 404 on /for-sites. That path must 301 to the hub."""
    from urllib.parse import urlparse
    client = app.app.test_client()
    r = client.get("/for-sites", follow_redirects=False)
    assert r.status_code == 301, f"/for-sites returned {r.status_code}"
    path = urlparse(r.headers.get("Location") or "").path
    assert path in ("/", ""), (
        f"/for-sites redirected to {r.headers.get('Location')}, expected /")
    print("PASS: /for-sites 301s to the sites homepage")


def main():
    tests = [
        test_geo_reverse_public_no_login,
        test_label_us_includes_zip,
        test_label_canada_city_region,
        test_results_hide_probably_not_by_default,
        test_find_trial_is_split_carousel,
        test_find_trial_book_a_call_uses_cal_link,
        test_for_sites_legacy_url_redirects_home,
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
    if os.path.exists(_TMP_DB):
        os.unlink(_TMP_DB)
    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed")


if __name__ == "__main__":
    main()
