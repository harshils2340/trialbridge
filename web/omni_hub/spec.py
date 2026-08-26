"""The spec: one JSON document that the builder produces and the inbox renders.

normalize() is the single funnel both the LLM path and the template fallback go
through, so one validator and one test suite cover both. apply_patch() is how
every later change (a prompt in the inbox, a Setup save, a rule from the Rules
tab) lands, and describe_patch() is the human-readable diff a person confirms.
"""
import copy
import re

SPEC_VERSION = 1

FIELD_TYPES = ("text", "number", "enum", "bool", "date", "list")
STAGE_TONES = ("brand", "info", "warn", "ok", "neutral", "danger", "violet")
CATEGORIES = ("routine", "pricing", "medical_legal", "distress")
POLICY_MODES = ("auto_send", "review")
TONE_VOICES = ("warm", "plain", "formal", "upbeat")
RULE_ACTIONS = ("flag", "set_priority", "set_stage", "auto_reject", "auto_reply")
RULE_TRIGGERS = ("on_message", "on_extract")

LIMITS = {"fields": 14, "stages": 7, "views": 8, "rules": 10, "connectors": 8,
          "options": 8, "playbook": 600}

# Every connector kind Omni knows about, with how it actually connects. "mode"
# is the honest answer to "does this source have an API": api = OAuth or a
# webhook; email_forward = the portal emails you and you forward that to the
# workspace's intake address; upload = scans, transcripts, exports; manual =
# paste a message. The demo simulates "connect" for all of them, but the
# email_forward, upload and manual paths also work for real via ingest.py.
CONNECTOR_KINDS = {
    "gmail": {"label": "Gmail", "mode": "api", "icon": "gmail",
              "feeds": "Email inquiries and replies",
              "how": "Connects with your Google account."},
    "outlook": {"label": "Outlook", "mode": "api", "icon": "outlook",
                "feeds": "Email inquiries and replies",
                "how": "Connects with your Microsoft account."},
    "instagram": {"label": "Instagram", "mode": "api", "icon": "instagram",
                  "feeds": "Direct messages",
                  "how": "Connects through your Instagram business account."},
    "facebook_leads": {"label": "Facebook Lead Ads", "mode": "api", "icon": "facebook",
                       "feeds": "Lead form submissions",
                       "how": "Connects through your Meta business account."},
    "web_form": {"label": "Website form", "mode": "api", "icon": "mail",
                 "feeds": "Form submissions",
                 "how": "Point your form at the workspace's webhook."},
    "sms": {"label": "Text messages", "mode": "api", "mono": "SMS",
            "feeds": "Texts to your business number",
            "how": "Connects a text-enabled number."},
    "voicemail": {"label": "Phone and voicemail", "mode": "upload", "icon": "phone",
                  "feeds": "Voicemail transcripts and call notes",
                  "how": "Upload or forward transcripts; call notes can be pasted."},
    "zillow": {"label": "Zillow", "mode": "email_forward", "mono": "ZL",
               "feeds": "Rental and buyer inquiries",
               "how": "Forward Zillow's lead emails to your intake address."},
    "avvo": {"label": "Avvo", "mode": "email_forward", "mono": "AV",
             "feeds": "Client inquiries",
             "how": "Forward Avvo's message emails to your intake address."},
    "zocdoc": {"label": "Zocdoc", "mode": "email_forward", "mono": "ZD",
               "feeds": "Appointment requests",
               "how": "Forward Zocdoc's booking emails to your intake address."},
    "psychology_today": {"label": "Psychology Today", "mode": "email_forward",
                         "mono": "PT", "feeds": "Client inquiries",
                         "how": "Forward Psychology Today's inquiry emails to your intake address."},
    "indeed": {"label": "Indeed", "mode": "email_forward", "mono": "IN",
               "feeds": "Applications",
               "how": "Forward Indeed's application emails to your intake address."},
    "care_com": {"label": "Care.com", "mode": "email_forward", "mono": "CC",
                 "feeds": "Family inquiries",
                 "how": "Forward Care.com's inquiry emails to your intake address."},
    "google_lsa": {"label": "Google Local Services", "mode": "email_forward",
                   "icon": "google", "feeds": "Calls and message leads",
                   "how": "Forward Google's lead emails to your intake address."},
    "fax": {"label": "Fax", "mode": "upload", "mono": "FX",
            "feeds": "Referrals and forms",
            "how": "Upload scans, or forward from your fax-to-email service."},
    "email": {"label": "Email inbox", "mode": "email_forward", "icon": "mail",
              "feeds": "Any email",
              "how": "Forward or auto-forward mail to your intake address."},
    "webhook": {"label": "Zapier or webhook", "mode": "api", "mono": "ZP",
                "feeds": "Anything Zapier or Make can send",
                "how": "Send JSON to the workspace's webhook."},
    "csv": {"label": "CSV import", "mode": "upload", "mono": "CSV",
            "feeds": "Exports from other tools",
            "how": "Upload a spreadsheet export."},
    "referral": {"label": "Referrals", "mode": "manual", "icon": "users",
                 "feeds": "Referred people you log by hand",
                 "how": "Paste or type the referral."},
}

