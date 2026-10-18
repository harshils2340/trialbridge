"""A located search always stays near the person, whatever they typed.

Background: on the live site "My breast cancer came back after chemo and
radiation" near Toronto showed 31 trials 6 km away, while "breast cancer came
back" showed ~160 trials from California and China with no distances. Both
extracted the same condition. The difference was geocoding: when the geocoders
failed (no Google key, Nominatim throttling the server), find() carried on with
no coordinates, so the CT.gov query had no geo filter and nothing could be
ranked by distance. The query cache, keyed on the typed location, then replayed
that worldwide set for every repeat, which made it look tied to the wording.

Then, with the location fixed, "breast cancer came back" still showed one New
Jersey trial: the model's daily token budget was spent, so the condition read
failed and the fallback searched the raw words, found nothing nearby and
widened. A sentence with no AI must still search its condition near the person.

Run: python test_search_location.py   (offline, no LLM key needed)
"""
import io
import json
import os
import re
import tempfile
import time
import urllib.error
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


def _trial(nct, facility, city, country, lat, lon,
           conditions=("Breast Cancer", "Recurrent Breast Cancer")):
    return {"nctId": nct, "title": f"{conditions[0]} study at {facility}",
            "studyType": "INTERVENTIONAL", "overallStatus": "RECRUITING",
            "conditions": list(conditions), "sex": "ALL",
            "locations": [{"facility": facility, "city": city,
                           "country": country, "status": "RECRUITING",
                           "lat": lat, "lon": lon, "contacts": []}]}


NEAR = [_trial("NCT90000001", "Sunnybrook", "Toronto", "Canada", 43.7220, -79.3760),
        _trial("NCT90000002", "Princess Margaret", "Toronto", "Canada", 43.6580, -79.3900)]
FAR = [_trial("NCT90000003", "City of Hope", "Duarte", "United States", 34.1295, -117.9720),
       _trial("NCT90000004", "Peking University Shenzhen Hospital", "Shenzhen",
              "China", 22.5590, 114.0560)]
BACK = [_trial("NCT90000005", "Toronto Western", "Toronto", "Canada", 43.6536, -79.4054,
               conditions=("Low Back Pain",))]
CALLS = []   # (condition, term, geo) for every CT.gov fetch


