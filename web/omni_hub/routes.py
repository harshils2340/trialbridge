"""All /omni routes. Plain GET filters and form posts for the inbox (like the
marketing hub), JSON for the composer, the builder and setup changes."""
import json
import os

from flask import (abort, flash, g, jsonify, make_response, redirect,
                   render_template, request, url_for)

from copy_sanitize import sanitize_copy

from . import actions as actions_mod
from . import builder
from . import demo as demo_mod
from . import extract
from . import inbox
from . import ingest as ingest_mod
from . import models
from . import rules as rules_mod
from . import seeds
from . import spec as spec_mod
from . import workspace
from .ai import compose as compose_mod
from .ai import policy
from .ai import refine as refine_mod


def _wants_json():
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    acc = request.accept_mimetypes
    return acc.best == "application/json" and acc["application/json"] > acc["text/html"]


def _err(msg, code=400):
    if _wants_json():
        return jsonify({"ok": False, "error": msg, "message": msg}), code
    flash(msg, "error")
    return redirect(request.referrer or url_for("om_landing"))


def _payload():
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form


def _ws_or_404(wid):
    ws = models.get_workspace(wid)
    if not ws:
        abort(404)
    models.touch_workspace(ws["id"])
    ex = (seeds.get(ws.get("template_key")).get("prompt_examples") or [""])[0]
    ws["example_change"] = ex or "add a field for whether they already have a lawyer"
    g.om_ws = ws
    return ws


def _template(ws):
    return seeds.get(ws.get("template_key"))


def _stage_maps(spec):
    labels = {s["key"]: s["label"] for s in spec.get("stages") or []}
    tones = {s["key"]: s.get("tone", "neutral") for s in spec.get("stages") or []}
    return labels, tones


def _source_meta(kind):
    return spec_mod.CONNECTOR_KINDS.get(kind, {"label": kind, "mode": "manual",
                                               "mono": kind[:2].upper(), "feeds": "", "how": ""})


def _inbox_url(ws, **args):
    clean = {k: v for k, v in args.items() if v not in (None, "", "all", 0)}
    return url_for("om_inbox", wid=ws["wid"], **clean)


def _filters_from_request():
    return {
        "view": request.args.get("view", "") or request.form.get("view", ""),
        "stage": request.args.get("stage", "") or request.form.get("stage_filter", ""),
        "status": request.args.get("status", "open") or "open",
        "source": request.args.get("source", "") or request.form.get("source", ""),
        "q": (request.args.get("q", "") or "")[:100],
    }


def _field_records(spec, conv, messages):
    """Side panel rows: every spec field with its value, provenance and quote."""
    out = []
    by_id = {m["id"]: m for m in messages}
    for f in spec.get("fields") or []:
        rec = (conv.get("f") or {}).get(f["key"]) or {}
        val = rec.get("value")
        known = val not in (None, "", [])
        src = by_id.get(rec.get("source_message_id")) if rec.get("source_message_id") else None
        out.append({
            "key": f["key"], "label": f["label"], "type": f["type"],
            "value": val, "text": inbox.display_value(f, val) if known else "Unknown",
            "known": known, "confidence": rec.get("confidence") or 0,
            "quote": rec.get("quote") or "", "provenance": rec.get("provenance") or "",
            "message_id": rec.get("source_message_id"),
            "message_time": inbox.time_label(src["sent_at"]) if src else "",
            "options": f.get("options") or [],
        })
    return out


def _propose_from_rule(ws, spec):
    return actions_mod.rule_proposer(ws, spec, _template(ws))


def _creation_limited():
    """Workspace creation is cheap but unbounded; cap it per IP so a bot cannot
    fill the database. Reuses the app's ip_rate_limits table."""
    import db as _db
    ip = (request.remote_addr or "").strip()
    if not ip:
        return False
    try:
        ok, _retry = _db.check_and_bump_ip_limit("omni_start", ip, 60, 600)
    except Exception:
        return False
    return not ok


