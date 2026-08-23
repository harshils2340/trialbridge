"""Regression checks for the shared marketing inbox.

Run: python test_marketing_hub.py
"""
import os
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ["NO_LOGIN"] = "0"
os.environ["SITE_DEMO"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["SECRET_KEY"] = "marketing-hub-test-secret"

import app as webapp  # noqa: E402
import db  # noqa: E402


CSRF = "marketing-hub-test-csrf"


def _client_for(user_id):
    client = webapp.app.test_client()
    with client.session_transaction() as session:
        session[webapp.USER_SESSION_KEY] = user_id
        session[webapp.CSRF_SESSION_KEY] = CSRF
    return client


def _post(client, path, data):
    payload = {"_csrf_token": CSRF, **data}
    return client.post(path, data=payload, follow_redirects=False)


def _create_users():
    with webapp.app.app_context():
        owner_id = db.create_user(
            "owner@example.com", "disabled", "Avery Stone", verified=True)
        cover_id = db.create_user(
            "cover@example.com", "disabled", "Riley Chen", verified=True)
        invite = db.create_org_invite(owner_id, "cover@example.com", "student")
        assert db.accept_org_invite(cover_id, invite)
        outsider_id = db.create_user(
            "outsider@example.com", "disabled", "Outside User", verified=True)
    return owner_id, cover_id, outsider_id


def test_marketing_hub_flow():
    owner_id, cover_id, outsider_id = _create_users()
    client = _client_for(owner_id)

    page = client.get("/app/inbox")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Inbox" in html
    assert 'aria-label="Manage connected accounts"' in html
    assert "Connect Gmail" in html
    assert 'id="studySwitch"' in html
    assert "mh-intake-nav" not in html

    added = _post(client, "/marketing-hub/sources", {
        "channel": "email",
        "label": "Main inbox",
        "identifier": "hello@example.com",
    })
    assert added.status_code == 302

    with webapp.app.app_context():
        sources = db.list_marketing_sources(owner_id)
        assert len(sources) == 1
        source_id = sources[0]["id"]

    created = _post(client, "/marketing-hub/threads", {
        "source_id": str(source_id),
        "contact_name": "Morgan Lee",
        "contact_handle": "morgan@example.com",
        "subject": "Campaign question",
        "body": "Can someone help me understand the next step?",
    })
    assert created.status_code == 302

    with webapp.app.app_context():
        threads = db.list_marketing_threads(owner_id)
        assert len(threads) == 1
        thread_id = threads[0]["id"]
        assert threads[0]["assigned_to"] == owner_id

    rendered = client.get("/app/inbox").get_data(as_text=True)
    assert "is-row-channel" in rendered
    assert 'aria-label="Email"' in rendered

    assert _post(client, f"/marketing-hub/threads/{thread_id}/reply", {
        "body": "Yes. I can help with that.",
    }).status_code == 302
    assert _post(client, f"/marketing-hub/threads/{thread_id}/note", {
        "body": "Follow up before Friday.",
    }).status_code == 302
    assert _post(client, f"/marketing-hub/threads/{thread_id}/assign", {
        "assignee_id": str(cover_id),
    }).status_code == 302

    with webapp.app.app_context():
        messages = db.list_marketing_messages(owner_id, thread_id)
        assert [message["kind"] for message in messages] == [
            "inbound", "outbound", "note"]
        assert messages[1]["delivery_status"] == "saved"
        assert db.get_marketing_thread(owner_id, thread_id)["assigned_to"] == cover_id

    coverage = _post(client, "/marketing-hub/coverage", {
        "primary_user_id": str(owner_id),
        "cover_user_id": str(cover_id),
        "vacation_mode": "1",
        "away_until": "2099-12-31",
        "note": "Handle new partnership messages first.",
    })
    assert coverage.status_code == 302

    with webapp.app.app_context():
        settings = db.get_marketing_handoff(owner_id)
        assert settings["vacation_mode"] == 1
        assert db.marketing_active_owner_id(settings) == cover_id

    routed = _post(client, "/marketing-hub/threads", {
        "source_id": str(source_id),
        "contact_name": "Taylor Brooks",
        "contact_handle": "taylor@example.com",
        "subject": "New inbound",
        "body": "Who is available today?",
    })
    assert routed.status_code == 302
    with webapp.app.app_context():
        newest = db.list_marketing_threads(owner_id)[0]
        assert newest["assigned_to"] == cover_id

        db.add_study_claim(
            owner_id, "NCT00000001", "Inbox Study", verified=True)
        existing_token = db.create_lead({
            "applicant_token": "existing-person",
            "nct": "NCT00000001",
            "title": "Inbox Study",
            "name": "Morgan Lee",
            "email": "morgan@example.com",
            "consent": 1,
            "owner_user_id": owner_id,
        })
        existing_lead_id = db.get_lead_by_token(existing_token)["id"]
        db.get_db().execute(
            "UPDATE marketing_threads SET nct = ?, study_label = ? WHERE id = ?",
            ("NCT00000001", "Inbox Study", thread_id))
        db.get_db().commit()

    no_csrf = client.post(
        f"/marketing-hub/threads/{thread_id}/applicant",
        data={"consent": "1"})
    assert no_csrf.status_code == 302
    with webapp.app.app_context():
        assert db.get_marketing_thread(owner_id, thread_id)["linked_lead_id"] is None

    denied_link = _post(
        client, f"/marketing-hub/threads/{thread_id}/applicant", {})
    assert denied_link.status_code == 302
    with webapp.app.app_context():
        assert db.get_marketing_thread(owner_id, thread_id)["linked_lead_id"] is None

    linked = _post(
        client, f"/marketing-hub/threads/{thread_id}/applicant",
        {"consent": "1", "age": "42"})
    assert linked.status_code == 302
    with webapp.app.app_context():
        linked_thread = db.get_marketing_thread(owner_id, thread_id)
        assert linked_thread["linked_lead_id"]
        linked_lead_id = linked_thread["linked_lead_id"]
        assert linked_lead_id != existing_lead_id
        assert db.get_lead(linked_lead_id)["consent"] == 1

    assert _post(
        client, f"/marketing-hub/threads/{thread_id}/stage",
        {"stage": "screening"}).status_code == 302
    with webapp.app.app_context():
        assert db.get_marketing_thread(
            owner_id, thread_id)["pipeline_stage"] == "screening"
        assert db.get_lead(linked_lead_id)["status"] == "screening"
        assert db.list_tasks(linked_lead_id)

    # Real/private workspaces cannot invoke the synthetic records provider.
    assert _post(
        client, f"/marketing-hub/threads/{thread_id}/records",
        {"authorization_confirmed": "1"}).status_code == 302
    with webapp.app.app_context():
        assert db.get_lead(linked_lead_id)["records_connected"] == 0

    outsider = _client_for(outsider_id)
    assert _post(
        outsider, f"/marketing-hub/threads/{thread_id}/stage",
        {"stage": "enrolled"}).status_code == 404
    assert _post(
        outsider, f"/marketing-hub/threads/{thread_id}/records",
        {"authorization_confirmed": "1"}).status_code == 404
    blocked = _post(outsider, f"/marketing-hub/threads/{thread_id}/status", {
        "status": "resolved",
    })
    assert blocked.status_code == 404
    outsider_page = outsider.get(f"/app/inbox?thread={thread_id}")
    assert "Morgan Lee" not in outsider_page.get_data(as_text=True)

    print("PASS: persistent inbox, replies, notes, assignment, handoff, isolation")


def test_demo_reseed_removes_connection_before_source():
    with webapp.app.app_context():
        demo_id = db.create_user(
            "dejosama@fieveclinical.com", "disabled", "Demo Owner",
            verified=True)
        connected = db.connect_marketing_account(
            demo_id, provider="gmail", channel="email",
            external_account_id="discarded-demo-account",
            account_identifier="discarded@example.com",
            access_token_encrypted="encrypted-placeholder",
            granted_scopes=[], token_expires_at="2099-01-01T00:00:00+00:00")
        assert connected
        db.seed_demo_marketing_hub(demo_id)
        assert db.get_db().execute(
            "SELECT COUNT(*) FROM marketing_connections WHERE org_id = ?",
            (db.user_org_id(demo_id),)).fetchone()[0] == 0
        assert any(source["identifier"] == "recruit@fieveclinical.com"
                   for source in db.list_marketing_sources(demo_id))
        # Day-0 demo must include at least one UNASSIGNED thread so the inbox's
        # "Unassigned" owner filter has real unclaimed inquiries to triage (and
        # the "no inquiry sits unclaimed" story is demonstrable out of the box).
        assert any(t["assigned_to"] is None
                   for t in db.list_marketing_threads(demo_id, status="all")), \
            "demo seed has no unassigned thread; Unassigned filter would be empty"
        demo_ncts = {
            "NCT05711940", "NCT07645924", "NCT06559306", "NCT07076407",
            "NCT06922110", "NCT07573176", "NCT07674654", "NCT06417775",
        }
        mine = db.list_marketing_threads(demo_id, status="open", assignee="mine")
        mine_ncts = {t["nct"] for t in mine if t["nct"]}
        assert demo_ncts <= mine_ncts, (
            "every demo trial needs coordinator-owned sample threads; missing "
            + ", ".join(sorted(demo_ncts - mine_ncts)))
        # Switching studies should change the people, not replay one shared cast.
        by_nct = {}
        for t in db.list_marketing_threads(demo_id, status="all"):
            by_nct.setdefault(t["nct"], set()).add(t["contact_name"])
        names = [frozenset(by_nct.get(nct) or ()) for nct in demo_ncts]
        assert len({frozenset(n) for n in names}) == len(demo_ncts), (
            "demo trials must not share the same contact set")
    print("PASS: demo reseed preserves marketing connection FK ordering")


def test_demo_records_and_checklist():
    with webapp.app.app_context():
        demo_id = db.get_user_by_email("dejosama@fieveclinical.com")["id"]
        linked = next(
            row for row in db.list_marketing_threads(demo_id, status="all")
            if row["linked_lead_id"])
        thread_id = linked["id"]
        lead_id = linked["linked_lead_id"]
    client = _client_for(demo_id)
    original_connect = webapp.records_mod.connect
    try:
        webapp.records_mod.connect = lambda *_args, **_kwargs: {
            "provider": "SMART Health IT (sandbox)",
            "age": 34,
            "sex": "female",
            "conditions": ["Migraine"],
            "meds": ["Topiramate"],
            "labs": ["Blood pressure 120/80"],
            "summary": "Synthetic record: migraine and topiramate.",
            "sync_status": "connected",
            "source_status": "sandbox_ready",
            "external_patient_id": "demo-patient",
            "external_query_id": "",
            "last_sync_at": db.now(),
            "last_sync_error": "",
            "completeness_score": 100,
        }
        pulled = _post(
            client, f"/marketing-hub/threads/{thread_id}/records",
            {"authorization_confirmed": "1"})
        assert pulled.status_code == 302
    finally:
        webapp.records_mod.connect = original_connect
    with webapp.app.app_context():
        lead = db.get_lead(lead_id)
        assert lead["records_connected"] == 1
        assert lead["records_authorized_at"]
        assert db.get_records_profile(lead["applicant_token"])["provider"] == \
            "SMART Health IT (sandbox)"

    assert _post(
        client, f"/marketing-hub/threads/{thread_id}/stage",
        {"stage": "screening"}).status_code == 302
    with webapp.app.app_context():
        task = db.list_tasks(lead_id)[0]
    assert _post(
        client,
        f"/marketing-hub/threads/{thread_id}/checklist/{task['id']}",
        {"done": "1"}).status_code == 302
    with webapp.app.app_context():
        assert next(
            row for row in db.list_tasks(lead_id)
            if row["id"] == task["id"])["status"] == "done"
    print("PASS: demo-only records and checklist workflow")


def test_quick_replies_and_context_drafts():
    """Regressions for two composer fixes:
    1. The quick-reply chips silently never rendered because nothing supplied the
       `quick_replies` template variable. They must render again.
    2. Bridget's first-pass draft must ADDRESS the question (scheduling vs visit
       costs), not emit one canned line for every thread."""
    import json as _json
    with webapp.app.app_context():
        owner_id = db.create_user(
            "qr-owner@example.com", "disabled", "QR Owner", verified=True)
    client = _client_for(owner_id)
    assert _post(client, "/marketing-hub/sources", {
        "channel": "email", "label": "Main", "identifier": "hi@example.com",
    }).status_code == 302
    with webapp.app.app_context():
        source_id = db.list_marketing_sources(owner_id)[0]["id"]

    assert _post(client, "/marketing-hub/threads", {
        "source_id": str(source_id), "contact_name": "Nadia Brooks",
        "contact_handle": "nadia@example.com", "subject": "Evening slots?",
        "body": "Do you have evening screening appointments this week?",
    }).status_code == 302
    assert _post(client, "/marketing-hub/threads", {
        "source_id": str(source_id), "contact_name": "Marcus Reed",
        "contact_handle": "marcus@example.com", "subject": "Travel",
        "body": "Is mileage or travel reimbursed for the study visits?",
    }).status_code == 302
    assert _post(client, "/marketing-hub/threads", {
        "source_id": str(source_id), "contact_name": "Elle Quinn",
        "contact_handle": "elle@example.com", "subject": "Eligibility",
        "body": "Do I qualify? What are the eligibility criteria for this study?",
    }).status_code == 302
    assert _post(client, "/marketing-hub/threads", {
        "source_id": str(source_id), "contact_name": "Sam Rivers",
        "contact_handle": "sam@example.com", "subject": "Safety",
        "body": "Is this safe? Will I just get a placebo?",
    }).status_code == 302
    assert _post(client, "/marketing-hub/threads", {
        "source_id": str(source_id), "contact_name": "Pat Long",
        "contact_handle": "pat@example.com", "subject": "Time",
        "body": "How long is the study and how many visits are there?",
    }).status_code == 302
    assert _post(client, "/marketing-hub/threads", {
        "source_id": str(source_id), "contact_name": "Dana Quit",
        "contact_handle": "dana@example.com", "subject": "Leaving",
        "body": "Can I withdraw or drop out later if I change my mind?",
    }).status_code == 302
    assert _post(client, "/marketing-hub/threads", {
        "source_id": str(source_id), "contact_name": "Lee Place",
        "contact_handle": "lee@example.com", "subject": "Location",
        "body": "Where is this located and can any visits be done remotely?",
    }).status_code == 302
    assert _post(client, "/marketing-hub/threads", {
        "source_id": str(source_id), "contact_name": "Priya Vault",
        "contact_handle": "priya@example.com", "subject": "Privacy",
        "body": "Is my information private? Who sees it and will you spam me?",
    }).status_code == 302

    with webapp.app.app_context():
        ids = {t["contact_name"]: t["id"]
               for t in db.list_marketing_threads(owner_id)}

    page = client.get(f"/app/inbox?thread={ids['Nadia Brooks']}").get_data(
        as_text=True)
    assert "mh-quick-chip" in page, "quick-reply chips did not render"
    assert "Offer a screening call" in page

    sched = _json.loads(client.get(
        f"/marketing-hub/threads/{ids['Nadia Brooks']}/draft.json").get_data(
        as_text=True))
    travel = _json.loads(client.get(
        f"/marketing-hub/threads/{ids['Marcus Reed']}/draft.json").get_data(
        as_text=True))
    elig = _json.loads(client.get(
        f"/marketing-hub/threads/{ids['Elle Quinn']}/draft.json").get_data(
        as_text=True))
    safety = _json.loads(client.get(
        f"/marketing-hub/threads/{ids['Sam Rivers']}/draft.json").get_data(
        as_text=True))
    length = _json.loads(client.get(
        f"/marketing-hub/threads/{ids['Pat Long']}/draft.json").get_data(
        as_text=True))
    withdraw = _json.loads(client.get(
        f"/marketing-hub/threads/{ids['Dana Quit']}/draft.json").get_data(
        as_text=True))
    location = _json.loads(client.get(
        f"/marketing-hub/threads/{ids['Lee Place']}/draft.json").get_data(
        as_text=True))
    privacy = _json.loads(client.get(
        f"/marketing-hub/threads/{ids['Priya Vault']}/draft.json").get_data(
        as_text=True))
    assert sched["ok"] and travel["ok"] and elig["ok"] and safety["ok"]
    assert length["ok"] and withdraw["ok"] and location["ok"] and privacy["ok"]
    assert "times" in sched["draft"].lower(), sched["draft"]
    assert "cost" in travel["draft"].lower(), travel["draft"]
    assert "pre-screening" in elig["draft"].lower(), elig["draft"]
    assert "safety" in safety["draft"].lower(), safety["draft"]
    assert "time commitment" in length["draft"].lower(), length["draft"]
    assert "voluntary" in withdraw["draft"].lower(), withdraw["draft"]
    assert "remotely" in location["draft"].lower(), location["draft"]
    assert "privacy" in privacy["draft"].lower(), privacy["draft"]
    # All eight intents must produce distinct drafts.
    assert len({sched["draft"], travel["draft"], elig["draft"], safety["draft"],
                length["draft"], withdraw["draft"], location["draft"],
                privacy["draft"]}) == 8, \
        "drafts are not context-aware"
    print("PASS: quick-reply chips render + context-aware Bridget drafts")


def test_unread_by_study_counts():
    """Top-switcher badge data: unread threads grouped by study NCT. Unassigned
    threads (no NCT) must be excluded so a badge only reflects a real trial."""
    with webapp.app.app_context():
        owner_id = db.create_user(
            "ubys-owner@example.com", "disabled", "UBYS Owner", verified=True)
        db.add_study_claim(owner_id, "NCT10000001", "Study A", verified=True)
        db.add_study_claim(owner_id, "NCT10000002", "Study B", verified=True)
    client = _client_for(owner_id)
    assert _post(client, "/marketing-hub/sources", {
        "channel": "email", "label": "Main", "identifier": "u@example.com",
    }).status_code == 302
    with webapp.app.app_context():
        source_id = db.list_marketing_sources(owner_id)[0]["id"]
    for name in ("A1", "A2", "B1", "Unassigned"):
        assert _post(client, "/marketing-hub/threads", {
            "source_id": str(source_id), "contact_name": name,
            "contact_handle": f"{name}@example.com", "subject": "hi",
            "body": "hello",
        }).status_code == 302
    with webapp.app.app_context():
        ids = {t["contact_name"]: t["id"]
               for t in db.list_marketing_threads(owner_id)}
        conn = db.get_db()
        for name, nct, unread in (("A1", "NCT10000001", 1),
                                  ("A2", "NCT10000001", 1),
                                  ("B1", "NCT10000002", 0),
                                  ("Unassigned", "", 1)):
            conn.execute(
                "UPDATE marketing_threads SET nct = ?, unread = ? WHERE id = ?",
                (nct, unread, ids[name]))
        conn.commit()
        counts = db.marketing_unread_by_study(owner_id)
        assert counts.get("NCT10000001") == 2, counts
        assert counts.get("NCT10000002", 0) == 0, counts
        assert "" not in counts, counts
    print("PASS: unread-by-study counts for switcher badges")


def test_marketing_seed_is_gated_to_demo_account():
    """Safety: the rich inbox seeder must NEVER populate (or wipe) a non-demo
    account. It assumes the demo org and clears existing marketing data on first
    run, so a fresh real account must be left with an empty inbox to connect its
    own channels. (This is why the demo-mode day-0 populated experience lives on
    the throwaway demo account, not on arbitrary fresh accounts.)"""
    with webapp.app.app_context():
        uid = db.create_user(
            "not-demo@example.com", "disabled", "Real Coordinator",
            verified=True)
        db.seed_demo_marketing_hub(uid)
        assert len(db.list_marketing_threads(uid, status="all")) == 0
        assert len(db.list_marketing_sources(uid)) == 0
    print("PASS: rich inbox seed stays gated to the demo account")


def test_inbox_study_scoper():
    """In-inbox study scoper (swap trials without leaving the inbox):
    1. A team with >1 study renders the .mh-scope select with an option per study.
    2. A single-study team does NOT render it (no useless control).
    3. set_scope actually re-scopes the thread list (pick study B -> only B's
       threads show), proving the control is wired end-to-end, not decorative."""
    with webapp.app.app_context():
        owner_id = db.create_user(
            "scoper-owner@example.com", "disabled", "Scoper Owner", verified=True)
        db.add_study_claim(owner_id, "NCT20000001", "Alpha Study", verified=True)
        db.add_study_claim(owner_id, "NCT20000002", "Beta Study", verified=True)
    client = _client_for(owner_id)
    assert _post(client, "/marketing-hub/sources", {
        "channel": "email", "label": "Main", "identifier": "s@example.com",
    }).status_code == 302
    with webapp.app.app_context():
        source_id = db.list_marketing_sources(owner_id)[0]["id"]
    # GammaThread stays UNASSIGNED (no study) on purpose - see the last check.
    for name in ("AlphaThread", "BetaThread", "GammaThread"):
        assert _post(client, "/marketing-hub/threads", {
            "source_id": str(source_id), "contact_name": name,
            "contact_handle": f"{name}@example.com", "subject": "hi",
            "body": "hello",
        }).status_code == 302
    with webapp.app.app_context():
        ids = {t["contact_name"]: t["id"]
               for t in db.list_marketing_threads(owner_id)}
        conn = db.get_db()
        conn.execute("UPDATE marketing_threads SET nct = ?, unread = 1 WHERE id = ?",
                     ("NCT20000001", ids["AlphaThread"]))
        conn.execute("UPDATE marketing_threads SET nct = ?, unread = 1 WHERE id = ?",
                     ("NCT20000002", ids["BetaThread"]))
        conn.execute("UPDATE marketing_threads SET nct = '' WHERE id = ?",
                     (ids["GammaThread"],))
        conn.commit()

    # 1. Multi-study team: scoper renders with an option per study, and each
    #    option surfaces its unread count (the inbox's only per-trial attention
    #    cue, since the top switcher is hidden here).
    html = client.get("/app/inbox").get_data(as_text=True)
    assert 'class="mh-scope"' in html, "scoper missing for a multi-study team"
    assert 'value="NCT20000001"' in html and 'value="NCT20000002"' in html
    assert "Alpha Study (1)" in html, "scoper option missing unread count"

    # 3. Selecting study B re-scopes the list to B's threads only.
    assert client.get(
        "/app/scope?nct=NCT20000002&next=/app/inbox").status_code == 302
    scoped = client.get("/app/inbox").get_data(as_text=True)
    assert "BetaThread" in scoped, "scoped-to-B inbox lost B's thread"
    assert "AlphaThread" not in scoped, "scoped-to-B inbox still shows A's thread"
    # "No inquiry sits unclaimed": an UNASSIGNED (no-study) thread must stay
    # visible in every study scope so it can't get lost behind the scoper.
    assert "GammaThread" in scoped, "unassigned thread vanished under a study scope"

    # 2. Single-study team: no scoper (would be a useless control).
    with webapp.app.app_context():
        solo_id = db.create_user(
            "scoper-solo@example.com", "disabled", "Solo", verified=True)
        db.add_study_claim(solo_id, "NCT20000009", "Only Study", verified=True)
    solo_html = _client_for(solo_id).get("/app/inbox").get_data(as_text=True)
    assert 'class="mh-scope"' not in solo_html, "scoper shown for single-study team"
    print("PASS: in-inbox study scoper renders, gates, and re-scopes the list")


def test_inbox_owner_filter():
    """Ownership triage: 'Mine' shows threads assigned to me, 'Unassigned' shows
    threads nobody owns yet (so nothing sits unclaimed), 'All owners' shows the
    whole team inbox. The filter persists in the session across navigation."""
    with webapp.app.app_context():
        owner_id = db.create_user(
            "own-filter-a@example.com", "disabled", "Owner A", verified=True)
        mate_id = db.create_user(
            "own-filter-b@example.com", "disabled", "Owner B", verified=True)
        invite = db.create_org_invite(
            owner_id, "own-filter-b@example.com", "student")
        assert db.accept_org_invite(mate_id, invite)
    client = _client_for(owner_id)
    assert _post(client, "/marketing-hub/sources", {
        "channel": "email", "label": "Main", "identifier": "of@example.com",
    }).status_code == 302
    with webapp.app.app_context():
        source_id = db.list_marketing_sources(owner_id)[0]["id"]
    for name in ("MineThread", "MateThread", "NobodyThread"):
        assert _post(client, "/marketing-hub/threads", {
            "source_id": str(source_id), "contact_name": name,
            "contact_handle": f"{name}@example.com", "subject": "hi",
            "body": "hello",
        }).status_code == 302
    with webapp.app.app_context():
        ids = {t["contact_name"]: t["id"]
               for t in db.list_marketing_threads(owner_id)}
        conn = db.get_db()
        conn.execute("UPDATE marketing_threads SET assigned_to = ? WHERE id = ?",
                     (owner_id, ids["MineThread"]))
        conn.execute("UPDATE marketing_threads SET assigned_to = ? WHERE id = ?",
                     (mate_id, ids["MateThread"]))
        conn.execute("UPDATE marketing_threads SET assigned_to = NULL WHERE id = ?",
                     (ids["NobodyThread"],))
        conn.commit()

    # All owners (default): every thread shows, and the owner control renders.
    allv = client.get("/app/inbox?status=all").get_data(as_text=True)
    assert 'name="owner"' in allv, "owner filter control did not render"
    assert ("MineThread" in allv and "MateThread" in allv
            and "NobodyThread" in allv)

    # Mine: only my thread.
    mine = client.get("/app/inbox?status=all&owner=mine").get_data(as_text=True)
    assert "MineThread" in mine
    assert "MateThread" not in mine and "NobodyThread" not in mine

    # Session persistence: a plain reload (no owner param) keeps the Mine filter.
    mine2 = client.get("/app/inbox?status=all").get_data(as_text=True)
    assert "MineThread" in mine2 and "MateThread" not in mine2

    # Unassigned: only the unowned thread ("no inquiry sits unclaimed").
    un = client.get("/app/inbox?status=all&owner=unassigned").get_data(as_text=True)
    assert "NobodyThread" in un
    assert "MineThread" not in un and "MateThread" not in un

    # Clear back to everyone.
    cleared = client.get("/app/inbox?status=all&owner=").get_data(as_text=True)
    assert ("MineThread" in cleared and "MateThread" in cleared
            and "NobodyThread" in cleared)

    # Contract lock: marketing_thread_counts must stay GLOBAL with no assignee
    # (the site-wide unread badge relies on this) and only narrow when asked.
    # Guards against a future change that owner-filters the shared helper for all
    # callers and silently breaks the global badge.
    with webapp.app.app_context():
        assert db.marketing_thread_counts(owner_id)["total"] == 3
        assert db.marketing_thread_counts(owner_id, assignee="mine")["total"] == 1
        assert db.marketing_thread_counts(
            owner_id, assignee="unassigned")["total"] == 1
    print("PASS: inbox owner filter (mine/unassigned/all) + session persistence")


def test_claim_unassigned_thread_end_to_end():
    """The full triage loop: an unclaimed inquiry shows under 'Unassigned', and
    claiming it via the real assign endpoint moves it into 'Mine' (and out of
    'Unassigned'). Proves the surfaced-unclaimed workflow is actually actionable,
    not just visible."""
    with webapp.app.app_context():
        owner_id = db.create_user(
            "claim-owner@example.com", "disabled", "Claim Owner", verified=True)
    client = _client_for(owner_id)
    assert _post(client, "/marketing-hub/sources", {
        "channel": "email", "label": "Main", "identifier": "claim@example.com",
    }).status_code == 302
    with webapp.app.app_context():
        source_id = db.list_marketing_sources(owner_id)[0]["id"]
    assert _post(client, "/marketing-hub/threads", {
        "source_id": str(source_id), "contact_name": "UnclaimedLead",
        "contact_handle": "lead@example.com", "subject": "hi", "body": "hello",
    }).status_code == 302
    with webapp.app.app_context():
        tid = db.list_marketing_threads(owner_id)[0]["id"]
        db.get_db().execute(
            "UPDATE marketing_threads SET assigned_to = NULL WHERE id = ?", (tid,))
        db.get_db().commit()

    # Starts unclaimed: in Unassigned, not in Mine.
    assert "UnclaimedLead" in client.get(
        "/app/inbox?status=all&owner=unassigned").get_data(as_text=True)
    assert "UnclaimedLead" not in client.get(
        "/app/inbox?status=all&owner=mine").get_data(as_text=True)

    # Opening the unclaimed thread shows the one-click Claim button.
    opened = client.get(f"/app/inbox?status=all&thread={tid}").get_data(as_text=True)
    assert ">Claim<" in opened, "Claim button missing on an unassigned thread"

    # Claim it through the real assign endpoint.
    assert _post(client, f"/marketing-hub/threads/{tid}/assign",
                 {"assignee_id": str(owner_id)}).status_code == 302

    # Once owned, the Claim button is gone (dropdown handles reassignment).
    reopened = client.get(f"/app/inbox?thread={tid}").get_data(as_text=True)
    assert ">Claim<" not in reopened, "Claim button lingered after claiming"

    # Now mine, and no longer in the unclaimed queue.
    assert "UnclaimedLead" in client.get(
        "/app/inbox?status=all&owner=mine").get_data(as_text=True)
    assert "UnclaimedLead" not in client.get(
        "/app/inbox?status=all&owner=unassigned").get_data(as_text=True)
    print("PASS: claim unassigned thread -> moves from Unassigned to Mine")


def test_owed_reply_sorts_first():
    """Triage default: a thread we still owe a reply to floats above a NEWER
    thread we've already answered - so an aging owed reply can't get buried under
    fresh-but-handled conversations."""
    with webapp.app.app_context():
        owner_id = db.create_user(
            "owed-sort@example.com", "disabled", "Owed Sort", verified=True)
    client = _client_for(owner_id)
    assert _post(client, "/marketing-hub/sources", {
        "channel": "email", "label": "Main", "identifier": "owed@example.com",
    }).status_code == 302
    with webapp.app.app_context():
        source_id = db.list_marketing_sources(owner_id)[0]["id"]
    # A arrives first and stays unanswered (owed).
    assert _post(client, "/marketing-hub/threads", {
        "source_id": str(source_id), "contact_name": "OldOwed",
        "contact_handle": "old@example.com", "subject": "hi", "body": "hello",
    }).status_code == 302
    # B arrives later; we reply, so it's newer but NOT owed.
    assert _post(client, "/marketing-hub/threads", {
        "source_id": str(source_id), "contact_name": "NewReplied",
        "contact_handle": "new@example.com", "subject": "hi", "body": "hello",
    }).status_code == 302
    with webapp.app.app_context():
        b_id = next(t["id"] for t in db.list_marketing_threads(owner_id)
                    if t["contact_name"] == "NewReplied")
    assert _post(client, f"/marketing-hub/threads/{b_id}/reply",
                 {"body": "answered you"}).status_code == 302

    with webapp.app.app_context():
        order = [t["contact_name"] for t in db.list_marketing_threads(owner_id)]
    # Pure recency would put NewReplied first (we just replied to it); owed-first
    # puts OldOwed first.
    assert order.index("OldOwed") < order.index("NewReplied"), \
        f"owed reply not floated above a newer answered thread: {order}"
    print("PASS: owed-reply thread sorts above a newer already-answered one")


def test_awaiting_reply_badge():
    """Response-aging triage: a thread whose last real message is inbound shows an
    'Awaiting reply' badge (we owe a response). An internal note must NOT clear it
    (a note isn't a reply), but an actual outbound reply must."""
    with webapp.app.app_context():
        owner_id = db.create_user(
            "await-owner@example.com", "disabled", "Await Owner", verified=True)
    client = _client_for(owner_id)
    assert _post(client, "/marketing-hub/sources", {
        "channel": "email", "label": "Main", "identifier": "await@example.com",
    }).status_code == 302
    with webapp.app.app_context():
        source_id = db.list_marketing_sources(owner_id)[0]["id"]
    assert _post(client, "/marketing-hub/threads", {
        "source_id": str(source_id), "contact_name": "WaitingLead",
        "contact_handle": "wait@example.com", "subject": "hi", "body": "hello",
    }).status_code == 302
    with webapp.app.app_context():
        tid = db.list_marketing_threads(owner_id)[0]["id"]

    # Fresh inbound thread -> we owe a reply.
    assert "Awaiting reply" in client.get("/app/inbox").get_data(as_text=True)

    # An internal note is not a reply: the badge must remain.
    assert _post(client, f"/marketing-hub/threads/{tid}/note",
                 {"body": "flagging for the PI"}).status_code == 302
    assert "Awaiting reply" in client.get("/app/inbox").get_data(as_text=True), \
        "an internal note should not clear the awaiting-reply badge"

    # An actual outbound reply clears it.
    assert _post(client, f"/marketing-hub/threads/{tid}/reply",
                 {"body": "thanks for reaching out"}).status_code == 302
    assert "Awaiting reply" not in client.get("/app/inbox").get_data(as_text=True), \
        "an outbound reply should clear the awaiting-reply badge"
    print("PASS: awaiting-reply badge (inbound owes reply; note doesn't count)")


def test_empty_inbox_prompts_channel_connect():
    """Day-0 orientation: a fresh account with no channels connected must show a
    'connect a channel' call to action (not a dead 'all caught up' state), so a
    new coordinator knows the very first step."""
    with webapp.app.app_context():
        uid = db.create_user(
            "empty-inbox@example.com", "disabled", "Empty Inbox", verified=True)
    client = _client_for(uid)
    html = client.get("/app/inbox").get_data(as_text=True)
    assert "Connect a channel to start" in html, "no connect prompt on empty inbox"
    assert "Connect your first channel" in html
    print("PASS: empty inbox prompts channel connect for day-0 orientation")


def main():
    try:
        test_marketing_hub_flow()
        test_quick_replies_and_context_drafts()
        test_unread_by_study_counts()
        test_inbox_study_scoper()
        test_inbox_owner_filter()
        test_claim_unassigned_thread_end_to_end()
        test_owed_reply_sorts_first()
        test_awaiting_reply_badge()
        test_empty_inbox_prompts_channel_connect()
        test_marketing_seed_is_gated_to_demo_account()
        test_demo_reseed_removes_connection_before_source()
        test_demo_records_and_checklist()
        print("PASS: marketing hub tests")
    finally:
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass


if __name__ == "__main__":
    main()
