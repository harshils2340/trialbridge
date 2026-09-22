"""On apply, email every public study address we can find (clinic, then sponsor).

Run: python test_clinic_notify.py
"""
import os
import tempfile

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP
os.environ["NO_LOGIN"] = "0"
os.environ["SITE_DEMO"] = "0"
os.environ["NOTIFY_LIVE"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["SECRET_KEY"] = "clinic-notify-test"
os.environ["CLINIC_LOOKUP"] = "0"
os.environ.pop("SITE_NOTIFY_EMAIL", None)

import app as webapp  # noqa: E402
import mailer  # noqa: E402


def _trial():
    return {
        "nctId": "NCT09990001",
        "title": "Diabetes site study",
        "centralContacts": [
            {"name": "Sponsor desk", "email": "recruit@sponsor-pharma.test",
             "role": "CENTRAL_CONTACT"},
        ],
        "locations": [
            {"facility": "Riverside Clinic", "city": "Columbus", "state": "OH",
             "country": "United States", "status": "RECRUITING",
             "lat": 39.96, "lon": -83.00,
             "contacts": [
                 {"name": "Maya Chen", "email": "maya@riversideclinic.test",
                  "role": "CONTACT"},
                 {"name": "Dr PI", "email": "pi@riversideclinic.test",
                  "role": "PRINCIPAL_INVESTIGATOR"},
             ]},
            {"facility": "Lakeside Research", "city": "Cleveland", "state": "OH",
             "country": "United States", "status": "RECRUITING",
             "lat": 41.50, "lon": -81.69,
             "contacts": [
                 {"name": "Sam Coord", "email": "sam@lakeside.test",
                  "role": "STUDY_COORDINATOR"},
             ]},
            {"facility": "Far Away Site", "city": "Seattle", "state": "WA",
             "country": "United States", "status": "RECRUITING",
             "lat": 47.61, "lon": -122.33,
             "contacts": [
                 {"name": "West Coord", "email": "west@faraway.test",
                  "role": "CONTACT"},
             ]},
        ],
    }


def test_clinic_emails_not_sponsor():
    lead = {"nct": "NCT09990001", "site": "Riverside Clinic, Columbus, OH, United States"}
    recs = webapp.resolve_clinic_notify_recipients(
        lead, trial=_trial(), lat=39.96, lon=-83.00, radius=80, unit="km")
    emails = [r["email"] for r in recs]
    assert emails[0] == "maya@riversideclinic.test", emails
    assert "pi@riversideclinic.test" in emails
    assert emails.index("maya@riversideclinic.test") < emails.index(
        "pi@riversideclinic.test")
    # The listing's central study contact is the exact source for the trial:
    # it is a recipient too, after the nearby site inboxes.
    assert "recruit@sponsor-pharma.test" in emails
    assert emails.index("pi@riversideclinic.test") < emails.index(
        "recruit@sponsor-pharma.test")
    print("PASS: clinic emails first, then PI, then the listed study contact")


def test_nearby_clinics_in_area_not_cross_country():
    lead = {"nct": "NCT09990001", "site": "Riverside Clinic, Columbus, OH, United States"}
    recs = webapp.resolve_clinic_notify_recipients(
        lead, trial=_trial(), lat=39.96, lon=-83.00, radius=250, unit="km")
    emails = [r["email"] for r in recs]
    assert "maya@riversideclinic.test" in emails
    assert "sam@lakeside.test" in emails
    assert "west@faraway.test" not in emails, emails
    # The listed study contact comes after every nearby site, never before.
    assert emails[-1] == "recruit@sponsor-pharma.test", emails
    print("PASS: nearby clinics included; distant sites stay out; study contact last")


def test_claimed_site_is_added_not_exclusive():
    lead = {"nct": "NCT09990001", "site": "Riverside Clinic"}
    recs = webapp.resolve_clinic_notify_recipients(
        lead, trial=_trial(), claimed_email="inbox@bridgemd-clinic.test",
        lat=39.96, lon=-83.00, radius=80, unit="km")
    emails = [r["email"] for r in recs]
    assert "inbox@bridgemd-clinic.test" in emails
    assert "maya@riversideclinic.test" in emails
    assert "recruit@sponsor-pharma.test" in emails
    print("PASS: a claimed inbox is added alongside clinic emails and the study contact")


def test_pi_used_when_no_coordinator():
    trial = {
        "nctId": "NCT09990002",
        "centralContacts": [{"email": "hq@sponsor.test"}],
        "locations": [{
            "facility": "Solo Practice", "city": "Dayton", "state": "OH",
            "country": "United States", "status": "RECRUITING",
            "lat": 39.76, "lon": -84.19,
            "contacts": [
                {"name": "Only PI", "email": "pi@solo.test",
                 "role": "PRINCIPAL_INVESTIGATOR"},
            ],
        }],
    }
    lead = {"nct": "NCT09990002", "site": "Solo Practice, Dayton, OH"}
    recs = webapp.resolve_clinic_notify_recipients(
        lead, trial=trial, lat=39.76, lon=-84.19, radius=50, unit="km")
    emails = [r["email"] for r in recs]
    assert emails[0] == "pi@solo.test", emails
    assert emails[1] == "hq@sponsor.test", emails
    print("PASS: PI email first when the facility has no coordinator, study contact second")


def test_fallback_when_no_public_email():
    trial = {
        "nctId": "NCT09990003",
        "centralContacts": [],
        "locations": [{
            "facility": "Phone Only Site", "city": "Akron", "state": "OH",
            "country": "United States", "status": "RECRUITING",
            "lat": 41.08, "lon": -81.52,
            "contacts": [{"name": "Desk", "phone": "330-555-0100", "role": "CONTACT"}],
        }],
    }
    lead = {"nct": "NCT09990003", "site": "Phone Only Site"}
    recs = webapp.resolve_clinic_notify_recipients(
        lead, trial=trial, fallback=webapp.SITE_NOTIFY_EMAIL,
        lat=41.08, lon=-81.52, radius=50, unit="km")
    # The default fallback is the brand inbox the operator reads. It used to
    # be an early partner clinic whose inbox bounced, which ate handoffs.
    assert [r["email"] for r in recs] == ["hello@bridgemd.health"]
    assert recs[0]["source"] == "fallback"
    print("PASS: no public email falls back to the operator inbox")


def test_looks_up_local_clinic_not_lilly():
    trial = {
        "nctId": "NCT07641504",
        "leadSponsor": "Eli Lilly and Company",
        "centralContacts": [
            {"name": "Lilly trials", "email": "LillyTrials@Lilly.com",
             "role": "CONTACT"},
        ],
        "locations": [{
            "facility": "Arizona Research Center", "city": "Phoenix",
            "state": "Arizona", "country": "United States", "status": "RECRUITING",
            "lat": 33.45, "lon": -112.07,
            "contacts": [
                {"name": "Louise Taber MD", "role": "PRINCIPAL_INVESTIGATOR"},
            ],
        }, {
            "facility": "Synexus Clinical Research US, Inc.", "city": "Phoenix",
            "state": "Arizona", "country": "United States", "status": "RECRUITING",
            "lat": 33.45, "lon": -112.07,
            "contacts": [
                {"name": "Shawn Searle", "role": "PRINCIPAL_INVESTIGATOR"},
            ],
        }],
    }

    def fake_lookup(site, sponsor=""):
        fac = (site.get("facility") or "")
        if "Arizona Research" in fac:
            return [{"email": "recruitment@azresearchcenter.com",
                     "facility": fac, "city": "Phoenix", "source": "lookup"}]
        if "Synexus" in fac:
            return [{"email": "phoenix@trialmed.com",
                     "facility": fac, "city": "Phoenix", "source": "lookup"}]
        return []

    lead = {"nct": "NCT07641504", "site": "Arizona Research Center, Phoenix, AZ"}
    recs = webapp.resolve_clinic_notify_recipients(
        lead, trial=trial, lat=33.45, lon=-112.07, radius=80, unit="km",
        lookup_fn=fake_lookup)
    emails = [r["email"] for r in recs]
    assert "recruitment@azresearchcenter.com" in emails, emails
    assert "phoenix@trialmed.com" in emails, emails
    assert "LillyTrials@Lilly.com" in emails
    assert emails.index("recruitment@azresearchcenter.com") < emails.index(
        "LillyTrials@Lilly.com")
    print("PASS: Phoenix clinic emails are used; Lilly sponsor inbox is skipped")


def test_guesses_state_clinic_domain():
    import clinic_lookup
    hosts = clinic_lookup._guess_hosts("Arizona Research Center")
    assert hosts[0] == "azresearchcenter.com", hosts
    syn = clinic_lookup._guess_hosts("Synexus Clinical Research US, Inc.")
    assert "trialmed.com" in syn and "synexus.com" in syn
    print("PASS: clinic domains are guessed from the CT.gov site name")


def test_rejects_html_escape_emails():
    import clinic_lookup
    found = clinic_lookup._emails_in(
        r'contact \u003edataprivacy@plains.com and recruitment@azresearchcenter.com')
    assert "recruitment@azresearchcenter.com" in found
    assert not any("dataprivacy" in e or "u003e" in e for e in found), found
    assert clinic_lookup._skip_email("u003edataprivacy@plains.com")
    assert clinic_lookup._skip_email("dataprivacy@plains.com")
    print("PASS: HTML-escaped privacy scrapes are not treated as clinic inboxes")


def test_abs_url_works_without_http_request():
    with webapp.app.app_context():
        url = webapp._abs_url("candidate_page", token="tok123")
    assert url.endswith("/c/tok123"), url
    assert url.startswith("http"), url
    print("PASS: review link can be built in a background notify thread")


def test_applicant_email_in_body_not_as_recipient():
    """The clinic email is the whole application, written as a letter from
    the founder: contact details, date of birth, their answers, no application
    link, no registry code in the subject, and the applicant never on To."""
    import json as _json
    import re as _re
    lead = {"nct": "NCT09990001", "title": "Diabetes site study",
            "condition": "Type 2 diabetes", "location": "Columbus, OH",
            "site": "Riverside Clinic", "email": "patient@secret.test",
            "name": "Alex Morgan", "phone": "614-555-0100", "dob": "1990-06-01",
            "sex": "female", "created_at": "2026-09-18 10:00",
            "screener": _json.dumps({"Have you been diagnosed with Type 2 diabetes?": "yes",
                                     "travel": "yes", "_flags": []}),
            "eligibility": _json.dumps({"met": ["Age 18+"], "unknown": ["HbA1c"],
                                        "not_met": []}),
            "notes": "Diagnosed in 2021."}
    recs = webapp.resolve_clinic_notify_recipients(
        lead, trial=_trial(), lat=39.96, lon=-83.00, radius=80, unit="km")
    assert "patient@secret.test" not in [r["email"] for r in recs]
    subject, body = mailer.build_candidate_message(
        lead, None, clinic={"facility": "Riverside Clinic"}, reach=12)
    assert subject == "Alex in Columbus, OH applied to your type 2 diabetes study"
    assert "NCT" not in subject
    assert "patient@secret.test" in body and "614-555-0100" in body
    assert "1990-06-01" in body and "(age " in body
    assert "Have you been diagnosed with Type 2 diabetes? Yes" in body
    assert "Can travel to the study site for visits: Yes" in body
    assert "HbA1c" in body and "Diagnosed in 2021." in body
    assert "not copied" in body.lower()
    assert "founder of BridgeMD" in body and "University of Waterloo" in body
    assert "12 people have applied" in body
    assert "reply to this email" in body
    # No application link. The only URL is the LinkedIn page in the signature.
    urls = _re.findall(r"https?://\S+", body)
    assert urls == [mailer.LINKEDIN_URL], urls
    assert not _re.search(r"[\u2014\u2013]", subject + body)
    html = mailer.branded_html(body)
    assert mailer.LINKEDIN_LOGO_URL in html and mailer.LINKEDIN_URL in html
    assert "Harshil Shah" in html
    # Every email comes from a person at the shared inbox, with replies to it.
    assert mailer.from_header() == "Harshil Shah at BridgeMD <hello@bridgemd.health>"
    assert mailer.reply_to_header() == "hello@bridgemd.health"
    # Chat notifications to the team carry the contact inline, no link.
    s2, b2 = mailer.build_dm_message(lead, "Can I come Tuesday?", "https://x/c/1", to="site")
    assert s2 == "Alex sent you a message about the type 2 diabetes study"
    assert "https://x/c/1" not in b2 and "patient@secret.test" in b2
    # Applicant-facing subjects are human too, and their thread link stays.
    s3, b3 = mailer.build_apply_confirmation(lead, "https://bridgemd.health/a/t1")
    assert s3 == "Your application to the type 2 diabetes study"
    assert "https://bridgemd.health/a/t1" in b3
    assert mailer.age_from_dob("1990-06-01").isdigit()
    assert mailer.age_from_dob("2999-01-01") == "" and mailer.age_from_dob("nope") == ""
    print("PASS: clinic letter carries the whole application, no link, human subject")

def test_missing_clinic_notify_list_clears_after_record():
    with webapp.app.app_context():
        tok = webapp.db.create_lead({
            "nct": "NCT09990099", "title": "Pending notify",
            "site": "Riverside Clinic", "name": "B", "email": "b@x.test",
            "consent": 1,
        })
        lead = webapp.db.get_lead_by_token(tok)
        ids = [r["id"] for r in webapp.db.leads_missing_clinic_notify()]
        assert lead["id"] in ids
        webapp.db.record_clinic_notify(lead["id"], [{
            "email": "maya@riversideclinic.test", "facility": "Riverside Clinic",
            "source": "facility",
        }])
        ids = [r["id"] for r in webapp.db.leads_missing_clinic_notify()]
        assert lead["id"] not in ids
        webapp.db.get_db().execute(
            "UPDATE leads SET clinic_notify_json = ? WHERE id = ?",
            ('[{"email":"maya@riversideclinic.test","source":"facility"}]',
             lead["id"]))
        webapp.db.get_db().commit()
        ids = [r["id"] for r in webapp.db.leads_missing_clinic_notify()]
        assert lead["id"] in ids
    print("PASS: current copy is skipped; older blinded notify is resent")


def test_failed_send_does_not_stamp():
    with webapp.app.app_context():
        tok = webapp.db.create_lead({
            "nct": "NCT09990001", "title": "Diabetes site study",
            "site": "Riverside Clinic", "name": "C", "email": "c@x.test",
            "consent": 1,
        })
        orig = webapp._notify
        webapp._notify = lambda *a, **k: False
        try:
            emails = webapp._notify_site_new_candidate_sync(
                tok, trial=_trial(), lat=39.96, lon=-83.00)
        finally:
            webapp._notify = orig
        assert emails == []
        lead = webapp.db.get_lead_by_token(tok)
        ids = [r["id"] for r in webapp.db.leads_missing_clinic_notify()]
        assert lead["id"] in ids
    print("PASS: failed SMTP does not stamp the lead, so backfill can retry")


def test_persists_clinic_notify_on_lead():
    with webapp.app.app_context():
        tok = webapp.db.create_lead({
            "nct": "NCT09990001", "title": "Diabetes site study",
            "site": "Riverside Clinic", "name": "A", "email": "a@x.test",
            "consent": 1,
        })
        lead = webapp.db.get_lead_by_token(tok)
        recs = [{"email": "maya@riversideclinic.test", "facility": "Riverside Clinic",
                 "source": "facility"}]
        assert webapp.db.record_clinic_notify(lead["id"], recs)
        saved = webapp.db.lead_clinic_notify(webapp.db.get_lead_by_token(tok))
        assert saved[0]["email"] == "maya@riversideclinic.test"
        assert saved[0]["copy"] == webapp.db.CLINIC_NOTIFY_COPY
        notes = [e["note"] for e in webapp.db.get_lead_events(lead["id"])]
        assert any("Riverside Clinic" in (n or "") for n in notes)
    print("PASS: chosen clinic is stored on the lead even when SMTP is off")


def test_placeholder_site_contacts_never_receive_mail():
    """Demo-seed and garbled addresses are refused on every recipient path."""
    import db
    assert db.is_placeholder_site_email("info@northwindclinical.com")
    assert db.is_placeholder_site_email("u003edataprivacy@plains.com")
    assert db.is_placeholder_site_email("x@bridgemd.local")
    # A company's legal or privacy desk is not a clinic, wherever it came from.
    assert db.is_placeholder_site_email("pclplegalnotices@plains.com")
    import clinic_lookup
    assert clinic_lookup._skip_email("pclplegalnotices@plains.com")
    assert clinic_lookup._skip_email("noreply@realclinic.test")
    assert not clinic_lookup._skip_email("research@realclinic.test")
    assert not db.is_placeholder_site_email("navarrs@ccf.org")
    # Resolver: a placeholder claimed contact with nothing else falls through
    # to the operator fallback instead of the demo profile.
    lead = {"nct": "NCT09990002", "site": ""}
    recs = webapp.resolve_clinic_notify_recipients(
        lead, trial={"locations": []},
        claimed_email="info@northwindclinical.com",
        fallback="owner@ops.test", lookup_fn=lambda *a, **k: [])
    assert [r["email"] for r in recs] == ["owner@ops.test"], recs


def test_demo_only_handoff_is_resent():
    """A handoff whose only recipient was a placeholder notified nobody, so
    the backfill must pick the lead up again; a real recipient settles it."""
    import db
    with webapp.app.app_context():
        token = db.create_lead({
            "applicant_token": "t-demo-handoff", "nct": "NCT09990003",
            "title": "T", "name": "P", "email": "p@x.test", "consent": 1,
            "source": "web"})
        lead = db.get_lead_by_token(token)
        db.record_clinic_notify(lead["id"], [
            {"email": "info@northwindclinical.com", "facility": "Demo",
             "source": "claimed"}])
        assert lead["id"] in [r["id"] for r in db.leads_missing_clinic_notify()]
        db.record_clinic_notify(lead["id"], [
            {"email": "coord@realclinic.test", "facility": "Real",
             "source": "ctgov"}])
        assert lead["id"] not in [r["id"] for r in db.leads_missing_clinic_notify()]


def test_site_contact_skips_demo_profile():
    """A study claimed by the demo site resolves to no contact (so callers use
    the operator fallback); a later real claim wins."""
    import db
    with webapp.app.app_context():
        uid = db.create_user("demo-clinician@x.test", "pw", "Demo Site")
        db.add_study_claim(uid, "NCT09990004", "T", verified=True)
        db.upsert_site_profile(uid, "Northwind Clinical Research", "Elena",
                               "info@northwindclinical.com", "+1 212 555 0148")
        assert db.site_contact_for_nct("NCT09990004") == ""
        uid2 = db.create_user("coord@realclinic.test", "pw", "Real Site")
        db.add_study_claim(uid2, "NCT09990004", "T", verified=True)
        assert db.site_contact_for_nct("NCT09990004") == "coord@realclinic.test"


def test_apply_double_submit_is_one_application():
    """Two identical applies within minutes make one lead and one email set."""
    import db
    with webapp.app.app_context():
        assert db.find_recent_duplicate_lead("dup@x.test", "NCT09990005") is None
        db.create_lead({
            "applicant_token": "t-dup-1", "nct": "NCT09990005", "title": "T",
            "name": "D", "email": "Dup@X.test", "consent": 1, "source": "web"})
        assert db.find_recent_duplicate_lead("dup@x.test", "nct09990005") is not None
        assert db.find_recent_duplicate_lead("dup@x.test", "NCT09990006") is None


def test_founder_connect_email_content_and_stamp():
    """The founder note carries real contacts, never a placeholder address,
    an em dash, an eligibility claim, or a BridgeMD application link; the
    stamp keeps the backfill from sending it twice."""
    import re
    import db
    lead = {"name": "George Cole", "email": "g@x.test", "nct": "NCT09990001",
            "title": "Diabetes site study", "location": "", "site": ""}
    sites = [{"facility": "Riverside Clinic", "city": "Columbus",
              "phone": "614-555-0100", "email": "maya@riversideclinic.test"}]
    central = [{"phone": "1-877-555-0199", "email": ""}]
    subject, body = mailer.build_founder_connect_message(lead, sites, central)
    assert subject == "How to reach the clinical trial team directly", subject
    assert "614-555-0100" in body and "1-877-555-0199" in body
    assert "bridgemd.health/a/" not in body
    assert "founder of BridgeMD" in body
    assert not re.search(r"[\u2014\u2013]", subject + body)
    assert "qualify" not in body.lower() and "$" not in body
    assert "medical advice" in body

    with webapp.app.app_context():
        token = db.create_lead({
            "applicant_token": "t-founder-1", "nct": "NCT09990001",
            "title": "T", "name": "G", "email": "g@x.test", "consent": 1,
            "source": "web"})
        row = db.get_lead_by_token(token)
        assert row["id"] in [r["id"] for r in db.leads_missing_founder_connect()]
        # Payload built from the trial fixture, no network: emails come from
        # the facility contacts and never from a placeholder.
        real_get = webapp._get_study
        webapp._get_study = lambda nct: _trial()
        try:
            found_sites, found_central = webapp._founder_connect_payload(row)
        finally:
            webapp._get_study = real_get
        assert found_sites and found_sites[0]["email"] == "maya@riversideclinic.test"
        assert found_central and found_central[0]["email"] == "recruit@sponsor-pharma.test"
        # A double-submit sibling (same email, same study) is stamped with it.
        tok2 = db.create_lead({
            "applicant_token": "t-founder-2", "nct": "NCT09990001",
            "title": "T", "name": "G", "email": "G@x.test", "consent": 1,
            "source": "web"})
        sib = db.get_lead_by_token(tok2)
        db.mark_founder_connect(row["id"])
        pending = [r["id"] for r in db.leads_missing_founder_connect()]
        assert row["id"] not in pending and sib["id"] not in pending


def test_owner_copy_has_the_application_and_no_links():
    """The operator's own email carries the whole application, no record or
    inbox links, only the website. A postal-code location becomes a place."""
    import json as _json
    import re as _re
    lead = {"nct": "NCT09990001", "title": "Diabetes site study",
            "condition": "Type 2 diabetes", "location": "Pembroke Pines, FL",
            "site": "Riverside Clinic", "email": "patient@secret.test",
            "name": "Alex Morgan", "phone": "614-555-0100", "dob": "1990-06-01",
            "source": "web", "created_at": "2026-09-18 10:00",
            "screener": _json.dumps({"travel": "yes", "_flags": []}),
            "eligibility": "", "notes": "", "records_connected": 0}
    subject, body = mailer.build_owner_new_application(lead)
    assert subject == "New application: type 2 diabetes study in Pembroke Pines, FL"
    assert "Alex Morgan" in body and "patient@secret.test" in body
    assert "614-555-0100" in body and "1990-06-01" in body
    assert "Can travel to the study site for visits: Yes" in body
    assert "/app/applicant" not in body and "/internal" not in body
    assert _re.findall(r"https?://\S+", body) == [mailer.FINDER_URL]
    # Postal codes turn into places; typed places pass through; a trailing
    # US ZIP is dropped.
    real = webapp._zippopotam_place
    webapp._zippopotam_place = lambda country, code: (
        "Pembroke Pines, FL" if code == "33029" else "Toronto, ON")
    try:
        assert webapp.display_area("33029") == "Pembroke Pines, FL"
        assert webapp.display_area("M5V 2T6") == "Toronto, ON"
        assert webapp.display_area("Dallas, TX 75201") == "Dallas, TX"
        assert webapp.display_area("Dallas, TX") == "Dallas, TX"
    finally:
        webapp._zippopotam_place = real
    print("PASS: owner copy is the whole application, no links; places not codes")


def test_listing_contacts_reach_applicant_and_letter():
    """Site phone and principal investigator from the listing show up in the
    applicant's contacts email and in the study-team letter; contacts aimed
    at doctors who want to run a site are left out."""
    trial = {
        "nctId": "NCT09990009", "conditions": ["Type 2 diabetes"],
        "centralContacts": [
            {"name": "Trial questions: 1-877-555-0100 or", "role": "CONTACT",
             "phone": "1-317-555-0100", "email": "trials@sponsor.test"},
            {"name": "Physicians interested in becoming principal investigators",
             "role": "CONTACT", "email": "inquiry_hub@sponsor.test"},
        ],
        "locations": [{
            "facility": "Helios Clinical Research", "city": "Phoenix",
            "state": "AZ", "country": "United States", "status": "RECRUITING",
            "lat": 33.44, "lon": -112.07,
            "contacts": [{"role": "CONTACT", "phone": "480-555-0100"},
                         {"name": "David Francyk", "role": "PRINCIPAL_INVESTIGATOR"}],
        }],
    }
    lead = {"name": "Pat Lee", "email": "pat@x.test", "nct": "NCT09990009",
            "title": "T2D study", "location": "", "site": "",
            "condition": "Type 2 diabetes"}
    central = webapp.central_contacts_for_patients(trial)
    assert [c["email"] for c in central] == ["trials@sponsor.test"], central
    cards = webapp.site_contact_cards(trial, lead)
    assert cards and cards[0]["phone"] == "480-555-0100"
    assert cards[0]["pi"] == "David Francyk"
    _, body = mailer.build_founder_connect_message(lead, cards, central)
    assert "480-555-0100" in body and "principal investigator: David Francyk" in body
    assert "Study contact listed on ClinicalTrials.gov: 1-317-555-0100 or trials@sponsor.test" in body
    assert "inquiry_hub" not in body
    _, letter = mailer.build_candidate_message(
        lead, None, clinic={"facility": "Helios Clinical Research",
                            "phone": "480-555-0100", "pi": "David Francyk"})
    assert "Site they chose: Helios Clinical Research (principal investigator David Francyk, 480-555-0100)" in letter
    print("PASS: listing phone, PI and study contact reach the applicant and the letter")


def main():
    tests = [
        test_clinic_emails_not_sponsor,
        test_nearby_clinics_in_area_not_cross_country,
        test_claimed_site_is_added_not_exclusive,
        test_pi_used_when_no_coordinator,
        test_fallback_when_no_public_email,
        test_looks_up_local_clinic_not_lilly,
        test_guesses_state_clinic_domain,
        test_rejects_html_escape_emails,
        test_abs_url_works_without_http_request,
        test_applicant_email_in_body_not_as_recipient,
        test_missing_clinic_notify_list_clears_after_record,
        test_failed_send_does_not_stamp,
        test_persists_clinic_notify_on_lead,
        test_placeholder_site_contacts_never_receive_mail,
        test_demo_only_handoff_is_resent,
        test_site_contact_skips_demo_profile,
        test_apply_double_submit_is_one_application,
        test_founder_connect_email_content_and_stamp,
        test_owner_copy_has_the_application_and_no_links,
        test_listing_contacts_reach_applicant_and_letter,
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
    print(f"\nAll {len(tests)} clinic-notify tests passed")


if __name__ == "__main__":
    main()