def register(app):

    @app.before_request
    def _om_before():
        if request.path.startswith("/omni"):
            workspace.sweep_if_due()

    @app.after_request
    def _om_after(resp):
        return workspace.apply_cookie(resp)

    # ------------------------------------------------------------------ #
    # Landing, start, quick start
    # ------------------------------------------------------------------ #
    @app.route("/omni", endpoint="om_landing")
    def om_landing():
        # ?variant=x renders templates/omni/_cand_x.html: a way to compare landing
        # page candidates side by side on a dev server. Ignored in production.
        variant = (request.args.get("variant") or "").strip()
        tpl = "omni/landing.html"
        if variant and variant.isalnum() and app.debug or (variant and variant.isalnum() and os.environ.get("OMNI_VARIANTS") == "1"):
            tpl = f"omni/_cand_{variant}.html"
        return render_template(tpl, templates=seeds.list_templates(),
                               current=workspace.current_workspace(),
                               demo_data=demo_mod.landing_demo())

    @app.route("/omni/start", methods=["POST"], endpoint="om_start")
    def om_start():
        if _creation_limited():
            return _err("That is a lot of new inboxes. Try again in a few minutes.", 429)
        data = _payload()
        prompt = sanitize_copy((data.get("prompt") or "").strip())[:1000]
        example = (data.get("example") or "").strip()
        if not prompt and not example:
            return _err("Tell me a little about your business first.")
        ws = builder.start(prompt, template_key=example if example in seeds.TEMPLATE_KEYS else None)
        workspace.remember_owner(ws["owner_token"])
        target = url_for("om_build", wid=ws["wid"])
        if _wants_json():
            return jsonify({"ok": True, "wid": ws["wid"], "redirect": target,
                            "template_key": ws["template_key"]})
        return redirect(target)

    @app.route("/omni/quickstart", methods=["POST"], endpoint="om_quickstart")
    def om_quickstart():
        if _creation_limited():
            return _err("That is a lot of new inboxes. Try again in a few minutes.", 429)
        data = _payload()
        key = (data.get("template_key") or "").strip()
        if key not in seeds.TEMPLATE_KEYS:
            return _err("Unknown example.")
        ws, spec, errors = builder.quickstart(key)
        workspace.remember_owner(ws["owner_token"])
        rules_mod.run_all(ws, spec, propose=_propose_from_rule(ws, spec))
        target = url_for("om_inbox", wid=ws["wid"])
        if _wants_json():
            return jsonify({"ok": True, "wid": ws["wid"], "redirect": target})
        return redirect(target)

    @app.route("/omni/app", endpoint="om_app")
    def om_app():
        ws = workspace.current_workspace()
        if ws and ws.get("builder_state") == "built":
            return redirect(url_for("om_inbox", wid=ws["wid"]))
        return redirect(url_for("om_landing"))

    # ------------------------------------------------------------------ #
    # Builder: the interview and the live preview
    # ------------------------------------------------------------------ #
    def _fragment(ws, what, **ctx):
        return render_template("omni/_turn.html", ws=ws, what=what, source_meta=_source_meta, **ctx)

    def _builder_state(ws, you_text=None):
        q = builder.next_question(ws)
        template = _template(ws)
        agent_html = ""
        if ws["builder_state"] == "asking" and q:
            agent_html = _fragment(ws, "question", question=q)
        elif ws["builder_state"] == "drafted":
            from omni_hub.ai import interview as interview_mod
            agent_html = _fragment(ws, "summary", lines=interview_mod.summary_lines(ws["draft"], template))
        elif ws["builder_state"] == "built":
            agent_html = _fragment(ws, "say", text="Your inbox is ready.", kind="built")
        return {"ok": True, "state": ws["builder_state"], "question": q,
                "you_html": _fragment(ws, "you", text=you_text) if you_text else "",
                "agent_html": agent_html,
                "preview_html": _fragment(ws, "preview", preview=builder.preview_data(ws)),
                "progress": builder.progress(ws),
                "done": ws["builder_state"] in ("drafted", "built"),
                "inbox_url": url_for("om_inbox", wid=ws["wid"])}

    @app.route("/omni/w/<wid>/build", endpoint="om_build")
    def om_build(wid):
        ws = _ws_or_404(wid)
        if ws.get("builder_state") == "built":
            return redirect(url_for("om_inbox", wid=wid))
        turns = models.list_turns(ws["id"])
        q = builder.next_question(ws) if ws["builder_state"] == "asking" else None
        return render_template("omni/build.html", ws=ws, om_tab="build", turns=turns, question=q,
                               progress=builder.progress(ws), preview=builder.preview_data(ws),
                               source_meta=_source_meta, is_owner=workspace.is_owner(ws))

    @app.route("/omni/w/<wid>/api/builder/answer", methods=["POST"], endpoint="om_builder_answer")
    def om_builder_answer(wid):
        ws = _ws_or_404(wid)
        data = _payload()
        qid = (data.get("qid") or "").strip()
        value = data.get("value")
        if isinstance(value, str):
            value = sanitize_copy(value)[:400]
        q = builder.next_question(ws)
        if not q or q["id"] != qid:
            return _err("That question has already been answered.", 409)
        ws = builder.answer(ws, qid, value)
        turns = models.list_turns(ws["id"])
        you = next((t["body"] for t in reversed(turns) if t["role"] == "user"), "")
        return jsonify(_builder_state(ws, you_text=you))

    @app.route("/omni/w/<wid>/api/builder/skip", methods=["POST"], endpoint="om_builder_skip")
    def om_builder_skip(wid):
        ws = _ws_or_404(wid)
        if ws["builder_state"] == "asking":
            ws = builder.skip_to_draft(ws)
        return jsonify(_builder_state(ws, you_text="Just build it with sensible defaults."))

    @app.route("/omni/w/<wid>/api/builder/build", methods=["POST"], endpoint="om_builder_build")
    def om_builder_build(wid):
        ws = _ws_or_404(wid)
        if ws["builder_state"] == "asking":
            ws = builder.skip_to_draft(ws)
        ws, spec, errors = builder.build_from_draft(ws)
        if errors:
            return _err(" ".join(errors), 422)
        return jsonify({"ok": True, "redirect": url_for("om_inbox", wid=wid),
                        "counts": models.counts(ws["id"])})

    @app.route("/omni/w/<wid>/api/builder/change", methods=["POST"], endpoint="om_builder_change")
    def om_builder_change(wid):
        """Free text after the summary: change the draft before building."""
        ws = _ws_or_404(wid)
        text = sanitize_copy((_payload().get("instruction") or "").strip())[:400]
        if not text:
            return _err("Say what you want changed.")
        template = _template(ws)
        norm = spec_mod.normalize(ws["draft"], template)
        parsed = refine_mod.parse(text, norm)
        if not parsed["ops"]:
            llm_parsed = refine_mod.parse_with_llm(text, norm)
            if llm_parsed:
                parsed = llm_parsed
        models.add_turn(ws["id"], "user", "change", text)
        if not parsed["ops"]:
            models.add_turn(ws["id"], "agent", "say", parsed.get("hint") or "I could not work that out.")
            st = _builder_state(ws, you_text=text)
            st["agent_html"] = _fragment(ws, "say", text=parsed.get("hint") or "I could not work that out.") + st["agent_html"]
            return jsonify(st)
        new_spec, applied, refused = spec_mod.apply_patch(norm, parsed["ops"], template)
        if applied:
            models.update_workspace(ws["id"], draft_json=new_spec, name=new_spec["business"]["name"])
            ws = models.get_workspace(wid)
        lines = spec_mod.describe_patch(applied, norm) + [r["reason"] for r in refused]
        say = ("Done: " + "; ".join(lines)) if applied else (refused[0]["reason"] if refused else "Nothing changed.")
        models.add_turn(ws["id"], "agent", "say", say)
        st = _builder_state(ws, you_text=text)
        st["agent_html"] = _fragment(ws, "say", text=say) + st["agent_html"]
        return jsonify(st)

    # ------------------------------------------------------------------ #
    # Inbox
    # ------------------------------------------------------------------ #
    @app.route("/omni/w/<wid>", endpoint="om_inbox")
    def om_inbox(wid):
        ws = _ws_or_404(wid)
        spec = ws["spec"]
        if ws.get("builder_state") != "built":
            return redirect(url_for("om_landing"))
        f = _filters_from_request()
        fields = spec.get("fields") or []
        views = spec.get("views") or []
        stage_labels, stage_tones = _stage_maps(spec)
        all_convs = inbox.load(ws["id"])
        vcounts = inbox.view_counts(views, all_convs)
        view_key = f["view"] or (views[0]["key"] if views else "")
        view = next((v for v in views if v["key"] == view_key), None)
        convs = inbox.apply_view(all_convs, view)
        status_filter = f["status"] if not view or view_key in ("inbox",) or f["status"] != "open" else "all"
        # A view already decides open/resolved; only the explicit Show filter narrows further.
        convs = inbox.narrow(convs, stage=f["stage"], status=f["status"] if f["status"] != "open" or view_key == "inbox" else "all",
                             source=f["source"], q=f["q"])
        for c in convs:
            c["facts"] = inbox.facts_line(c, fields)
        active = None
        thread_id = request.args.get("thread", type=int)
        if thread_id:
            active = next((c for c in all_convs if c["id"] == thread_id), None)
            if not active:
                active = models.get_conversation(ws["id"], thread_id)
                if active:
                    active["f"] = models.values_for_conversation(ws["id"], active["id"])
                    active["time_label"] = inbox.time_label(active.get("last_message_at"))
        messages, field_records, pending = [], [], []
        if active:
            messages = models.list_messages(active["id"])
            for m in messages:
                m["time_label"] = inbox.time_label(m["sent_at"])
            field_records = _field_records(spec, active, messages)
            pending = models.list_pending_actions(ws["id"], active["id"])
            if active.get("unread"):
                models.update_conversation(ws["id"], active["id"], unread=0)
                active["unread"] = 0
        sources = models.list_sources(ws["id"])
        for s in sources:
            s["meta"] = _source_meta(s["kind"])
        unread = sum(1 for c in all_convs if c.get("unread"))
        pinned = [v for v in views if v.get("pinned")][:3]
        more_views = [v for v in views if v not in pinned]
        return render_template(
            "omni/inbox.html", ws=ws, spec=spec, om_tab="inbox", fields=fields,
            views=views, pinned_views=pinned, more_views=more_views, view_key=view_key,
            view=view, view_counts=vcounts, stage_filter=f["stage"],
            status_filter=f["status"], source_filter=f["source"], q=f["q"],
            threads=convs, active=active, messages=messages,
            field_records=field_records, pending=pending, sources=sources,
            stage_labels=stage_labels, stage_tones=stage_tones, unread=unread,
            stage_counts=inbox.stage_counts(spec.get("stages") or [], all_convs),
            template=_template(ws), is_owner=workspace.is_owner(ws),
            source_meta=_source_meta, category_words=policy.category_words)

    def _back_to_thread(ws, cid):
        f = _filters_from_request()
        return redirect(_inbox_url(ws, thread=cid, view=f["view"], stage=f["stage"],
                                   status=f["status"] if f["status"] != "open" else "",
                                   source=f["source"], q=f["q"]))

    @app.route("/omni/w/<wid>/t/<int:cid>/reply", methods=["POST"], endpoint="om_reply")
    def om_reply(wid, cid):
        ws = _ws_or_404(wid)
        conv = models.get_conversation(ws["id"], cid) or abort(404)
        body = sanitize_copy((_payload().get("body") or "").strip())
        if not body:
            return _err("Write something first.")
        mid = models.add_message(ws["id"], cid, "outbound", body, author="You",
                                 delivery_status="saved")
        models.update_conversation(ws["id"], cid, unread=0)
        models.add_event(ws["id"], "reply_saved", {"message_id": mid}, conversation_id=cid)
        if _wants_json():
            return jsonify({"ok": True, "message": {"id": mid, "body": body,
                                                    "kind": "outbound", "time_label": "Just now"}})
        return _back_to_thread(ws, cid)

    @app.route("/omni/w/<wid>/t/<int:cid>/note", methods=["POST"], endpoint="om_note")
    def om_note(wid, cid):
        ws = _ws_or_404(wid)
        models.get_conversation(ws["id"], cid) or abort(404)
        body = sanitize_copy((_payload().get("body") or "").strip())
        if not body:
            return _err("Write something first.")
        mid = models.add_message(ws["id"], cid, "note", body, author="You")
        if _wants_json():
            return jsonify({"ok": True, "message": {"id": mid, "body": body, "kind": "note"}})
        return _back_to_thread(ws, cid)

    @app.route("/omni/w/<wid>/t/<int:cid>/stage", methods=["POST"], endpoint="om_stage")
    def om_stage(wid, cid):
        ws = _ws_or_404(wid)
        conv = models.get_conversation(ws["id"], cid) or abort(404)
        stage = (_payload().get("stage") or "").strip()
        stages = {s["key"]: s for s in ws["spec"].get("stages") or []}
        if stage not in stages:
            return _err("Unknown stage.")
        models.update_conversation(ws["id"], cid, stage=stage,
                                   status="resolved" if stages[stage].get("terminal") else conv["status"])
        models.add_event(ws["id"], "stage_changed", {"stage": stage}, conversation_id=cid)
        if _wants_json():
            return jsonify({"ok": True, "stage": stage})
        return _back_to_thread(ws, cid)

    @app.route("/omni/w/<wid>/t/<int:cid>/assign", methods=["POST"], endpoint="om_assign")
    def om_assign(wid, cid):
        ws = _ws_or_404(wid)
        models.get_conversation(ws["id"], cid) or abort(404)
        who = sanitize_copy((_payload().get("assignee") or "").strip())[:40]
        models.update_conversation(ws["id"], cid, assignee=who)
        if _wants_json():
            return jsonify({"ok": True, "assignee": who})
        return _back_to_thread(ws, cid)

    @app.route("/omni/w/<wid>/t/<int:cid>/resolve", methods=["POST"], endpoint="om_resolve")
    def om_resolve(wid, cid):
        ws = _ws_or_404(wid)
        conv = models.get_conversation(ws["id"], cid) or abort(404)
        status = "open" if conv["status"] == "resolved" else "resolved"
        models.update_conversation(ws["id"], cid, status=status, unread=0)
        if _wants_json():
            return jsonify({"ok": True, "status": status})
        return _back_to_thread(ws, cid)

    @app.route("/omni/w/<wid>/t/<int:cid>/fields/<key>", methods=["POST"], endpoint="om_field_set")
    def om_field_set(wid, cid, key):
        ws = _ws_or_404(wid)
        models.get_conversation(ws["id"], cid) or abort(404)
        field = next((f for f in ws["spec"].get("fields") or [] if f["key"] == key), None)
        if not field:
            return _err("Unknown field.")
        raw = _payload().get("value")
        value = _coerce(field, raw)
        models.upsert_field_value(ws["id"], cid, key, value, 1.0 if value not in (None, "") else 0,
                                  "", None, provenance="manual",
                                  status="found" if value not in (None, "") else "unknown")
        rules_mod.run_all(ws, ws["spec"], propose=_propose_from_rule(ws, ws["spec"]))
        if _wants_json():
            return jsonify({"ok": True, "value": value, "text": inbox.display_value(field, value)})
        return _back_to_thread(ws, cid)

    @app.route("/omni/w/<wid>/t/<int:cid>/draft.json", methods=["POST"], endpoint="om_draft")
    def om_draft(wid, cid):
        ws = _ws_or_404(wid)
        conv = models.get_conversation(ws["id"], cid) or abort(404)
        conv["f"] = models.values_for_conversation(ws["id"], cid)
        instruction = sanitize_copy((_payload().get("instruction") or "").strip())[:400]
        messages = models.list_messages(cid)
        d = compose_mod.compose(ws["spec"], _template(ws), conv, messages, instruction)
        if d.get("blocked"):
            return jsonify({"ok": False, "error": d["blocked"], "message": d["blocked"]}), 422
        return jsonify({"ok": True, "draft": d["text"], "policy": d["policy"],
                        "category": d["category"], "category_words": policy.category_words(d["category"]),
                        "human_review_required": True})

    # ------------------------------------------------------------------ #
    # Connections
    # ------------------------------------------------------------------ #
    @app.route("/omni/w/<wid>/connect", endpoint="om_connect")
    def om_connect(wid):
        ws = _ws_or_404(wid)
        spec = ws["spec"]
        sources = models.list_sources(ws["id"])
        have = {s["kind"] for s in sources}
        for s in sources:
            s["meta"] = _source_meta(s["kind"])
            s["count"] = 0
        counts = {}
        for c in models.list_conversations(ws["id"], include_hidden_sources=True):
            counts[c["source_key"]] = counts.get(c["source_key"], 0) + 1
        for s in sources:
            s["count"] = counts.get(s["key"], 0)
        others = [dict(kind=k, **m) for k, m in spec_mod.CONNECTOR_KINDS.items() if k not in have]
        intake_address = f"{ws['wid']}@in.{request.host.split(':')[0]}"
        return render_template("omni/connect.html", ws=ws, spec=spec, om_tab="connect",
                               sources=sources, others=others, intake_address=intake_address,
                               samples=_template(ws).get("sample_inputs") or [],
                               is_owner=workspace.is_owner(ws))

    @app.route("/omni/w/<wid>/connect/<key>", methods=["POST"], endpoint="om_connect_source")
    def om_connect_source(wid, key):
        ws = _ws_or_404(wid)
        spec = ws["spec"]
        src = models.get_source(ws["id"], key) or abort(404)
        before = len(models.list_conversations(ws["id"], include_hidden_sources=True))
        models.set_source_status(ws["id"], key, "connected")
        seeds.seed_workspace(ws, _template(ws), spec, source_keys=[key])
        extract.backfill(ws, spec)
        rules_mod.run_all(ws, spec, propose=_propose_from_rule(ws, spec))
        after = len(models.list_conversations(ws["id"], include_hidden_sources=True))
        models.add_event(ws["id"], "source_connected", {"key": key, "seeded": after - before})
        label = src.get("label") or _source_meta(src["kind"])["label"]
        n = after - before
        flash(f"{label} connected." + (f" {n} conversation{'s' if n != 1 else ''} arrived." if n else ""), "success")
        if _wants_json():
            return jsonify({"ok": True, "seeded": n})
        return redirect(url_for("om_connect", wid=wid))

    @app.route("/omni/w/<wid>/disconnect/<key>", methods=["POST"], endpoint="om_disconnect_source")
    def om_disconnect_source(wid, key):
        ws = _ws_or_404(wid)
        models.get_source(ws["id"], key) or abort(404)
        models.set_source_status(ws["id"], key, "disconnected")
        flash("Disconnected. Its conversations are hidden until you reconnect.", "success")
        return redirect(url_for("om_connect", wid=wid))

    @app.route("/omni/w/<wid>/add/<kind>", methods=["POST"], endpoint="om_add_source")
    def om_add_source(wid, kind):
        ws = _ws_or_404(wid)
        spec, applied, refused = spec_mod.apply_patch(
            ws["spec"], [{"op": "add_connector", "kind": kind}], _template(ws))
        if refused:
            return _err(refused[0]["reason"])
        builder.rebuild(ws, spec, _template(ws))
        models.set_source_status(ws["id"], kind, "connected")
        seeds.seed_workspace(ws, _template(ws), spec, source_keys=[kind])
        extract.backfill(ws, spec)
        rules_mod.run_all(ws, spec, propose=_propose_from_rule(ws, spec))
        flash(f"{_source_meta(kind)['label']} added and connected.", "success")
        return redirect(url_for("om_connect", wid=wid))

    # ------------------------------------------------------------------ #
    # Rules and setup (read side now; changes arrive through /change)
    # ------------------------------------------------------------------ #
    @app.route("/omni/w/<wid>/rules", endpoint="om_rules")
    def om_rules(wid):
        ws = _ws_or_404(wid)
        spec = ws["spec"]
        convs = inbox.load(ws["id"])
        rules = spec.get("rules") or []
        counts = rules_mod.match_counts(rules, convs)
        for r in rules:
            r["words"] = spec_mod.rule_words(r, spec)
            r["count"] = counts.get(r["key"], 0)
        return render_template("omni/rules.html", ws=ws, spec=spec, om_tab="rules",
                               rules=rules, examples=_template(ws).get("prompt_examples") or [],
                               is_owner=workspace.is_owner(ws))

    @app.route("/omni/w/<wid>/rules/<key>/toggle", methods=["POST"], endpoint="om_rule_toggle")
    def om_rule_toggle(wid, key):
        ws = _ws_or_404(wid)
        cur = next((r for r in ws["spec"].get("rules") or [] if r["key"] == key), None)
        if not cur:
            abort(404)
        spec, applied, refused = spec_mod.apply_patch(
            ws["spec"], [{"op": "toggle_rule", "key": key, "enabled": not cur.get("enabled", True)}],
            _template(ws))
        builder.rebuild(ws, spec, _template(ws), propose=_propose_from_rule(ws, spec))
        return redirect(url_for("om_rules", wid=wid))

    @app.route("/omni/w/<wid>/rules/<key>/delete", methods=["POST"], endpoint="om_rule_delete")
    def om_rule_delete(wid, key):
        ws = _ws_or_404(wid)
        spec, applied, refused = spec_mod.apply_patch(
            ws["spec"], [{"op": "remove_rule", "key": key}], _template(ws))
        builder.rebuild(ws, spec, _template(ws))
        flash("Rule removed.", "success")
        return redirect(url_for("om_rules", wid=wid))

    @app.route("/omni/w/<wid>/setup", endpoint="om_setup")
    def om_setup(wid):
        ws = _ws_or_404(wid)
        spec = ws["spec"]
        sources = models.list_sources(ws["id"])
        for s in sources:
            s["meta"] = _source_meta(s["kind"])
        views = [dict(v, words=spec_mod.expr_words(v.get("filter"), spec)) for v in spec.get("views") or []]
        rules = [dict(r, words=spec_mod.rule_words(r, spec)) for r in spec.get("rules") or []]
        return render_template("omni/setup.html", ws=ws, spec=spec, om_tab="setup",
                               sources=sources, views=views, rules=rules,
                               must_review=_template(ws).get("must_review") or [],
                               is_owner=workspace.is_owner(ws))

    @app.route("/omni/w/<wid>/setup", methods=["POST"], endpoint="om_setup_save")
    def om_setup_save(wid):
        ws = _ws_or_404(wid)
        spec = ws["spec"]
        f = request.form
        ops = []
        name = sanitize_copy((f.get("business_name") or "").strip())[:80]
        if name and name != spec["business"]["name"]:
            ops.append({"op": "set_business", "name": name})
        agent = sanitize_copy((f.get("agent_name") or "").strip())[:24]
        if agent and agent != spec["agent"]["name"]:
            ops.append({"op": "set_business", "agent_name": agent})
        voice = f.get("voice")
        signoff = sanitize_copy((f.get("signoff") or "").strip())[:60]
        if (voice and voice != spec["tone"]["voice"]) or signoff != spec["tone"].get("signoff", ""):
            ops.append({"op": "set_tone", "voice": voice if voice in spec_mod.TONE_VOICES else None, "signoff": signoff})
        shown = set(f.getlist("show_in_list"))
        for field in spec["fields"]:
            want = field["key"] in shown
            if bool(field.get("show_in_list")) != want:
                ops.append({"op": "update_field", "key": field["key"], "show_in_list": want})
        for cat in ("routine", "pricing", "medical_legal"):
            mode = f.get(f"policy_{cat}")
            if mode in spec_mod.POLICY_MODES and mode != spec["approval_policy"].get(cat):
                ops.append({"op": "set_policy", "category": cat, "mode": mode})
        playbook = sanitize_copy(f.get("playbook") or "")[:600]
        if playbook.strip() != (spec.get("playbook") or "").strip():
            ops.append({"op": "set_playbook", "text": playbook})
        if not ops:
            flash("Nothing changed.", "success")
            return redirect(url_for("om_setup", wid=wid))
        new_spec, applied, refused = spec_mod.apply_patch(spec, ops, _template(ws))
        if applied:
            builder.rebuild(ws, new_spec, _template(ws))
        msg = "Setup saved."
        if refused:
            msg += " " + " ".join(r["reason"] for r in refused)
        flash(msg, "success" if applied else "error")
        return redirect(url_for("om_setup", wid=wid))

    @app.route("/omni/w/<wid>/change", methods=["POST"], endpoint="om_change")
    def om_change(wid):
        ws = _ws_or_404(wid)
        spec = ws["spec"]
        text = sanitize_copy((_payload().get("instruction") or "").strip())[:400]
        if not text:
            return _err("Say what you want changed.")
        parsed = refine_mod.parse(text, spec)
        if not parsed["ops"]:
            llm_parsed = refine_mod.parse_with_llm(text, spec)
            if llm_parsed:
                parsed = llm_parsed
        if not parsed["ops"]:
            return _err(parsed.get("hint") or "I could not work that out. Try different words.", 422)
        new_spec, applied, refused = spec_mod.apply_patch(spec, parsed["ops"], _template(ws))
        diff = spec_mod.describe_patch(applied, spec)
        refused_lines = [r["reason"] for r in refused]
        if not applied:
            return jsonify({"ok": True, "proposal": {"token": None, "title": "I cannot make that change",
                                                     "diff": [], "refused": refused_lines, "after": ""}})
        convs = len(models.list_conversations(ws["id"], include_hidden_sources=True))
        after = parsed.get("reply") or ""
        if any(o.get("op") == "add_field" for o in applied):
            after = (after + f" I will re-read {convs} conversation{'s' if convs != 1 else ''} for it.").strip()
        a = models.create_action(ws["id"], "config_patch",
                                 {"ops": applied, "base_version": ws.get("spec_version"),
                                  "diff": diff, "instruction": text}, policy="review")
        return jsonify({"ok": True, "proposal": {"token": a["token"], "title": "Change to your setup",
                                                 "diff": diff, "refused": refused_lines, "after": after}})

    @app.route("/omni/w/<wid>/change/apply", methods=["POST"], endpoint="om_change_apply")
    def om_change_apply(wid):
        ws = _ws_or_404(wid)
        token = (_payload().get("token") or "").strip()
        result, err = actions_mod.confirm(ws, ws["spec"], token)
        if err:
            return _err(err, 409)
        kinds = set(result.get("kinds") or [])
        if kinds & {"add_rule", "remove_rule", "toggle_rule"}:
            target = url_for("om_rules", wid=wid)
        elif kinds & {"add_view"}:
            new_ws = models.get_workspace(wid)
            v = (new_ws["spec"].get("views") or [])[-1]
            target = url_for("om_inbox", wid=wid, view=v["key"])
        elif kinds & {"add_connector", "remove_connector"}:
            target = url_for("om_connect", wid=wid)
        elif kinds & {"set_policy", "set_tone", "set_playbook", "rename_stage", "add_stage", "remove_stage", "set_business"}:
            target = url_for("om_setup", wid=wid)
        else:
            target = request.referrer or url_for("om_inbox", wid=wid)
        flash(result.get("answer") or "Applied.", "success")
        return jsonify({"ok": True, "redirect": target, **result})

    @app.route("/omni/w/<wid>/change/cancel", methods=["POST"], endpoint="om_change_cancel")
    def om_change_cancel(wid):
        ws = _ws_or_404(wid)
        actions_mod.cancel(ws, (_payload().get("token") or "").strip())
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ #
    # Ingest: paste or upload (works for real, no API needed)
    # ------------------------------------------------------------------ #
    @app.route("/omni/w/<wid>/ingest/paste", methods=["POST"], endpoint="om_ingest_paste")
    def om_ingest_paste(wid):
        ws = _ws_or_404(wid)
        spec = ws["spec"]
        key = (_payload().get("source") or "").strip()
        text = (_payload().get("text") or "").strip()
        if not text and "file" in request.files and request.files["file"].filename:
            text = ingest_mod.read_upload(request.files["file"])
        if not text:
            return _err("Paste or upload something first.")
        src = models.get_source(ws["id"], key) or (models.list_sources(ws["id"]) or [None])[0]
        if not src:
            return _err("Add a source first.")
        if src["status"] != "connected":
            models.set_source_status(ws["id"], src["key"], "connected")
        cid = ingest_mod.ingest_text(ws, spec, src, text[:20000],
                                     propose=_propose_from_rule(ws, spec))
        flash("Added to the inbox and read for fields.", "success")
        if _wants_json():
            return jsonify({"ok": True, "conversation_id": cid,
                            "redirect": url_for("om_inbox", wid=wid, thread=cid)})
        return redirect(url_for("om_inbox", wid=wid, thread=cid))

    # ------------------------------------------------------------------ #
    # Agent actions: propose from the side panel, confirm or cancel
    # ------------------------------------------------------------------ #
    @app.route("/omni/w/<wid>/agent", methods=["POST"], endpoint="om_agent")
    def om_agent(wid):
        ws = _ws_or_404(wid)
        data = _payload()
        cid = data.get("thread_id")
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            return _err("Pick a conversation first.")
        conv = models.get_conversation(ws["id"], cid)
        if not conv:
            return _err("That conversation is gone.", 404)
        conv["f"] = models.values_for_conversation(ws["id"], cid)
        intent = (data.get("action") or "").strip()
        instruction = sanitize_copy((data.get("instruction") or "").strip())[:400]
        if intent not in ("booking", "check_in", "decline", "thanks", "ask_missing", "reply"):
            intent = compose_mod.intent_from_instruction(instruction) if instruction else "reply"
        if models.list_pending_actions(ws["id"], cid):
            return _err("There is already a proposal waiting on this conversation.")
        a, err = actions_mod.propose_reply(ws, ws["spec"], _template(ws), conv, intent, instruction)
        if not a:
            return _err(err or "Could not propose that.")
        return jsonify({"ok": True, "proposal": {"token": a["token"], "kind": a["kind"],
                                                 "text": a["payload"]["text"], "policy": a["policy"]}})

    @app.route("/omni/w/<wid>/act", methods=["POST"], endpoint="om_act")
    def om_act(wid):
        ws = _ws_or_404(wid)
        data = _payload()
        token = (data.get("token") or "").strip()
        if (data.get("action") or "confirm") == "cancel":
            err = actions_mod.cancel(ws, token)
            if err:
                return _err(err)
            return jsonify({"ok": True, "canceled": True})
        result, err = actions_mod.confirm(ws, ws["spec"], token, data.get("text"))
        if err:
            return _err(err)
        return jsonify({"ok": True, "message": True, **result})

    # ------------------------------------------------------------------ #
    # Workspace lifecycle
    # ------------------------------------------------------------------ #
    @app.route("/omni/w/<wid>/reset", methods=["POST"], endpoint="om_reset")
    def om_reset(wid):
        ws = _ws_or_404(wid)
        if not workspace.is_owner(ws):
            return _err("Only the person who created this workspace can reset it.", 403)
        models.wipe_conversations(ws["id"])
        spec = ws["spec"]
        seeds.seed_workspace(ws, _template(ws), spec, source_keys=[
            s["key"] for s in models.list_sources(ws["id"]) if s["status"] == "connected"])
        extract.backfill(ws, spec)
        rules_mod.run_all(ws, spec, propose=_propose_from_rule(ws, spec))
        flash("Fresh conversations loaded.", "success")
        return redirect(url_for("om_inbox", wid=wid))

    @app.route("/omni/w/<wid>/start-over", methods=["POST"], endpoint="om_start_over")
    def om_start_over(wid):
        _ws_or_404(wid)
        key = (_payload().get("template_key") or "").strip()
        if key in seeds.TEMPLATE_KEYS:
            ws, spec, errors = builder.quickstart(key)
            workspace.remember_owner(ws["owner_token"])
            rules_mod.run_all(ws, spec, propose=_propose_from_rule(ws, spec))
            return redirect(url_for("om_inbox", wid=ws["wid"]))
        return redirect(url_for("om_landing"))

    @app.route("/omni/w/<wid>/share", endpoint="om_share")
    def om_share(wid):
        ws = _ws_or_404(wid)
        return jsonify({"ok": True, "url": url_for("om_inbox", wid=ws["wid"], _external=True)})


def _coerce(field, raw):
    if raw in (None, ""):
        return None
    t = field.get("type")
    if t == "bool":
        return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")
    if t == "number":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    if t == "enum":
        opts = field.get("options") or []
        s = str(raw).strip()
        return s if s in opts else None
    if t == "list":
        if isinstance(raw, list):
            return raw
        return [x.strip() for x in str(raw).split(",") if x.strip()]
    return sanitize_copy(str(raw).strip())[:200]
