"""The builder: a short interview that turns a first prompt into a spec, and
the build step that turns the spec into tables, seeds, extraction and rules.

State lives on the workspace row: builder_state intro -> asking -> drafted ->
built; draft_json is the working spec; builder_json holds answers, pending
question ids and what was prefilled from the prompt. Every turn is stored so
the page can be refreshed mid-interview.
"""
import copy

from copy_sanitize import sanitize_copy

from . import actions as actions_mod
from . import extract
from . import models
from . import rules as rules_mod
from . import seeds
from . import spec as spec_mod
from .ai import extract_llm
from .ai import interview
from .ai import llm as llm_mod
from .ai import refine


# --------------------------------------------------------------------------- #
# Materialize + build
# --------------------------------------------------------------------------- #
def materialize(ws, spec):
    """Project the spec onto om_sources/om_fields/om_views/om_rules. Keeps
    connection status for sources that already exist."""
    ws_id = ws["id"]
    existing = {s["key"]: s for s in models.list_sources(ws_id)}
    for c in spec.get("connectors") or []:
        cur = existing.get(c["key"])
        models.upsert_source(ws_id, c["key"], c["kind"], mode=c.get("mode", "api"),
                             label=c.get("label", ""),
                             status=None if cur else "disconnected")
    keep = {c["key"] for c in spec.get("connectors") or []}
    for key in existing:
        if key not in keep:
            models.delete_source(ws_id, key)
    models.replace_fields(ws_id, spec.get("fields") or [])
    models.replace_views(ws_id, spec.get("views") or [])
    models.replace_rules(ws_id, spec.get("rules") or [])
    stage_keys = [s["key"] for s in spec.get("stages") or []]
    if stage_keys:
        for c in models.list_conversations(ws_id, include_hidden_sources=True):
            if c.get("stage") not in stage_keys:
                models.update_conversation(ws_id, c["id"], stage=stage_keys[0])


def build(ws, raw_spec, template=None, seed=True, llm=None, propose=None):
    """Normalize, validate, persist, materialize, seed, extract, run rules.
    Idempotent: building twice bumps the version and changes no rows."""
    template = template or seeds.get(ws.get("template_key"))
    spec = spec_mod.normalize(raw_spec, template)
    errors = spec_mod.validate(spec)
    if errors:
        return spec, errors
    version = models.save_spec(ws["id"], spec)
    models.update_workspace(ws["id"], builder_state="built", template_key=template["key"])
    ws = models.get_workspace_by_id(ws["id"])
    materialize(ws, spec)
    if seed:
        seeds.seed_workspace(ws, template, spec)
    extract.backfill(ws, spec, llm=llm or (extract_llm.extract if llm_mod.available() else None))
    rules_mod.run_all(ws, spec, propose=propose or actions_mod.rule_proposer(ws, spec, template))
    models.add_event(ws["id"], "built", {"version": version})
    return spec, []


def quickstart(template_key, prompt="", origin="quickstart", propose=None):
    """One click: a workspace from a template with every default."""
    template = seeds.get(template_key)
    raw = copy.deepcopy(template["spec"])
    ws = models.create_workspace(
        raw.get("business", {}).get("name", ""), template["key"], origin=origin,
        intro_prompt=prompt or template.get("example_prompt", ""),
        spec={}, draft=raw, state="drafted")
    spec, errors = build(ws, raw, template, propose=propose)
    return models.get_workspace(ws["wid"]), spec, errors


def rebuild(ws, spec, template=None, only_keys=None, llm=None, propose=None):
    """After a patch: persist, re-materialize, backfill new fields, run rules."""
    template = template or seeds.get(ws.get("template_key"))
    spec = spec_mod.normalize(spec, template)
    version = models.save_spec(ws["id"], spec)
    ws = models.get_workspace_by_id(ws["id"])
    materialize(ws, spec)
    seeds.seed_workspace(ws, template, spec, source_keys=[
        s["key"] for s in models.list_sources(ws["id"]) if s["status"] == "connected"])
    n = extract.backfill(ws, spec, only_keys=only_keys, llm=llm or (extract_llm.extract if llm_mod.available() else None))
    rules_mod.run_all(ws, spec, propose=propose or actions_mod.rule_proposer(ws, spec, template))
    models.add_event(ws["id"], "spec_patched", {"version": version, "backfilled": n})
    return spec, n