# Fields no business should collect through an intake inbox, regardless of
# template. Templates add their own (fair housing, EEOC) in their guardrails.
PROTECTED_PATTERNS = {
    "race": "race",
    "ethnic": "race or ethnicity",
    "religio": "religion",
    "national origin": "national origin",
    "nationality": "national origin",
    "sexual orientation": "sexual orientation",
}


def slug(text, maxlen=32):
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s[:maxlen] or "field"


def _dedupe(items, key="key"):
    seen, out = set(), []
    for it in items:
        k = it.get(key)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def protected_reason(text, guardrails=None):
    """Return the reason a field label/key/hint is off limits, or ''."""
    low = (text or "").lower()
    for pat, reason in PROTECTED_PATTERNS.items():
        if pat in low:
            return reason
    for g in guardrails or []:
        if g.get("enforce") != "refuse_field":
            continue
        for pat in g.get("patterns") or []:
            if pat.lower() in low:
                return g.get("reason") or g.get("text") or pat
    return ""


def _norm_field(f, guardrails):
    if not isinstance(f, dict):
        return None
    label = (f.get("label") or f.get("key") or "").strip()
    key = slug(f.get("key") or label)
    if not label:
        label = key.replace("_", " ").capitalize()
    typ = (f.get("type") or "text").lower()
    aliases = {"boolean": "bool", "yes_no": "bool", "yesno": "bool", "choice": "enum",
               "select": "enum", "int": "number", "integer": "number", "float": "number",
               "money": "number", "phone": "text", "email": "text", "string": "text",
               "datetime": "date"}
    typ = aliases.get(typ, typ)
    if typ not in FIELD_TYPES:
        typ = "text"
    options = [str(o).strip() for o in (f.get("options") or []) if str(o).strip()]
    options = list(dict.fromkeys(options))[:LIMITS["options"]]
    if typ == "enum" and not options:
        typ = "text"
    reason = protected_reason(f"{key} {label} {f.get('hint') or ''}", guardrails)
    return {
        "key": key, "label": label[:60], "type": typ, "options": options,
        "hint": (f.get("hint") or "")[:200],
        "filterable": bool(f.get("filterable", True)),
        "show_in_list": bool(f.get("show_in_list", False)),
        "protected": bool(reason),
        "protected_reason": reason,
        "origin": f.get("origin") or "template",
    }


def _norm_stage(s):
    if isinstance(s, str):
        s = {"key": slug(s), "label": s}
    if not isinstance(s, dict):
        return None
    label = (s.get("label") or s.get("key") or "").strip()
    key = slug(s.get("key") or label)
    if not label:
        label = key.replace("_", " ").capitalize()
    tone = s.get("tone") or "neutral"
    if tone not in STAGE_TONES:
        tone = "neutral"
    return {"key": key, "label": label[:40], "tone": tone,
            "terminal": bool(s.get("terminal", False))}


