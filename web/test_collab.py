"""Tests for the collaboration layer: @mentions, away coverage, and blasts.

Script-style like the other test_*.py in this folder - run it directly:

    python test_collab.py

Each check asserts the behaviour a coordinator actually depends on, not the
shape of the SQL: a mention notifies exactly one right person, a handoff moves
the real queue and gives it back, and a blast can never escape its study.
"""
import datetime as dt
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="bmd-collab-")) / "t.db"
os.environ["DB_PATH"] = str(_TMP)

import flask  # noqa: E402

import db  # noqa: E402

app = flask.Flask(__name__)
app.config["SECRET_KEY"] = "test"
app.teardown_appcontext(db.close_db)


def _mk_user(name, email):
    return db.create_user(email, "pw", name)


def _join(org_id, user_id, role="student"):
    """Put a user on an existing team (the invite flow, minus the email)."""
    db.get_db().execute(
        "DELETE FROM memberships WHERE user_id = ?", (user_id,))
    db.get_db().execute(
        "INSERT INTO memberships (org_id, user_id, role, role_label, "
        "created_at) VALUES (?,?,?,?,?)", (org_id, user_id, role, "", db.now()))
    db.get_db().execute("UPDATE users SET org_id = ? WHERE id = ?",
                        (org_id, user_id))
    db.get_db().commit()


def _reveal(lead_id):
    db.get_db().execute("UPDATE leads SET revealed = 1 WHERE id = ?", (lead_id,))
    db.get_db().commit()


def _seed():
    """One org, three teammates, one claimed study, four accepted applicants."""
    sarah = _mk_user("Sarah Chen", "sarah@site.org")
    priya = _mk_user("Priya Kaur", "priya@site.org")
    dan = _mk_user("Dan Ruiz", "dan@site.org")
    oid = db.user_org_id(sarah)
    for uid in (priya, dan):
        _join(oid, uid)
    nct = "NCT00000001"
    db.add_study_claim(sarah, nct, "Test Study", verified=True)
    other = "NCT00000002"
    db.add_study_claim(sarah, other, "Other Study", verified=True)

    leads = []
    for i, (status, tag) in enumerate(
            [("prescreen", ""), ("screening", "docs"),
             ("screening", ""), ("enrolled", "")]):
        t = db.create_lead({"nct": nct, "title": "Test Study",
                            "name": f"P{i}", "email": f"p{i}@x.com",
                            "owner_user_id": sarah})
        lead = db.get_lead_by_token(t)
        db.update_lead_status(lead["id"], status)
        _reveal(lead["id"])
        db.set_lead_assignee(lead["id"], sarah)
        if tag:
            db.toggle_lead_tag(lead["id"], tag)
        leads.append(db.get_lead(lead["id"]))

    # One applicant in the OTHER study, and one opted out, as negative controls.
    t = db.create_lead({"nct": other, "title": "Other Study",
                        "name": "Outsider", "owner_user_id": sarah})
    outsider = db.get_lead_by_token(t)
    _reveal(outsider["id"])

    t = db.create_lead({"nct": nct, "title": "Test Study",
                        "name": "NoContact", "owner_user_id": sarah})
    opted = db.get_lead_by_token(t)
    _reveal(opted["id"])
    db.get_db().execute("UPDATE leads SET contact_opt_out = 1 WHERE id = ?",
                        (opted["id"],))
    db.get_db().commit()

    return {"sarah": sarah, "priya": priya, "dan": dan, "nct": nct,
            "other": other, "leads": leads, "outsider": outsider["id"],
            "opted": opted["id"]}