def _words(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _fake_fetch(condition, max_n=300, geo=None, intervention="", term="", **kw):
    """CT.gov stand-in: a trial matches when its title and conditions hold
    every searched word, and filter.geo is honoured like the real API does."""
    CALLS.append((condition, term, geo))
    want = _words(condition) | _words(term)
    pool = [t for t in NEAR + FAR + BACK
            if want <= _words(" ".join([t["title"]] + t["conditions"]))]
    m = re.match(r"distance\(([-\d.]+),([-\d.]+),(\d+)(km|mi)\)", geo or "")
    if not m:
        return [dict(t) for t in pool]
    lat, lon, radius, unit = float(m[1]), float(m[2]), int(m[3]), m[4]
    return [dict(t) for t in pool
            if any(app.haversine(lat, lon, s["lat"], s["lon"], unit) <= radius
                   for s in t["locations"])]


def _chat(system, user, retries=2, urgent=False):
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
    CALLS.clear()
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
            assert CALLS and all(g == "distance(43.6532,-79.3832,50km)"
                                 for _, _, g in CALLS), (q, CALLS)
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
        for p in reversed(patches):
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
        assert CALLS == [], CALLS                  # nothing searched worldwide
        assert not app._SEARCH_QUERY_CACHE         # and nothing cached
        page = c.get(loc).get_data(as_text=True)
        assert "find “Nowhereville” on the map" in page
    finally:
        for p in reversed(patches):
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
        assert CALLS[-1][2].startswith("distance(43.589,-79.6441,50km")
    finally:
        for p in reversed(patches):
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
        for p in reversed(patches):
            p.stop()
    order = [x["trial"]["nctId"] for x in results]
    assert order[-1] == "BMD-POSTED-1", order
    assert all(x["distance"] is not None for x in results[:-1]), order
    print("PASS: a result with no known location ranks below nearby trials")


def test_describe_tries_without_the_ai():
    cases = {
        "breast cancer came back": ["recurrent breast cancer", "breast cancer"],
        LONG: ["recurrent breast cancer", "breast cancer"],
        "stage 4 breast cancer": ["metastatic breast cancer", "breast cancer"],
        "my breast cancer has spread to my bones":
            ["metastatic breast cancer", "breast cancer"],
        "migraines keep coming back": ["recurrent migraines", "migraines"],
        "chronic low back pain": ["chronic low back pain", "low back pain",
                                  "back pain"],
        "my son has type 1 diabetes": ["type 1 diabetes"],
        "3 years of knee arthritis": ["knee arthritis"],
    }
    for text, want in cases.items():
        got = app._describe_tries(text)
        assert got == want, (text, got)
    print("PASS: a sentence becomes its condition, most specific first")


def test_no_ai_sentence_still_searches_its_condition_nearby():
    def _down(system, user, retries=2, urgent=False):
        raise app.mt.LLMCoolingDown("every model is out of tokens")

    geo50 = "distance(43.6532,-79.3832,50km)"
    cases = {
        # The raw words match nothing; the condition does, inside 50 km.
        SHORT: ([("recurrent breast cancer", "", geo50)],
                ["NCT90000001", "NCT90000002"]),
        # No "recurrent back pain" trial: fall back to the plain condition.
        "my back pain came back": ([("recurrent back pain", "", geo50),
                                    ("back pain", "", geo50)], ["NCT90000005"]),
    }
    patches = _offline() + [mock.patch.object(app.mt, "llm_chat", _down)]
    for p in patches:
        p.start()
    try:
        for location in ("Toronto, Ontario", "Toronto"):
            for q, (want_calls, want_ncts) in cases.items():
                _reset()
                _, r = _search(q, location=location)
                loc = r.headers.get("Location", "")
                assert loc.startswith("/find/"), (q, loc)
                assert CALLS == want_calls, (q, CALLS)
                entry = app._load_search(loc.rsplit("/", 1)[1])
                assert not entry["ctx"]["widened"], q
                assert entry["ctx"]["condition"] == want_calls[-1][0], entry["ctx"]
                ncts = sorted(x["trial"]["nctId"] for x in entry["results"])
                assert ncts == want_ncts, (q, ncts)
                assert all(x["distance"] is not None and x["distance"] <= 50
                           for x in entry["results"]), q
    finally:
        for p in reversed(patches):
            p.stop()
    print("PASS: with no AI, a sentence searches its condition near the person")


def test_condition_read_still_tries_models_that_are_cooling_down():
    mt = app.mt
    sent = []

    class _Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def _urlopen(req, timeout=60):
        sent.append(json.loads(req.data)["model"])
        return _Resp(json.dumps({"choices": [{"message": {
            "content": "Recurrent breast cancer"}}]}).encode())

    def _429(message):
        body = json.dumps({"error": {"message": message}}).encode()
        return urllib.error.HTTPError("https://llm.test", 429, "Too Many Requests",
                                      {}, io.BytesIO(body))

    now = time.time()
    cooling = {"busy": now + 600, "gone": now + mt._DEAD_MODEL_SECONDS}
    with mock.patch.object(mt, "LLM_API_KEY", "test-key"), \
            mock.patch.object(mt, "LLM_MODELS", ["busy", "gone"]), \
            mock.patch.object(mt, "LLM_COOLDOWN_UNTIL", cooling), \
            mock.patch.object(mt.urllib.request, "urlopen", _urlopen):
        try:
            mt.llm_chat("sys", "user")
            raise AssertionError("an ordinary call should wait out the cooldown")
        except mt.LLMCoolingDown:
            pass
        assert sent == []
        # The condition read is short, so it is sent anyway; a model that
        # doesn't exist is still skipped.
        assert mt.llm_chat("sys", "user", urgent=True) == "Recurrent breast cancer"
        assert sent == ["busy"], sent
        # "Request too large" is about one long request, not a rate: shorter
        # calls still fit, so the model gets no cooldown.
        cooling.pop("busy")
        mt._note_429("busy", _429("Request too large for model `busy` on tokens "
                                  "per minute (TPM): Limit 6000, Requested 7200"))
        assert "busy" not in cooling
        # A short call's own short wait never shortens a long cooldown.
        cooling["busy"] = now + 600
        mt._note_429("busy", _429("Rate limit reached for model `busy` on tokens "
                                  "per day (TPD). Please try again in 5s."))
        assert cooling["busy"] >= now + 600
    print("PASS: the condition read is still sent while long calls cool down")


def test_cooling_models_give_the_light_read_not_an_error():
    tried = []

    def _match(note, t, retries=3):
        tried.append(t["nctId"])
        raise app.mt.LLMCoolingDown("provider asked us to back off on every model")

    cooling = {m: time.time() + 600 for m in app.mt.LLM_MODELS}
    patches = _offline() + [
        mock.patch.object(app.mt, "llm_match", _match),
        mock.patch.object(app.mt, "LLM_COOLDOWN_UNTIL", cooling),
    ]
    for p in patches:
        p.start()
    try:
        _reset()
        _, r = _search(LONG)        # the condition read (urgent) goes through
        loc = r.headers.get("Location", "")
        results = app._load_search(loc.rsplit("/", 1)[1])["results"]
    finally:
        for p in reversed(patches):
            p.stop()
    assert results and tried == [], tried
    assert not any("back off" in x["match"]["rationale"] for x in results), \
        [x["match"]["rationale"] for x in results]
    print("PASS: with every model cooling down, cards get the light read")


if __name__ == "__main__":
    with app.app.app_context():
        app.db.init_db()
    test_short_and_long_wording_both_stay_near()
    test_unplaceable_location_is_not_searched_worldwide()
    test_geocoder_outage_is_not_replayed_from_cache()
    test_known_cities_when_geocoders_are_down()
    test_unplaced_result_never_outranks_a_nearby_trial()
    test_describe_tries_without_the_ai()
    test_no_ai_sentence_still_searches_its_condition_nearby()
    test_condition_read_still_tries_models_that_are_cooling_down()
    test_cooling_models_give_the_light_read_not_an_error()
    print("All search location tests passed")
