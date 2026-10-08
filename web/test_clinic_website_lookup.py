"""When a study site lists no email, find the clinic's own website and its
address, and never send an application anywhere that is not that clinic.

Hermetic: the search engines and every web page are faked.
Run: python test_clinic_website_lookup.py
"""
import os
import tempfile
import urllib.error

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP
os.environ["NO_LOGIN"] = "0"
os.environ["SITE_DEMO"] = "0"
os.environ["NOTIFY_LIVE"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["SECRET_KEY"] = "clinic-website-lookup-test"
os.environ["CLINIC_LOOKUP"] = "1"
os.environ.pop("SITE_NOTIFY_EMAIL", None)

import app as webapp  # noqa: E402
import clinic_lookup  # noqa: E402
import db  # noqa: E402

MILESTONE = {"facility": "Milestone Research", "city": "London",
             "state": "Ontario", "country": "Canada", "zip": "N5W 6A2",
             "contacts": []}
CONDITIONS = ["Overweight", "Obesity", "Weight loss"]

# A page builder's home page: megabytes of inline CSS before the menu.
_PADDING = "<style>" + ("." * 1_600_000) + "</style>"
_NAV = (
    '<a href="https://milestoneresearch.ca/">Home</a>'
    '<a href="https://milestoneresearch.ca/active-research-studies/">Active Studies</a>'
    '<a href="https://milestoneresearch.ca/research-team/">Research Team</a>'
    '<a href="https://milestoneresearch.ca/partners/">Partners</a>'
    '<a href="https://milestoneresearch.ca/contact-us/">Contact Us</a>'
)


def _milestone_pages():
    base = "https://milestoneresearch.ca"
    return {
        base + "/": (
            "<html><head><title>Home - Milestone Research</title></head><body>"
            + _PADDING + _NAV
            + "<p>Milestone Research, a modern clinical research facility "
              "located in London, Ontario.</p></body></html>"),
        base + "/contact-us/": (
            "<title>Contact Us - Milestone Research</title>" + _NAV
            + "<p>Milestone Research 295 Saskatoon St, London, ON N5W 6A2</p>"
            # A share-this-page button and a contact form, no address.
            + '<a href="mailto:A&#32;modern clinical research facility">'
              "Email</a><form>Your Name Your Email</form>"),
        base + "/active-research-studies/": (
            "<title>Active Studies - Milestone Research</title>" + _NAV
            + '<a href="/study/type-2-diabetes/">Type 2 Diabetes</a>'
              '<a href="/study/overweight-and-obesity/">Overweight and Obesity</a>'),
        base + "/study/type-2-diabetes/": (
            "<title>Diabetes - Milestone Research</title>" + _NAV
            + "<p>Diabetes study. Contact diabetes@milestoneresearch.ca</p>"),
        base + "/study/overweight-and-obesity/": (
            "<title>Obesity Research Study - Milestone Research</title>" + _NAV
            + "<p>Are you struggling with weight management or obesity?</p>"
              "<p>(519) 659-4040 <a>info@milestoneresearch.ca</a></p>"),
        base + "/partners/": (
            "<title>Partners - Milestone Research</title>" + _NAV
            + "<p>Trial Management Group john@tmginvestigators.com, "
              "Novo Nordisk clinicaltrials@novonordisk.com</p>"),
        base + "/research-team/": (
            "<title>Research Team</title>" + _NAV + "<p>Dr. Dzongowski</p>"),
    }


class FakeWeb:
    """Serves fixed pages, follows listed redirects, counts every request."""

    def __init__(self, pages, redirects=None, results=None, search_down=False):
        self.pages = pages
        self.redirects = redirects or {}
        self.results = results or []
        self.search_down = search_down
        self.fetched = []
        self.searches = []

    def fetch(self, url, timeout=8):
        self.fetched.append(url)
        url = self.redirects.get(url, url)
        if url not in self.pages:
            raise urllib.error.URLError("no such page")
        return url, self.pages[url]

    def search(self, query, deadline):
        self.searches.append(query)
        if self.search_down:
            raise urllib.error.URLError("search down")
        return list(self.results)

    def __enter__(self):
        self._orig = (clinic_lookup._fetch, clinic_lookup._SEARCHERS)
        clinic_lookup._fetch = self.fetch
        clinic_lookup._SEARCHERS = (("fake", self.search),)
        clinic_lookup._MEMO.clear()
        with webapp.app.app_context():
            db.get_db().execute("DELETE FROM clinic_lookup_cache")
            db.get_db().commit()
        return self

    def __exit__(self, *exc):
        clinic_lookup._fetch, clinic_lookup._SEARCHERS = self._orig
        clinic_lookup._MEMO.clear()


def _lookup(site=MILESTONE, sponsor="Novo Nordisk A/S", conditions=CONDITIONS):
    with webapp.app.app_context():
        return clinic_lookup.lookup_site_emails(
            site, sponsor=sponsor, conditions=conditions)


def test_finds_the_clinic_address_on_its_study_page():
    results = [
        "https://www.yelp.ca/biz/milestone-research-london",
        "https://joinastudy.ca/research-site/london-east-medical-centre/",
        "https://milestoneresearch.ca/",
        "https://milestoneresearch.ca/contact-us/",
    ]
    with FakeWeb(_milestone_pages(), results=results) as web:
        found = _lookup()
    assert [r["email"] for r in found] == ["info@milestoneresearch.ca"], found
    assert found[0]["page"] == (
        "https://milestoneresearch.ca/study/overweight-and-obesity/"), found
    assert found[0]["source"] == "lookup"
    assert not any("yelp" in u or "joinastudy" in u for u in web.fetched)
    print("PASS: the clinic's own address is found on its obesity study page")


def test_condition_picks_the_matching_study_page():
    with FakeWeb(_milestone_pages(),
                 results=["https://milestoneresearch.ca/"]):
        found = _lookup(conditions=["Type 2 Diabetes"])
    assert [r["email"] for r in found] == ["diabetes@milestoneresearch.ca"], found
    print("PASS: a diabetes study gets the clinic's diabetes page address")


def test_never_an_address_at_another_domain():
    pages = _milestone_pages()
    # Only the partners page lists any addresses: a site network's and the
    # sponsor's. Neither is this clinic's.
    for url in list(pages):
        if "study/" in url:
            pages[url] = pages[url].replace("info@milestoneresearch.ca", "").replace(
                "diabetes@milestoneresearch.ca", "")
    with FakeWeb(pages, results=["https://milestoneresearch.ca/"]):
        found = _lookup()
    assert found == [], found
    print("PASS: partner and sponsor addresses on a clinic's site are never used")


def test_a_namesake_site_in_another_city_is_rejected():
    namesake = {
        "https://www.milestone.com/": (
            "<title>Milestone Systems</title><p>Milestone Systems, video "
            "software, Copenhagen, Denmark. sales@milestone.com "
            "research@milestone.com</p>"),
    }
    with FakeWeb(namesake, redirects={"https://milestone.com/":
                                      "https://www.milestone.com/"},
                 results=["https://www.milestone.com/"]):
        found = _lookup()
    assert found == [], found
    print("PASS: a same-name company that is not in London, Ontario is rejected")


def test_directories_that_name_the_clinic_are_not_its_website():
    pages = {
        "https://joinastudy.ca/": (
            "<title>JoinAStudy</title><p>Milestone Research, London, Ontario. "
            "info@joinastudy.ca</p>"),
        "https://www.yelp.ca/": (
            "<p>Milestone Research London Ontario hello@yelp.ca</p>"),
        "https://research.com/": (
            "<p>Milestone Research London Ontario contact@research.com</p>"),
    }
    results = ["https://joinastudy.ca/", "https://www.yelp.ca/",
               "https://research.com/"]
    with FakeWeb(pages, results=results) as web:
        found = _lookup()
    assert found == [], found
    assert not any("joinastudy" in u or "yelp" in u for u in web.fetched), web.fetched
    assert "https://research.com/" not in web.fetched, web.fetched
    print("PASS: listings, directories and lookalike domains are never treated as the clinic")


def test_hospital_complaints_desk_is_never_the_study_team():
    lhsc = {"facility": "London Health Sciences Centre", "city": "London",
            "state": "Ontario", "country": "Canada", "contacts": []}
    nav = ('<a href="/patients-visitors/patient-relations">Patient Relations</a>'
           '<a href="/foundation">Donate</a>')
    pages = {
        "https://www.lhsc.on.ca/": (
            "<title>LHSC</title>" + nav + "<p>London Health Sciences Centre, "
            "London, Ontario</p>"),
        "https://www.lhsc.on.ca/patients-visitors/patient-relations": (
            "<title>Patient Relations</title><p>patientrelations@lhsc.on.ca "
            "feedback@lhsc.on.ca</p>"),
        "https://www.lhsc.on.ca/foundation": (
            "<title>Foundation</title><p>giving@lhsc.on.ca</p>"),
    }
    with FakeWeb(pages, results=["https://www.lhsc.on.ca/"]):
        assert _lookup(site=lhsc, sponsor="", conditions=["Asthma"]) == []
    pages["https://www.lhsc.on.ca/"] += (
        '<a href="/research/clinical-trials">Clinical Trials</a>')
    pages["https://www.lhsc.on.ca/research/clinical-trials"] = (
        "<title>Clinical Trials</title><p>Contact clinicaltrials@lhsc.on.ca</p>")
    with FakeWeb(pages, results=["https://www.lhsc.on.ca/"]):
        found = _lookup(site=lhsc, sponsor="", conditions=["Asthma"])
    assert [r["email"] for r in found] == ["clinicaltrials@lhsc.on.ca"], found
    print("PASS: a hospital's complaints and donations desks are never used; its trials inbox is")


def test_junk_search_results_do_not_crowd_out_the_clinics_own_domain():
    sundance = {"facility": "Sundance Clinical Research", "city": "St Louis",
                "state": "Missouri", "country": "United States", "contacts": []}
    film = ('<title>Sundance Institute</title><p>Sundance Institute, Park City, '
            'Utah. info@sundance.org</p>'
            + "".join(f'<a href="/program-{i}">Research program {i}</a>'
                      for i in range(20)))
    pages = {
        "https://www.sundance.org/": film,
        "https://sundancecollege.com/": "<p>Sundance College, Calgary</p>",
        "https://sundanceskishop.com/": "<p>Ski shop</p>",
        "https://www.sundanceclinicalresearch.com/": (
            '<title>Sundance Clinical Research</title><a href="/contact">Contact</a>'
            '<script type="application/ld+json">{"addressLocality":"St Louis",'
            '"addressRegion":"MO","postalCode":"63141"}</script>'),
        "https://www.sundanceclinicalresearch.com/contact": (
            "<title>Contact</title><p>Sundance Clinical Research, St Louis, MO "
            "info@sundanceclinicalresearch.com studies@sundanceclinicalresearch.com</p>"),
    }
    for i in range(20):
        pages[f"https://www.sundance.org/program-{i}"] = "<p>Film</p>"
    redirects = {"https://sundanceclinicalresearch.com/":
                 "https://www.sundanceclinicalresearch.com/",
                 "https://sundance.org/": "https://www.sundance.org/"}
    results = ["https://www.sundance.org/", "https://sundancecollege.com/",
               "https://sundanceskishop.com/"]
    with FakeWeb(pages, redirects=redirects, results=results) as web:
        found = _lookup(site=sundance, sponsor="", conditions=["Type 2 Diabetes"])
    assert [r["email"] for r in found] == [
        "studies@sundanceclinicalresearch.com"], (found, web.fetched)
    film_pages = [u for u in web.fetched if "sundance.org/program" in u]
    assert len(film_pages) <= 2, film_pages
    print("PASS: the clinic's own-name domain is tried before look-alike search "
          "results, and its studies inbox beats info@")


def test_unnamed_sites_are_not_searched():
    with FakeWeb({}, results=["https://milestoneresearch.ca/"]) as web:
        for name in ("Research Site", "Novo Nordisk Investigational Site",
                     "Investigational Site Number : 1240045",
                     "Local Institution - 0123"):
            site = dict(MILESTONE, facility=name)
            assert _lookup(site=site) == [], name
    assert web.searches == [] and web.fetched == [], (web.searches, web.fetched)
    print("PASS: anonymous sponsor site labels are not searched")


def test_sponsor_site_code_is_stripped_from_a_real_name():
    assert clinic_lookup._clean_facility(
        "Clinical Research Institute, Merz Investigational Site #0010487"
    ) == "Clinical Research Institute"
    print("PASS: a sponsor's site code after a clinic's name is dropped for the search")


def test_hidden_addresses_are_decoded():
    entity = "".join(f"&#{ord(c)};" for c in "info@clinic.ca")
    key = 0x42
    cf = f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in "study@clinic.ca")
    text = (f"<p>{entity}</p><a data-cfemail=\"{cf}\">[email protected]</a>"
            "<p>recruit [at] clinic [dot] ca</p>")
    found = clinic_lookup._emails_in(text)
    for want in ("info@clinic.ca", "study@clinic.ca", "recruit@clinic.ca"):
        assert want in found, (want, found)
    print("PASS: entity-encoded, Cloudflare-protected and [at]/[dot] addresses are read")