_OPS = {"eq", "neq", "in", "not_in", "contains", "gt", "gte", "lt", "lte",
        "is_empty", "not_empty", "is_unknown", "within_days", "older_than_days",
        "has_flag"}
BUILTIN_FIELDS = {"stage", "status", "source", "source_key", "assignee", "unread",
                  "priority", "flags", "awaiting_reply", "last_message_at", "subject",
                  "contact_name", "text"}


def _norm_expr(expr, field_keys):
    """Drop clauses that reference unknown fields or ops. Returns None when
    nothing valid remains (the caller then drops the view or rule)."""
    if not isinstance(expr, dict):
        return None
    for group in ("all", "any"):
        if group in expr:
            kept = [e for e in (_norm_expr(c, field_keys) for c in (expr[group] or []))
                    if e]
            return {group: kept} if kept else None
    field = expr.get("field") or ""
    op = expr.get("op") or "eq"
    if op not in _OPS:
        return None
    if field.startswith("f."):
        if field[2:] not in field_keys:
            return None
    elif field not in BUILTIN_FIELDS:
        # Bare extracted-field keys are accepted and prefixed.
        if field in field_keys:
            field = "f." + field
        else:
            return None
    out = {"field": field, "op": op}
    if op not in ("is_empty", "not_empty", "is_unknown"):
        out["value"] = expr.get("value")
    return out


def _norm_view(v, field_keys):
    if not isinstance(v, dict):
        return None
    name = (v.get("name") or v.get("label") or "").strip()
    key = slug(v.get("key") or name)
    if not name:
        return None
    filt = _norm_expr(v.get("filter") or {"all": []}, field_keys)
    if filt is None and (v.get("filter") or {}).get("all") != []:
        # A filter that referenced only unknown fields is meaningless; keep the
        # view only if it was meant to be "everything".
        if v.get("filter"):
            return None
        filt = {"all": []}
    sort = v.get("sort") or {"by": "last_message_at", "dir": "desc"}
    return {"key": key, "name": name[:40], "filter": filt or {"all": []},
            "sort": {"by": sort.get("by") or "last_message_at",
                     "dir": "asc" if sort.get("dir") == "asc" else "desc"},
            "pinned": bool(v.get("pinned", False)),
            "origin": v.get("origin") or "template"}


def _norm_rule(r, field_keys, stage_keys):
    if not isinstance(r, dict):
        return None
    name = (r.get("name") or r.get("text") or "").strip()
    key = slug(r.get("key") or name)
    if not name:
        return None
    when = _norm_expr(r.get("when") or {}, field_keys)
    if when is None:
        return None
    then = dict(r.get("then") or {})
    typ = then.get("type")
    if typ not in RULE_ACTIONS:
        return None
    if typ in ("set_stage", "auto_reject"):
        if then.get("stage") not in stage_keys:
            return None
    if typ == "set_priority" and then.get("value") not in ("low", "normal", "high", "urgent"):
        then["value"] = "high"
    if typ == "flag" and not then.get("label"):
        then["label"] = name[:30]
    if typ == "auto_reply" and not then.get("intent"):
        then["intent"] = "acknowledge"
    trigger = r.get("trigger") or "on_extract"
    if trigger not in RULE_TRIGGERS:
        trigger = "on_extract"
    return {"key": key, "name": name[:60], "text": (r.get("text") or name)[:200],
            "trigger": trigger, "when": when, "then": then,
            "enabled": bool(r.get("enabled", True)),
            "origin": r.get("origin") or "template"}


