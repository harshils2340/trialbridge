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
export LLM_MODEL=gpt-4.1-mini   # MATCHER MODEL - measured to beat 4o-mini AND 4o
PY=/Users/harshils/GraphMD/matcher/.venv/bin/python
```
Current bests on **gpt-4.1-mini**: dev headline 0.588, test headline 0.614
(critical-rate ~0.08). These are the numbers to beat. The cache is model-scoped,
so keep LLM_MODEL fixed at gpt-4.1-mini or you'll re-score from scratch.

## Step 0 - Budget gate (HARD STOP)
```
$PY budget.py
```
If spent >= cap (NIGHT_BUDGET_USD, default $5): DO NOT edit or eval. Append a
line to NIGHT_LOG.md ("budget reached, holding") and end the tick. Nothing else.

## Split protocol (READ THIS)
Three DISJOINT sets (no patient/trial pair shared). Objective = generalization.
- **dev** (`eval_set.json`, `eval_fails_dev.json`): where you DIAGNOSE (read fails).
  Also an anti-overfit GUARD: dev must not regress much when you accept a change.
- **test** (`eval_test.json`, `eval_fails_test.json`): the OPTIMIZATION OBJECTIVE.
  Maximize test headline. Read its fails too - they're the truest signal.
- **final** (`eval_final.json`): NEVER run or inspect during the loop. It is only
  for the morning to report one honest, untouched number.

## Step 1 - Read state
- `tail -8 match_eval_log.jsonl` -> best `headline` per split so far (rows tagged
  with `"split"`). Track best-dev and best-test.
- Read `eval_fails_dev.json` AND `eval_fails_test.json` -> misclassified cases +
  rationale. The test fails are the truest signal of what to fix.
- Read NIGHT_LOG.md tail -> hypotheses already tried (don't repeat a failed one).

## Step 2 - Diagnose the dominant failure (balance-aware)
There is NO fixed priority - read the CURRENT numbers and attack the largest
critical error WITHOUT regressing the opposite side. The two sides are in tension:
- `false_exclude` (gold ELIGIBLE -> unlikely): too STRICT, dropping real candidates.
- `false_eligible` (gold EXCLUDED -> likely_eligible): too LENIENT, advancing
  ineligible patients. `excl_leak_possible` (EXCLUDED -> possible) and
  `irr_overmatch` (IRRELEVANT -> likely_eligible) are the same leniency failure.
The hard skill is telling ELIGIBLE from EXCLUDED for the SAME condition: read the
exclusion criteria in the fails - is the model ignoring a clearly-triggered
exclusion? Not distinguishing "has condition" from "has condition BUT excluded"?
Mis-scoring irrelevant conditions as a match? Form a hypothesis that fixes the
dominant error without swinging the pendulum back.

## Step 3 - Make ONE targeted change
Edit ONLY `matcher-night/match_trials.py`: `MATCH_SYSTEM`, `MATCH_SCHEMA`, or the
`normalize_match` parser. One coherent change per tick so the delta is attributable.
No other files.

## Step 4 - Evaluate on dev AND test (never final)
```
NIGHT_BUDGET_USD=5 $PY match_eval.py --split dev  --workers 6
NIGHT_BUDGET_USD=5 $PY match_eval.py --split test --workers 6
```
(Prompt hash changed -> both re-score, ~$0.14, bounded by the cap. If the cap is
hit mid-run the eval stops clean and marks PARTIAL - then treat as no-improvement
and revert.)

## Step 5 - Green-or-revert (objective = TEST, guard = dev)
Accept & commit ONLY if ALL hold:
- **test** headline improves > 0.02 over best-test (this is the objective), AND
- **test** crit-rate not worse by > 0.01 vs best-test, AND
- **dev** headline does NOT regress > 0.03 vs best-dev (anti-overfit guard).
If accepted:
```
git add -A && git commit -m "eligibility: <what changed> (test A->B, dev C->D)"
```
If test improved but dev collapsed = suspicious/overfit -> REVERT, record why.
If test did not improve -> REVERT (`git checkout -- match_trials.py`), log the
failed hypothesis in NIGHT_LOG.md so it isn't retried. NEVER touch `final`.

## Step 6 - Log
Append one line to NIGHT_LOG.md: tick time, hypothesis, result (accepted/reverted),
new metrics. Keep it terse. End the tick (the loop arms the next one).
```