# --------------------------------------------------------------------------- #
# Interview
# --------------------------------------------------------------------------- #
def start(prompt, template_key=None, origin="builder"):
    """Create a workspace in the interview state. Returns the workspace."""
    prompt = sanitize_copy((prompt or "").strip())[:1000]
    key = template_key if template_key in seeds.TEMPLATE_KEYS else seeds.detect(prompt)[0]
    template = seeds.get(key)
    draft = copy.deepcopy(template["spec"])
    pre = interview.prefill(prompt, template)
    if pre.get("business_name"):
        draft["business"]["name"] = pre["business_name"]
    if key == "generic" and prompt:
        draft["business"]["summary"] = pre.get("summary") or prompt[:300]
    for f in pre.get("extra_fields") or []:
        draft["fields"].append(dict(f, origin="prompt"))
    questions = [q["id"] for q in template.get("questions") or []]
    builder = {"answers": {}, "pending": questions, "detected": key, "prefill": pre}
    ws = models.create_workspace(draft["business"].get("name", ""), key, origin=origin,
                                 intro_prompt=prompt, spec={}, draft=draft,
                                 state="asking", builder=builder)
    if prompt:
        models.add_turn(ws["id"], "user", "intro", prompt)
    say = interview.opener(template, pre, prompt)
    models.add_turn(ws["id"], "agent", "say", say)
    q = next_question(ws)
    if q:
        models.add_turn(ws["id"], "agent", "question", q["prompt"], {"qid": q["id"]})
    return models.get_workspace(ws["wid"])


def _question_def(template, qid):
    return next((q for q in template.get("questions") or [] if q["id"] == qid), None)


def render_question(ws, q):
    """A question with options resolved for the UI."""
    template = seeds.get(ws.get("template_key"))
    draft = ws.get("draft") or {}
    pre = (ws.get("builder") or {}).get("prefill") or {}
    out = {"id": q["id"], "kind": q["kind"], "prompt": q["prompt"], "why": q.get("why", ""),
           "multi": bool(q.get("multi")), "options": [], "default": q.get("default")}
    if q["id"] == "sources":
        kinds = list(q.get("options") or [])
        for k in pre.get("connectors") or []:
            if k not in kinds:
                kinds.append(k)
        default = set(q.get("default") or [])
        if pre.get("connectors"):
            default = set(pre["connectors"]) | (default if not pre["connectors"] else set())
        out["options"] = [{"value": k, "label": spec_mod.CONNECTOR_KINDS[k]["label"],
                           "meta": spec_mod.CONNECTOR_KINDS[k], "feeds": spec_mod.CONNECTOR_KINDS[k]["feeds"]}
                          for k in kinds if k in spec_mod.CONNECTOR_KINDS]
        out["default"] = sorted(default)
    elif q["id"] == "business_name":
        out["default"] = draft.get("business", {}).get("name") or q.get("default", "")
    elif q["id"] == "business_type":
        out["default"] = draft.get("business", {}).get("summary") or ""
    elif q["id"] == "extract_fields":
        labels = {f["key"]: f["label"] for f in template["spec"].get("fields") or []}
        opts = q.get("options") or [[f["key"], f["label"]] for f in template["spec"].get("fields") or []]
        out["options"] = [{"value": o[0], "label": o[1] if len(o) > 1 else labels.get(o[0], o[0])} for o in opts]
        for f in draft.get("fields") or []:
            if f.get("origin") == "prompt":
                out["options"].append({"value": f.get("key") or spec_mod.slug(f["label"]), "label": f["label"], "custom": True})
        out["default"] = list(q.get("default") or [o["value"] for o in out["options"]])
        for f in draft.get("fields") or []:
            if f.get("origin") == "prompt":
                out["default"].append(f.get("key") or spec_mod.slug(f["label"]))
    elif q["id"] == "slices":
        out["options"] = [{"value": o[0], "label": o[1]} for o in q.get("options") or []]
    else:
        out["options"] = [{"value": o[0], "label": o[1]} for o in q.get("options") or []]
    return out