def test_domain_match_rules():
    m = clinic_lookup._domain_matches
    assert m("milestoneresearch.ca", "Milestone Research", "London", "Ontario")
    assert m("www.azresearchcenter.com", "Arizona Research Center", "Phoenix", "Arizona")
    assert m("swedish.org", "Swedish Medical Center", "Seattle", "Washington")
    assert m("www.lhsc.on.ca", "London Health Sciences Centre", "London", "Ontario")
    assert m("clinicalresearchassociates.com", "Clinical Research Associates",
             "Nashville", "Tennessee")
    assert not m("research.com", "Milestone Research", "London", "Ontario")
    assert not m("londonclinic.co.uk", "London Health Sciences Centre", "London", "Ontario")
    assert not m("clinical.com", "Clinical Research Associates", "Nashville", "Tennessee")
    print("PASS: a domain must carry the clinic's own name, not a generic word or its city")


def test_search_down_falls_back_to_name_guesses_and_retries_later():
    pages = _milestone_pages()
    with FakeWeb(pages, redirects={
            "https://milestoneresearch.com/": "https://milestoneresearch.ca/"},
            search_down=True) as web:
        found = _lookup()
        assert [r["email"] for r in found] == ["info@milestoneresearch.ca"], found
    # Nothing at all reachable and no search: nothing is cached, so the next
    # try (the boot backfill) looks again instead of trusting a miss.
    with FakeWeb({}, search_down=True):
        assert _lookup() == []
        with webapp.app.app_context():
            n = db.get_db().execute(
                "SELECT COUNT(*) FROM clinic_lookup_cache").fetchone()[0]
        assert n == 0, n
    print("PASS: with search down, name guesses still find it; a blind miss is not cached")


