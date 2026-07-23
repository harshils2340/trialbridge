"""Overnight search-quality benchmark.

Runs a fixed battery of realistic patient searches through the REAL /find
pipeline (spelling correction, lay-term normalization, LLM interpretation,
CT.gov union fetch) and scores how many surface a sensible number of trials.

Key design choices:
- A *control* query ("diabetes", thousands of trials) runs first. If it comes
  back near-empty, CT.gov is throttling this IP, so the whole run is marked
  THROTTLED and NOT scored - that stops us from "fixing" phantom failures.
- We use a large radius so geographic sparsity (e.g. few ADHD trials near one
  city) doesn't masquerade as a matching failure; this isolates MATCH quality.
- Each case is spaced out to stay under CT.gov's rate limit - perfect for a slow
  overnight loop.
- Results append to search_eval_log.jsonl so we can watch the score climb over
  the night and catch regressions.

Usage: python search_eval.py            # one scored run
       python search_eval.py --quick    # smaller/faster battery
"""
import json
import os
import re
import sys
import time
import datetime as dt

import app as A

LOG = os.path.join(os.path.dirname(__file__), "search_eval_log.jsonl")
LOC = "New York"          # dense hub; big radius makes it ~nationwide
RADIUS = "4000"           # km - neutralize geo so we measure matching, not distance
SPACING = 18.0            # seconds between cases. Each /find makes 2-4 CT.gov
                          # calls (primary + suggest + corrected + broad-term),
                          # so we pace slowly - the whole point of an 8h loop is
                          # gentle, polite testing that never trips CT.gov's rate
                          # limit mid-run.
CONTROL_MIN = 40          # if the control returns fewer, assume throttled

# (query, freeform, min_expected, category, note)
# min_expected is a conservative national floor; the point is "did matching work"
# not an exact count. freeform="1" simulates the "Describe it" mode.
CASES = [
    # --- typos (should spell-correct via CT.gov suggest) ---
    ("brian", "0", 20, "typo", "brain"),
    ("diabetis", "0", 40, "typo", "diabetes"),
    ("cancr", "0", 40, "typo", "cancer"),
    ("alzheimers", "0", 15, "typo", "alzheimer"),
    ("parkinsons", "0", 20, "typo", "parkinson"),
    ("athsma", "0", 20, "typo", "asthma"),
    ("depresion", "0", 20, "typo", "depression"),
    ("arthritus", "0", 20, "typo", "arthritis"),
    # --- lay / slang (should normalize to canonical term) ---
    ("sugar disease", "0", 40, "lay", "diabetes"),
    ("high blood pressure", "0", 40, "lay", "hypertension"),
    ("heart attack", "0", 20, "lay", "MI"),
    ("water pill", "0", 1, "lay", "diuretics"),
    ("blood clot", "0", 10, "lay", "thrombosis"),
    # --- brand / common drug names ---
    ("ozempic", "0", 5, "drug", "semaglutide"),
    ("wegovy", "0", 5, "drug", "semaglutide"),
    ("mounjaro", "0", 5, "drug", "tirzepatide"),
    ("humira", "0", 5, "drug", "adalimumab"),
    ("keytruda", "0", 5, "drug", "pembrolizumab"),
    # --- abbreviations ---
    ("adhd", "0", 20, "abbrev", "attention deficit"),
    ("copd", "0", 20, "abbrev", "chronic obstructive"),
    ("ibs", "0", 10, "abbrev", "irritable bowel"),
    ("nash", "0", 10, "abbrev", "steatohepatitis"),
    ("ckd", "0", 20, "abbrev", "chronic kidney"),
    ("als", "0", 10, "abbrev", "amyotrophic"),
    # --- vague symptoms (freeform / describe-it) ---
    ("trouble sleeping", "1", 5, "vague", "insomnia/sleep"),
    ("cant lose weight", "1", 10, "vague", "obesity"),
    ("always tired and cold", "1", 3, "vague", "fatigue/thyroid"),
    ("short of breath when walking", "1", 3, "vague", "COPD/heart"),
    # --- niche / rare ---
    ("long covid", "0", 3, "niche", "post-acute covid"),
    ("pots syndrome", "0", 1, "niche", "dysautonomia"),
    ("ehlers danlos", "0", 1, "niche", "connective tissue"),
    ("lupus", "0", 20, "niche", "SLE"),
]

QUICK = [c for c in CASES if c[3] in ("typo", "lay", "drug")][:10]


_ip_ctr = [0]


def _next_ip():
    # Distinct source IP per request so our per-IP abuse limiter (keyed on
    # remote_addr) treats each benchmark query like a different patient instead
    # of blocking the whole run after a few POSTs.
    _ip_ctr[0] += 1
    n = _ip_ctr[0]
    return f"203.0.113.{n % 254 + 1}" if n < 254 else f"198.51.100.{n % 254 + 1}"


