"""Overnight ELIGIBILITY-matching accuracy harness.

Given a patient narrative + a trial's eligibility criteria, does our matcher
(match_trials.llm_match) reach the right verdict? Ground truth comes from the
TrialGPT / TREC clinical-trials relevance judgments:

    label 2 = ELIGIBLE      (patient qualifies)
    label 1 = EXCLUDED      (right condition, but a criterion rules them out)
    label 0 = NOT RELEVANT  (wrong condition entirely)

We score three things that actually matter for enrollment velocity:
  * eligible-recall  - of truly-eligible patients, how many we DON'T wrongly reject
  * exclusion-catch  - of truly-excluded patients, how many we correctly reject
  * critical errors  - false-eligible (advance someone ineligible) +
                       false-exclude (drop a real candidate). These are the
                       expensive mistakes; we want this near zero.

Cost control:
  * Every LLM call's real token usage is metered through budget.py against a hard
    NIGHT_BUDGET_USD cap. When the cap is hit the run stops cleanly (PARTIAL).
  * Verdicts are cached by (prompt-version-hash, patient, trial). If a /loop tick
    didn't change the prompt/parser, every case is a cache hit and costs $0 - so
    the budget is only spent when there's a real change to measure.

Usage:
    python match_eval.py --n 300 --workers 6
    NIGHT_BUDGET_USD=15 python match_eval.py
"""
import argparse
import datetime as dt
import glob
import hashlib
import inspect
import json
import os
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))          # so `import match_trials` works
import match_trials as mt                      # noqa: E402
import budget                                  # noqa: E402

DATA_DIR = os.environ.get("MATCH_EVAL_DATA", "/tmp/bmd_eval/TrialGPT/dataset")
SET_FILE = {"dev": HERE / "eval_set.json", "test": HERE / "eval_test.json",
            "final": HERE / "eval_final.json"}
CACHE = HERE / "eval_cache.json"
LOG = HERE / "match_eval_log.jsonl"
NIGHT_LOG = HERE / "NIGHT_LOG.md"

# Wire the cost meter into the matcher's LLM calls.
mt.USAGE_HOOK = budget.record

