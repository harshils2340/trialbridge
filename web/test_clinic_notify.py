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
    assert "recruit@sponsor-pharma.test" not in emails
    print("PASS: clinic emails first, then PI; sponsor inbox is skipped")


def test_nearby_clinics_in_area_not_cross_country():
    lead = {"nct": "NCT09990001", "site": "Riverside Clinic, Columbus, OH, United States"}
    recs = webapp.resolve_clinic_notify_recipients(
        lead, trial=_trial(), lat=39.96, lon=-83.00, radius=250, unit="km")
    emails = [r["email"] for r in recs]
    assert "maya@riversideclinic.test" in emails
    assert "sam@lakeside.test" in emails
    assert "west@faraway.test" not in emails, emails
    assert "recruit@sponsor-pharma.test" not in emails
    print("PASS: nearby clinics included; distant sites and sponsor stay out")


def test_claimed_site_is_added_not_exclusive():
    lead = {"nct": "NCT09990001", "site": "Riverside Clinic"}
    recs = webapp.resolve_clinic_notify_recipients(
        lead, trial=_trial(), claimed_email="inbox@bridgemd-clinic.test",
        lat=39.96, lon=-83.00, radius=80, unit="km")
    emails = [r["email"] for r in recs]
    assert "inbox@bridgemd-clinic.test" in emails
    assert "maya@riversideclinic.test" in emails
    assert "recruit@sponsor-pharma.test" not in emails
    print("PASS: a claimed inbox is added alongside clinic emails")


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
    assert "hq@sponsor.test" not in emails
    print("PASS: PI email is used when the facility has no coordinator; sponsor skipped")


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
    assert [r["email"] for r in recs] == ["contact@sonicmedicaltrust.com"]
    assert recs[0]["source"] == "fallback"
    print("PASS: no public email falls back to contact@sonicmedicaltrust.com")


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
    assert "LillyTrials@Lilly.com" not in emails
    print("PASS: Phoenix clinic emails are used; Lilly sponsor inbox is skipped")


def test_guesses_state_clinic_domain():
    import clinic_lookup
    hosts = clinic_lookup._guess_hosts("Arizona Research Center")
    assert hosts[0] == "azresearchcenter.com", hosts
    syn = clinic_lookup._guess_hosts("Synexus Clinical Research US, Inc.")
    assert "trialmed.com" in syn and "synexus.com" in syn
    print("PASS: clinic domains are guessed from the CT.gov site name")


def test_abs_url_works_without_http_request():
    with webapp.app.app_context():
        url = webapp._abs_url("candidate_page", token="tok123")
    assert url.endswith("/c/tok123"), url
    assert url.startswith("http"), url
    print("PASS: review link can be built in a background notify thread")


def test_applicant_email_in_body_not_as_recipient():
    lead = {"nct": "NCT09990001", "title": "Diabetes site study",
            "condition": "Type 2 diabetes", "location": "Columbus, OH",
            "site": "Riverside Clinic", "email": "patient@secret.test",
            "name": "Alex Morgan", "phone": "614-555-0100"}
    recs = webapp.resolve_clinic_notify_recipients(
        lead, trial=_trial(), lat=39.96, lon=-83.00, radius=80, unit="km")
    assert "patient@secret.test" not in [r["email"] for r in recs]
    subject, body = mailer.build_candidate_message(
        lead, "https://bridgemd.health/c/abc123",
        clinic={"facility": "Riverside Clinic"})
    assert "patient@secret.test" in body
    assert "not copied" in body.lower()
    assert "https://bridgemd.health/find-trial" in body
    assert "https://bridgemd.health/c/abc123" in body
    assert "614-555-0100" not in body
    assert "NCT09990001" in subject
    html = mailer.branded_html(body)
    assert "https://bridgemd.health/static/apple-touch-icon.png" in html
    assert "https://bridgemd.health/find-trial" in html
    assert "BridgeMD" in html
    print("PASS: applicant email is in the body only; branding + finder link included")


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


def main():
    tests = [
        test_clinic_emails_not_sponsor,
        test_nearby_clinics_in_area_not_cross_country,
        test_claimed_site_is_added_not_exclusive,
        test_pi_used_when_no_coordinator,
        test_fallback_when_no_public_email,
        test_looks_up_local_clinic_not_lilly,
        test_guesses_state_clinic_domain,
        test_abs_url_works_without_http_request,
        test_applicant_email_in_body_not_as_recipient,
        test_missing_clinic_notify_list_clears_after_record,
        test_persists_clinic_notify_on_lead,
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