def normalize(spec, template=None):
    """Fill defaults, coerce, cap, and strip anything unsafe. Idempotent."""
    spec = copy.deepcopy(spec or {})
    tpl = template or {}
    tspec = tpl.get("spec") or {}
    out = {"version": SPEC_VERSION,
           "template_key": spec.get("template_key") or tpl.get("key") or "generic",
           "created_from": spec.get("created_from") or "fallback"}

    biz = dict(tspec.get("business") or {})
    biz.update({k: v for k, v in (spec.get("business") or {}).items() if v})
    out["business"] = {
        "name": (biz.get("name") or "Your business")[:80],
        "type": biz.get("type") or out["template_key"],
        "summary": (biz.get("summary") or "")[:300],
        "audience": biz.get("audience") or "leads",
    }

    # Guardrails always come from the template; a generated copy is never trusted.
    out["guardrails"] = copy.deepcopy(tspec.get("guardrails") or spec.get("guardrails") or [])

    conns = []
    for c in spec.get("connectors") or []:
        if isinstance(c, str):
            c = {"kind": c}
        kind = (c.get("kind") or "").lower()
        if kind not in CONNECTOR_KINDS:
            continue
        meta = CONNECTOR_KINDS[kind]
        conns.append({"key": c.get("key") or kind, "kind": kind,
                      "label": (c.get("label") or meta["label"])[:60],
                      "mode": meta["mode"],
                      "auto_connect": bool(c.get("auto_connect", True))})
    out["connectors"] = _dedupe(conns)[:LIMITS["connectors"]]

    fields = [f for f in (_norm_field(f, out["guardrails"]) for f in spec.get("fields") or []) if f]
    stripped = [f for f in fields if f["protected"]]
    fields = [f for f in fields if not f["protected"]]
    out["fields"] = _dedupe(fields)[:LIMITS["fields"]]
    out["_stripped"] = [{"label": f["label"], "reason": f["protected_reason"]}
                        for f in stripped]
    field_keys = {f["key"] for f in out["fields"]}

    stages = [s for s in (_norm_stage(s) for s in spec.get("stages") or []) if s]
    stages = _dedupe(stages)
    if len(stages) < 2:
        stages = [_norm_stage(s) for s in (tspec.get("stages") or
                                           [{"key": "new", "label": "New", "tone": "brand"},
                                            {"key": "in_progress", "label": "In progress", "tone": "info"},
                                            {"key": "done", "label": "Done", "tone": "ok", "terminal": True}])]
    out["stages"] = stages[:LIMITS["stages"]]
    stage_keys = {s["key"] for s in out["stages"]}

    views = [v for v in (_norm_view(v, field_keys) for v in spec.get("views") or []) if v]
    views = _dedupe(views)
    keys = {v["key"] for v in views}
    if "inbox" not in keys:
        views.insert(0, {"key": "inbox", "name": "All open",
                         "filter": {"all": [{"field": "status", "op": "eq", "value": "open"}]},
                         "sort": {"by": "last_message_at", "dir": "desc"},
                         "pinned": True, "origin": "template"})
    if "needs_reply" not in keys:
        views.insert(1, {"key": "needs_reply", "name": "Needs reply",
                         "filter": {"all": [{"field": "awaiting_reply", "op": "eq", "value": True}]},
                         "sort": {"by": "last_message_at", "dir": "asc"},
                         "pinned": True, "origin": "template"})
    out["views"] = views[:LIMITS["views"]]

    rules = [r for r in (_norm_rule(r, field_keys, stage_keys) for r in spec.get("rules") or []) if r]
    out["rules"] = _dedupe(rules)[:LIMITS["rules"]]

    pol = dict(spec.get("approval_policy") or tspec.get("approval_policy") or {})
    policy = {}
    for cat in CATEGORIES:
        mode = pol.get(cat) or "review"
        policy[cat] = mode if mode in POLICY_MODES else "review"
    policy["distress"] = "review"
    for cat in tpl.get("must_review") or []:
        if cat in policy:
            policy[cat] = "review"
    out["approval_policy"] = policy

    tone = dict(tspec.get("tone") or {})
    tone.update({k: v for k, v in (spec.get("tone") or {}).items() if v})
    voice = tone.get("voice") or "warm"
    if voice not in TONE_VOICES:
        voice = "warm"
    presets = tone.get("presets") or [
        {"label": "Answer their question", "instruction": "Answer the question they just asked"},
        {"label": "Offer a call", "instruction": "Offer a short call and ask which times work"},
        {"label": "Ask for details", "instruction": "Ask for the details we still need"},
        {"label": "Check in", "instruction": "Check in and ask whether they are still interested"},
    ]
    out["tone"] = {"voice": voice,
                   "length": tone.get("length") if tone.get("length") in ("short", "medium") else "short",
                   "signoff": (tone.get("signoff") or "")[:60],
                   "presets": [{"label": str(p.get("label", ""))[:28],
                                "instruction": str(p.get("instruction", ""))[:120]}
                               for p in presets if isinstance(p, dict)][:4]}

    agent = dict(tspec.get("agent") or {})
    agent.update({k: v for k, v in (spec.get("agent") or {}).items() if v})
    out["agent"] = {"name": (agent.get("name") or "Omni")[:24],
                    "persona": (agent.get("persona") or "Intake coordinator")[:120],
                    "intro": (agent.get("intro") or "")[:240]}

    pb = spec.get("playbook") or tspec.get("playbook") or ""
    lines = [ln.strip() for ln in str(pb).splitlines() if ln.strip()]
    lines = [ln if ln.startswith("- ") else "- " + ln.lstrip("-* ") for ln in lines]
    out["playbook"] = "\n".join(lines)[:LIMITS["playbook"]]
    return out


