"""Regression checks for Omni (/omni): engine, routes, rules, drafting and
setup changes by prompt. No network, no LLM key.

Run: python test_omni_hub.py
"""
import os
import re
import tempfile

_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["DB_PATH"] = _TMP_DB
os.environ["NO_LOGIN"] = "0"
os.environ["SITE_DEMO"] = "0"
os.environ["ALERTS_BACKGROUND"] = "0"
os.environ["REMINDERS_BACKGROUND"] = "0"
os.environ["SECRET_KEY"] = "omni-test-secret"
os.environ.pop("LLM_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)

import app as webapp  # noqa: E402
import db  # noqa: E402
from copy_sanitize import contains_em_dash  # noqa: E402
from omni_hub import filters, models, seeds, spec as spec_mod  # noqa: E402
from omni_hub.ai import refine, policy  # noqa: E402

CSRF = "omni-test-csrf"
PASSED = []


def _client():
    c = webapp.app.test_client()
    with c.session_transaction() as s:
        s[webapp.CSRF_SESSION_KEY] = CSRF
    return c


def _post(c, path, data=None, json=None):
    if json is not None:
        return c.post(path, json=json, headers={"X-CSRF-Token": CSRF})
    return c.post(path, data={"_csrf_token": CSRF, **(data or {})})


def _quickstart(c, key="pi_law_firm"):
    r = _post(c, "/omni/quickstart", {"template_key": key})
    assert r.status_code == 302, r.status_code
    wid = r.headers["Location"].rsplit("/", 1)[-1]
    return wid


def _ws(wid):
    with webapp.app.app_context():
        return models.get_workspace(wid)


def _convs(wid):
    with webapp.app.app_context():
        ws = models.get_workspace(wid)
        from omni_hub import inbox
        return inbox.load(ws["id"], include_hidden_sources=True)


def ok(name):
    PASSED.append(name)
    print(f"ok {name}")


# --------------------------------------------------------------------------- #
def test_schema_installs():
    con = db.sqlite3.connect(_TMP_DB)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    for t in ("om_workspaces", "om_conversations", "om_messages", "om_fields",
              "om_field_values", "om_views", "om_rules", "om_actions"):
        assert t in names, t
    ok("schema installs alongside the main schema")


def test_quickstart_builds_and_sets_cookie():
    c = _client()
    r = _post(c, "/omni/quickstart", {"template_key": "pi_law_firm"})
    assert r.status_code == 302
    assert any(h[0] == "Set-Cookie" and "om_owner=" in h[1] for h in r.headers)
    wid = r.headers["Location"].rsplit("/", 1)[-1]
    ws = _ws(wid)
    assert ws["builder_state"] == "built" and ws["spec_version"] == 1
    convs = _convs(wid)
    assert 14 <= len(convs) <= 20, len(convs)
    assert all(c_["f"] for c_ in convs), "every conversation has field records"
    r = c.get(f"/omni/w/{wid}")
    assert r.status_code == 200 and b"Harbor Point Injury Law" in r.data
    ok("quick start builds a workspace and sets the owner cookie")


def test_seed_is_idempotent():
    c = _client()
    wid = _quickstart(c)
    before = len(_convs(wid))
    with webapp.app.app_context():
        ws = models.get_workspace(wid)
        seeds.seed_workspace(ws, seeds.get("pi_law_firm"), ws["spec"])
    assert len(_convs(wid)) == before
    ok("seeding twice adds nothing")


def test_filters_evaluate():
    conv = {"stage": "new", "status": "open", "assignee": "", "priority": "urgent", "unread": 1,
            "awaiting_reply": True, "last_message_at": "2026-08-25 10:00",
            "subject": "Court date", "last_body": "my hearing is tomorrow",
            "flags": [{"rule_key": "x", "label": "Court soon"}],
            "f": {"has_attorney": {"value": False, "status": "found"},
                  "incident_date": {"value": "2026-08-20", "status": "found"},
                  "severity": {"value": "serious", "status": "found"}}}
    ev = filters.evaluate
    assert ev({"field": "f.has_attorney", "op": "eq", "value": False}, conv)
    assert ev({"field": "f.has_attorney", "op": "neq", "value": True}, conv)
    assert ev({"field": "f.missing", "op": "neq", "value": True}, conv), "missing is not true"
    assert not ev({"field": "f.missing", "op": "eq", "value": True}, conv)
    assert ev({"field": "f.missing", "op": "is_unknown"}, conv)
    assert ev({"field": "f.severity", "op": "in", "value": ["serious", "catastrophic"]}, conv)
    assert ev({"field": "f.incident_date", "op": "within_days", "value": 30}, conv)
    assert not ev({"field": "f.incident_date", "op": "older_than_days", "value": 540}, conv)
    assert ev({"field": "assignee", "op": "is_empty"}, conv)
    assert ev({"field": "awaiting_reply", "op": "eq", "value": True}, conv)
    assert ev({"field": "text", "op": "contains", "value": "hearing"}, conv)
    assert ev({"field": "flags", "op": "has_flag", "value": "Court soon"}, conv)
    assert ev({"all": [{"field": "stage", "op": "eq", "value": "new"},
                       {"any": [{"field": "priority", "op": "eq", "value": "low"},
                                {"field": "priority", "op": "eq", "value": "urgent"}]}]}, conv)
    ok("filter expressions evaluate, unknowns never raise")


def test_rules_fired_on_build():
    c = _client()
    wid = _quickstart(c)
    convs = {x["seed_key"]: x for x in _convs(wid)}
    wi = convs["pi_06"]
    assert wi["stage"] == "declined", wi["stage"]
    assert any(f.get("rule_key") == "reject_out_of_state" for f in wi["flags"])
    assert convs["pi_05"]["priority"] == "urgent"
    assert any(f.get("label") == "No recorded statement" for f in convs["pi_03"]["flags"])
    with webapp.app.app_context():
        ws = models.get_workspace(wid)
        pending = models.list_pending_actions(ws["id"], wi["id"])
    assert pending and pending[0]["kind"] == "send_reply", "auto reject proposes a decline"
    assert "not a matter we can take on" in pending[0]["payload"]["text"]
    ok("rules fire on build: auto reject moves, flags and proposes a decline")


def test_views_and_counts():
    c = _client()
    wid = _quickstart(c)
    ws = _ws(wid)
    from omni_hub import inbox
    convs = _convs(wid)
    counts = inbox.view_counts(ws["spec"]["views"], convs)
    assert counts["needs_reply"] >= 8, counts
    assert counts["serious_no_attorney"] >= 3, counts
    assert counts["insurer_pressure"] >= 2, counts
    r = c.get(f"/omni/w/{wid}?view=serious_no_attorney")
    assert r.status_code == 200 and b"Marcus Bell" in r.data
    ok("views count and filter the seeded conversations")


def test_reply_and_note_are_saved():
    c = _client()
    wid = _quickstart(c)
    conv = next(x for x in _convs(wid) if x["seed_key"] == "pi_16")
    r = _post(c, f"/omni/w/{wid}/t/{conv['id']}/reply", {"body": "Hi, yes we do. What days work?"})
    assert r.status_code == 302
    with webapp.app.app_context():
        msgs = models.list_messages(conv["id"])
    assert msgs[-1]["kind"] == "outbound" and msgs[-1]["delivery_status"] == "saved"
    after = next(x for x in _convs(wid) if x["seed_key"] == "pi_16")
    assert not after["awaiting_reply"]
    r = _post(c, f"/omni/w/{wid}/t/{conv['id']}/note", {"body": "Called, no answer."})
    assert r.status_code == 302
    ok("reply stores a saved outbound message and clears needs-reply")


def test_draft_uses_canned_then_ladder():
    c = _client()
    wid = _quickstart(c)
    conv = next(x for x in _convs(wid) if x["seed_key"] == "pi_08")
    r = _post(c, f"/omni/w/{wid}/t/{conv['id']}/draft.json", json={"instruction": ""})
    d = r.get_json()
    assert r.status_code == 200 and d["ok"]
    assert d["draft"].startswith("Hi Ana, good question")
    assert d["policy"] == "review" and d["category"] == "pricing", d
    r = _post(c, f"/omni/w/{wid}/t/{conv['id']}/draft.json", json={"instruction": "send the booking link"})
    d = r.get_json()
    assert "link to book" in d["draft"], d["draft"]
    r = _post(c, f"/omni/w/{wid}/t/{conv['id']}/draft.json", json={"instruction": "tell them they qualify"})
    assert r.status_code == 422
    ok("composer: canned draft, ladder by intent, guardrail blocks forbidden instructions")


def test_policy_categories():
    assert policy.categorize("how much do you charge") == "pricing"
    assert policy.categorize("some days i dont want to be here anymore") == "distress"
    assert policy.categorize("do I qualify for this study") == "medical_legal"
    assert policy.categorize("what days are you open") == "routine"
    spec = {"approval_policy": {"routine": "auto_send", "pricing": "review",
                                "medical_legal": "review", "distress": "auto_send"}}
    assert policy.decide(spec, "routine") == "auto_send"
    assert policy.decide(spec, "distress") == "review", "distress never auto sends"
    ok("policy categories and decisions")


def test_refine_parses_plain_words():
    ws = None
    c = _client()
    wid = _quickstart(c)
    spec = _ws(wid)["spec"]
    p = refine.parse("add a field for whether a police report was filed", spec)
    assert p["ops"][0]["op"] == "add_field" and p["ops"][0]["field"]["type"] == "bool", p
    p = refine.parse("add a view of serious injuries with no lawyer", spec)
    assert p["ops"][0]["op"] == "add_view", p
    clauses = p["ops"][0]["view"]["filter"]["all"]
    fields = {cl["field"] for cl in clauses}
    assert "f.injury_severity" in fields and "f.has_attorney" in fields, clauses
    p = refine.parse("flag anyone mentioning a court date", spec)
    assert p["ops"][0]["op"] == "add_rule" and p["ops"][0]["rule"]["then"]["type"] == "flag", p
    assert p["ops"][0]["rule"]["when"]["all"][0] == {"field": "text", "op": "contains", "value": "court date"}
    p = refine.parse("auto reject anyone outside IL and IN", spec)
    assert p["ops"][0]["rule"]["then"]["type"] == "auto_reject", p
    assert {"field": "f.state", "op": "not_in", "value": ["IL", "IN"]} in p["ops"][0]["rule"]["when"]["all"]
    p = refine.parse("let Dana send pricing replies on her own", spec)
    assert p["ops"] == [{"op": "set_policy", "category": "pricing", "mode": "auto_send"}], p
    p = refine.parse("review everything before sending", spec)
    assert len(p["ops"]) == 4 and all(o["mode"] == "review" for o in p["ops"]), p
    p = refine.parse("make it more formal", spec)
    assert p["ops"] == [{"op": "set_tone", "voice": "formal"}], p
    p = refine.parse("rename Contacted to Reached out", spec)
    assert p["ops"] == [{"op": "rename_stage", "key": "contacted", "label": "Reached out"}], p
    p = refine.parse("sign off as Dana at Harbor Point", spec)
    assert p["ops"][0]["op"] == "set_tone" and p["ops"][0]["signoff"] == "Dana at Harbor Point"
    p = refine.parse("add a view of people available in 30 days", spec)
    assert p["ops"][0]["view"]["filter"]["all"][0]["op"] == "within_days"
    p = refine.parse("blorp the fizz", spec)
    assert not p["ops"] and "add a field" in p["hint"]
    ok("refine parses fields, views, flags, auto reject, policy, tone, rename, sign off")


def test_change_flow_add_field_backfills():
    c = _client()
    wid = _quickstart(c)
    r = _post(c, f"/omni/w/{wid}/change", json={"instruction": "add a field for whether a police report was filed"})
    d = r.get_json()
    assert r.status_code == 200 and d["ok"], d
    prop = d["proposal"]
    assert prop["token"] and prop["diff"][0].startswith("Add field: Police report"), prop
    assert "re-read" in prop["after"]
    r = _post(c, f"/omni/w/{wid}/change/apply", json={"token": prop["token"]})
    d = r.get_json()
    assert r.status_code == 200 and d["ok"], d
    ws = _ws(wid)
    keys = [f["key"] for f in ws["spec"]["fields"]]
    assert "police_report_was_filed" in keys, keys
    assert ws["spec_version"] == 2
    convs = {x["seed_key"]: x for x in _convs(wid)}
    rec = convs["pi_06"]["f"].get("police_report_was_filed")
    assert rec and rec["value"] is True, rec
    # Second apply of the same token is refused.
    r = _post(c, f"/omni/w/{wid}/change/apply", json={"token": prop["token"]})
    assert r.status_code == 409
    ok("setup change by prompt: propose, apply once, backfill the new field")


def test_change_flow_view_and_rule():
    c = _client()
    wid = _quickstart(c)
    r = _post(c, f"/omni/w/{wid}/change", json={"instruction": "add a view of serious injuries with no lawyer"})
    tok = r.get_json()["proposal"]["token"]
    r = _post(c, f"/omni/w/{wid}/change/apply", json={"token": tok})
    assert r.status_code == 200 and "view=" in r.get_json()["redirect"]
    r = _post(c, f"/omni/w/{wid}/change", json={"instruction": "flag anyone mentioning a court date"})
    tok = r.get_json()["proposal"]["token"]
    r = _post(c, f"/omni/w/{wid}/change/apply", json={"token": tok})
    assert r.status_code == 200 and r.get_json()["redirect"].endswith("/rules")
    ws = _ws(wid)
    assert any("court date" in (x.get("text") or "") for x in ws["spec"]["rules"])
    r = c.get(f"/omni/w/{wid}/rules")
    assert r.status_code == 200 and b"court date" in r.data
    ok("setup change by prompt: views and rules land on the right tab")


def test_protected_field_is_refused():
    c = _client()
    wid = _quickstart(c)
    r = _post(c, f"/omni/w/{wid}/change", json={"instruction": "add a field for immigration status"})
    d = r.get_json()
    assert r.status_code == 200 and d["proposal"]["token"] is None, d
    assert "never in writing" in d["proposal"]["refused"][0], d
    ok("a protected field is refused with the template's reason")


def test_agent_action_confirm_once():
    c = _client()
    wid = _quickstart(c)
    conv = next(x for x in _convs(wid) if x["seed_key"] == "pi_01")
    r = _post(c, f"/omni/w/{wid}/agent", json={"action": "booking", "thread_id": conv["id"]})
    d = r.get_json()
    assert r.status_code == 200 and d["proposal"]["token"], d
    tok = d["proposal"]["token"]
    r = _post(c, f"/omni/w/{wid}/act", json={"token": tok, "text": "Hi Teresa, here is the link to book."})
    assert r.status_code == 200 and r.get_json()["ok"]
    with webapp.app.app_context():
        msgs = models.list_messages(conv["id"])
    assert msgs[-1]["by_agent"] == 1 and msgs[-1]["body"].startswith("Hi Teresa")
    r = _post(c, f"/omni/w/{wid}/act", json={"token": tok})
    assert r.status_code == 400 and "Already handled" in r.get_json()["message"]
    ok("agent proposal confirms exactly once")


def test_ingest_paste_creates_conversation():
    c = _client()
    wid = _quickstart(c)
    sample = seeds.get("pi_law_firm")["sample_inputs"][0]
    r = _post(c, f"/omni/w/{wid}/ingest/paste", {"source": "avvo", "text": sample["text"]})
    assert r.status_code == 302 and "thread=" in r.headers["Location"]
    cid = int(r.headers["Location"].rsplit("thread=", 1)[-1])
    conv = next(x for x in _convs(wid) if x["id"] == cid)
    assert conv["contact"]["name"] == "Renee Castillo", conv["contact"]
    assert conv["f"]["incident_type"]["value"] == "slip_and_fall", conv["f"]["incident_type"]
    assert conv["f"]["treated"]["value"] is True
    ok("pasted lead email becomes a conversation with extracted fields")


def test_owner_only_reset():
    c = _client()
    wid = _quickstart(c)
    other = _client()
    r = _post(other, f"/omni/w/{wid}/reset")
    assert r.status_code == 302 and "/omni" in r.headers["Location"]
    r = _post(c, f"/omni/w/{wid}/reset")
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/omni/w/{wid}")
    assert 14 <= len(_convs(wid)) <= 20
    ok("reset is owner only and re-seeds")


def test_every_template_builds():
    c = _client()
    for t in seeds.available():
        wid = _quickstart(c, t["key"])
        ws = _ws(wid)
        assert ws["builder_state"] == "built", t["key"]
        convs = _convs(wid)
        assert 12 <= len(convs) <= 20, (t["key"], len(convs))
        r = c.get(f"/omni/w/{wid}?thread={convs[0]['id']}")
        assert r.status_code == 200, t["key"]
        for sub in ("connect", "rules", "setup"):
            assert c.get(f"/omni/w/{wid}/{sub}").status_code == 200, (t["key"], sub)
    ok(f"every template builds and renders ({len(seeds.available())} templates)")


def test_templates_have_no_em_dashes():
    def walk(o):
        if isinstance(o, str):
            assert not contains_em_dash(o), o[:80]
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)
    for t in seeds.available():
        walk(t)
    ok("no em dashes in any template")


