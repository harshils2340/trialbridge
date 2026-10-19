"""SEO surface: study titles, robots, llms.txt, IndexNow, city pages, outages,
and the study sitemap.

Run: python test_seo.py
"""
import os
import tempfile
import urllib.error
from unittest import mock

os.environ["DB_PATH"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["SITE_DEMO"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["INDEXNOW"] = "0"

import app  # noqa: E402
import db  # noqa: E402

TRIAL = {
    "title": "A Research Study Investigating How Well the Medicine Zenagamtide Helps "
             "People With Excess Body Weight Lose Weight Compared to Semaglutide",
    "conditions": ["Overweight"],
    "interventions": [{"type": "DRUG", "name": "Zenagamtide"},
                      {"type": "DRUG", "name": "Semaglutide 2.4 mg"},
                      {"type": "DRUG", "name": "Placebo"}],
    "locations": [{"city": "Houston", "state": "Texas", "status": "RECRUITING"}],
    "minAge": "18 Years", "sex": "ALL",
}


def test_study_titles():
    facts = app._study_facts(TRIAL)
    seo = app._study_seo(TRIAL, "Overweight", {}, facts)
    assert seo["title"] == "Overweight clinical trial: Zenagamtide vs Semaglutide", seo
    assert len(seo["title"]) <= 75
    assert seo["heading"] == "Overweight study: Zenagamtide vs Semaglutide"
    assert seo["description"].endswith("Apply free on BridgeMD.")
    assert len(seo["description"]) <= 158 and "—" not in seo["description"]
    # Comparison arms and vague phrases never become the "treatment".
    t = dict(TRIAL, interventions=[{"type": "OTHER", "name": "Usual Medical Care"},
                                   {"type": "BEHAVIORAL", "name": "High frequency"}])
    assert app._seo_interventions(t) == ""
    # "Paid" only when the listing says participants are paid.
    paid = dict(TRIAL, briefSummary="Participants will receive $50 per visit.")
    assert app._study_seo(paid, "Overweight", {}, facts)["title"].startswith("Paid ")
    print("PASS: study titles lead with the condition, treatment and place")


def test_pay_words_need_participants():
    P = app._pay_likelihood
    assert P({"briefSummary": "patients with compensated cirrhosis"})["tier"] == "none"
    assert P({"briefSummary": "history of hepatic decompensation"})["tier"] == "none"
    assert P({"briefSummary": "time away from paid employment"})["tier"] == "none"
    assert P({"briefSummary": "you will be compensated for your time"})["tier"] != "none"
    assert not app._pays_participants({"criteria": "receiving workers' compensation for their knee injury"})
    assert app._pays_participants({"briefSummary": "Volunteers will receive a $25 gift card"})
    print("PASS: medical and job uses of pay words are not counted as payment")


def test_robots_llms_indexnow():
    c = app.app.test_client()
    robots = c.get("/robots.txt").data.decode()
    assert "User-agent: AhrefsBot\nDisallow: /" in robots
    assert "User-agent: *" in robots and "Allow: /" in robots
    for allowed in ("Googlebot", "Bingbot", "GPTBot", "ClaudeBot", "PerplexityBot"):
        assert allowed not in robots, allowed
    assert "Sitemap:" in robots
    llms = c.get("/llms.txt")
    assert llms.status_code == 200 and b"# BridgeMD" in llms.data
    key = c.get(f"/{app.INDEXNOW_KEY}.txt")
    assert key.status_code == 200 and key.data.decode() == app.INDEXNOW_KEY
    sent = []
    with app.app.app_context():
        db.init_db()
        n = app.indexnow_sweep(post=lambda p: sent.append(p) or True)
        assert n > 0 and sent[0]["key"] == app.INDEXNOW_KEY
        assert app.indexnow_sweep(post=lambda p: sent.append(p) or True) == 0
    print("PASS: robots blocks SEO crawlers only; llms.txt and IndexNow work")


def _trial(nct, *sites):
    """A recruiting trial with active sites at the given (lat, lon) points."""
    return {"nctId": nct, "title": f"Study {nct}", "briefSummary": "A study.",
            "overallStatus": "RECRUITING", "studyType": "INTERVENTIONAL",
            "phase": "PHASE2", "lastUpdated": "2026-09-30",
            "locations": [{"facility": f"Clinic {i}", "city": f"Town {i}",
                           "status": "RECRUITING", "lat": la, "lon": lo}
                          for i, (la, lo) in enumerate(sites)]}


TORONTO = (43.6532, -79.3832)
NEAR = (43.70, -79.40)        # about 5 km from downtown Toronto
METRO = (43.85, -79.20)       # about 16 mi
FAR = (43.4516, -80.4925)     # Kitchener, about 57 mi


def _city_page(trials, path="/trials/obesity/toronto-on"):
    app._SEO_TRIAL_CACHE.clear()
    with mock.patch.object(app.mt, "fetch_trials", lambda *a, **k: list(trials)), \
            mock.patch.object(app.mt, "count_trials", lambda *a, **k: len(trials)):
        return app.app.test_client().get(path)


def _city_row(slug, city_slug):
    with app.app.app_context():
        return db.get_db().execute(
            "SELECT is_local, trials FROM seo_city_page WHERE slug=? AND city_slug=?",
            (slug, city_slug)).fetchone()


def test_city_pages_lead_with_their_own_metro():
    with app.app.app_context():
        db.init_db()
    trials = [_trial("NCT00000001", FAR), _trial("NCT00000002"),
              _trial("NCT00000003", FAR, NEAR), _trial("NCT00000004", METRO)]
    r = _city_page(trials)
    html = r.data.decode()
    assert r.status_code == 200
    order = [html.index(n) for n in ("NCT00000003", "NCT00000004",
                                     "NCT00000001", "NCT00000002")]
    assert order == sorted(order), "nearest site first, no-site trials last"
    assert "Clinic 1, Town 1 · 5 km" in html, "nearest site and distance shown"
    assert 'content="noindex' not in html
    assert tuple(_city_row("obesity", "toronto-on")) == (1, 2)
    sm = app.app.test_client().get("/sitemap-cities.xml").data.decode()
    assert "/trials/obesity/toronto-on<" in sm

    # Only one trial in the metro: the page still helps a visitor but is a copy
    # of a neighbouring city's page as far as Google can tell. Noindex, and out
    # of the sitemap.
    r = _city_page([_trial("NCT00000005", NEAR), _trial("NCT00000006", FAR)],
                   path="/trials/obesity/hamilton-on")
    assert 'content="noindex' in r.data.decode()
    assert tuple(_city_row("obesity", "hamilton-on")) == (0, 0)
    sm = app.app.test_client().get("/sitemap-cities.xml").data.decode()
    assert "/trials/obesity/hamilton-on<" not in sm

    # The condition page links the indexable city page, not the other one.
    with mock.patch.object(app, "_seo_ctis_trials", lambda c, limit=6: []):
        html = _city_page(trials, path="/trials/obesity").data.decode()
    assert "/trials/obesity/toronto-on" in html
    assert "/trials/obesity/hamilton-on" not in html
    print("PASS: city pages sort by distance and index only real local pages")


def test_registry_outage_is_a_503_not_an_empty_page():
    with app.app.app_context():
        db.init_db()
    app._SEO_TRIAL_CACHE.clear()
    down = mock.patch.object(app.mt, "fetch_trials",
                             side_effect=urllib.error.URLError("timed out"))
    with down, mock.patch.object(app.mt, "count_trials", lambda *a, **k: 0):
        r = app.app.test_client().get("/trials/migraine/boston-ma")
        assert r.status_code == 503 and r.headers.get("Retry-After")
        assert _city_row("migraine", "boston-ma") is None, "a failure is never recorded"
        # An older list is served rather than an error.
        stale = [_trial("NCT00000007", (42.36, -71.06))]
        app._SEO_TRIAL_CACHE[("migraine", "boston, ma")] = (0, stale, True)
        r = app.app.test_client().get("/trials/migraine/boston-ma")
        assert r.status_code == 200 and b"NCT00000007" in r.data

    c = app.app.test_client()
    app._STUDY_CACHE.clear()
    with mock.patch.object(app.mt, "fetch_study",
                           side_effect=urllib.error.URLError("timed out")):
        r = c.get("/study/NCT01234567")
        assert r.status_code == 503 and r.headers.get("Retry-After")
    gone = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    with mock.patch.object(app.mt, "fetch_study", side_effect=gone):
        assert c.get("/study/NCT01234567").status_code == 404
    print("PASS: a registry outage answers 503; a missing study answers 404")


def test_study_sitemap_uses_registry_dates_and_drops_stale_trials():
    with app.app.app_context():
        db.init_db()
        db.upsert_seo_study("NCT00000021", "Fresh", "Gout", "2025-03-04")
        db.upsert_seo_study("NCT00000021", "Fresh", "Gout")   # date kept
        db.upsert_seo_study("NCT00000022", "Stale", "Gout", "2025-01-01")
        d = db.get_db()
        d.execute("UPDATE seo_study_index SET updated_at='2020-01-01 00:00' "
                  "WHERE nct='NCT00000022'")
        d.commit()
    sm = app.app.test_client().get("/sitemap-studies.xml").data.decode()
    assert "/study/NCT00000021</loc><lastmod>2025-03-04<" in sm
    assert "NCT00000022" not in sm
    print("PASS: study sitemap lastmod is the registry date; stale trials drop out")


if __name__ == "__main__":
    test_study_titles()
    test_pay_words_need_participants()
    test_robots_llms_indexnow()
    test_city_pages_lead_with_their_own_metro()
    test_registry_outage_is_a_503_not_an_empty_page()
    test_study_sitemap_uses_registry_dates_and_drops_stale_trials()
    print("All SEO tests passed")