def validate(spec):
    """Return a list of human-readable problems (empty when the spec is sound)."""
    errors = []
    if not spec.get("connectors"):
        errors.append("At least one source is needed.")
    if not spec.get("fields"):
        errors.append("At least one field to extract is needed.")
    if len(spec.get("stages") or []) < 2:
        errors.append("At least two stages are needed.")
    keys = [f["key"] for f in spec.get("fields") or []]
    if len(keys) != len(set(keys)):
        errors.append("Duplicate field keys.")
    return errors


# --------------------------------------------------------------------------- #
# Patches: the vocabulary every setup change is expressed in
# --------------------------------------------------------------------------- #
PATCH_OPS = ("add_field", "remove_field", "update_field", "add_view", "remove_view",
             "add_rule", "remove_rule", "toggle_rule", "add_stage", "rename_stage",
             "remove_stage", "set_policy", "set_tone", "add_connector",
             "remove_connector", "set_playbook", "set_business")


def apply_patch(spec, ops, template=None):
    """Apply ops to a spec. Returns (new_spec, applied_ops, refused).
    refused is a list of {op, reason}. Protected fields are refused, not stripped
    silently, so the person sees why."""
    spec = copy.deepcopy(spec)
    applied, refused = [], []
    guardrails = spec.get("guardrails") or []
    for op in ops or []:
        kind = op.get("op")
        try:
            if kind == "add_field":
                f = _norm_field(dict(op.get("field") or {}, origin="prompt"), guardrails)
                if not f:
                    refused.append({"op": op, "reason": "That field needs a name."})
                    continue
                if f["protected"]:
                    refused.append({"op": op, "reason": refusal_text(f["protected_reason"], guardrails)})
                    continue
                if any(x["key"] == f["key"] for x in spec["fields"]):
                    refused.append({"op": op, "reason": f"There is already a field called {f['label']}."})
                    continue
                if len(spec["fields"]) >= LIMITS["fields"]:
                    refused.append({"op": op, "reason": "That is the most fields an inbox can track."})
                    continue
                spec["fields"].append(f)
            elif kind == "remove_field":
                key = slug(op.get("key") or op.get("label"))
                before = len(spec["fields"])
                spec["fields"] = [f for f in spec["fields"] if f["key"] != key]
                if len(spec["fields"]) == before:
                    refused.append({"op": op, "reason": "No field by that name."})
                    continue
            elif kind == "update_field":
                key = slug(op.get("key") or "")
                target = next((f for f in spec["fields"] if f["key"] == key), None)
                if not target:
                    refused.append({"op": op, "reason": "No field by that name."})
                    continue
                for k in ("label", "hint", "show_in_list", "filterable", "type", "options"):
                    if k in op:
                        target[k] = op[k]
                nf = _norm_field(target, guardrails)
                target.clear()
                target.update(nf)
            elif kind == "add_view":
                v = _norm_view(dict(op.get("view") or {}, origin="prompt"),
                               {f["key"] for f in spec["fields"]})
                if not v:
                    refused.append({"op": op, "reason": "I could not turn that into a view. Tell me which field to filter on."})
                    continue
                if any(x["key"] == v["key"] for x in spec["views"]):
                    refused.append({"op": op, "reason": f"There is already a view called {v['name']}."})
                    continue
                v["pinned"] = bool(op.get("pinned", True))
                spec["views"].append(v)
            elif kind == "remove_view":
                key = slug(op.get("key") or op.get("name"))
                if key in ("inbox",):
                    refused.append({"op": op, "reason": "The All open view stays."})
                    continue
                spec["views"] = [v for v in spec["views"] if v["key"] != key]
            elif kind == "add_rule":
                r = _norm_rule(dict(op.get("rule") or {}, origin="prompt"),
                               {f["key"] for f in spec["fields"]},
                               {s["key"] for s in spec["stages"]})
                if not r:
                    refused.append({"op": op, "reason": "I could not turn that into a rule. Tell me which field it should watch."})
                    continue
                if any(x["key"] == r["key"] for x in spec["rules"]):
                    refused.append({"op": op, "reason": "That rule already exists."})
                    continue
                spec["rules"].append(r)
            elif kind == "remove_rule":
                key = op.get("key")
                spec["rules"] = [r for r in spec["rules"] if r["key"] != key]
            elif kind == "toggle_rule":
                key = op.get("key")
                for r in spec["rules"]:
                    if r["key"] == key:
                        r["enabled"] = bool(op.get("enabled", not r["enabled"]))
            elif kind == "add_stage":
                s = _norm_stage(op.get("stage") or {})
                if not s or any(x["key"] == s["key"] for x in spec["stages"]):
                    refused.append({"op": op, "reason": "That stage already exists."})
                    continue
                pos = op.get("position")
                if isinstance(pos, int) and 0 <= pos <= len(spec["stages"]):
                    spec["stages"].insert(pos, s)
                else:
                    terminals = [i for i, x in enumerate(spec["stages"]) if x.get("terminal")]
                    spec["stages"].insert(terminals[0] if terminals else len(spec["stages"]), s)
            elif kind == "rename_stage":
                key = slug(op.get("key") or "")
                for s in spec["stages"]:
                    if s["key"] == key:
                        s["label"] = (op.get("label") or s["label"])[:40]
            elif kind == "remove_stage":
                key = slug(op.get("key") or "")
                if len(spec["stages"]) <= 2:
                    refused.append({"op": op, "reason": "An inbox needs at least two stages."})
                    continue
                spec["stages"] = [s for s in spec["stages"] if s["key"] != key]
            elif kind == "set_policy":
                cat, mode = op.get("category"), op.get("mode")
                if cat not in CATEGORIES or mode not in POLICY_MODES:
                    refused.append({"op": op, "reason": "Unknown approval setting."})
                    continue
                if cat == "distress" and mode == "auto_send":
                    refused.append({"op": op, "reason": "Anything that reads as distress or urgent always waits for a person."})
                    continue
                if cat in (template or {}).get("must_review", []) and mode == "auto_send":
                    refused.append({"op": op, "reason": f"{cat.replace('_', ' ').capitalize()} questions always wait for a person in this kind of business."})
                    continue
                spec["approval_policy"][cat] = mode
            elif kind == "set_tone":
                voice = op.get("voice")
                if voice in TONE_VOICES:
                    spec["tone"]["voice"] = voice
                if op.get("signoff") is not None:
                    spec["tone"]["signoff"] = str(op["signoff"])[:60]
            elif kind == "add_connector":
                kindc = (op.get("kind") or "").lower()
                if kindc not in CONNECTOR_KINDS:
                    refused.append({"op": op, "reason": "I do not know that source yet."})
                    continue
                if any(c["kind"] == kindc for c in spec["connectors"]):
                    refused.append({"op": op, "reason": "That source is already set up."})
                    continue
                meta = CONNECTOR_KINDS[kindc]
                spec["connectors"].append({"key": kindc, "kind": kindc, "label": meta["label"],
                                           "mode": meta["mode"], "auto_connect": True})
            elif kind == "remove_connector":
                key = op.get("key") or op.get("kind")
                spec["connectors"] = [c for c in spec["connectors"] if c["key"] != key]
            elif kind == "set_playbook":
                spec["playbook"] = str(op.get("text") or "")
            elif kind == "set_business":
                for k in ("name", "summary"):
                    if op.get(k):
                        spec["business"][k] = str(op[k])
                if op.get("agent_name"):
                    spec.setdefault("agent", {})["name"] = str(op["agent_name"])[:24]
            else:
                refused.append({"op": op, "reason": "I do not know how to make that change yet."})
                continue
            applied.append(op)
        except Exception as e:  # never let one bad op sink the batch
            refused.append({"op": op, "reason": f"Could not apply: {e}"})
    return normalize(spec, template), applied, refused


