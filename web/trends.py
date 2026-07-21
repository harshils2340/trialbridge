"""Externally-sourced "trending" terms for the landing page.

Two independent, real data sources feed this (no vanity/hardcoded ordering):

1. ClinicalTrials.gov recruiting VOLUME. For drugs especially, we rank a curated
   pool of viral metabolic/weight peptides (GLP-1s + next-gen candidates) by how
   many trials are actively RECRUITING for each right now. That's a live external
   signal of where real research momentum is - it needs no LLM and never
   dead-ends (every ranked term is guaranteed to have recruiting trials).
2. The LLM (GPT), when configured, proposes what the public is most actively
   searching to join trials for; its picks are merged into the pool above and
   then VALIDATED against CT.gov so nothing hallucinated ever shows.

Results are cached in SQLite (24h TTL) and refreshed in a background thread so
page loads stay instant. If neither source is available, callers fall back to
their seed lists.
"""
import datetime as dt
import threading

import match_trials as mt
import db

TTL_SECONDS = 24 * 3600
_MAX = 8              # terms to keep per kind
_lock = threading.Lock()
_refreshing = False
_app = None
_drug_seeds = []
_condition_seeds = []


def configure(app):
    """Give the module a Flask app so background refreshes can open a DB
    connection outside of a request."""
    global _app
    _app = app


def set_seeds(drug_seeds=None, condition_seeds=None):
    """Provide the curated candidate pools (defined in app.py). These are the
    baseline terms we rank by live CT.gov volume, and the backfill the LLM's
    picks are merged with."""
    global _drug_seeds, _condition_seeds
    if drug_seeds is not None:
        _drug_seeds = list(drug_seeds)
    if condition_seeds is not None:
        _condition_seeds = list(condition_seeds)


def enabled():
    return bool(mt.LLM_API_KEY)


_SYSTEM = (
    "You are a clinical research analyst focused on metabolic & weight-loss "
    "treatments. You know which drugs/treatments and conditions the public is "
    "most actively searching to join clinical trials for right now - especially "
    "viral, news- and social-media-driven ones (GLP-1s and next-generation "
    "weight-loss peptides). Answer only with real, current, high-interest topics.")

_USER = (
    "List what people are MOST looking to join clinical trials for right now.\n"
    "Return strict JSON: {\"conditions\": [...], \"drugs\": [...]}.\n"
    "- conditions: 8 health conditions people search trials for (plain consumer "
    "wording, e.g. \"Obesity\", \"Type 2 diabetes\", \"Fatty liver disease\").\n"
    "- drugs: 8 specific, currently-trending drug/peptide names (generic names, "
    "e.g. \"Semaglutide\", \"Tirzepatide\", \"Retatrutide\", \"Orforglipron\", "
    "\"Mazdutide\"). Favor viral GLP-1 / next-gen weight-loss peptides.\n"
    "Order each list most-trending first. No commentary, JSON only.")


def _generate():
    """Ask the LLM for candidate trending terms. Returns (conditions, drugs)."""
    raw = mt.llm_chat(_SYSTEM, _USER)
    data = mt._extract_json(raw)
    conds = [str(x).strip() for x in data.get("conditions", []) if str(x).strip()]
    drugs = [str(x).strip() for x in data.get("drugs", []) if str(x).strip()]
    return conds, drugs


def _dedup(terms):
    out, seen = [], set()
    for t in terms:
        k = " ".join((t or "").lower().split())
        if k and k not in seen:
            seen.add(k)
            out.append(t)
    return out


def _has_recruiting(term, is_drug):
    """True if ClinicalTrials.gov has at least one recruiting trial for term."""
    try:
        if is_drug:
            return mt.count_trials(intervention=term) > 0
        return mt.count_trials(condition=term) > 0
    except Exception:
        return False


def _validate(terms, is_drug):
    out = []
    for t in terms:
        if _has_recruiting(t, is_drug):
            out.append(t)
        if len(out) >= _MAX:
            break
    return out


def _do_refresh():
    global _refreshing
    try:
        # Another worker/thread may have refreshed already - skip to avoid
        # burning duplicate LLM/API calls.
        with _app.app_context():
            _, c_at = db.get_trend_cache("condition")
            _, d_at = db.get_trend_cache("drug")
        if not (_is_stale(c_at) or _is_stale(d_at)):
            return

        llm_conds, llm_drugs = [], []
        if enabled():
            try:
                llm_conds, llm_drugs = _generate()
            except Exception:
                if _app is not None:
                    _app.logger.exception("trend LLM generate failed")

        # Drugs: keep the viral-first ordering (LLM's fresh picks, then the
        # curated next-gen peptide seeds) but use CT.gov recruiting counts as the
        # truth filter so we only ever surface peptides with active trials -
        # legit "different data source", no dead-end chips, no legacy drugs
        # crowding out the buzzy ones by raw volume.
        good_drugs = _validate(_dedup(llm_drugs + _drug_seeds), is_drug=True)
        # Conditions: keep the LLM's ordering but validate; skip if no LLM
        # (callers just fall back to condition seeds).
        good_conds = _validate(llm_conds, is_drug=False) if llm_conds else []

        with _app.app_context():
            if good_conds:
                db.set_trend_cache("condition", good_conds)
            if good_drugs:
                db.set_trend_cache("drug", good_drugs)
    except Exception:
        if _app is not None:
            _app.logger.exception("trend refresh failed")
    finally:
        with _lock:
            _refreshing = False


def _is_stale(updated_at):
    if not updated_at:
        return True
    try:
        ts = dt.datetime.strptime(updated_at, "%Y-%m-%d %H:%M")
    except ValueError:
        return True
    return (dt.datetime.now() - ts).total_seconds() > TTL_SECONDS


def _maybe_refresh():
    """Kick off a background refresh at most once at a time. Runs even without an
    LLM key, since the CT.gov volume ranking is a valid source on its own."""
    global _refreshing
    if _app is None:
        return
    with _lock:
        if _refreshing:
            return
        _refreshing = True
    threading.Thread(target=_do_refresh, daemon=True).start()


def get_trending(kind):
    """Return cached trending terms for kind ('condition'|'drug'), refreshing in
    the background when stale. Returns [] when nothing is cached yet (callers
    fall back to seeds)."""
    try:
        terms, updated_at = db.get_trend_cache(kind)
    except Exception:
        terms, updated_at = [], None
    if _is_stale(updated_at):
        _maybe_refresh()
    return terms
