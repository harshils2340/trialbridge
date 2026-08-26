"""Score the model's extraction against the templates' canned values.

    cd web && ../.venv/bin/python -m omni_hub.ai.evals [template_key ...]

Skips with exit 0 when no LLM key is set. For every seed conversation it runs
the model over the inbound messages with the template's fields and compares:
exact match for enum/bool/number/date, case-insensitive substring for text,
set overlap of at least half for lists. Unknown vs unknown counts as agreement;
a value where the canned answer is unknown counts as a hallucination. Prints a
per-template table and writes nothing to disk.
"""
import datetime as dt
import sys

from . import extract_llm, llm
from .. import seeds
from .. import spec as spec_mod


def _agree(field, got, want):
    if want in (None, "", []):
        return got in (None, "", [])
    if got in (None, "", []):
        return False
    t = field["type"]
    if t in ("enum", "bool", "date"):
        return str(got).lower() == str(want).lower()
    if t == "number":
        try:
            return abs(float(got) - float(want)) < 1e-6
        except (TypeError, ValueError):
            return False
    if t == "list":
        a = {str(x).lower() for x in (got if isinstance(got, list) else [got])}
        b = {str(x).lower() for x in (want if isinstance(want, list) else [want])}
        return len(a & b) >= max(1, len(b) // 2)
    return str(want).lower() in str(got).lower() or str(got).lower() in str(want).lower()


def run(keys=None):
    if not llm.available():
        print("no key, skipped")
        return 0
    keys = keys or [t["key"] for t in seeds.available()]
    now = dt.datetime.now()
    print(f"{'template':22} {'fields':>7} {'agree':>7} {'halluc':>7} {'conf ok':>8} {'conf bad':>9}")
    for key in keys:
        t = seeds.get(key)
        spec = spec_mod.normalize(t["spec"], t)
        fields = spec["fields"]
        n = agree = halluc = 0
        conf_ok, conf_bad = [], []
        for c in t.get("conversations") or []:
            msgs = [{"id": i + 1, "body": m["body"],
                     "sent_at": (now - dt.timedelta(minutes=m["ago"])).strftime("%Y-%m-%d %H:%M")}
                    for i, m in enumerate(c["messages"]) if m["kind"] == "inbound"]
            got = extract_llm.extract(spec, fields, msgs) or {}
            for f in fields:
                want = (c.get("fields") or {}).get(f["key"], (None, 0, ""))[0]
                rec = got.get(f["key"])
                val = rec["value"] if rec else None
                n += 1
                if _agree(f, val, want):
                    agree += 1
                    if rec:
                        conf_ok.append(rec["confidence"])
                else:
                    if want in (None, "", []) and val not in (None, "", []):
                        halluc += 1
                    if rec:
                        conf_bad.append(rec["confidence"])
        pct = 100.0 * agree / n if n else 0.0
        mo = sum(conf_ok) / len(conf_ok) if conf_ok else 0.0
        mb = sum(conf_bad) / len(conf_bad) if conf_bad else 0.0
        print(f"{key:22} {n:7d} {pct:6.1f}% {halluc:7d} {mo:8.2f} {mb:9.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
