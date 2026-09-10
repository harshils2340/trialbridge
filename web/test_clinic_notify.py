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


def test_clinic_first_then_sponsor():
    lead = {"nct": "NCT09990001", "site": "Riverside Clinic, Columbus, OH, United States"}
    recs = webapp.resolve_clinic_notify_recipients(
        lead, trial=_trial(), lat=39.96, lon=-83.00, radius=80, unit="km")
    emails = [r["email"] for r in recs]
    assert emails[0] == "maya@riversideclinic.test", emails
    assert "pi@riversideclinic.test" in emails
    assert emails.index("maya@riversideclinic.test") < emails.index(
        "pi@riversideclinic.test")
    assert "recruit@sponsor-pharma.test" in emails
    assert emails.index("pi@riversideclinic.test") < emails.index(
        "recruit@sponsor-pharma.test")
    print("PASS: clinic emails first, then PI, then sponsor, all in one list")


def test_nearby_clinics_in_area_not_cross_country():
    lead = {"nct": "NCT09990001", "site": "Riverside Clinic, Columbus, OH, United States"}
    recs = webapp.resolve_clinic_notify_recipients(
        lead, trial=_trial(), lat=39.96, lon=-83.00, radius=250, unit="km")
    emails = [r["email"] for r in recs]
    assert "maya@riversideclinic.test" in emails
    assert "sam@lakeside.test" in emails
    assert "west@faraway.test" not in emails, emails
    assert "recruit@sponsor-pharma.test" in emails
    print("PASS: nearby clinics plus sponsor; distant sites stay out")


def test_claimed_site_is_added_not_exclusive():
    lead = {"nct": "NCT09990001", "site": "Riverside Clinic"}
    recs = webapp.resolve_clinic_notify_recipients(
        lead, trial=_trial(), claimed_email="inbox@bridgemd-clinic.test",
        lat=39.96, lon=-83.00, radius=80, unit="km")
    emails = [r["email"] for r in recs]
    assert "inbox@bridgemd-clinic.test" in emails
    assert "maya@riversideclinic.test" in emails
    assert "recruit@sponsor-pharma.test" in emails
    print("PASS: a claimed inbox is added alongside clinic and sponsor emails")


def test_pi_and_sponsor_when_no_coordinator():
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
    assert "hq@sponsor.test" in emails
    print("PASS: PI email is used when the facility has no coordinator, plus sponsor")


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


def test_email_is_blinded_and_has_secure_link():
    lead = {"nct": "NCT09990001", "title": "Diabetes site study",
            "condition": "Type 2 diabetes", "location": "Columbus, OH",
            "site": "Riverside Clinic", "email": "patient@secret.test",
            "name": "Alex Morgan", "phone": "614-555-0100"}
    subject, body = mailer.build_candidate_message(
        lead, "https://bridgemd.health/c/abc123",
        clinic={"facility": "Riverside Clinic"})
    assert "Riverside Clinic" in body
    assert "https://bridgemd.health/c/abc123" in body
    assert "patient@secret.test" not in body
    assert "Alex Morgan" not in body
    assert "614-555-0100" not in body
    assert "not the trial sponsor" not in body.lower()
    assert "NCT09990001" in subject
    print("PASS: clinic email carries the secure link and no patient contact")


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
    print("PASS: recorded clinic notify is not selected for backfill again")


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
        notes = [e["note"] for e in webapp.db.get_lead_events(lead["id"])]
        assert any("Riverside Clinic" in (n or "") for n in notes)
    print("PASS: chosen clinic is stored on the lead even when SMTP is off")


def main():
    tests = [
        test_clinic_first_then_sponsor,
        test_nearby_clinics_in_area_not_cross_country,
        test_claimed_site_is_added_not_exclusive,
        test_pi_and_sponsor_when_no_coordinator,
        test_fallback_when_no_public_email,
        test_email_is_blinded_and_has_secure_link,
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
