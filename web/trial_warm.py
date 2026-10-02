"""Pre-generate trial summaries and pre-screen questions for trials patients
are about to see, so the first real page view finds them already written
instead of waiting on (and competing with every other visitor for) the LLM
provider's per-minute rate limit.

Source of "about to see": recent search_cache rows. A patient's search
returns a page of trial cards, and whichever of those don't have a summary or
a pre-screen yet are exactly the ones that would otherwise all fire their
first LLM call at once when the results render - several first-time
generations landing in the same couple of minutes is what was driving the
burst of 429s on the Groq dashboard on 1 October. Warming them here, off the
request path and paced well under the rate limit, spreads that same work out
over time instead of bursting it, and does it before any patient is actually
looking at the page.

Entirely additive: db.py's trial_summaries/trial_prescreens tables and every
existing cache check and fallback in app.py and summarize.py are unchanged -
this only tends to find a trial already warm by the time someone requests it.
Triggered the same way as alerts/reminders/SEO warm (see web/app.py and
render.yaml): an external scheduler hits a keyed endpoint on a schedule, no
in-process background thread.
"""
import time

import db
import summarize
import match_trials as mt

# Seconds between model calls while warming. Groq's per-minute request limit
# is what the 1 October burst hit; at this pace a batch of WARM_TRIALS_LIMIT
# stays comfortably under it even run back to back with the live traffic that
# prompted the search in the first place.
PACE_SECONDS = 3.0


def _candidates(searches_limit, trials_limit):
    """Distinct (nct -> trial dict) from the most recent cached searches,
    newest first, that are missing a summary or a pre-screen (or both)."""
    seen = {}
    for payload in db.recent_search_payloads(searches_limit):
        for r in (payload.get("results") or []):
            trial = r.get("trial") or {}
            nct = trial.get("nctId")
            if not nct or nct in seen:
                continue
            has_summary = db.get_trial_summary(nct) is not None
            has_prescreen = db.get_trial_prescreen(nct) is not None
            if has_summary and has_prescreen:
                continue
            seen[nct] = trial
            if len(seen) >= trials_limit:
                return seen
    return seen


def warm_recent(searches_limit=40, trials_limit=20):
    """Warm up to `trials_limit` trials found in the last `searches_limit`
    cached searches. Never raises - a single trial's failure just moves on to
    the next one, and the whole batch stops early (without erroring) the
    moment the provider is cooling us down, same as the live request path.
    Returns a small dict for the endpoint's response / a cron's logs."""
    warmed_summary = warmed_prescreen = 0
    cooling_down = False
    candidates = _candidates(searches_limit, trials_limit)
    for nct, trial in candidates.items():
        if not mt.llm_available():
            cooling_down = True
            break
        touched = False
        try:
            if db.get_trial_summary(nct) is None:
                summarize.plain(trial)  # caches itself, same as the live path
                warmed_summary += 1
                touched = True
        except mt.LLMCoolingDown:
            cooling_down = True
            break
        except Exception:
            pass
        try:
            if db.get_trial_prescreen(nct) is None:
                questions = mt.prescreen_questions(trial)
                if questions:
                    db.set_trial_prescreen(nct, questions)
                    warmed_prescreen += 1
                    touched = True
        except mt.LLMCoolingDown:
            cooling_down = True
            break
        except Exception:
            pass
        if touched:
            time.sleep(PACE_SECONDS)
    return {
        "candidates": len(candidates),
        "warmed_summary": warmed_summary,
        "warmed_prescreen": warmed_prescreen,
        "cooling_down": cooling_down,
    }
