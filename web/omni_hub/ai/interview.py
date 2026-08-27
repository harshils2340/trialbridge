"""The interview: what the builder says, how it prefills from the first
prompt, and (with a key) how the model adds business-specific colour.

The scripted path is complete on its own. The model only ever fills in
defaults (business name, sources named in the prompt, extra fields) and
rewrites the agent's one-line "say"; the question bank and every decision
stay deterministic so the two paths converge on the same spec.
"""
import json
import re

from . import llm
from .. import spec as spec_mod

PREFILL_SYSTEM = """You read one sentence a business owner wrote about their business and where leads come from.
Return JSON: {"business_name": "" or the name if they gave one, "connectors": [kinds from this list that they
mentioned: %s], "extra_fields": [up to 3 {"label","type" (text|number|enum|bool|date|list),"hint"} for things
they clearly want tracked that a generic template would miss - facts ABOUT a lead, never the channels
themselves (no "ClinicalTrials.gov ID", "form submissions", "referring doctors" style fields)],
"summary": "one plain sentence about the business"}.
Never invent a name. Never include fields about race, religion, national origin, familial status, disability,
age or immigration status. Never use em dashes. Respond with ONLY the JSON object."""

SAY_SYSTEM = """You are the setup assistant for an intake inbox. Rewrite the line below so it sounds like a
helpful person who just read what the user wrote, in one or two short sentences, plain and warm, no
marketing language, no em dashes. Keep every fact. Return only the rewritten line."""


# Words that describe where leads COME FROM rather than facts about a lead.
# The model sometimes turns "leads come from ClinicalTrials.gov and our trial
# finder" into fields like "ClinicalTrials.gov ID" or "Trial Finder Form
# Submissions"; a field whose meaningful words are all channel words is an echo
# of the sources, not something to extract, and is dropped.
_SOURCE_WORDS = {
    "clinicaltrials", "gov", "clinicaltrialsgov", "trial", "finder", "form",
    "forms", "submission", "submissions", "facebook", "meta", "instagram",
    "ads", "ad", "lead", "leads", "referring", "referral", "referrals",
    "doctor", "doctors", "physician", "physicians", "email", "emails", "inbox",
    "sms", "text", "texts", "voicemail", "phone", "website", "web", "site",
    "id", "ids", "count", "counts", "number", "source", "sources", "channel",
    "channels", "listing", "listings", "page", "portal",
}


def _source_echo(label):
    toks = [t for t in re.split(r"[^a-z0-9]+", (label or "").lower()) if t]
    toks = [t for t in toks if t not in spec_mod._FIELD_FILLER]
    return bool(toks) and all(t in _SOURCE_WORDS for t in toks)


def connectors_in_prompt(prompt):
    """Connector kinds named in a free-text prompt (deterministic)."""
    low = (prompt or "").lower()
    found = []
    aliases = {
        "gmail": ["gmail", "google mail"], "outlook": ["outlook", "office 365", "microsoft"],
        "instagram": ["instagram", " ig ", "dms"], "facebook_leads": ["facebook", "meta", "lead ads"],
        "web_form": ["web form", "website form", "site form", "website", "web site", "our site", "my site", "contact form", "form on", "landing page"],
        "sms": ["text", "sms"], "voicemail": ["voicemail", "phone", "calls"],
        "zillow": ["zillow"], "avvo": ["avvo"], "zocdoc": ["zocdoc"],
        "psychology_today": ["psychology today"], "indeed": ["indeed"], "care_com": ["care.com"],
        "google_lsa": ["google local", "local services", "google calls", "google ads"],
        "fax": ["fax"], "email": ["email", "e-mail", "referring doctors", "referring offices"],
        "webhook": ["zapier", "webhook", "make.com", "ehr export"], "csv": ["csv", "spreadsheet", "import"],
        "referral": ["referral", "referrals", "referred"],
    }
    for kind, words in aliases.items():
        if any(re.search(r"(?<![a-z])" + re.escape(w.strip()) + r"(?![a-z])", low) for w in words):
            found.append(kind)
    if "gmail" in found and "email" in found:
        found.remove("email")
    return found


def business_name_in_prompt(prompt):
    """A name if the prompt states one plainly ("We are Harbor Point Law", "at Cedar Grove")."""
    t = (prompt or "").strip()
    for rx in (r"\b(?:we are|we're|this is|i run|i own|i manage|i work at|at)\s+([A-Z][\w&'.]*(?:\s+[A-Z][\w&'.]*){0,4})",
               r"^([A-Z][\w&'.]*(?:\s+[A-Z][\w&'.]*){1,4})\s+(?:is|here)\b"):
        m = re.search(rx, t)
        if m:
            name = m.group(1).strip(" .,")
            if 2 <= len(name.split()) <= 5 and not re.search(r"\b(Our|The|A|An|We|I|My)\b$", name):
                return name[:60]
    return ""