_cache_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Prompt version: any change to the prompt or the parser invalidates the cache.
# --------------------------------------------------------------------------- #
def prompt_hash():
    parts = [
        getattr(mt, "LLM_MODEL", ""),   # different model = different verdicts
        getattr(mt, "MATCH_SYSTEM", ""),
        getattr(mt, "MATCH_SCHEMA", ""),
        str(getattr(mt, "VALID_VERDICTS", "")),
    ]
    for fn in ("llm_match", "normalize_match", "_extract_json"):
        try:
            parts.append(inspect.getsource(getattr(mt, fn)))
        except (OSError, TypeError):
            pass
    return hashlib.sha1("\x1e".join(parts).encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Dataset -> frozen, balanced eval set (built once, reused every run).
# --------------------------------------------------------------------------- #
def _trial_dict(t):
    crit = "\n\n".join(x for x in (t.get("inclusion_criteria", ""),
                                   t.get("exclusion_criteria", "")) if x).strip()
    return {
        "title": t.get("brief_title", "") or "",
        "nctId": t.get("NCTID", "") or "",
        "phase": t.get("phase", "") or "NA",
        "sex": "", "minAge": "", "maxAge": "", "healthyVolunteers": "",
        "criteria": crit,
    }


def _shuffled_pools(seed):
    files = sorted(glob.glob(os.path.join(DATA_DIR, "*", "retrieved_trials.json")))
    if not files:
        sys.exit(f"No retrieved_trials.json under {DATA_DIR}. Set MATCH_EVAL_DATA.")
    buckets = {"2": [], "1": [], "0": []}
    seen = set()
    for path in files:
        ds = Path(path).parent.name
        try:
            data = json.load(open(path))
        except Exception as e:
            print(f"  skip {path}: {e}")
            continue
        recs = data if isinstance(data, list) else list(data.values())
        for rec in recs:
            pid = str(rec.get("patient_id", ""))
            patient = (rec.get("patient") or "").strip()
            if not patient:
                continue
            for label in ("2", "1", "0"):
                for t in (rec.get(label) or []):
                    nct = t.get("NCTID", "")
                    crit = (t.get("inclusion_criteria", "") or "") + \
                           (t.get("exclusion_criteria", "") or "")
                    if not nct or not crit.strip():
                        continue
                    key = (ds, pid, nct)
                    if key in seen:
                        continue
                    seen.add(key)
                    buckets[label].append({
                        "ds": ds, "pid": pid, "nct": nct, "label": int(label),
                        "patient": patient, "trial": _trial_dict(t),
                    })
    rng = random.Random(seed)
    for b in buckets.values():
        rng.shuffle(b)
    print(f"  pool sizes: eligible={len(buckets['2'])} excluded={len(buckets['1'])} "
          f"irrelevant={len(buckets['0'])}")
    return buckets


def build_splits(seed, comp, n_dev, n_test, n_final):
    """Build 3 DISJOINT sets from one seeded shuffle: consecutive per-label slices
    so no patient/trial pair appears in more than one split.

    - dev:   diagnose + iterate on it.
    - test:  optimization objective (generalization), guarded so we don't overfit.
    - final: NEVER inspected during the loop; run once at the end for the honest
      reported number.

    Deterministic in `seed`, so rebuilding reproduces identical sets (cache valid).
    """
    pools = _shuffled_pools(seed)
    dev, test, final = [], [], []
    for lab, frac in comp.items():
        kd, kt, kf = (int(round(n * frac)) for n in (n_dev, n_test, n_final))
        dev.extend(pools[lab][:kd])
        test.extend(pools[lab][kd:kd + kt])
        final.extend(pools[lab][kd + kt:kd + kt + kf])
    random.Random(seed + 1).shuffle(dev)
    random.Random(seed + 2).shuffle(test)
    random.Random(seed + 3).shuffle(final)
    return {"dev": dev, "test": test, "final": final}


def load_split(split, seed, comp, n_dev, n_test, n_final, rebuild):
    fp = SET_FILE[split]
    if fp.exists() and not rebuild:
        cases = json.load(open(fp))
        print(f"  reusing frozen {split} set: {len(cases)} cases ({fp.name})")
        return cases
    sets = build_splits(seed, comp, n_dev, n_test, n_final)
    for name, cases in sets.items():
        json.dump(cases, open(SET_FILE[name], "w"))
    print(f"  built frozen sets: dev={len(sets['dev'])} test={len(sets['test'])} "
          f"final={len(sets['final'])} (disjoint)")
    return sets[split]


# --------------------------------------------------------------------------- #
# Verdict cache (prompt-hash scoped).
# --------------------------------------------------------------------------- #
def load_cache():
    if CACHE.exists():
        try:
            return json.load(open(CACHE))
        except Exception:
            return {}
    return {}


def save_cache(c):
    with _cache_lock:
        json.dump(c, open(CACHE, "w"))


# --------------------------------------------------------------------------- #
# Run.
# --------------------------------------------------------------------------- #
def _as_match(v):
    """Cache holds full match dicts; tolerate legacy string entries."""
    if isinstance(v, dict):
        return v
    return {"verdict": v, "rationale": "", "not_met": [], "met": [], "unknown": []}


def verdict_for(case, phash, cache):
    ckey = f"{phash}:{case['ds']}:{case['pid']}:{case['nct']}"
    with _cache_lock:
        if ckey in cache:
            return _as_match(cache[ckey]), True
    try:
        m = mt.llm_match(case["patient"], case["trial"])
    except Exception as e:
        m = {"verdict": "error", "rationale": str(e)[:120],
             "not_met": [], "met": [], "unknown": []}
        print(f"    call failed ({case['nct']}): {str(e)[:80]}")
    with _cache_lock:
        cache[ckey] = m
    return m, False


def evaluate(cases, workers):
    phash = prompt_hash()
    cache = load_cache()
    results = {}          # ckey-order preserved via list
    order = list(cases)
    stopped = False
    done = 0

    def work(case):
        return case, verdict_for(case, phash, cache)

    # Cache-hit pass first (free), then spend budget on misses.
    misses = []
    for case in order:
        ckey = f"{phash}:{case['ds']}:{case['pid']}:{case['nct']}"
        if ckey in cache:
            results[id(case)] = _as_match(cache[ckey])
        else:
            misses.append(case)
    print(f"  cache: {len(order) - len(misses)} hits, {len(misses)} to score "
          f"(prompt {phash})")

    if misses and budget.over_cap():
        print(f"  BUDGET already at/over cap "
              f"(${budget.spent()['usd']:.4f}/${budget.cap_usd():.2f}); "
              f"scoring cache only.")
        stopped = True
        misses = []

    # Score misses in bounded parallel batches; re-check budget before each batch.
    i = 0
    while i < len(misses) and not stopped:
        if budget.over_cap():
            print(f"  BUDGET cap hit (${budget.spent()['usd']:.4f}); stopping early.")
            stopped = True
            break
        batch = misses[i:i + workers]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for case, (m, _hit) in ex.map(work, batch):
                results[id(case)] = m
        i += len(batch)
        done += len(batch)
        if done % (workers * 5) == 0 or i >= len(misses):
            save_cache(cache)
            print(f"    scored {i}/{len(misses)}  spent ${budget.spent()['usd']:.4f}")
    save_cache(cache)

    scored = [(c, results[id(c)]) for c in order if id(c) in results]
    return scored, stopped, phash


# --------------------------------------------------------------------------- #
# Metrics.
# --------------------------------------------------------------------------- #
PASS = {"likely_eligible", "possible"}


def score_report(scored):
    by = {2: [], 1: [], 0: []}
    errors = 0
    for c, m in scored:
        v = m.get("verdict", "error")
        if v == "error":
            errors += 1
            continue
        by[c["label"]].append(v)

    def frac(lst, pred):
        return (sum(1 for v in lst if pred(v)) / len(lst)) if lst else 0.0

    elig = by[2]
    excl = by[1]
    irr = by[0]

    eligible_recall = frac(elig, lambda v: v in PASS)            # not wrongly rejected
    strong_pass = frac(elig, lambda v: v == "likely_eligible")
    false_exclude = frac(elig, lambda v: v == "unlikely")        # CRITICAL
    exclusion_catch = frac(excl, lambda v: v == "unlikely")      # correctly rejected
    false_eligible = frac(excl, lambda v: v == "likely_eligible")  # CRITICAL
    excl_leak_possible = frac(excl, lambda v: v == "possible")
    irr_correct = frac(irr, lambda v: v == "unlikely")
    irr_overmatch = frac(irr, lambda v: v == "likely_eligible")

    n_scored = len(elig) + len(excl) + len(irr)
    n_crit = (sum(1 for v in elig if v == "unlikely")
              + sum(1 for v in excl if v == "likely_eligible"))
    critical_rate = (n_crit / n_scored) if n_scored else 0.0
    balanced = (eligible_recall + exclusion_catch) / 2
    headline = round(balanced - critical_rate, 4)   # optimize this upward

    return {
        "n_scored": n_scored, "errors": errors,
        "n_eligible": len(elig), "n_excluded": len(excl), "n_irrelevant": len(irr),
        "eligible_recall": round(eligible_recall, 4),
        "strong_pass": round(strong_pass, 4),
        "false_exclude": round(false_exclude, 4),
        "exclusion_catch": round(exclusion_catch, 4),
        "false_eligible": round(false_eligible, 4),
        "excl_leak_possible": round(excl_leak_possible, 4),
        "irr_correct": round(irr_correct, 4),
        "irr_overmatch": round(irr_overmatch, 4),
        "critical_rate": round(critical_rate, 4),
        "balanced": round(balanced, 4),
        "headline": headline,
    }


def print_report(rep, spent, phash, partial, split="dev"):
    print("\n" + "=" * 62)
    print(f"  ELIGIBILITY EVAL [{split.upper()}]  (prompt {phash})"
          + ("  [PARTIAL - budget]" if partial else ""))
    print("=" * 62)
    print(f"  scored {rep['n_scored']}  errors {rep['errors']}  "
          f"(elig {rep['n_eligible']} / excl {rep['n_excluded']} / irr {rep['n_irrelevant']})")
    print(f"  eligible-recall   {rep['eligible_recall']:.3f}   "
          f"(strong {rep['strong_pass']:.3f})")
    print(f"  exclusion-catch   {rep['exclusion_catch']:.3f}   "
          f"(leaked-possible {rep['excl_leak_possible']:.3f})")
    print(f"  irrelevant-correct{rep['irr_correct']:.3f}")
    print(f"  ---- CRITICAL ----")
    print(f"  false-exclude     {rep['false_exclude']:.3f}  (dropped real candidates)")
    print(f"  false-eligible    {rep['false_eligible']:.3f}  (advanced ineligible)")
    print(f"  irr-overmatch     {rep['irr_overmatch']:.3f}")
    print(f"  critical-rate     {rep['critical_rate']:.3f}")
    print("  " + "-" * 58)
    print(f"  HEADLINE (balanced - critical) = {rep['headline']:.4f}   (higher better)")
    print(f"  spent ${spent['usd']:.4f} / cap ${budget.cap_usd():.2f}  "
          f"({spent['calls']} calls)")
    print("=" * 62 + "\n")


def append_logs(rep, spent, phash, partial, split="dev"):
    row = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
           "prompt": phash, "split": split, "partial": partial,
           "spent_usd": round(spent["usd"], 4), "calls": spent["calls"], **rep}
    with open(LOG, "a") as f:
        f.write(json.dumps(row) + "\n")
    line = (f"| {row['ts']} | {split} | `{phash}` | **{rep['headline']:.4f}** | "
            f"{rep['eligible_recall']:.3f} | {rep['exclusion_catch']:.3f} | "
            f"{rep['false_eligible']:.3f} | {rep['false_exclude']:.3f} | "
            f"{rep['critical_rate']:.3f} | ${row['spent_usd']:.3f} |"
            + (" _(partial)_" if partial else "") + "\n")
    if not NIGHT_LOG.exists():
        NIGHT_LOG.write_text(
            "# Overnight eligibility-matching log\n\n"
            "Higher **headline** = better (balanced accuracy minus critical-error rate).\n"
            "The loop diagnoses + selects on **dev**; **test** is the held-out "
            "generalization check it never tunes against.\n\n"
            "| time | split | prompt | headline | elig-recall | excl-catch | "
            "false-elig | false-excl | crit-rate | spent |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n")
    with open(NIGHT_LOG, "a") as f:
        f.write(line)


