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

## Split protocol (READ THIS)
- **dev** (`eval_set.json`, `eval_fails_dev.json`): diagnose + select on it.
- **test** (`eval_test.json`, `eval_fails_test.json`): DISJOINT held-out set. It is
  the honest generalization number. Use it only as a GUARD (never diagnose changes
  purely to fit test). The headline we care about reporting is the **test** headline.

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

## Step 4 - Evaluate (dev first, then test only if dev is promising)
```
NIGHT_BUDGET_USD=5 $PY match_eval.py --split dev  --workers 6
```
If dev did NOT improve headline by > 0.02 over best-dev (or crit-rate worsened by
> 0.01): REVERT now (`git checkout -- match_trials.py`), log, end tick. Save the
test spend. Otherwise it's a candidate -> run the guard:
```
NIGHT_BUDGET_USD=5 $PY match_eval.py --split test --workers 6
```

## Step 5 - Green-or-revert (test-guarded)
Accept & commit ONLY if ALL hold:
- dev headline improves > 0.02 over best-dev, AND dev crit-rate not worse by >0.01, AND
- test headline does NOT regress > 0.02 vs best-test, AND test crit-rate not worse
  by > 0.02 vs best-test.
If accepted:
```
git add -A && git commit -m "eligibility: <what changed> (dev X->Y, test A->B)"
```
If test regressed while dev improved = OVERFIT -> REVERT and record it as an
overfit hypothesis in NIGHT_LOG.md. Otherwise (dev fine, test fine) keep it.

## Step 6 - Log
Append one line to NIGHT_LOG.md: tick time, hypothesis, result (accepted/reverted),
new metrics. Keep it terse. End the tick (the loop arms the next one).
```