def test_detect_template():
    key, _ = seeds.detect("Personal injury law firm. Leads come from Avvo and our website form.")
    assert key == "pi_law_firm", key
    key, _ = seeds.detect("I run a pottery studio and get inquiries from Instagram")
    assert key == "generic" or key in seeds.TEMPLATE_KEYS
    for t in seeds.list_templates():
        key, score = seeds.detect(t["example_prompt"])
        assert key == t["key"], (t["key"], key, score)
    ok("template detection from the example prompts")


def test_builder_interview_flow():
    c = _client()
    r = _post(c, "/omni/start", {"prompt": "Personal injury law firm. Leads come from our website form, Avvo, Google calls, texts, and voicemail."})
    assert r.status_code == 302 and r.headers["Location"].endswith("/build"), r.headers.get("Location")
    wid = r.headers["Location"].split("/omni/w/")[1].split("/")[0]
    ws = _ws(wid)
    assert ws["template_key"] == "pi_law_firm" and ws["builder_state"] == "asking"
    pre = ws["builder"]["prefill"]["connectors"]
    assert "voicemail" in pre and "email" not in pre, pre
    r = c.get(f"/omni/w/{wid}/build")
    assert r.status_code == 200 and b"Where do new leads come in?" in r.data
    r = _post(c, f"/omni/w/{wid}/api/builder/answer", json={"qid": "sources", "value": ["web_form", "avvo", "sms"]})
    d = r.get_json()
    assert r.status_code == 200 and d["question"]["id"] == "business_name", d.get("question")
    assert d["progress"]["sources"] is True and "preview_html" in d and "Avvo" in d["preview_html"]
    r = _post(c, f"/omni/w/{wid}/api/builder/answer", json={"qid": "business_name", "value": "Lakeside Injury Law"})
    assert r.get_json()["question"]["id"] == "extract_fields"
    r = _post(c, f"/omni/w/{wid}/api/builder/answer", json={"qid": "extract_fields", "value": ["incident_type", "has_attorney", "whether they have a police report"]})
    d = r.get_json()
    assert d["question"]["id"] == "slices", d.get("question")
    ws = _ws(wid)
    keys = [f.get("key") or f["label"] for f in ws["draft"]["fields"]]
    assert "incident_type" in keys and "has_attorney" in keys and any("police" in k for k in keys), keys
    assert "incident_date" not in keys
    r = _post(c, f"/omni/w/{wid}/api/builder/skip", json={})
    d = r.get_json()
    assert d["state"] == "drafted" and "Build my inbox" in d["agent_html"], d["state"]
    r = _post(c, f"/omni/w/{wid}/api/builder/change", json={"instruction": "make it more formal"})
    assert r.get_json()["ok"] and _ws(wid)["draft"]["tone"]["voice"] == "formal"
    r = _post(c, f"/omni/w/{wid}/api/builder/build", json={})
    d = r.get_json()
    assert r.status_code == 200 and d["redirect"].endswith(f"/omni/w/{wid}"), d
    ws = _ws(wid)
    assert ws["builder_state"] == "built" and ws["name"] == "Lakeside Injury Law"
    conns = {c_["kind"] for c_ in ws["spec"]["connectors"] if c_["auto_connect"]}
    assert conns == {"web_form", "avvo", "sms"}, conns
    convs = _convs(wid)
    assert convs and all(x["source_kind"] in conns for x in convs if x["source_status"] == "connected")
    r = c.get(f"/omni/w/{wid}")
    assert r.status_code == 200 and b"Lakeside Injury Law" in r.data
    r = c.get(f"/omni/w/{wid}/build")
    assert r.status_code == 302
    ok("builder interview: prefill, answers patch the draft, skip, change, build")