def test_a_found_address_is_cached_across_workers():
    with FakeWeb(_milestone_pages(),
                 results=["https://milestoneresearch.ca/"]) as web:
        first = _lookup()
        n = len(web.fetched)
        clinic_lookup._MEMO.clear()  # a fresh gunicorn worker
        again = _lookup()
        assert again == first and len(web.fetched) == n, (n, web.fetched)
        with webapp.app.app_context():
            cached = clinic_lookup.cached_site_emails(
                MILESTONE, sponsor="Novo Nordisk A/S", conditions=CONDITIONS)
        assert cached == first
    print("PASS: one lookup per clinic; later sends and the owner page reuse it")


# --------------------------------------------------------------------------- #
# Sending: a handoff that reached only the sponsor gets the clinic's address
# --------------------------------------------------------------------------- #
def _amaze_trial():
    return {
        "nctId": "NCT07339423",
        "title": "AMAZE 1",
        "leadSponsor": "Novo Nordisk A/S",
        "conditions": ["Overweight", "Obesity"],
        "centralContacts": [{"name": "Novo Nordisk", "role": "CONTACT",
                             "email": "clinicaltrials@novonordisk.com",
                             "phone": "(+1) 866-867-7178"}],
        "locations": [dict(MILESTONE, status="RECRUITING",
                           lat=42.98, lon=-81.23)],
    }


