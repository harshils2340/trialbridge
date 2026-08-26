"""Validate a template module the way the app will use it.

    cd web && DB_PATH=/tmp/omni_check.db ../.venv/bin/python -m omni_hub.seeds._check pi_law_firm

Exits non-zero with a list of problems. Run it after editing any seed file.
"""
import os
import re
import sys

_WEB = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_WEB, os.path.dirname(_WEB)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from copy_sanitize import contains_em_dash  # noqa: E402

from omni_hub import spec as spec_mod  # noqa: E402
from omni_hub.seeds import get, TEMPLATE_KEYS  # noqa: E402


def _walk(obj, path, out):
    if isinstance(obj, str):
        if contains_em_dash(obj):
            out.append(f"em dash at {path}: {obj[:60]!r}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _walk(v, f"{path}.{k}", out)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _walk(v, f"{path}[{i}]", out)


def check(key):
    problems = []
    t = get(key)
    if t.get("key") != key:
        problems.append(f"TEMPLATE key is {t.get('key')!r}, expected {key!r}")
    for req in ("name", "tagline", "example_prompt", "signals", "spec", "questions",
                "personas", "ladders", "sample_inputs", "conversations"):
        if req not in t:
            problems.append(f"missing TEMPLATE['{req}']")
    _walk(t, "TEMPLATE", problems)
    sp = spec_mod.normalize(t.get("spec") or {}, t)
    problems += [f"spec: {e}" for e in spec_mod.validate(sp)]
    if sp.get("_stripped"):
        problems.append(f"spec has protected fields that got stripped: {sp['_stripped']}")
    conn_kinds = {c["kind"] for c in sp["connectors"]}
    conn_keys = {c["key"] for c in sp["connectors"]}
    raw_conns = t.get("spec", {}).get("connectors") or []
    for c in raw_conns:
        if c.get("kind") not in spec_mod.CONNECTOR_KINDS:
            problems.append(f"unknown connector kind {c.get('kind')!r}")
    fields = {f["key"]: f for f in sp["fields"]}
    raw_fields = {f.get("key") for f in (t.get("spec", {}).get("fields") or [])}
    if raw_fields - set(fields):
        problems.append(f"fields dropped by normalize: {raw_fields - set(fields)}")
    stages = {s["key"] for s in sp["stages"]}
    if len(sp["views"]) < 3:
        problems.append("fewer than 3 views")
    if not sp["rules"]:
        problems.append("no rules survived normalize")
    raw_rules = t.get("spec", {}).get("rules") or []
    if len(sp["rules"]) != len(raw_rules):
        problems.append(f"{len(raw_rules) - len(sp['rules'])} rule(s) dropped by normalize")
    raw_views = t.get("spec", {}).get("views") or []
    if len(sp["views"]) < len(raw_views):
        problems.append(f"{len(raw_views) - len(sp['views'])} view(s) dropped by normalize")
    convs = t.get("conversations") or []
    if not 14 <= len(convs) <= 20:
        problems.append(f"{len(convs)} conversations (want 14 to 20)")
    keys = [c["key"] for c in convs]
    if len(keys) != len(set(keys)):
        problems.append("duplicate conversation keys")
    auto = {c["kind"] for c in sp["connectors"] if c.get("auto_connect")}
    n_auto = 0
    for c in convs:
        p = f"conversation {c['key']}"
        if c["source"] not in conn_kinds and c["source"] not in conn_keys:
            problems.append(f"{p}: source {c['source']!r} is not a connector")
        if c["source"] in auto:
            n_auto += 1
        if c.get("stage") not in stages:
            problems.append(f"{p}: stage {c.get('stage')!r} not in spec stages")
        if not c.get("messages"):
            problems.append(f"{p}: no messages")
        bodies = " ".join(m["body"].lower() for m in c.get("messages", []) if m["kind"] == "inbound")
        for m in c.get("messages", []):
            if m["kind"] not in ("inbound", "outbound", "note"):
                problems.append(f"{p}: bad message kind {m['kind']!r}")
        for fk, rec in (c.get("fields") or {}).items():
            if fk not in fields:
                problems.append(f"{p}: canned field {fk!r} not in spec")
                continue
            if not (isinstance(rec, tuple) and len(rec) == 3):
                problems.append(f"{p}: field {fk} must be a (value, confidence, quote) tuple")
                continue
            value, conf, quote = rec
            f = fields[fk]
            if value is not None:
                if f["type"] == "enum" and value not in f["options"]:
                    problems.append(f"{p}: {fk}={value!r} not in options {f['options']}")
                if f["type"] == "bool" and not isinstance(value, bool):
                    problems.append(f"{p}: {fk} should be a bool")
                if f["type"] == "date" and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(value)):
                    problems.append(f"{p}: {fk} should be YYYY-MM-DD")
                if f["type"] == "number" and not isinstance(value, (int, float)):
                    problems.append(f"{p}: {fk} should be a number")
                if f["type"] == "list" and not isinstance(value, list):
                    problems.append(f"{p}: {fk} should be a list")
            if quote and quote.lower() not in bodies:
                problems.append(f"{p}: quote for {fk} not found in inbound text: {quote!r}")
        missing = set(fields) - set((c.get("fields") or {}).keys())
        if missing:
            problems.append(f"{p}: no canned value for {sorted(missing)} (use UNKNOWN)")
        if c.get("messages") and c["messages"][-1]["kind"] == "inbound" and c.get("status", "open") == "open" \
                and not (c.get("drafts") or {}).get("reply"):
            problems.append(f"{p}: awaiting reply but no drafts['reply']")
    if n_auto < 12:
        problems.append(f"only {n_auto} conversations on auto-connect sources (want 12+)")
    for q in t.get("questions") or []:
        if q.get("kind") not in ("tiles", "chips", "text", "toggle"):
            problems.append(f"question {q.get('id')}: bad kind {q.get('kind')!r}")
    lad = t.get("ladders") or {}
    for need in ("reply", "fallback", "check_in", "booking", "thanks", "decline", "ask_missing"):
        if need not in lad:
            problems.append(f"ladders missing {need!r}")
    if len(t.get("sample_inputs") or []) < 2:
        problems.append("fewer than 2 sample_inputs")
    return problems


def main():
    keys = sys.argv[1:] or TEMPLATE_KEYS
    bad = 0
    for k in keys:
        try:
            probs = check(k)
        except ModuleNotFoundError:
            print(f"[skip] {k}: no module")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"[BAD] {k}: crashed: {e!r}")
            bad += 1
            continue
        if probs:
            bad += 1
            print(f"[BAD] {k}:")
            for p in probs:
                print("   -", p)
        else:
            t = get(k)
            print(f"[ok ] {k}: {len(t['conversations'])} conversations, "
                  f"{len(t['spec']['fields'])} fields, {len(t['spec']['views'])} views, "
                  f"{len(t['spec']['rules'])} rules")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
