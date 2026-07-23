# Overnight eligibility-loop tick runbook

Each loop tick = ONE hypothesis-driven attempt to improve trial-eligibility
matching accuracy, measured against a frozen 300-pair benchmark, kept only if it
genuinely helps (green-or-revert). All work stays on branch `night/eligibility`
in the `matcher-night` worktree. Never touch `main`, never add product features,
never introduce PHI.

## Environment (every tick)
```
cd /Users/harshils/GraphMD/matcher-night/web
set -a && . /Users/harshils/GraphMD/outreach/secrets.env && set +a
PY=/Users/harshils/GraphMD/matcher/.venv/bin/python
```

## Step 0 - Budget gate (HARD STOP)
```
$PY budget.py
```
If spent >= cap (NIGHT_BUDGET_USD, default $5): DO NOT edit or eval. Append a
line to NIGHT_LOG.md ("budget reached, holding") and end the tick. Nothing else.

## Step 1 - Read state
- `tail -5 match_eval_log.jsonl` -> current BEST `headline` seen so far.
- Read `eval_fails.json` -> concrete misclassified cases with rationale.
- Read NIGHT_LOG.md tail -> hypotheses already tried (don't repeat a failed one).

## Step 2 - Diagnose the dominant failure
Priority order of what to fix (biggest lever first):
1. `false_exclude` (gold ELIGIBLE -> we said unlikely) - we're too strict,
   dropping real candidates. This is the current biggest problem (~0.42).
2. `false_eligible` (gold EXCLUDED -> we said likely_eligible) - dangerous.
3. `excl_leak_possible` (gold EXCLUDED -> possible) - missed an exclusion.
4. `irr_overmatch` (gold IRRELEVANT -> likely_eligible) - wrong condition passed.
Read the rationales: is it treating UNKNOWN info as a failure? Over-weighting one
unmet minor criterion? Misreading age/sex embedded in criteria text? Parser
downgrading correctly-eligible verdicts?

## Step 3 - Make ONE targeted change
Edit ONLY `matcher-night/match_trials.py`: the `MATCH_SYSTEM` prompt, the
`MATCH_SCHEMA`, or the `normalize_match` parser. One coherent change per tick so
the metric delta is attributable. No other files.

## Step 4 - Re-evaluate
```
NIGHT_BUDGET_USD=5 $PY match_eval.py --workers 6
```
(Prompt hash changes -> full re-score, bounded by the budget cap.)

## Step 5 - Green-or-revert
Accept the change ONLY if BOTH hold vs the previous best:
- `headline` improves by > 0.02 (run-to-run noise is ~0.01), AND
- `critical_rate` does not get worse by more than 0.01.
If accepted:
```
git add -A && git commit -m "eligibility: <what changed> (headline X -> Y)"
```
If rejected:
```
git checkout -- match_trials.py
```
and record the failed hypothesis in NIGHT_LOG.md so it isn't retried.

## Step 6 - Log
Append one line to NIGHT_LOG.md: tick time, hypothesis, result (accepted/reverted),
new metrics. Keep it terse. End the tick (the loop arms the next one).
```
