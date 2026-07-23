"""Hard dollar cap + token/cost ledger for the overnight eligibility loop.

Every LLM call's REAL token usage (from the API `usage` field) is recorded to a
file-locked JSON ledger, so the nightly cap holds ACROSS processes - each /loop
tick is a fresh Python process, but they all share this ledger. Prices are USD
per 1M tokens; unknown models fall back to a conservative estimate so we never
under-count and blow the cap.

Cap is read from NIGHT_BUDGET_USD (default 15). Ledger path from NIGHT_LEDGER.
"""
import fcntl
import json
import os
import time

LEDGER = os.environ.get(
    "NIGHT_LEDGER", os.path.join(os.path.dirname(__file__), "night_spend.json"))

# USD per 1M tokens: (input, output). Extend as needed.
PRICING = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    "o3-mini": (1.10, 4.40),
}
_FALLBACK = (1.00, 4.00)   # conservative when the model is unknown


def cap_usd():
    # Hard default $5 for the night (per the agreed cap). NIGHT_BUDGET_USD can
    # only be used to LOWER it further, never to silently raise it above $5.
    try:
        req = max(0.0, float(os.environ.get("NIGHT_BUDGET_USD", "5")))
    except ValueError:
        return 5.0
    return min(req, 5.0)


def price_for(model):
    m = (model or "").lower().strip()
    if m in PRICING:
        return PRICING[m]
    for k, v in PRICING.items():
        if m.startswith(k):
            return v
    return _FALLBACK


def cost_usd(usage, model):
    pin, pout = price_for(model)
    it = int((usage or {}).get("prompt_tokens", 0) or 0)
    ot = int((usage or {}).get("completion_tokens", 0) or 0)
    return (it * pin + ot * pout) / 1_000_000.0


def _blank():
    return {"usd": 0.0, "calls": 0, "in_tok": 0, "out_tok": 0, "started": time.time()}


def _ensure():
    if not os.path.exists(LEDGER):
        with open(LEDGER, "w") as f:
            json.dump(_blank(), f)
    return LEDGER


def record(usage, model):
    """Add one call's usage to the ledger. Returns new cumulative USD."""
    it = int((usage or {}).get("prompt_tokens", 0) or 0)
    ot = int((usage or {}).get("completion_tokens", 0) or 0)
    add = cost_usd(usage, model)
    with open(_ensure(), "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            try:
                f.seek(0)
                d = json.load(f)
            except Exception:
                d = _blank()
            d["usd"] = round(float(d.get("usd", 0.0)) + add, 6)
            d["calls"] = int(d.get("calls", 0)) + 1
            d["in_tok"] = int(d.get("in_tok", 0)) + it
            d["out_tok"] = int(d.get("out_tok", 0)) + ot
            f.seek(0)
            f.truncate()
            json.dump(d, f)
            f.flush()
            return d["usd"]
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def spent():
    if not os.path.exists(LEDGER):
        return _blank()
    try:
        with open(LEDGER) as f:
            return json.load(f)
    except Exception:
        return _blank()


def remaining():
    return max(0.0, cap_usd() - float(spent().get("usd", 0.0)))


def over_cap():
    return float(spent().get("usd", 0.0)) >= cap_usd()


if __name__ == "__main__":
    s = spent()
    print(f"spent ${s.get('usd', 0):.4f} / cap ${cap_usd():.2f}  "
          f"({s.get('calls', 0)} calls, in={s.get('in_tok', 0)}, "
          f"out={s.get('out_tok', 0)})  remaining ${remaining():.4f}")
