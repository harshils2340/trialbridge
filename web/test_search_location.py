"""A located search always stays near the person, whatever they typed.

Background: on the live site "My breast cancer came back after chemo and
radiation" near Toronto showed 31 trials 6 km away, while "breast cancer came
back" showed ~160 trials from California and China with no distances. Both
extracted the same condition. The difference was geocoding: when the geocoders
failed (no Google key, Nominatim throttling the server), find() carried on with
no coordinates, so the CT.gov query had no geo filter and nothing could be
ranked by distance. The query cache, keyed on the typed location, then replayed
that worldwide set for every repeat, which made it look tied to the wording.

Run: python test_search_location.py   (offline, no LLM key needed)
"""
import os
import re
import tempfile
from unittest import mock

os.environ["DB_PATH"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["SITE_DEMO"] = "0"
os.environ["NOTIFY_LIVE"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["SECRET_KEY"] = "search-location-test"
for _k in ("LLM_API_KEY", "OPENAI_API_KEY", "GOOGLE_MAPS_API_KEY"):
    os.environ.pop(_k, None)

import app  # noqa: E402

CSRF = "search-location-csrf"
LONG = "My breast cancer came back after chemo and radiation"
SHORT = "breast cancer came back"


def _trial(nct, facility, city, country, lat, lon):
    return {"nctId": nct, "title": f"Breast cancer study at {facility}",
            "studyType": "INTERVENTIONAL", "overallStatus": "RECRUITING",
            "conditions": ["Breast Cancer"], "sex": "ALL",
            "locations": [{"facility": facility, "city": city,
                           "country": country, "status": "RECRUITING",
                           "lat": lat, "lon": lon, "contacts": []}]}


NEAR = [_trial("NCT90000001", "Sunnybrook", "Toronto", "Canada", 43.7220, -79.3760),
        _trial("NCT90000002", "Princess Margaret", "Toronto", "Canada", 43.6580, -79.3900)]
FAR = [_trial("NCT90000003", "City of Hope", "Duarte", "United States", 34.1295, -117.9720),
       _trial("NCT90000004", "Peking University Shenzhen Hospital", "Shenzhen",
              "China", 22.5590, 114.0560)]
GEO_CALLS = []


def _fake_fetch(condition, max_n=300, geo=None, intervention="", term="", **kw):
    """CT.gov stand-in: honours filter.geo like the real API does."""
    GEO_CALLS.append(geo)
    m = re.match(r"distance\(([-\d.]+),([-\d.]+),(\d+)(km|mi)\)", geo or "")
    if not m:
        return [dict(t) for t in NEAR + FAR]
    lat, lon, radius, unit = float(m[1]), float(m[2]), int(m[3]), m[4]
    return [dict(t) for t in NEAR + FAR
            if any(app.haversine(lat, lon, s["lat"], s["lon"], unit) <= radius
                   for s in t["locations"])]


def _chat(system, user, retries=2):
    # Both wordings extract the same condition, as they did on the live site.
    # Anything else (the homepage's trending refresh) gets an empty answer.
    if system.startswith("You map a patient's"):
        return "Recurrent breast cancer"
    return '{"conditions": [], "drugs": []}'


def _offline():
    """No network, no LLM key; every geocoder is down unless a test says so."""
    return [
        mock.patch("urllib.request.urlopen",
                   side_effect=AssertionError("test must not use the network")),
        mock.patch.object(app.mt, "LLM_API_KEY", "stub"),
        mock.patch.object(app.mt, "llm_chat", _chat),
        mock.patch.object(app.mt, "llm_match", lambda note, t, retries=3: {
            "verdict": "possible", "score": 50, "rationale": "Stub read.",
            "met": [], "not_met": [], "unknown": []}),
        mock.patch.object(app.mt, "fetch_trials", _fake_fetch),
        mock.patch.object(app.codes, "condition_tokens", lambda term: set()),
        mock.patch.object(app, "_llm_search_budget_ok", lambda: True),
        mock.patch.object(app, "ISRCTN_ENABLED", False),
        mock.patch.object(app, "_google_geocode", lambda p: None),
        mock.patch.object(app, "_nominatim", lambda q: None),
    ]


def _reset():
    GEO_CALLS.clear()
    app._GEOCODE_CACHE.clear()
    app._SEARCH_QUERY_CACHE.clear()


def _search(q, location="Toronto, Ontario"):
    c = app.app.test_client()
    with c.session_transaction() as s:
        s[app.CSRF_SESSION_KEY] = CSRF
    r = c.post("/find", data={"condition": q, "freeform": "1",
                              "location": location, "radius": "50",
                              "_csrf_token": CSRF})
    return c, r


def test_short_and_long_wording_both_stay_near():
    patches = _offline()
    for p in patches:
        p.start()
    try:
        seen = {}
        for q in (LONG, SHORT, "metastatic breast cancer", "stage 4 breast cancer"):
            _reset()
            c, r = _search(q)
            loc = r.headers.get("Location", "")
            assert r.status_code == 302 and loc.startswith("/find/"), (q, loc)
            assert GEO_CALLS and all(g == "distance(43.6532,-79.3832,50km)"
                                     for g in GEO_CALLS), (q, GEO_CALLS)
            results = app._load_search(loc.rsplit("/", 1)[1])["results"]
            ncts = sorted(x["trial"]["nctId"] for x in results)
            assert ncts == ["NCT90000001", "NCT90000002"], (q, ncts)
            assert all(x["distance"] is not None and x["distance"] <= 50
                       for x in results), q
            page = c.get(loc).get_data(as_text=True)
            cards = re.findall(r'<article class="pr-card".*?</article>', page, re.S)
            assert len(cards) == 2 and all("km away" in x for x in cards), q
            assert "Duarte" not in page and "Shenzhen" not in page, q
            seen[q] = ncts
        assert len({tuple(v) for v in seen.values()}) == 1, seen
    finally:
        for p in patches:
            p.stop()
    print("PASS: short and long wording search the same 50 km near Toronto")


def test_unplaceable_location_is_not_searched_worldwide():
    patches = _offline()
    for p in patches:
        p.start()
    try:
        _reset()
        c, r = _search(SHORT, location="Nowhereville")
        loc = r.headers.get("Location", "")
        assert r.status_code == 302 and not loc.startswith("/find/"), loc
        assert "location=Nowhereville" in loc and "condition=breast" in loc, loc
        assert GEO_CALLS == [], GEO_CALLS          # nothing searched worldwide
        assert not app._SEARCH_QUERY_CACHE         # and nothing cached
        page = c.get(loc).get_data(as_text=True)
        assert "find “Nowhereville” on the map" in page
    finally:
        for p in patches:
            p.stop()
    print("PASS: a place we can't find asks again instead of going worldwide")


def test_geocoder_outage_is_not_replayed_from_cache():
    patches = _offline()
    for p in patches:
        p.start()
    try:
        _reset()
        _, r = _search(SHORT, location="Mississauga, Ontario")
        assert not r.headers.get("Location", "").startswith("/find/")
        # The geocoder comes back: the same search must now search near.
        with mock.patch.object(app, "_nominatim",
                               lambda q: (43.5890, -79.6441, "CA")):
            _, r = _search(SHORT, location="Mississauga, Ontario")
        loc = r.headers.get("Location", "")
        assert loc.startswith("/find/"), loc
        results = app._load_search(loc.rsplit("/", 1)[1])["results"]
        assert results and all(x["distance"] is not None and x["distance"] <= 50
                               for x in results)
        assert GEO_CALLS[-1].startswith("distance(43.589,-79.6441,50km")
    finally:
        for p in patches:
            p.stop()
    print("PASS: a geocoder outage leaves no worldwide set behind in the cache")


def test_known_cities_when_geocoders_are_down():
    toronto = (43.6532, -79.3832, "CA")
    for typed in ("Toronto, Ontario", "toronto", "Toronto, ON",
                  "Toronto, Ontario, Canada", "  TORONTO,  ON  "):
        assert app._known_city(typed) == toronto, typed
    assert app._known_city("Montréal, Québec") == (45.5019, -73.5674, "CA")
    assert app._known_city("Boston, Massachusetts, USA") == (42.3601, -71.0589, "US")
    assert app._known_city("London, ON") == (42.9849, -81.2453, "CA")
    for typed in ("London", "Washington", "Springfield", "Toronto, Ohio", ""):
        assert app._known_city(typed) is None, typed
    for name in app.SEO_CITY_COORDS:            # every listed city resolves
        assert app._known_city(name), name
    print("PASS: the major cities still resolve when the geocoders are down")


def test_unplaced_result_never_outranks_a_nearby_trial():
    posted = {"nct": "BMD-POSTED-1", "title": "Breast cancer study posted by a clinic",
              "condition": "Breast cancer", "site_name": "A clinic",
              "location": "", "lat": None, "lon": None}
    patches = _offline() + [
        mock.patch.object(app.mt, "LLM_API_KEY", ""),
        mock.patch.object(app.db, "list_site_posted_studies",
                          lambda status="": [posted]),
    ]
    for p in patches:
        p.start()
    try:
        with app.app.test_request_context("/"):
            _, results = app.run_search("Breast cancer", "Breast cancer", "",
                                        False, (43.6532, -79.3832), 50, "km",
                                        assess=False)
    finally:
        for p in patches:
            p.stop()
    order = [x["trial"]["nctId"] for x in results]
    assert order[-1] == "BMD-POSTED-1", order
    assert all(x["distance"] is not None for x in results[:-1]), order
    print("PASS: a result with no known location ranks below nearby trials")


if __name__ == "__main__":
    with app.app.app_context():
        app.db.init_db()
    test_short_and_long_wording_both_stay_near()
    test_unplaceable_location_is_not_searched_worldwide()
    test_geocoder_outage_is_not_replayed_from_cache()
    test_known_cities_when_geocoders_are_down()
    test_unplaced_result_never_outranks_a_nearby_trial()
    print("All search location tests passed")