def refusal_text(reason, guardrails=None):
    """The sentence a person sees when a field is off limits."""
    for g in guardrails or []:
        if g.get("enforce") == "refuse_field" and g.get("refusal"):
            for pat in g.get("patterns") or []:
                if pat.lower() in (reason or "").lower():
                    return g["refusal"]
    return (f"I cannot add that. {reason.capitalize()} is not something an intake "
            "inbox should collect or sort by. I can add fields that apply to everyone "
            "the same way.")


def _field_words(f):
    if f["type"] == "enum":
        return "(" + " / ".join(f["options"] + ["unknown"]) + ")"
    return {"bool": "(yes / no / unknown)", "date": "(a date)", "number": "(a number)",
            "list": "(a list)"}.get(f["type"], "(text)")


def expr_words(expr, spec):
    """A filter expression in plain words, for diffs and the Setup page."""
    labels = {f["key"]: f["label"] for f in spec.get("fields") or []}
    stages = {s["key"]: s["label"] for s in spec.get("stages") or []}
    if not expr:
        return "everything"
    for group, joiner in (("all", " and "), ("any", " or ")):
        if group in expr:
            parts = [expr_words(c, spec) for c in expr[group]]
            return joiner.join(p for p in parts if p) or "everything"
    field = expr.get("field", "")
    name = labels.get(field[2:], field[2:].replace("_", " ")) if field.startswith("f.") \
        else {"awaiting_reply": "needs a reply", "last_message_at": "last message",
              "contact_name": "name", "source": "source"}.get(field, field.replace("_", " "))
    op, val = expr.get("op"), expr.get("value")
    if field == "stage" and isinstance(val, list):
        val = [stages.get(v, v) for v in val]
    elif field == "stage":
        val = stages.get(val, val)
    if isinstance(val, bool):
        val = "yes" if val else "no"
    if isinstance(val, list):
        val = ", ".join(str(v).replace("_", " ") for v in val)
    elif val is not None:
        val = str(val).replace("_", " ")
    if field == "awaiting_reply" and op == "eq":
        return "needs a reply" if expr.get("value") else "does not need a reply"
    return {
        "eq": f"{name} is {val}", "neq": f"{name} is not {val}",
        "in": f"{name} is one of {val}", "not_in": f"{name} is not {val}",
        "contains": f"{name} mentions {val}", "gt": f"{name} is more than {val}",
        "gte": f"{name} is at least {val}", "lt": f"{name} is less than {val}",
        "lte": f"{name} is at most {val}", "is_empty": f"{name} is empty",
        "not_empty": f"{name} is set", "is_unknown": f"{name} is unknown",
        "within_days": f"{name} is within {val} days",
        "older_than_days": f"{name} is older than {val} days",
        "has_flag": f"flagged {val}",
    }.get(op, f"{name} {op} {val}")