def test_mentions(s):
    lead_id = s["leads"][0]["id"]
    note_id = db.add_note(lead_id, "@priya can you take this one?",
                          author="Sarah Chen")
    people = db.record_mentions(s["sarah"], "lead", lead_id, "lead_note",
                                note_id, "@priya can you take this one?")
    assert [p["user_id"] for p in people] == [s["priya"]], people
    assert db.unread_mention_count(s["priya"]) == 1
    assert db.unread_mention_count(s["dan"]) == 0, "only the named person"

    # A self-mention is not a notification.
    n2 = db.add_note(lead_id, "@sarah reminder to self", author="Sarah Chen")
    assert db.record_mentions(s["sarah"], "lead", lead_id, "lead_note", n2,
                              "@sarah reminder to self") == []

    # An unknown handle stays plain text rather than guessing.
    assert db.parse_mentions(s["sarah"], "@nobody hello") == []

    # Email addresses in a note are not mentions.
    assert db.parse_mentions(s["sarah"], "forward to priya@site.org") == []

    rows = db.list_mentions(s["priya"])
    assert len(rows) == 1 and rows[0]["object_id"] == lead_id
    assert rows[0]["excerpt"].startswith("@priya")

    db.mark_mentions_read(s["priya"], "lead", lead_id)
    assert db.unread_mention_count(s["priya"]) == 0

    # `messages` is patient-visible, so it must be refused as a mention surface.
    assert db.record_mentions(s["sarah"], "lead", lead_id, "message", 1,
                              "@priya") == []
    print("PASS: @mentions notify exactly the right teammate, never the patient")


def test_mention_ambiguity(s):
    """Two teammates answering to the same handle must notify neither."""
    twin = _mk_user("Priya Sharma", "psharma@site.org")
    _join(db.user_org_id(s["sarah"]), twin)
    assert db.parse_mentions(s["sarah"], "@priya look") == [], \
        "ambiguous handle must not guess"
    # The unambiguous full handle still resolves.
    got = db.parse_mentions(s["sarah"], "@priya.kaur look")
    assert [p["user_id"] for p in got] == [s["priya"]], got
    db.get_db().execute("DELETE FROM memberships WHERE user_id = ?", (twin,))
    db.get_db().commit()
    print("PASS: ambiguous @handle stays plain text; full handle resolves")


def test_away_handoff(s):
    sarah, priya = s["sarah"], s["priya"]
    open_before = db._open_lead_ids_for(sarah, sarah)
    assert len(open_before) == 4, open_before

    tomorrow = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    away, err = db.start_away(sarah, sarah, priya, until=tomorrow,
                              note="Back Monday")
    assert err is None, err
    assert away["moved_leads"] == 4, dict(away)

    assert db._open_lead_ids_for(sarah, sarah) == [], "queue should be empty"
    assert len(db._open_lead_ids_for(sarah, priya)) == 4

    assert db.active_away_for(sarah)["cover_user_id"] == priya
    assert [r["user_id"] for r in db.covering_for(priya)] == [sarah]

    # New work routes to the cover without any caller knowing about coverage.
    assert db.effective_assignee(sarah) == priya
    assert db.effective_assignee(priya) == priya, "one hop only"

    # Two people away at once - the bug the old org-keyed table could not do.
    a2, err2 = db.start_away(sarah, s["dan"], priya)
    assert err2 is None, err2
    assert db.active_away_for(s["dan"]) is not None
    assert db.active_away_for(sarah) is not None, "both stay active"
    db.end_away(a2["id"])

    # The cover deliberately passes one thread to Dan; hand-back must respect it.
    kept = db._open_lead_ids_for(sarah, priya)[0]
    db.set_lead_assignee(kept, s["dan"])

    db.end_away(away["id"])
    back = db._open_lead_ids_for(sarah, sarah)
    assert len(back) == 3, back
    assert db.get_lead(kept)["assigned_user_id"] == s["dan"], \
        "a deliberate reassignment must survive hand-back"
    assert db.active_away_for(sarah) is None
    print("PASS: handoff moves the real queue, several can be away, hand-back "
          "respects deliberate reassignment")