def _sponsor_only_lead(created_at=None, email="ken@x.test"):
    with webapp.app.app_context():
        tok = db.create_lead({
            "applicant_token": "t-" + email, "nct": "NCT07339423",
            "title": "AMAZE 1", "condition": "Weight loss",
            "site": "Milestone Research, London, Ontario, Canada",
            "name": "Ken Test", "email": email, "consent": 1,
            "source": "web"})
        lead = db.get_lead_by_token(tok)
        db.record_clinic_notify(lead["id"], [{
            "email": "clinicaltrials@novonordisk.com",
            "facility": "Study contact", "source": "central",
            "subject": "Weight loss study applicant"}])
        if created_at:
            db.get_db().execute("UPDATE leads SET created_at = ? WHERE id = ?",
                                (created_at, lead["id"]))
            db.get_db().commit()
        return tok, lead["id"]


class Patched:
    def __init__(self, found):
        self.found = found
        self.sent = []

    def __enter__(self):
        self._orig = (webapp._notify, webapp.notifications_ready,
                      webapp._get_study, clinic_lookup.lookup_site_emails)
        webapp._notify = lambda to, subject, body: self.sent.append(
            (to, subject, body)) or True
        webapp.notifications_ready = lambda: True
        webapp._get_study = lambda nct: _amaze_trial()
        clinic_lookup.lookup_site_emails = (
            lambda site, sponsor="", conditions=(), **k: [dict(r) for r in self.found])
        return self

    def __exit__(self, *exc):
        (webapp._notify, webapp.notifications_ready, webapp._get_study,
         clinic_lookup.lookup_site_emails) = self._orig