def test_builder_generic_path():
    c = _client()
    r = _post(c, "/omni/start", {"prompt": "I run a pottery studio and get inquiries from Instagram and my website."})
    wid = r.headers["Location"].split("/omni/w/")[1].split("/")[0]
    ws = _ws(wid)
    assert ws["template_key"] == "generic"
    assert set(ws["builder"]["prefill"]["connectors"]) >= {"instagram", "web_form"}
    r = _post(c, f"/omni/w/{wid}/api/builder/build", json={})
    assert r.status_code == 200
    assert _ws(wid)["builder_state"] == "built"
    assert len(_convs(wid)) >= 5
    ok("generic path builds from a prompt no template matches")


def test_setup_save():
    c = _client()
    wid = _quickstart(c)
    ws = _ws(wid)
    r = c.get(f"/omni/w/{wid}/setup")
    assert r.status_code == 200 and b"Sign replies as" in r.data
    r = _post(c, f"/omni/w/{wid}/setup", {"business_name": "Harbor Point Injury Law", "agent_name": "Dana",
                                          "signoff": "Dana at Harbor Point", "voice": "formal",
                                          "show_in_list": ["incident_type", "state"],
                                          "policy_routine": "review", "playbook": "- Free consultation."})
    assert r.status_code == 302
    ws = _ws(wid)
    assert ws["spec"]["tone"]["voice"] == "formal" and ws["spec"]["tone"]["signoff"] == "Dana at Harbor Point"
    shown = {f["key"] for f in ws["spec"]["fields"] if f["show_in_list"]}
    assert shown == {"incident_type", "state"}, shown
    assert ws["spec"]["approval_policy"]["routine"] == "review"
    assert ws["spec"]["playbook"] == "- Free consultation."
    ok("setup page saves the plain parts through the patch vocabulary")


