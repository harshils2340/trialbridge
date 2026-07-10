"""Turn raw ClinicalTrials.gov descriptions into short, plain-English summaries.

Two levels, both following the structure in trial_summary.md:
  - card_blurb(trial): a short, clean teaser for result cards. Deterministic
    (no LLM, no network) so it's instant for a whole list of results.
  - plain(trial): a structured plain-English summary for the trial detail page.
    Uses the LLM when a key is configured, and caches the result per trial so
    each study is only summarized once.
"""
import html
import os
import pathlib
import re

import match_trials as mt
import db

_SPEC_PATH = pathlib.Path(__file__).resolve().parent / "trial_summary.md"
try:
    _SPEC = _SPEC_PATH.read_text(encoding="utf-8")
except OSError:
    _SPEC = ""

KEYS = ["one_liner", "purpose", "who", "what", "commitment"]
DETAIL_SUMMARY_LLM = os.environ.get("DETAIL_SUMMARY_LLM", "0") == "1"


def tidy(text):
    """Clean raw source text: unescape, strip HTML, drop artifacts, collapse space."""
    if not text:
        return ""
    t = html.unescape(text)
    t = re.sub(r"<[^>]+>", " ", t)                       # strip HTML tags
    t = (t.replace("\\[", "[").replace("\\]", "]")
          .replace("\\(", "(").replace("\\)", ")"))      # unescape punctuation
    t = re.sub(r"\[\s*s\s*\]", "(s)", t)                 # "question\[s\]" -> "question(s)"
    t = re.sub(r"[*•·]+", " ", t)                        # bullet artifacts
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def card_blurb(trial, limit=170):
    """Short, clean teaser for a result card (about two lines). No LLM."""
    txt = tidy((trial or {}).get("briefSummary") or "")
    if not txt:
        return ""
    if len(txt) <= limit:
        return txt
    out = ""
    for s in _sentences(txt):
        cand = (out + " " + s).strip()
        if out and len(cand) > limit:
            break
        out = cand
        if len(out) >= limit:
            break
    if not out or len(out) > limit:
        out = txt[:limit].rsplit(" ", 1)[0]
    return out.rstrip(" .") + "…"


def _fallback(trial):
    """Deterministic structure when the LLM is unavailable: still short + clean,
    just less polished than a model rewrite."""
    sents = _sentences(tidy(trial.get("briefSummary") or ""))
    return {
        "one_liner": card_blurb(trial, 140),
        "purpose": " ".join(sents[:2]),
        "who": "",
        "what": "",
        "commitment": "",
        "_ai": False,
    }


def _system():
    return ("You rewrite clinical-trial descriptions into short, plain English "
            "for patients. Follow this specification exactly and reply with ONLY "
            "a JSON object using the keys one_liner, purpose, who, what, "
            "commitment.\n\n" + _SPEC)


def plain(trial):
    """Structured plain-English summary for the detail page (LLM + cache)."""
    trial = trial or {}
    nct = trial.get("nctId") or ""
    if nct:
        cached = db.get_trial_summary(nct)
        if cached:
            return cached

    data = _fallback(trial)
    source = tidy(trial.get("briefSummary") or "")
    if DETAIL_SUMMARY_LLM and mt.LLM_API_KEY and source:
        try:
            user = (f"TITLE: {trial.get('title', '')}\n"
                    f"PHASE: {trial.get('phase') or 'NA'}\n"
                    f"STUDY TYPE: {trial.get('studyType') or ''}\n"
                    f"CONDITIONS: {trial.get('conditions') or ''}\n"
                    f"SOURCE DESCRIPTION:\n{source}")
            parsed = mt._extract_json(mt.llm_chat(_system(), user))
            out = {k: str(parsed.get(k, "") or "").strip() for k in KEYS}
            if out["one_liner"] or out["purpose"]:
                out["_ai"] = True
                data = out
        except Exception:
            pass  # fall back to the deterministic version

    # Only cache polished (AI) results, so a fallback can upgrade later once a
    # key is configured.
    if nct and data.get("_ai"):
        try:
            db.set_trial_summary(nct, data)
        except Exception:
            pass
    return data
