"""Every study-team route must stay reachable in the public site demo.

Run: python test_demo_routes.py

A study-team path has to be registered in TWO independent places, and forgetting
either fails in a way that looks like a product bug rather than a config slip:

  * ``_STUDY_TEAM_DEMO_PREFIXES`` - decides whether an anonymous visitor is shown
    the demo account. Miss it and ``login_required`` bounces the page to
    /login, so a demo click lands on an email sign-up screen.
  * the ``pov == "study"`` prefix check in the context processor - decides which
    shell renders. Miss it and the page loads inside the *clinician* nav
    (Find trials / My referrals) with no Bridget dock.

Both went wrong for /app/mentions when it was added, which is what this guards.
"""
import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ["NO_LOGIN"] = "0"
os.environ["SITE_DEMO"] = "1"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["SECRET_KEY"] = "demo-routes-test-secret"

import app as webapp  # noqa: E402

# Study-team pages an anonymous visitor should be able to click through in the
# demo. Owner-only surfaces are deliberately absent - see NOT_DEMOABLE below.
DEMO_PATHS = [
    "/app/inbox",
    "/app/leads",
    "/app/mentions",
    "/app/team",
    "/app/calendar",
    "/app/documents",
    "/app/team/mentionable.json",
    "/app/leads/blast/preview.json?nct=NCT00000000",
]

# Owner-only. These SHOULD bounce to login for an anonymous visitor; if one ever
# starts returning 200 in demo mode, that is a leak, not a fix.
NOT_DEMOABLE = ["/app/analytics"]


def test_demo_paths_do_not_bounce_to_login():
    client = webapp.app.test_client()
    for path in DEMO_PATHS:
        r = client.get(path)
        assert r.status_code != 302 or "/login" not in (
            r.headers.get("Location") or ""), (
            f"{path} redirects to the sign-up screen in demo mode - add its "
            f"prefix to _STUDY_TEAM_DEMO_PREFIXES in app.py")
        assert r.status_code in (200, 304), f"{path} returned {r.status_code}"
    print("PASS: study-team routes stay inside the demo (no sign-up wall)")


def test_owner_only_paths_still_gated():
    client = webapp.app.test_client()
    for path in NOT_DEMOABLE:
        r = client.get(path)
        assert r.status_code == 302 and "/login" in (
            r.headers.get("Location") or ""), (
            f"{path} is owner-only and must not be exposed by the demo")
    print("PASS: owner-only routes stay gated in demo mode")


def test_demo_pages_render_the_study_shell():
    """A study-team page must get the study nav + Bridget, not the clinician
    shell. Catches a route missing from the pov prefix list."""
    client = webapp.app.test_client()
    for path in ["/app/inbox", "/app/leads", "/app/mentions"]:
        html = client.get(path).get_data(as_text=True)
        assert 'id="copilot"' in html, f"{path} renders without the Bridget dock"
        assert "Find trials" not in html, (
            f"{path} rendered the clinician shell - add its prefix to the "
            f"pov == 'study' check in app.py")
    print("PASS: study-team pages render the study shell with Bridget")


def test_walk_up_visitor_cannot_open_a_real_applicant():
    """SITE_DEMO signs any visitor into the demo account. That account may
    open seeded demo applicants only. A real person's application must be a
    404 to a walk-up visitor (a link-preview crawler once fetched one)."""
    import db
    with webapp.app.app_context():
        real = db.create_lead({
            "applicant_token": "real-person-1", "nct": "NCT09990077",
            "title": "T", "name": "Real Person", "email": "real@gmail.com",
            "consent": 1, "source": "web"})
        real_id = db.get_lead_by_token(real)["id"]
        demo = db.create_lead({
            "applicant_token": "demo-seed-1", "nct": "NCT09990078",
            "title": "T", "name": "Demo Person", "email": "demo@example.com",
            "consent": 1, "source": "demo"})
        demo_id = db.get_lead_by_token(demo)["id"]
    client = webapp.app.test_client()
    assert client.get(f"/app/applicant/{real_id}").status_code == 404, \
        "a walk-up visitor could open a real applicant's record"
    assert client.get(f"/app/applicant/{demo_id}").status_code == 200
    print("PASS: real applicants are hidden from the demo account")


def main():
    test_demo_paths_do_not_bounce_to_login()
    test_owner_only_paths_still_gated()
    test_demo_pages_render_the_study_shell()
    test_walk_up_visitor_cannot_open_a_real_applicant()
    print("PASS: demo route tests")


if __name__ == "__main__":
    main()
