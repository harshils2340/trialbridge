"""Bridget routing and study-scope tests."""
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bmd-copilot-")) / "t.db"
os.environ["DB_PATH"] = str(_TMP)

import flask  # noqa: E402

import db  # noqa: E402
from copilot import agent, context, tools  # noqa: E402

app = flask.Flask(__name__)
app.config["SECRET_KEY"] = "test"
app.teardown_appcontext(db.close_db)


def _uid(suffix=""):
    email = f"copilot{suffix}@test.org"
    return db.create_user(email, "pw", "Test User")


def _two_studies(uid, a="NCT11111111", b="NCT22222222"):
    db.add_study_claim(uid, a, "Alpha Depression Study", verified=True)
    db.add_study_claim(uid, b, "Beta Migraine Study", verified=True)


def test_open_thread_routes_instructions_to_the_drafter():
    """With an inbox conversation open, what you type at Bridget is a reply to
    write unless it is plainly a workspace question."""
    ctx = {"has_thread": True}
    for q in ("Offer a call", "tell them the next step is a phone screen",
              "thank them for the records", "Answer their question",
              "We can see you Tuesday at 10"):
        assert agent._classify(q, ctx)[0] == "draft_reply", q
    # Workspace questions still read.
    assert agent._classify("list my studies", ctx)[0] == "list_studies"
    assert agent._classify("who is out of window", ctx)[0] == "visits_out_of_window"
    # Same words without a conversation open: not a draft.
    assert agent._classify("Offer a call", {})[0] != "draft_reply"
    assert agent._classify("tell them the next step", {})[0] != "draft_reply"


def test_list_studies_routing():
    intent, _ = agent._classify("send me the studies I have", {})
    assert intent == "list_studies", intent


def test_blast_this_trial_uses_scope():
    intent, params = agent._classify(
        "send a text blast to all applicants in this trial",
        {"active_nct": "NCT11111111"},
    )
    assert intent == "blast"
    assert params.get("nct") == "NCT11111111"


def test_resolve_deictic_and_scoped_cohort():
    uid = _uid("a")
    _two_studies(uid)
    nct, err = tools.resolve_study_nct(
        uid, query="message everyone in this study", active_nct="NCT22222222")
    assert err is None
    assert nct == "NCT22222222"


def test_resolve_all_studies_deictic_fails_clearly():
    uid = _uid("b")
    _two_studies(uid)
    nct, err = tools.resolve_study_nct(uid, query="blast this trial", active_nct="")
    assert not nct
    assert "switcher" in (err or "").lower()


def test_list_studies_payload():
    uid = _uid("c")
    _two_studies(uid)
    payload = tools.list_studies(uid, active_nct="NCT11111111")
    assert "2 studies" in payload["summary"]
    assert len(payload["items"]) == 2
    assert any("current view" in it["detail"] for it in payload["items"])


def test_context_reads_session_scope():
    uid = _uid("d")
    _two_studies(uid)
    with app.test_request_context():
        flask.session["active_nct"] = "NCT11111111"
        ctx = context.build({"id": uid}, {})
    assert ctx["active_nct"] == "NCT11111111"
    assert len(ctx["studies"]) == 2


def test_blast_blocked_lists_studies_hint():
    uid = _uid("e")
    _two_studies(uid)
    _, _, _, _, err = tools.blast_targets(uid, query="blast everyone", active_nct="")
    assert err
    assert "NCT11111111" in err or "Alpha" in err


def test_answer_list_studies():
    uid = _uid("f")
    _two_studies(uid)
    with app.test_request_context():
        flask.session["active_nct"] = ""
        ctx = context.build({"id": uid}, {})
    out = agent.answer(uid, "what studies do I have", ctx)
    assert "2 stud" in out["answer"].lower()
    assert len(out.get("items") or []) == 2


if __name__ == "__main__":
    tests = [
        test_open_thread_routes_instructions_to_the_drafter,
        test_list_studies_routing,
        test_blast_this_trial_uses_scope,
        test_resolve_deictic_and_scoped_cohort,
        test_resolve_all_studies_deictic_fails_clearly,
        test_list_studies_payload,
        test_context_reads_session_scope,
        test_blast_blocked_lists_studies_hint,
        test_answer_list_studies,
    ]
    with app.app_context():
        db.init_db()
        for t in tests:
            t()
            print("ok", t.__name__)
    print(f"All {len(tests)} passed.")