def next_question(ws):
    b = ws.get("builder") or {}
    pending = b.get("pending") or []
    if not pending:
        return None
    template = seeds.get(ws.get("template_key"))
    q = _question_def(template, pending[0])
    if not q:
        return None
    return render_question(ws, q)


def progress(ws):
    answered = set(((ws.get("builder") or {}).get("answers") or {}).keys())
    return {"sources": "sources" in answered, "fields": "extract_fields" in answered,
            "views": "slices" in answered, "autonomy": "approval" in answered,
            "tone": "tone" in answered}


# --- patches: how an answer changes the draft ------------------------------- #
def _p_set_connectors(draft, value, template):
    kinds = [v for v in (value if isinstance(value, list) else [value]) if v in spec_mod.CONNECTOR_KINDS]
    have = {c["kind"]: c for c in draft.get("connectors") or []}
    out = []
    for c in draft.get("connectors") or []:
        c["auto_connect"] = c["kind"] in kinds
        out.append(c)
    for k in kinds:
        if k not in have:
            meta = spec_mod.CONNECTOR_KINDS[k]
            out.append({"key": k, "kind": k, "label": meta["label"], "auto_connect": True})
    draft["connectors"] = out


def _p_set_business_name(draft, value, template):
    v = sanitize_copy(str(value or "").strip())[:80]
    if v:
        draft.setdefault("business", {})["name"] = v


def _p_set_business_summary(draft, value, template):
    v = sanitize_copy(str(value or "").strip())[:300]
    if v:
        draft.setdefault("business", {})["summary"] = v


def _p_pick_fields(draft, value, template):
    if isinstance(value, str):
        # Typed answer: comma-separated things to track, added as custom fields.
        for phrase in [p.strip() for p in value.split(",") if p.strip()]:
            typ, options = refine._infer_type(phrase)
            label = refine._label_from_phrase(phrase)
            if label:
                draft["fields"].append({"key": spec_mod.slug(label), "label": label, "type": typ,
                                        "options": options, "hint": phrase, "origin": "prompt"})
        return
    keys = set(value or [])
    kept = [f for f in draft.get("fields") or [] if (f.get("key") or spec_mod.slug(f["label"])) in keys
            or f.get("origin") == "prompt"]
    if kept:
        draft["fields"] = kept


def _p_pick_views(draft, value, template):
    keys = set(value or [])
    for v in draft.get("views") or []:
        if v["key"] in ("inbox", "needs_reply"):
            v["pinned"] = True
        else:
            v["pinned"] = v["key"] in keys


def _p_set_approval(draft, value, template):
    pol = {"routine": "review", "pricing": "review", "medical_legal": "review", "distress": "review"}
    v = value if isinstance(value, str) else (value[0] if value else "none")
    if v == "routine":
        pol["routine"] = "auto_send"
    elif v == "most":
        pol["routine"] = "auto_send"
        pol["pricing"] = "auto_send"
    for c in template.get("must_review") or []:
        pol[c] = "review"
    draft["approval_policy"] = pol


def _p_set_tone(draft, value, template):
    v = value if isinstance(value, str) else (value[0] if value else "warm")
    if v in spec_mod.TONE_VOICES:
        draft.setdefault("tone", {})["voice"] = v


def _p_set_stages(draft, value, template):
    return  # the template's stages are the standard ones; renaming happens later by prompt


PATCHES = {"set_connectors": _p_set_connectors, "set_business_name": _p_set_business_name,
           "set_business_summary": _p_set_business_summary, "pick_fields": _p_pick_fields,
           "pick_views": _p_pick_views, "set_approval": _p_set_approval,
           "set_tone": _p_set_tone, "set_stages": _p_set_stages}


def answer_words(q, value):
    """What the person's bubble says for a chosen answer."""
    if isinstance(value, list):
        labels = {o["value"]: o["label"] for o in q.get("options") or []}
        names = [labels.get(v, str(v).replace("_", " ")) for v in value]
        return interview._join(names) if names else "None of these"
    labels = {o["value"]: o["label"] for o in q.get("options") or []}
    return labels.get(value, str(value))


