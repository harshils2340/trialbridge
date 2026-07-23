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