def _count(client, csrf, query, freeform):
    r = client.post("/find", data={
        "condition": query, "location": LOC, "radius": RADIUS,
        "freeform": freeform, "_csrf_token": csrf,
    }, follow_redirects=True, environ_base={"REMOTE_ADDR": _next_ip()})
    html = r.get_data(as_text=True)
    return len(set(re.findall(r"/trial/[A-Za-z0-9]+/NCT\d+", html)))


STATE = os.path.join(os.path.dirname(__file__), "search_eval_state.json")
BATCH = 8   # cases tested per tick - small burst keeps CT.gov from throttling


def _load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"offset": 0, "latest": {}}


def _save_state(st):
    with open(STATE, "w") as f:
        json.dump(st, f, indent=2)


def _test_case(client, csrf, query, ff, floor):
    """Test one case, retrying once on a 0 (spaced) to shrug off a brief blip."""
    n = _count(client, csrf, query, ff)
    if n < floor:
        time.sleep(SPACING)
        n = _count(client, csrf, query, ff)   # second chance
    return n


def run(quick=False):
    """Test a small ROTATING batch each tick (gentle on CT.gov), persist the
    latest result per case, and score the whole suite from those latest results
    so the aggregate improves as batches clear over the night."""
    client = A.app.test_client()
    with client.session_transaction() as s:
        s["_csrf_token"] = "eval"
    csrf = "eval"
    cases = QUICK if quick else CASES

    # Control gatekeeper: "diabetes" has thousands of trials, so a near-zero
    # result means CT.gov is throttling us. Give it a few spaced tries first - a
    # single momentary blip (200-empty / timeout) shouldn't abort the whole tick.
    # CT.gov throttles this IP in short (~1-2 min) intermittent bursts. Ride one
    # out with a longer cooldown between control checks so a transient burst
    # doesn't waste a whole tick.
    control = 0
    for attempt, cooldown in enumerate((75, 120, 150)):
        control = _count(client, csrf, "diabetes", "0")
        if control >= CONTROL_MIN:
            break
        print(f"  control low ({control}) - cooling {cooldown}s "
              f"(retry {attempt + 1}/3)")
        time.sleep(cooldown)
    if control < CONTROL_MIN:
        rec = {"ts": dt.datetime.utcnow().isoformat(), "throttled": True,
               "control": control}
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"THROTTLED (control diabetes={control}) - skipping scored run.")
        return rec
    time.sleep(SPACING)

    st = _load_state()
    off = st.get("offset", 0) % len(cases)
    batch = [cases[(off + i) % len(cases)] for i in range(min(BATCH, len(cases)))]

    for query, ff, floor, cat, note in batch:
        try:
            n = _test_case(client, csrf, query, ff, floor)
        except Exception as e:
            n = -1
            print(f"  ERROR {query!r}: {e}")
        ok = n >= floor
        st["latest"][query] = {"n": n, "ok": ok, "min": floor, "cat": cat,
                               "expect": note, "ts": dt.datetime.utcnow().isoformat()}
        print(f"  [{'ok ' if ok else 'FAIL'}] {cat:7} {query!r:34} -> {n:>4} (min {floor})")
        time.sleep(SPACING)

    st["offset"] = (off + len(batch)) % len(cases)
    _save_state(st)

    # Aggregate over every case's LATEST known result (untested-this-tick cases
    # keep their prior result). Cases never tested yet are treated as unknown.
    latest = st["latest"]
    known = [c for c in cases if c[0] in latest]
    passed = sum(1 for c in known if latest[c[0]]["ok"])
    score = round(100 * passed / len(known), 1) if known else 0.0
    fails = [{"q": c[0], "n": latest[c[0]]["n"], "min": c[2], "cat": c[3],
              "expect": c[4]} for c in known if not latest[c[0]]["ok"]]

    rec = {"ts": dt.datetime.utcnow().isoformat(), "throttled": False,
           "control": control, "batch": [b[0] for b in batch],
           "score": score, "passed": passed, "covered": len(known),
           "total": len(cases), "failures": fails}
    with open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"\nSCORE {score}%  ({passed}/{len(known)} of {len(cases)} covered)  "
          f"control={control}")
    if fails:
        print("CURRENT FAILURES (latest result):")
        for fl in fails:
            print(f"  - {fl['cat']:7} {fl['q']!r} got {fl['n']} "
                  f"(want >= {fl['min']}; expect ~{fl['expect']})")
    return rec


if __name__ == "__main__":
    run(quick="--quick" in sys.argv)