FOUND = [{"email": "info@milestoneresearch.ca", "facility": "Milestone Research",
          "city": "London", "source": "lookup", "role": "LOOKUP",
          "page": "https://milestoneresearch.ca/study/overweight-and-obesity/"}]


def test_sponsor_only_handoff_goes_to_the_clinic_once():
    tok, lead_id = _sponsor_only_lead()
    with Patched(FOUND) as p, webapp.app.app_context():
        webapp._backfill_clinic_notify_existing()
        to = [t for t, _, _ in p.sent]
        # The clinic gets the application; the sponsor is not mailed again.
        assert to.count("info@milestoneresearch.ca") == 1, to
        assert "clinicaltrials@novonordisk.com" not in to, to
        # The owner is told where it went and where the address came from.
        notes = [(s, b) for t, s, b in p.sent if t == webapp.OWNER_NOTIFY_EMAIL]
        assert len(notes) == 1, p.sent
        subject, body = notes[0]
        assert "Milestone Research" in subject, subject
        assert "clinicaltrials@novonordisk.com" in body
        assert "overweight-and-obesity" in body
        assert "—" not in body and "—" not in subject
        recs = db.lead_clinic_notify(db.get_lead(lead_id))
        emails = [r["email"] for r in recs]
        assert emails == ["clinicaltrials@novonordisk.com",
                          "info@milestoneresearch.ca"], emails
        assert recs[1]["page"].endswith("/overweight-and-obesity/")
        assert db.lead_clinic_notify_reached_site(db.get_lead(lead_id))
        # The clinic copy is the application itself, same as the sponsor got.
        clinic_body = next(b for t, s, b in p.sent
                           if t == "info@milestoneresearch.ca")
        assert "Ken Test" in clinic_body and "ken@x.test" in clinic_body
        p.sent.clear()
        webapp._backfill_clinic_notify_existing()
        assert p.sent == [], p.sent
    print("PASS: a sponsor-only handoff is sent to the clinic once, and the owner is told")


