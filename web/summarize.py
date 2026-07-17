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


def _first_condition(trial):
    """Best-effort first condition label for patient-facing copy."""
    c = (trial or {}).get("conditions") or []
    if isinstance(c, str):
        parts = [x.strip() for x in re.split(r"[;|,/]", c) if x.strip()]
        return parts[0] if parts else ""
    if isinstance(c, list):
        for x in c:
            s = str(x or "").strip()
            if s:
                return s
    return ""


def _plainify(text):
    """Light deterministic jargon cleanup for patient-facing snippets."""
    t = tidy(text)
    if not t:
        return ""
    repl = [
        (r"\befficacy\b", "how well it works"),
        (r"\beffectiveness\b", "how well it works"),
        (r"\btolerability\b", "side effects"),
        (r"\binterventional\b", "treatment"),
        (r"\brandomi[sz]ed\b", "assigned by chance"),
        (r"\bplacebo-controlled\b", "compared with an inactive treatment"),
        (r"\bdouble-blind\b", "blinded"),
        (r"\bsingle-blind\b", "partly blinded"),
        (r"\bsubjects\b", "people"),
    ]
    for pat, rep in repl:
        t = re.sub(pat, rep, t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def patient_card_title(trial, limit=96):
    """Short, plain-language heading for patient result cards."""
    trial = trial or {}
    raw = tidy(trial.get("title") or "")
    if not raw:
        return "Recruiting clinical trial"
    if (trial.get("source") or "") == "site_posted" and len(raw) <= limit:
        return raw

    cond = _first_condition(trial)
    low = raw.lower()
    if cond:
        if any(k in low for k in ("comparing", "compare", "versus", " vs ", "switching")):
            base = f"Compares treatment options for {cond}"
        elif "prevention" in low:
            base = f"Prevention study for {cond}"
        elif "safety" in low or "efficacy" in low or "effectiveness" in low:
            base = f"Tests treatment safety and results for {cond}"
        else:
            base = f"New treatment option for {cond}"
        return base if len(base) <= limit else base[:limit].rsplit(" ", 1)[0] + "..."

    t = _plainify(raw)
    if len(t) <= limit:
        return t
    return t[:limit].rsplit(" ", 1)[0].rstrip(" .") + "..."


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
    txt = _plainify((trial or {}).get("briefSummary") or "")
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


def _crit_bullets(block):
    """Split a criteria block into clean bullet items, preserving the source
    line/bullet structure (tidy() alone would flatten it)."""
    block = html.unescape(block or "")
    parts = re.split(r"(?:\r?\n|^)\s*(?:[\*\-\u2022\u25aa\u25cf]|\d+[.)])\s+", block)
    items = [tidy(p) for p in parts]
    items = [s for s in items if len(s) >= 4]
    if len(items) <= 1:  # no bullet markers: fall back to line breaks
        items = [tidy(x) for x in re.split(r"\r?\n+", block)]
        items = [s for s in items if len(s) >= 4]
    return items


def _split_criteria(raw):
    """Return (inclusion_items, exclusion_items) from raw CT.gov criteria text."""
    raw = raw or ""
    m = re.search(r"exclusion\s+criteria\s*:?", raw, re.I)
    if m:
        inc_txt, exc_txt = raw[:m.start()], raw[m.end():]
    else:
        inc_txt, exc_txt = raw, ""
    inc_txt = re.sub(r"inclusion\s+criteria\s*:?", "", inc_txt, flags=re.I)
    return _crit_bullets(inc_txt), _crit_bullets(exc_txt)


def _short(text, limit=150):
    s = _plainify(text)
    if len(s) <= limit:
        return s
    return s[:limit].rsplit(" ", 1)[0].rstrip(" .,;:") + "..."


def _phase_plain(phase):
    """Map a CT.gov phase string to a short, honest plain-language label."""
    p = (phase or "").upper().replace(" ", "").replace("_", "")
    if not p or p == "NA":
        return ""
    if "PHASE4" in p:
        return "Phase 4: studies an already-approved treatment"
    if "PHASE3" in p:
        return "Phase 3: a large, late-stage study"
    if "PHASE2" in p:
        return "Phase 2: a mid-size study of how well it works"
    if "PHASE1" in p or "EARLYPHASE1" in p:
        return "Phase 1: an early, usually small safety study"
    return ""


def _age_sex_basics(trial):
    """Short 'who' line from age range, sex, and healthy-volunteer fields."""
    def yrs(v):
        mt_ = re.search(r"(\d+)", str(v or ""))
        return mt_.group(1) if mt_ else ""
    lo, hi = yrs(trial.get("minAge")), yrs(trial.get("maxAge"))
    if lo and hi:
        age = f"Ages {lo} to {hi}"
    elif lo:
        age = f"Ages {lo} and older"
    elif hi:
        age = f"Ages up to {hi}"
    else:
        age = "Adults"
    sex = (trial.get("sex") or "ALL").upper()
    if sex == "FEMALE":
        age += ", women only"
    elif sex == "MALE":
        age += ", men only"
    if str(trial.get("healthyVolunteers") or "").lower() in ("yes", "true", "y"):
        age += ". Healthy volunteers may be eligible."
    return age


def _design_line(trial):
    """Plain note on randomization, placebo, and blinding from the study text."""
    text = " ".join([
        trial.get("criteria") or "", trial.get("briefSummary") or "",
        trial.get("detailedDescription") or "", trial.get("title") or "",
    ]).lower()
    randomized = bool(re.search(r"randomi[sz]ed", text))
    placebo = "placebo" in text
    blinded = bool(re.search(r"double-?blind|single-?blind|\bblinded\b|masking", text))
    if not (randomized or placebo or blinded):
        return ""
    if placebo:
        note = ("You might receive a placebo (an inactive treatment) instead of "
                "the study drug")
        if randomized:
            note += ", decided by chance"
        note += "."
    elif randomized:
        note = "Which group you join is decided by chance."
    else:
        note = "This is a blinded study."
    if blinded and placebo:
        note += " You may not know which one you got."
    return note


def _duration_plain(trial):
    """Best-effort per-patient time commitment, only when the text states it
    clearly (avoids inventing a number). Returns '' when unsure."""
    text = tidy(f"{trial.get('briefSummary') or ''} "
                f"{trial.get('detailedDescription') or ''}")
    best = None
    pat = re.compile(
        r"(?:treatment period of|study (?:duration|period) of|over a period of|"
        r"for (?:up to|about|approximately)?|over|during|lasts?|last for)"
        r"\s+(\d{1,3})\s*(weeks?|months?|years?)", re.I)
    for m in pat.finditer(text):
        n, unit = int(m.group(1)), m.group(2).lower()
        if not unit.endswith("s") and n != 1:
            unit += "s"
        weeks = n * (52 if "year" in unit else 4 if "month" in unit else 1)
        if 1 <= weeks <= 520 and (best is None or weeks > best[0]):
            best = (weeks, f"about {n} {unit}")
    return best[1] if best else ""


def plain_terms(trial):
    """Compact, neutral 'in plain terms' facts for the trial detail page: who
    can join, what likely rules you out, study design (placebo/randomization),
    and a rough time commitment. Deterministic (no LLM, no network) and built
    only from public CT.gov fields, so it's compliance-safe (no sponsor claims)."""
    trial = trial or {}
    inc, exc = _split_criteria(trial.get("criteria") or "")
    who = [_short(x) for x in inc[:5]]
    rule_out = [_short(x) for x in exc[:6]]
    return {
        "phase": _phase_plain(trial.get("phase")),
        "design": _design_line(trial),
        "time": _duration_plain(trial),
        "who_basics": _age_sex_basics(trial),
        "who": who,
        "rule_out": rule_out,
        "inc_all": [_short(x, 220) for x in inc],
        "exc_all": [_short(x, 220) for x in exc],
        "has_more": len(inc) > len(who) or len(exc) > len(rule_out),
    }


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