def test_away_lazy_expiry(s):
    sarah, priya = s["sarah"], s["priya"]
    away, err = db.start_away(sarah, sarah, priya)
    assert err is None, err
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    db.get_db().execute("UPDATE away_periods SET until = ? WHERE id = ?",
                        (yesterday, away["id"]))
    db.get_db().commit()
    # Reading is enough to end it - correctness must not depend on cron.
    assert db.active_away_for(sarah) is None
    assert db.get_away(away["id"])["status"] == "ended"
    assert len(db._open_lead_ids_for(sarah, sarah)) == 3, "handed back on read"

    # And the sweep is idempotent over already-ended periods.
    assert db.sweep_expired_aways() == 0
    print("PASS: away expires lazily on read and the cron sweep is idempotent")


def test_away_recap(s):
    row = db.unseen_away_recap(s["sarah"])
    assert row is not None, "a finished period should surface a recap once"
    recap = db.away_recap(s["sarah"], row["id"])
    assert recap["leads"] >= 1 and recap["cover_name"] == "Priya Kaur", recap
    assert db.mark_away_seen(s["sarah"], row["id"])
    print("PASS: away recap is grounded in the ledger and dismisses once")


def test_blast_audience(s):
    sarah, nct = s["sarah"], s["nct"]

    everyone, err = db.blast_audience(sarah, nct)
    assert err is None and len(everyone) == 4, (err, len(everyone))
    ids = {l["id"] for l in everyone}
    assert s["opted"] not in ids, "opted-out applicants must be excluded"
    assert s["outsider"] not in ids, "other studies must be excluded"

    screening, err = db.blast_audience(sarah, nct, "stage", "screening")
    assert err is None and len(screening) == 2, (err, len(screening))

    tagged, err = db.blast_audience(sarah, nct, "tag", "docs")
    assert err is None and len(tagged) == 1, (err, len(tagged))

    idle, err = db.blast_audience(sarah, nct, "idle", "1")
    assert err is None, err

    # A hand-picked selection reaching into another study fails loudly.
    _, err = db.blast_audience(sarah, nct, "selected", lead_ids=[s["outsider"]])
    assert err and "one study" in err, err

    # And so does an opted-out person, even if hand-picked.
    _, err = db.blast_audience(sarah, nct, "selected", lead_ids=[s["opted"]])
    assert err, "hand-picking must not bypass the opt-out rail"

    # An unclaimed study is refused outright.
    _, err = db.blast_audience(sarah, "NCT99999999")
    assert err and "claimed" in err, err

    picked = [everyone[0]["id"], everyone[1]["id"]]
    sel, err = db.blast_audience(sarah, nct, "selected", lead_ids=picked)
    assert err is None and len(sel) == 2, (err, sel)
    print("PASS: blast audience honours every rail - study, accepted, "
          "not closed, not opted out")


def test_blast_record(s):
    sarah, nct = s["sarah"], s["nct"]
    leads, _ = db.blast_audience(sarah, nct, "stage", "screening")
    ids = [l["id"] for l in leads]
    label = db.blast_audience_label("stage", "screening", len(ids))
    assert label == "Stage: Screening", label
    bid = db.create_blast(sarah, nct, "stage", "screening", label,
                          "Reminder body", "", ids)
    assert bid
    rows = db.list_blasts(sarah, nct)
    assert rows and rows[0]["recipients"] == 2, dict(rows[0])
    assert db.leads_blasted_since(sarah, ids) == 2, "double-send guard sees them"
    assert db.leads_blasted_since(sarah, [s["outsider"]]) == 0
    print("PASS: a blast is recorded, auditable, and feeds the double-send guard")


def main():
    with app.app_context():
        db.init_db()
        s = _seed()
        test_mentions(s)
        test_mention_ambiguity(s)
        test_away_handoff(s)
        test_away_lazy_expiry(s)
        test_away_recap(s)
        test_blast_audience(s)
        test_blast_record(s)
    print("PASS: collaboration tests")


if __name__ == "__main__":
    main()