def prefill(prompt, template):
    """Defaults derived from the opening prompt: deterministic, then the model."""
    out = {"business_name": business_name_in_prompt(prompt),
           "connectors": connectors_in_prompt(prompt),
           "extra_fields": [], "summary": ""}
    if llm.available() and prompt:
        kinds = ", ".join(spec_mod.CONNECTOR_KINDS.keys())
        data = llm.chat_json(PREFILL_SYSTEM % kinds, f"SENTENCE: {prompt[:600]}\nRespond with JSON now.",
                             max_tokens=350)
        if data:
            if data.get("business_name") and not out["business_name"]:
                out["business_name"] = str(data["business_name"])[:60]
            for k in data.get("connectors") or []:
                if k in spec_mod.CONNECTOR_KINDS and k not in out["connectors"]:
                    out["connectors"].append(k)
            for f in (data.get("extra_fields") or [])[:3]:
                if isinstance(f, dict) and f.get("label"):
                    if _source_echo(str(f["label"])):
                        continue
                    out["extra_fields"].append({"label": str(f["label"])[:60],
                                                "type": str(f.get("type") or "text"),
                                                "hint": str(f.get("hint") or "")[:200]})
            if data.get("summary"):
                out["summary"] = str(data["summary"])[:300]
    return out


OPENER_NOUNS = {
    "clinical_trial_site": "a clinical research site", "pi_law_firm": "a personal injury law firm",
    "group_therapy": "a group therapy practice", "dental_medspa": "a dental office with a med spa",
    "specialty_referrals": "a specialty clinic", "home_health": "a home health agency",
    "nurse_staffing": "a nurse staffing agency", "bootcamp_admissions": "a coding bootcamp",
    "legal_aid": "a legal aid nonprofit", "property_leasing": "a property management company",
}


def opener(template, prefill_data, prompt):
    """First thing the assistant says after reading the prompt."""
    noun = OPENER_NOUNS.get(template.get("key"), "")
    if template.get("key") == "generic" or not noun:
        line = "Got it. A few quick questions so I set the inbox up the way you work."
    else:
        line = f"Got it, {noun}. A few quick questions so I set the inbox up the way you work."
    conns = prefill_data.get("connectors") or []
    if conns:
        labels = [spec_mod.CONNECTOR_KINDS[k]["label"] for k in conns if k in spec_mod.CONNECTOR_KINDS]
        if labels:
            line += " I saw " + _join(labels) + " in what you wrote, so those are already ticked."
    return polish(line)


def polish(line):
    if not llm.available():
        return line
    out = llm.chat_text(SAY_SYSTEM, line)
    return out.strip() if out and 0 < len(out) < 400 else line


def _join(items):
    items = [str(i) for i in items]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def summary_lines(draft, template):
    """The confirm-the-plan summary, 3 to 5 plain lines."""
    conns = [c for c in draft.get("connectors") or [] if c.get("auto_connect", True)]
    labels = [c.get("label") or spec_mod.CONNECTOR_KINDS.get(c["kind"], {}).get("label", c["kind"]) for c in conns]
    fields = [f["label"] for f in draft.get("fields") or []]
    views = [v["name"] for v in draft.get("views") or [] if v.get("pinned")]
    pol = draft.get("approval_policy") or {}
    auto = [c for c in ("routine", "pricing", "medical_legal") if pol.get(c) == "auto_send"]
    words = {"routine": "routine replies", "pricing": "pricing questions", "medical_legal": "questions that need a professional"}
    agent = (draft.get("agent") or {}).get("name") or "Omni"
    lines = [
        f"Connect: {_join(labels) if labels else 'nothing yet'}",
        f"Pull out of every message: {_join(fields[:8]).lower() if fields else 'nothing yet'}",
        f"Views on the front: {_join(views) if views else 'All open and Needs reply'}",
        (f"{agent} sends {_join(words[c] for c in auto)} on its own; everything else waits for you"
         if auto else f"{agent} drafts everything and you press Send"),
    ]
    rules = draft.get("rules") or []
    if rules:
        lines.append(f"Rules: {len(rules)} to start, on the Rules tab")
    return lines