_LABEL_NAME = {2: "ELIGIBLE", 1: "EXCLUDED", 0: "IRRELEVANT"}


def dump_fails(scored, split, per_cat=12):
    """Write misclassified cases (with rationale) so the loop can diagnose and
    form a targeted prompt/parser hypothesis instead of guessing."""
    fails_path = HERE / f"eval_fails_{split}.json"
    cats = {"false_exclude": [], "false_eligible": [],
            "excl_leak_possible": [], "irr_overmatch": []}
    for c, m in scored:
        v = m.get("verdict", "error")
        lab = c["label"]
        cat = None
        if lab == 2 and v == "unlikely":
            cat = "false_exclude"
        elif lab == 1 and v == "likely_eligible":
            cat = "false_eligible"
        elif lab == 1 and v == "possible":
            cat = "excl_leak_possible"
        elif lab == 0 and v == "likely_eligible":
            cat = "irr_overmatch"
        if cat and len(cats[cat]) < per_cat:
            cats[cat].append({
                "nct": c["nct"], "gold": _LABEL_NAME[lab], "verdict": v,
                "rationale": m.get("rationale", "")[:400],
                "not_met": (m.get("not_met") or [])[:6],
                "unknown": (m.get("unknown") or [])[:6],
                "patient": c["patient"][:600],
                "criteria": (c["trial"].get("criteria", ""))[:1400],
            })
    json.dump(cats, open(fails_path, "w"), indent=2)
    return fails_path.name, {k: len(v) for k, v in cats.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=("dev", "test", "final"), default="dev",
                    help="dev=diagnose; test=optimize (guarded); final=morning-only")
    ap.add_argument("--n", type=int, default=300, help="dev set size")
    ap.add_argument("--n-test", type=int, default=300, help="test set size")
    ap.add_argument("--n-final", type=int, default=300, help="final holdout size")
    ap.add_argument("--seed", type=int, default=20260723)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--rebuild", action="store_true", help="rebuild frozen sets")
    args = ap.parse_args()

    if not mt.LLM_API_KEY:
        sys.exit("No LLM_API_KEY in env. Source outreach/secrets.env first.")

    comp = {"2": 0.40, "1": 0.40, "0": 0.20}
    print(f"Eligibility eval [{args.split}]  model={mt.LLM_MODEL}  "
          f"cap=${budget.cap_usd():.2f}  spent so far=${budget.spent()['usd']:.4f}")
    cases = load_split(args.split, args.seed, comp, args.n, args.n_test,
                       args.n_final, args.rebuild)
    scored, partial, phash = evaluate(cases, args.workers)
    rep = score_report(scored)
    spent = budget.spent()
    print_report(rep, spent, phash, partial, args.split)
    append_logs(rep, spent, phash, partial, args.split)
    name, fc = dump_fails(scored, args.split)
    print(f"  wrote failure samples -> {name} {fc}")


if __name__ == "__main__":
    main()