def answer(ws, qid, value):
    """Record an answer, patch the draft, advance. Returns the updated ws."""
    template = seeds.get(ws.get("template_key"))
    qdef = _question_def(template, qid)
    if not qdef:
        return ws
    q = render_question(ws, qdef)
    if q["kind"] in ("tiles", "chips") and q["multi"]:
        if isinstance(value, str):
            value = [v for v in [x.strip() for x in value.split(",")] if v]
        allowed = {o["value"] for o in q["options"]}
        typed = [v for v in value if v not in allowed]
        value = [v for v in value if v in allowed]
        if typed and qid == "extract_fields":
            _p_pick_fields(ws["draft"], ", ".join(typed), template)
            value = value + [spec_mod.slug(refine._label_from_phrase(t)) for t in typed]
    elif q["kind"] in ("chips", "tiles"):
        if isinstance(value, list):
            value = value[0] if value else q.get("default")
    elif q["kind"] == "toggle":
        value = bool(value) if not isinstance(value, str) else value.lower() in ("1", "true", "yes", "on")
    else:
        value = sanitize_copy(str(value or "").strip())
    draft = ws["draft"]
    patch = PATCHES.get(qdef.get("patch"))
    if patch:
        patch(draft, value, template)
    b = ws.get("builder") or {}
    b.setdefault("answers", {})[qid] = value
    b["pending"] = [p for p in b.get("pending") or [] if p != qid]
    models.add_turn(ws["id"], "user", "answer", answer_words(q, value), {"qid": qid, "value": value})
    state = "asking" if b["pending"] else "drafted"
    models.update_workspace(ws["id"], draft_json=draft, builder_json=b, builder_state=state,
                            name=draft.get("business", {}).get("name", ""))
    ws = models.get_workspace(ws["wid"])
    nq = next_question(ws)
    if nq:
        models.add_turn(ws["id"], "agent", "question", nq["prompt"], {"qid": nq["id"]})
    else:
        models.add_turn(ws["id"], "agent", "summary", "That is everything. Here is what I will build.",
                        {"lines": interview.summary_lines(ws["draft"], template)})
    return models.get_workspace(ws["wid"])


def skip_to_draft(ws):
    """Apply defaults for every unanswered question."""
    template = seeds.get(ws.get("template_key"))
    b = ws.get("builder") or {}
    for qid in list(b.get("pending") or []):
        qdef = _question_def(template, qid)
        if not qdef:
            b["pending"].remove(qid)
            continue
        q = render_question(ws, qdef)
        ws = answer(ws, qid, q.get("default") if q.get("default") is not None else "")
        b = ws.get("builder") or {}
    return models.get_workspace(ws["wid"])


def build_from_draft(ws, propose=None):
    template = seeds.get(ws.get("template_key"))
    spec, errors = build(ws, ws["draft"], template, propose=propose)
    if not errors:
        models.add_turn(ws["id"], "agent", "built", "Your inbox is ready.")
    return models.get_workspace(ws["wid"]), spec, errors


def preview_data(ws):
    """What the live preview shows for the current draft: connectors, fields,
    views, stages, and a few seed conversations for the ticked sources."""
    template = seeds.get(ws.get("template_key"))
    draft = ws.get("draft") or {}
    norm = spec_mod.normalize(draft, template)
    kinds = {c["kind"] for c in norm["connectors"] if c.get("auto_connect", True)}
    rows = []
    for sc in template.get("conversations") or []:
        if sc["source"] in kinds and len(rows) < 6 and sc.get("status", "open") == "open":
            facts = []
            for f in norm["fields"]:
                if not f.get("show_in_list"):
                    continue
                rec = (sc.get("fields") or {}).get(f["key"])
                if rec and rec[0] not in (None, "", []):
                    from . import inbox
                    facts.append((f["label"], inbox.display_value(f, rec[0]), True))
                if len(facts) >= 3:
                    break
            rows.append({"name": sc["contact"]["name"], "subject": sc["subject"],
                         "snippet": models.snippet_of(sc["messages"][-1]["body"], 110), "source_kind": sc["source"],
                         "stage": sc.get("stage") or (norm["stages"][0]["key"] if norm["stages"] else ""),
                         "facts": facts, "awaiting": sc["messages"][-1]["kind"] == "inbound"})
    return {"spec": norm, "rows": rows,
            "stage_labels": {s["key"]: s["label"] for s in norm["stages"]},
            "stage_tones": {s["key"]: s["tone"] for s in norm["stages"]}}