def test_rules_tab_toggle_and_delete():
    c = _client()
    wid = _quickstart(c)
    r = _post(c, f"/omni/w/{wid}/rules/flag_serious/toggle")
    assert r.status_code == 302
    rule = next(x for x in _ws(wid)["spec"]["rules"] if x["key"] == "flag_serious")
    assert rule["enabled"] is False
    r = _post(c, f"/omni/w/{wid}/rules/flag_serious/delete")
    assert r.status_code == 302
    assert not any(x["key"] == "flag_serious" for x in _ws(wid)["spec"]["rules"])
    ok("rules tab toggles and deletes through the spec")


def main():
    try:
        test_schema_installs()
        test_quickstart_builds_and_sets_cookie()
        test_seed_is_idempotent()
        test_filters_evaluate()
        test_rules_fired_on_build()
        test_views_and_counts()
        test_reply_and_note_are_saved()
        test_draft_uses_canned_then_ladder()
        test_policy_categories()
        test_refine_parses_plain_words()
        test_change_flow_add_field_backfills()
        test_change_flow_view_and_rule()
        test_protected_field_is_refused()
        test_agent_action_confirm_once()
        test_ingest_paste_creates_conversation()
        test_owner_only_reset()
        test_every_template_builds()
        test_templates_have_no_em_dashes()
        test_detect_template()
        test_builder_interview_flow()
        test_builder_generic_path()
        test_setup_save()
        test_rules_tab_toggle_and_delete()
        print(f"All {len(PASSED)} passed.")
    finally:
        try:
            os.unlink(_TMP_DB)
        except OSError:
            pass


if __name__ == "__main__":
    main()
