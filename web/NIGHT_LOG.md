# Overnight eligibility-matching log

Higher **headline** = better (balanced accuracy minus critical-error rate).

| time | prompt | headline | elig-recall | excl-catch | false-elig | false-excl | crit-rate | spent |
|---|---|---|---|---|---|---|---|---|
| 2026-07-23T04:12:47 | `9594f845616d` | **0.4442** | 0.575 | 0.733 | 0.100 | 0.425 | 0.210 | $0.135 |
| 2026-07-23T04:20:14 | `d30cc26d6812` | **0.5383** | 0.808 | 0.575 | 0.192 | 0.192 | 0.153 | $0.211 |

> **Tick 1** (`d30cc26`): hypothesis — matcher conflated UNKNOWN with FAIL (fabricated `not_met` from silence, chose `unlikely` on mere unknowns). Fix: sharpened rules 2-5 so `not_met` needs an explicit contradiction, silence=unknown, `unlikely` reserved for explicit contradictions. **ACCEPTED** (headline 0.4442→0.5383; elig-recall 0.575→0.808). Next: tighten irrelevant/excluded side (irr-overmatch 0.05→0.217, false-eligible 0.10→0.192) without re-breaking eligible-recall.
