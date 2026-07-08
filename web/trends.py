"""Externally-sourced "trending" terms for the landing page.

We don't have our own traffic yet, so trending can't come from our own searches.
Instead we ask the LLM (GPT) what people are most actively looking to join
clinical trials for right now, then VALIDATE every suggestion against
ClinicalTrials.gov so we only ever show terms that actually return recruiting
trials (no hallucinated drugs, no dead-end chips).

The result is cached in SQLite (24h TTL) and refreshed in a background thread so
page loads stay instant. If the LLM isn't configured, callers fall back to their
seed lists.
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


def configure(app):
    """Give the module a Flask app so background refreshes can open a DB
    connection outside of a request."""
    global _app
    _app = app


def enabled():
    return bool(mt.LLM_API_KEY)


_SYSTEM = (
    "You are a clinical research analyst. You know which health conditions and "
    "which drugs/treatments the public is most actively searching to join "
    "clinical trials for right now (recent news, viral treatments, common "
    "chronic diseases). Answer only with real, current, high-interest topics.")

_USER = (
    "List what people are MOST looking to join clinical trials for right now.\n"
    "Return strict JSON: {\"conditions\": [...], \"drugs\": [...]}.\n"
    "- conditions: 8 health conditions people search trials for (plain consumer "
    "wording, e.g. \"Obesity\", \"Alzheimer's disease\", \"Type 2 diabetes\").\n"
    "- drugs: 8 specific drug or treatment names that are trending (e.g. "
    "\"Semaglutide\", \"Tirzepatide\", \"Donanemab\").\n"
    "Order each list most-trending first. No commentary, JSON only.")


def _generate():
    """Ask the LLM for candidate trending terms. Returns (conditions, drugs)."""
    raw = mt.llm_chat(_SYSTEM, _USER)
    data = mt._extract_json(raw)
    conds = [str(x).strip() for x in data.get("conditions", []) if str(x).strip()]
    drugs = [str(x).strip() for x in data.get("drugs", []) if str(x).strip()]
    return conds, drugs


def _has_recruiting(term, is_drug):
    """True if ClinicalTrials.gov has at least one recruiting trial for term."""
    try:
        if is_drug:
            res = mt.fetch_trials("", max_n=1, intervention=term)
        else:
            res = mt.fetch_trials(term, max_n=1)
        return bool(res)
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
        # burning a duplicate LLM call.
        with _app.app_context():
            _, c_at = db.get_trend_cache("condition")
            _, d_at = db.get_trend_cache("drug")
        if not (_is_stale(c_at) or _is_stale(d_at)):
            return
        conds, drugs = _generate()
        good_conds = _validate(conds, is_drug=False)
        good_drugs = _validate(drugs, is_drug=True)
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
    """Kick off a background refresh at most once at a time."""
    global _refreshing
    if not (enabled() and _app is not None):
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