def test_no_clinic_found_sends_nothing_and_alerts_nobody():
    tok, lead_id = _sponsor_only_lead(email="nofind@x.test")
    with Patched([]) as p, webapp.app.app_context():
        webapp._backfill_clinic_notify_existing()
        assert p.sent == [], p.sent
        recs = db.lead_clinic_notify(db.get_lead(lead_id))
        assert [r["email"] for r in recs] == ["clinicaltrials@novonordisk.com"]
    print("PASS: when no clinic address is found the sponsor-only handoff is left as is")


def test_older_sponsor_only_handoffs_are_left_alone():
    tok, lead_id = _sponsor_only_lead(created_at="2026-09-20 10:00",
                                      email="old@x.test")
    with Patched(FOUND) as p, webapp.app.app_context():
        webapp._backfill_clinic_notify_existing()
        assert not any("old@x.test" in b for _, _, b in p.sent), p.sent
        recs = db.lead_clinic_notify(db.get_lead(lead_id))
        assert [r["email"] for r in recs] == ["clinicaltrials@novonordisk.com"]
    print("PASS: applications from before the website lookup shipped are not re-sent")


def test_new_apply_sends_clinic_and_sponsor_together():
    with webapp.app.app_context():
        tok = db.create_lead({
            "applicant_token": "t-new", "nct": "NCT07339423", "title": "AMAZE 1",
            "condition": "Weight loss",
            "site": "Milestone Research, London, Ontario, Canada",
            "name": "New Person", "email": "new@x.test", "consent": 1,
            "source": "web"})
    with Patched(FOUND) as p, webapp.app.app_context():
        emails = webapp._notify_site_new_candidate_sync(
            tok, trial=_amaze_trial(), lat=42.98, lon=-81.23)
        assert emails == ["info@milestoneresearch.ca",
                          "clinicaltrials@novonordisk.com"], emails
        lead = db.get_lead_by_token(tok)
        assert db.lead_clinic_notify_reached_site(lead)
    print("PASS: a new application goes to the clinic first and the sponsor's contact too")


def main():
    tests = [
        test_finds_the_clinic_address_on_its_study_page,
        test_condition_picks_the_matching_study_page,
        test_never_an_address_at_another_domain,
        test_a_namesake_site_in_another_city_is_rejected,
        test_directories_that_name_the_clinic_are_not_its_website,
        test_hospital_complaints_desk_is_never_the_study_team,
        test_junk_search_results_do_not_crowd_out_the_clinics_own_domain,
        test_unnamed_sites_are_not_searched,
        test_sponsor_site_code_is_stripped_from_a_real_name,
        test_hidden_addresses_are_decoded,
        test_domain_match_rules,
        test_search_down_falls_back_to_name_guesses_and_retries_later,
        test_a_found_address_is_cached_across_workers,
        test_sponsor_only_handoff_goes_to_the_clinic_once,
        test_no_clinic_found_sends_nothing_and_alerts_nobody,
        test_older_sponsor_only_handoffs_are_left_alone,
        test_new_apply_sends_clinic_and_sponsor_together,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"FAIL: {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        raise SystemExit(1)
    print(f"\nAll {len(tests)} clinic website lookup tests passed")


if __name__ == "__main__":
    main()
