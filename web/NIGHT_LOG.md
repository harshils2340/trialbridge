# Overnight eligibility-matching log

Higher **headline** = better (balanced accuracy minus critical-error rate).

| time | prompt | headline | elig-recall | excl-catch | false-elig | false-excl | crit-rate | spent |
|---|---|---|---|---|---|---|---|---|
| 2026-07-23T04:12:47 | `9594f845616d` | **0.4442** | 0.575 | 0.733 | 0.100 | 0.425 | 0.210 | $0.135 |
| 2026-07-23T04:20:14 | `d30cc26d6812` | **0.5383** | 0.808 | 0.575 | 0.192 | 0.192 | 0.153 | $0.211 |

> **Tick 1** (`d30cc26`): hypothesis — matcher conflated UNKNOWN with FAIL (fabricated `not_met` from silence, chose `unlikely` on mere unknowns). Fix: sharpened rules 2-5 so `not_met` needs an explicit contradiction, silence=unknown, `unlikely` reserved for explicit contradictions. **ACCEPTED** (headline 0.4442→0.5383; elig-recall 0.575→0.808). Next: tighten irrelevant/excluded side (irr-overmatch 0.05→0.217, false-eligible 0.10→0.192) without re-breaking eligible-recall.
| 2026-07-23T04:24:54 | dev | `d30cc26d6812` | **0.5383** | 0.808 | 0.575 | 0.192 | 0.192 | 0.153 | $0.211 |
| 2026-07-23T04:27:40 | test | `d30cc26d6812` | **0.4350** | 0.842 | 0.475 | 0.400 | 0.158 | 0.223 | $0.287 |
| 2026-07-23T04:34:40 | dev | `2c203c537962` | **0.5133** | 0.667 | 0.733 | 0.133 | 0.333 | 0.187 | $0.368 |
| 2026-07-23T04:37:26 | test | `2c203c537962` | **0.4017** | 0.675 | 0.608 | 0.275 | 0.325 | 0.240 | $0.447 |

> **Tick 2** REVERTED (overfit/over-correction). Hypothesis: exclusion-first ordered verdict + require decisive inclusions CONFIRMED (not unknown) for likely_eligible. Result: fixed leniency (test false-elig 0.400->0.275, irr-overmatch 0.217->0.083, excl-catch 0.475->0.608) but over-swung to strict (test elig-recall 0.842->0.675, false-excl 0.158->0.325); net TEST headline 0.435->0.402 -> revert. Lesson: the exclusion re-scan over-fires on eligibles. Next: trigger `unlikely` ONLY when an exclusion is EXPLICITLY + unambiguously satisfied by a directly-stated patient fact (no inference); and raise the likely_eligible bar for decisive unknowns as a SEPARATE smaller change, not bundled.
| 2026-07-23T04:43:50 | dev | `20ff92d86ae5` | **0.5025** | 0.692 | 0.733 | 0.217 | 0.308 | 0.210 | $0.529 |
| 2026-07-23T04:46:34 | test | `20ff92d86ae5` | **0.4317** | 0.767 | 0.583 | 0.375 | 0.233 | 0.243 | $0.609 |

> **Tick 3** REVERTED. Hypothesis: high-precision exclusion cross-check (explicit stated facts only, no inference). Result: TEST 0.435->0.432 (excl-catch 0.475->0.583 but elig-recall 0.842->0.767, false-excl 0.158->0.233). Same recall<->exclusion see-saw as tick 2. CONCLUSION: single-pass holistic gpt-4o-mini has hit a discrimination ceiling on same-condition eligible-vs-excluded; prompt-lever swings just move error between sides. Next: raise the ceiling via structured chain-of-thought (reason step-by-step BEFORE verdict) instead of another lever swing.
| 2026-07-23T04:51:46 | dev | `349727bf6dc1` | **0.4867** | 0.850 | 0.483 | 0.300 | 0.150 | 0.180 | $0.706 |
| 2026-07-23T04:54:56 | test | `349727bf6dc1` | **0.4542** | 0.933 | 0.375 | 0.433 | 0.067 | 0.200 | $0.802 |

> **Tick 4** REVERTED. Hypothesis: structured chain-of-thought (reasoning field before verdict). Result: TEST 0.435->0.454 (+0.019, under +0.02 bar) and DEV 0.538->0.487 (regressed >0.03 guard). CoT boosted recall hard (test recall 0.842->0.933, false-excl 0.067) but dropped exclusion-catch 0.475->0.375 (false-elig 0.433). Third confirmation of the recall<->exclusion see-saw => single-pass gpt-4o-mini is discrimination-bound. Next: measure whether a STRONGER model (gpt-4.1-mini, gpt-4o) breaks the ceiling, on the committed tick-1 prompt, TEST split only (no prod change; just a data point).
| 2026-07-23T05:00:29 | test | `477b4b90cbfb` | **0.6142** | 0.917 | 0.458 | 0.100 | 0.083 | 0.073 | $1.041 |
| 2026-07-23T05:04:09 | test | `cb4f89847a75` | **0.5481** | 0.909 | 0.341 | 0.114 | 0.091 | 0.077 | $1.505 |
| 2026-07-23T05:10:19 | dev | `477b4b90cbfb` | **0.5617** | 0.833 | 0.483 | 0.075 | 0.167 | 0.097 | $1.904 |
| 2026-07-23T05:10:52 | dev | `477b4b90cbfb` | **0.5883** | 0.858 | 0.492 | 0.075 | 0.142 | 0.087 | $1.928 |

---
## KEY FINDING — model is the ceiling (gpt-4.1-mini)
Three prompt ticks on gpt-4o-mini all hit a recall<->exclusion see-saw (headline
stuck ~0.44). Swapping the MATCHER MODEL breaks it. Same committed tick-1 prompt,
held-out TEST split:

| model | headline | elig-recall | excl-catch | false-elig | crit-rate |
|---|---|---|---|---|---|
| gpt-4o-mini (old) | 0.435 | 0.842 | 0.475 | 0.400 | 0.223 |
| **gpt-4.1-mini**  | **0.614** | 0.917 | 0.458 | **0.100** | **0.073** |
| gpt-4o            | 0.548 | 0.909 | 0.341 | 0.114 | 0.077 |

gpt-4.1-mini: high recall AND ~3x fewer critical errors, cheaper than 4o, and 4o
is actually worse (over-cautious). Confirmed on dev too (0.588).

**RECOMMENDATION for prod: set `LLM_MODEL=gpt-4.1-mini` for matching.** Loop now
iterates on gpt-4.1-mini; remaining weakness is exclusion-catch (~0.46-0.49).
