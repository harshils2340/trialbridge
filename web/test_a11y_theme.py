"""Tests for the accessibility + responsiveness + dark-mode work.

Covers:
1. Dark mode is wired into BOTH base templates (no-flash script that reads a
   saved choice or the OS preference, plus a theme toggle control).
2. Basic a11y landmarks are present: a skip link and a #main target.
3. Colorblind-safe cues: fit badges carry DISTINCT icons per verdict (not just
   red/green), and the CSS gives the match-quality dots distinct shapes.
4. The clinician sidebar wraps on mobile (the rule that stopped it running
   ~300px off-screen) and tables/tab-bars scroll instead of overflowing.

Run: python test_a11y_theme.py   (offline)
"""
import os
import re
import tempfile

os.environ.setdefault("DB_PATH", tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)
os.environ.setdefault("NO_LOGIN", "1")
os.environ.setdefault("ALERTS_BACKGROUND", "0")
os.environ.setdefault("REMINDERS_BACKGROUND", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")

import app  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(HERE, "templates")
CSS = os.path.join(HERE, "static", "style.css")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_theme_wired_in_both_bases():
    for name in ("public_base.html", "base.html"):
        html = _read(TPL, name)
        assert "prefers-color-scheme: dark" in html, f"{name}: no OS-pref fallback"
        assert "localStorage.getItem('theme')" in html, f"{name}: no saved-choice read"
        assert "data-theme" in html, f"{name}: never sets data-theme"
        assert 'id="themeToggle"' in html, f"{name}: no theme toggle control"
        assert 'aria-label="Switch between light and dark theme"' in html, \
            f"{name}: toggle missing accessible name"
    print("PASS: dark-mode toggle + no-flash script wired into both base templates")


def test_a11y_landmarks():
    for name in ("public_base.html", "base.html"):
        html = _read(TPL, name)
        assert 'class="skip-link"' in html and 'href="#main"' in html, \
            f"{name}: missing skip link"
        assert 'id="main"' in html, f"{name}: missing #main landmark"
    print("PASS: skip link + #main landmark present in both bases")


def test_css_has_dark_theme_and_focus():
    css = _read(CSS)
    assert '[data-theme="dark"]' in css, "CSS has no dark theme block"
    assert "--bg: #0f1115" in css, "dark tokens not defined"
    assert ":focus-visible" in css, "no visible keyboard focus styles"
    assert ".skip-link" in css and ".theme-toggle" in css, "a11y component styles missing"
    print("PASS: CSS defines dark theme, focus-visible, skip link + toggle styles")


def test_css_colorblind_shapes():
    css = _read(CSS)
    # distinct shapes for the three match-quality dots (not color-only)
    assert ".dot-maybe { border-radius: 2px; }" in css, "maybe dot not a square"
    assert "rotate(45deg)" in css, "no dot uses no diamond shape"
    print("PASS: match-quality dots use distinct shapes (circle/square/diamond)")


def test_css_mobile_sidebar_and_scroll():
    css = _read(CSS)
    assert ".sidebar { flex-wrap: wrap;" in css, "sidebar won't wrap on mobile"
    assert ".inv-table" in css, "invite table not in mobile scroll rule"
    assert ".rev-tabs { overflow-x: auto;" in css, "tab bar not scrollable on mobile"
    print("PASS: mobile sidebar wraps; wide tables/tab-bars scroll")


def test_visual_system_is_single_source():
    css = _read(CSS)
    bases = _read(TPL, "base.html") + _read(TPL, "public_base.html")
    assert css.count(":root {") == 1, "design tokens must have one :root source"
    for token in ("--text-xs", "--text-md", "--duration-base", "--ease-standard"):
        assert token in css, f"missing shared visual token {token}"
    assert "Manrope" not in css + bases and "Newsreader" not in css + bases, \
        "multiple UI typefaces reintroduced"
    assert "brand-blue" not in css + bases, "split blue/teal brand reintroduced"
    assert "prefers-reduced-motion: reduce" in css
    assert "data-ui-reveal" in bases
    print("PASS: typography, brand, and motion use one shared system")


def test_demo_tour_starts_on_canonical_inbox():
    tour = _read(TPL, "_demo_tour.html")
    inbox = _read(TPL, "marketing_hub.html")
    assert "path: '/app/home'" not in tour, "tour still targets retired dashboard"
    assert "path: '/app/inbox'" in tour
    assert 'data-demo-tour-target="inbox"' in inbox
    print("PASS: demo tour starts on the canonical inbox")


def _fake_result(nct, verdict):
    return {
        "trial": {"nctId": nct, "title": f"Study {nct}", "phase": "PHASE2",
                  "leadSponsor": "Acme", "source": "ctgov",
                  "briefSummary": "A study."},
        "match": {"verdict": verdict, "score": 50, "rationale": "",
                  "met": [], "not_met": [], "unknown": []},
        "site": None, "site_str": "Acme Clinic", "coordinator": None,
        "distance": 10.0, "unit": "km", "nearby": [], "nearby_total": 0,
        "other_count": 0, "other_regions": [], "pay": None, "support": None,
    }


def test_fit_badges_have_distinct_icons():
    """good -> check (polyline '20 6 ...'), not-a-fit -> x (two crossed lines).
    A colorblind user must be able to tell them apart without relying on hue."""
    ctx = {
        "results": [_fake_result("NCT1", "likely_eligible"),
                    _fake_result("NCT2", "possible"),
                    _fake_result("NCT3", "ineligible")],
        "search_id": "sid", "applied": [], "condition": "X", "location": "Toronto",
        "unit": "km", "q_condition": "X", "q_intervention": "", "q_age": "",
        "q_sex": "", "q_about": "", "q_radius": 50, "q_lat": "", "q_lon": "",
        "q_cc": "",
    }
    with app.app.test_request_context("/"):
        app.app.preprocess_request()
        html = app.render_template("patient_results.html", **ctx)
    # the "x" icon (crossed lines) must appear for the ineligible verdict badge
    assert re.search(r'fit fit-no"[^>]*>\s*<svg[^>]*><line x1="18" y1="6"', html), \
        "'Probably not a fit' badge is missing its distinct X icon"
    # the good badge keeps the check (polyline)
    assert re.search(r'fit fit-good"[^>]*>\s*<svg[^>]*><polyline points="20 6', html), \
        "good badge missing its check icon"
    print("PASS: fit badges use distinct icons per verdict (colorblind-safe)")


def main():
    import sys
    tests = [
        test_theme_wired_in_both_bases,
        test_a11y_landmarks,
        test_css_has_dark_theme_and_focus,
        test_css_colorblind_shapes,
        test_css_mobile_sidebar_and_scroll,
        test_visual_system_is_single_source,
        test_demo_tour_starts_on_canonical_inbox,
        test_fit_badges_have_distinct_icons,
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
