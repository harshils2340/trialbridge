"""Live applies are browser submits with an apply event, not seed rows.

Run: python test_live_applies.py
"""
import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ["NO_LOGIN"] = "0"
os.environ["SITE_DEMO"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["SECRET_KEY"] = "live-apply-test"

import app as webapp  # noqa: E402
import db  # noqa: E402


def _fail(name, msg):
    raise AssertionError(f"{name}: {msg}")


def test_found_via_label():
    assert db.found_via_label("bing.com", "bing.com") == "Bing"
    assert db.found_via_label("duckduckgo.com", "") == "DuckDuckGo"
    assert db.found_via_label("", "") == "Direct"
    assert db.found_via_label("google.com", "") == "Google"


def test_live_index_excludes_seeds():
    with webapp.app.app_context():
        db.init_db()
        seed = db.create_lead({
            "nct": "NCT00000001", "title": "Seed", "name": "Victor O.",
            "email": "victor.owens84@example.com", "source": "web",
            "applicant_token": "seeded-volume-84",
        })
        fake_gmail = db.create_lead({
            "nct": "NCT00000002", "title": "Fake", "name": "Jordan Blake",
            "email": "jordan.blake@bridgemd.local", "source": "web",
        })
        live_tok = db.create_lead({
            "nct": "NCT07564414", "title": "Weight", "name": "Becki Mazzola",
            "email": "izzibi@gmail.com", "source": "web",
            "applicant_token": "real-token-1",
        })
        seed_lead = db.get_lead_by_token(seed)
        fake_lead = db.get_lead_by_token(fake_gmail)
        live_lead = db.get_lead_by_token(live_tok)
        con = db.get_db()
        con.execute(
            "INSERT INTO web_events (ts, visitor, name, path, source, medium, "
            "campaign, referrer, detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (live_lead["created_at"], "vid-becki", "search", "/find",
             "bing.com", "referral", "", "bing.com",
             '{"q":"Weight loss","results":15}'))
        con.execute(
            "INSERT INTO web_events (ts, visitor, name, path, source, medium, "
            "campaign, referrer, detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (live_lead["created_at"], "vid-becki", "apply", "/interest",
             "bing.com", "referral", "", "bing.com",
             '{"nct":"NCT07564414"}'))
        con.commit()

        assert not webapp._is_inbound_application(seed_lead)
        assert not webapp._is_inbound_application(fake_lead)
        assert webapp._is_inbound_application(live_lead)
        idx = db.live_apply_index()
        assert webapp._live_apply_key(live_lead) in idx
        assert webapp._live_apply_key(seed_lead) not in idx
        att = idx[webapp._live_apply_key(live_lead)]
        if att["found_via"] != "Bing":
            _fail("found_via", att)
        if att["search_q"] != "Weight loss":
            _fail("search_q", att)
        apps = webapp._live_operator_apps()
        if len(apps) != 1 or apps[0]["name"] != "Becki Mazzola":
            _fail("inbox", apps)
        uid = db.create_user(webapp.OWNER_EMAIL, "pw", "Owner")
    client = webapp.app.test_client()
    with client.session_transaction() as s:
        s[webapp.USER_SESSION_KEY] = uid
    page = client.get("/internal/inbox")
    if page.status_code != 200:
        _fail("inbox page", page.status_code)
    html = page.get_data(as_text=True)
    for needle in ("Live", "Bing", "Weight loss", "Becki Mazzola"):
        if needle not in html:
            _fail("inbox html", needle)
    if "Victor O." in html or "Jordan Blake" in html:
        _fail("inbox html", "seed row leaked")


if __name__ == "__main__":
    test_found_via_label()
    test_live_index_excludes_seeds()
    print("ok")
