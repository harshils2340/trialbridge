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
import json

import match_trials as mt
import db

_SPEC_PATH = pathlib.Path(__file__).resolve().parent / "trial_summary.md"
try:
    _SPEC = _SPEC_PATH.read_text(encoding="utf-8")
except OSError:
    _SPEC = ""

KEYS = ["one_liner", "purpose", "who", "what", "commitment"]
DETAIL_SUMMARY_LLM = os.environ.get("DETAIL_SUMMARY_LLM", "0") == "1"
SUMMARY_EVAL_LLM = os.environ.get("SUMMARY_EVAL_LLM", "0") == "1"
_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "your",
    "their", "there", "about", "which", "when", "where", "what", "will",
    "have", "has", "been", "are", "were", "can", "may", "might", "than",
    "then", "also", "only", "some", "more", "most", "much", "many", "over",
    "under", "into", "onto", "while", "after", "before", "each", "per",
    "study", "trial", "participant", "participants", "patient", "patients",
}


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


def _summary_text(summary):
    return " ".join(str(summary.get(k, "") or "").strip() for k in KEYS).strip()


def _content_tokens(text):
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [t for t in toks if len(t) > 2 and t not in _STOP]


def _jargon_terms(text):
    out = []
    for m in re.finditer(r"\b([A-Z]{2,}|[A-Za-z]+-\d+[A-Za-z0-9-]*)\b", text or ""):
        out.append(m.group(1))
    return out


def _clarity_score(text):
    """Cheap readability proxy for internal QA (0-100)."""
    t = (text or "").strip()
    if not t:
        return {"score": 0, "flags": ["empty_summary"]}
    flags = []
    score = 100
    sents = [s for s in re.split(r"(?<=[.!?])\s+", t) if s.strip()]
    words = re.findall(r"\b[\w-]+\b", t)
    avg_len = (len(words) / max(1, len(sents)))
    if avg_len > 22:
        score -= 20
        flags.append("long_sentences")
    elif avg_len > 18:
        score -= 10
        flags.append("some_long_sentences")
    jargon = _jargon_terms(t)
    if jargon:
        score -= min(24, 4 * len(jargon))
        flags.append("contains_jargon")
    long_words = [w for w in words if len(w) >= 14]
    if len(long_words) >= 3:
        score -= 10
        flags.append("many_long_words")
    return {"score": max(0, min(100, score)),
            "flags": flags,
            "avg_words_per_sentence": round(avg_len, 1),
            "jargon_terms": jargon[:8]}


def _fidelity_score(summary_text, source_text):
    """Groundedness proxy: how much summary language is supported by source text."""
    summ = _content_tokens(summary_text)
    src = set(_content_tokens(source_text))
    if not summ:
        return {"score": 0, "flags": ["empty_summary"]}
    supported = sum(1 for t in summ if t in src)
    score = int(round(100 * supported / len(summ)))
    flags = []
    if score < 60:
        flags.append("low_grounding")
    elif score < 75:
        flags.append("medium_grounding")
    return {"score": max(0, min(100, score)), "flags": flags}


def _llm_eval(trial, summary_text, source_text):
    if not (SUMMARY_EVAL_LLM and mt.LLM_API_KEY and summary_text and source_text):
        return None
    prompt = (
        "You are evaluating a patient-facing clinical trial summary.\n"
        "Score two things from 0-100 and return strict JSON only:\n"
        "1) fidelity_score: factual alignment with the source text (no hallucinations)\n"
        "2) clarity_score: understandable to a layperson (minimal jargon)\n"
        "Also return short flags[] and one_sentence_feedback.\n\n"
        f"TRIAL TITLE: {trial.get('title','')}\n\n"
        f"SOURCE:\n{source_text[:7000]}\n\n"
        f"SUMMARY:\n{summary_text[:3000]}"
    )
    try:
        raw = mt.llm_chat(
            "Return JSON only with keys: fidelity_score, clarity_score, flags, one_sentence_feedback.",
            prompt,
        )
        obj = mt._extract_json(raw)
        return {
            "fidelity_score": int(max(0, min(100, float(obj.get("fidelity_score", 0))))),
            "clarity_score": int(max(0, min(100, float(obj.get("clarity_score", 0))))),
            "flags": [str(x) for x in (obj.get("flags") or [])][:6],
            "one_sentence_feedback": str(obj.get("one_sentence_feedback", "")).strip(),
        }
    except Exception:
        return None


def evaluate_summary_quality(trial, summary):
    """Internal QA: deterministic score + optional LLM judge."""
    trial = trial or {}
    summary = summary or {}
    source_text = tidy(
        f"{trial.get('briefSummary') or ''} {trial.get('detailedDescription') or ''}"
    )
    rendered = _summary_text(summary)
    fidelity = _fidelity_score(rendered, source_text)
    clarity = _clarity_score(rendered)
    out = {
        "deterministic": {
            "fidelity_score": fidelity["score"],
            "clarity_score": clarity["score"],
            "flags": fidelity.get("flags", []) + clarity.get("flags", []),
            "avg_words_per_sentence": clarity.get("avg_words_per_sentence", 0),
            "jargon_terms": clarity.get("jargon_terms", []),
        }
    }
    llm = _llm_eval(trial, rendered, source_text)
    if llm:
        out["llm_judge"] = llm
    return out


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
    data["_qa"] = evaluate_summary_quality(trial, data)
    if nct and data.get("_ai"):
        try:
            db.set_trial_summary(nct, data)
        except Exception:
            pass
    return data