def rule_words(rule, spec):
    stages = {s["key"]: s["label"] for s in spec.get("stages") or []}
    then = rule.get("then") or {}
    t = then.get("type")
    what = {"flag": f"flag it \"{then.get('label', '')}\"",
            "set_priority": f"mark it {then.get('value', 'high')} priority",
            "set_stage": f"move it to {stages.get(then.get('stage'), then.get('stage'))}",
            "auto_reject": f"move it to {stages.get(then.get('stage'), then.get('stage'))} and draft a polite decline",
            "auto_reply": f"send a {then.get('intent', 'acknowledge').replace('_', ' ')} reply"}.get(t, t)
    return f"When {expr_words(rule.get('when'), spec)}, {what}."


def describe_patch(ops, spec):
    """One plain line per op, the diff a person confirms."""
    labels = {f["key"]: f["label"] for f in spec.get("fields") or []}
    stages = {s["key"]: s["label"] for s in spec.get("stages") or []}
    lines = []
    for op in ops or []:
        k = op.get("op")
        if k == "add_field":
            f = _norm_field(op.get("field") or {}, spec.get("guardrails"))
            if f:
                lines.append(f"Add field: {f['label']} {_field_words(f)}; extracted from messages; "
                             + ("filterable" if f["filterable"] else "not filterable"))
        elif k == "remove_field":
            key = slug(op.get("key") or op.get("label"))
            lines.append(f"Remove field: {labels.get(key, key)}")
        elif k == "update_field":
            key = slug(op.get("key") or "")
            lines.append(f"Change field: {labels.get(key, key)}")
        elif k == "add_view":
            v = _norm_view(op.get("view") or {}, set(labels))
            if v:
                lines.append(f"Add view: {v['name']}, where {expr_words(v['filter'], spec)}")
        elif k == "remove_view":
            lines.append(f"Remove view: {op.get('name') or op.get('key')}")
        elif k == "add_rule":
            r = _norm_rule(op.get("rule") or {}, set(labels), set(stages))
            if r:
                lines.append("Add rule: " + rule_words(r, spec))
        elif k == "remove_rule":
            lines.append(f"Remove rule: {op.get('key')}")
        elif k == "toggle_rule":
            lines.append(f"Turn rule {'on' if op.get('enabled') else 'off'}: {op.get('key')}")
        elif k == "add_stage":
            s = _norm_stage(op.get("stage") or {})
            if s:
                lines.append(f"Add stage: {s['label']}")
        elif k == "rename_stage":
            lines.append(f"Rename stage {stages.get(slug(op.get('key') or ''), op.get('key'))} to {op.get('label')}")
        elif k == "remove_stage":
            lines.append(f"Remove stage: {stages.get(slug(op.get('key') or ''), op.get('key'))}")
        elif k == "set_policy":
            cur = (spec.get("approval_policy") or {}).get(op.get("category"), "review")
            words = {"auto_send": "send automatically", "review": "review before sending"}
            lines.append(f"{str(op.get('category', '')).replace('_', ' ').capitalize()} questions: "
                         f"{words.get(op.get('mode'), op.get('mode'))} (was: {words.get(cur, cur)})")
        elif k == "set_tone":
            cur = (spec.get("tone") or {}).get("voice", "warm")
            if op.get("voice"):
                lines.append(f"Tone: {op['voice']} (was: {cur})")
            if op.get("signoff") is not None:
                lines.append(f"Sign off as: {op['signoff']}")
        elif k == "add_connector":
            meta = CONNECTOR_KINDS.get((op.get("kind") or "").lower(), {})
            lines.append(f"Add source: {meta.get('label', op.get('kind'))}")
        elif k == "remove_connector":
            lines.append(f"Remove source: {op.get('key') or op.get('kind')}")
        elif k == "set_playbook":
            lines.append("Update the playbook")
        elif k == "set_business":
            if op.get("agent_name"):
                lines.append(f"Agent name: {op['agent_name']}")
            else:
                lines.append(f"Business: {op.get('name') or op.get('summary')}")
    return lines
