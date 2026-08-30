"""Guardrails for Bridget's inbox drafts, enforced in code.

Two checks, both deterministic and always on, whether or not an LLM key is
set. The prompts in ``drafts.py`` ask the model to behave; this module makes
sure the output did, and looks at the inbound message before anything is
written at all.

  1. ``categorize(inbound, instruction)`` triages the conversation:

       distress       self-harm, a medical emergency, danger. Nothing is drafted;
                      a person handles it (and the rail says so).
       opt_out        "stop contacting me". Nothing is drafted; the request is
                      honored, not answered with more marketing.
       medical_legal  eligibility, placebo, doses, symptoms, lawyers. Drafted,
                      but flagged for review before sending.
       pricing        payment, reimbursement, cost, insurance. Drafted, flagged.
       routine        everything else.

  2. ``check_draft(text, instruction)`` scans a finished draft, wherever it came
     from, for the claims COMPLIANCE.md says Bridget must never make: dollar
     amounts, placebo odds, eligibility or enrollment promises, medical
     directives, safety assurances, and concrete visit counts or study length.
     A fact the coordinator typed into the instruction is theirs to state and
     is allowed through (they read and send the result); an eligibility
     promise or a medical directive is never allowed, whoever asked for it.

Nothing here sends anything. A draft that fails is not written; the reason is
returned so the coordinator sees why.
"""
import re

CATEGORIES = ("routine", "pricing", "medical_legal", "distress", "opt_out")

_DISTRESS = (
    "suicid", "kill myself", "hurt myself", "harm myself", "self harm",
    "self-harm", "end it all", "end my life", "don't want to be here",
    "dont want to be here", "don't want to live", "dont want to live",
    "not worth living", "overdose", "took too many", "emergency", "911",
    "chest pain", "can't breathe", "cant breathe", "bleeding", "passed out",
    "unresponsive", "allergic reaction", "in danger", "not safe at home",
)
_OPT_OUT = (
    "stop contacting", "stop texting", "stop emailing", "stop messaging",
    "unsubscribe", "remove me", "take me off", "do not contact", "don't contact",
    "dont contact", "leave me alone", "not interested anymore",
    "no longer interested",
)
_MEDICAL_LEGAL = (
    "qualify", "eligib", "placebo", "side effect", "dose", "dosage",
    "stop taking", "should i take", "should i stop", "my medication",
    "my meds", "symptom", "pregnan", "breastfeed", "is it safe", "safe?",
    "risk", "diagnos", "lawsuit", "sue ", "lawyer", "attorney", "my rights",
    "will it cure", "does it work", "guarantee",
)
_PRICING = (
    "paid", "pay ", "pay?", "payment", "compensat", "stipend", "reimburs",
    "how much", "money", "cost", "insurance", "copay", "gas money", "mileage",
    "free?", "is it free", "do i have to pay",
)


def _hit(text, needles):
    return any(n in text for n in needles)


def categorize(inbound="", instruction=""):
    """Return the category for a conversation. The inbound message decides;
    the coordinator's instruction can raise it (an instruction about payment
    is a pricing conversation) but never lower it."""
    text = f" {(inbound or '').lower()} {(instruction or '').lower()} "
    text = re.sub(r"\s+", " ", text)
    if _hit(text, _DISTRESS):
        return "distress"
    if _hit(text, _OPT_OUT):
        return "opt_out"
    if _hit(text, _MEDICAL_LEGAL):
        return "medical_legal"
    if _hit(text, _PRICING):
        return "pricing"
    return "routine"


# What each category means for the coordinator, in plain words. The rail shows
# these; the trace records them.
HOLD_REASONS = {
    "distress": ("Their message mentions self-harm or an emergency. Nothing "
                 "written. Please handle this one personally and follow your "
                 "site's escalation plan."),
    "opt_out": ("They asked not to be contacted. Nothing written. Honor the "
                "request and resolve the conversation."),
}
FLAG_REASONS = {
    "medical_legal": ("They asked about eligibility, safety or their "
                      "medication. The draft offers a call and decides "
                      "nothing. Review before sending."),
    "pricing": ("They asked about payment or costs. The draft offers to "
                "confirm and states no amount. Review before sending."),
}


# --- Draft scanner ----------------------------------------------------------- #
# (key, pattern, allowed_when_instructed). "Allowed when instructed" means the
# matched text may stand if the coordinator's instruction contains the same
# fact; the coordinator is the source and reviews the result.
_RULES = [
    ("amount", re.compile(
        r"\$\s?\d[\d,]*(?:\.\d+)?|\b\d[\d,]*\s?(?:dollars|usd|bucks)\b",
        re.I), True),
    ("odds", re.compile(
        r"\b\d{1,3}\s?(?:%|percent\b)|\b(?:one|two|three|1|2|3)\s+in\s+"
        r"(?:two|three|four|five|2|3|4|5)\b|\b50/50\b|\b(?:half|most) of "
        r"(?:participants|people) (?:get|receive)\b", re.I), False),
    ("eligibility", re.compile(
        r"\byou(?:'re| are| will be| would be| have been)\s+"
        r"(?:eligible|ineligible|qualified|enrolled|accepted|approved|excluded|"
        r"a (?:good|great|perfect) fit|in the study)\b|"
        r"\byou\s+(?:do(?:n't| not)?\s+)?(?:qualify|meet the criteria)\b|"
        r"\bguarantee[ds]?\b|\byou(?:'ll| will) (?:get in|be in the study|"
        r"receive the (?:drug|medication|treatment))\b", re.I), False),
    ("medical", re.compile(
        r"\b(?:stop|start|increase|decrease|double|skip|taper|pause|halve)\s+"
        r"(?:taking\s+)?(?:your|the|any)\s+(?:medication|medications|meds|"
        r"dose|pill|pills|antidepressant|treatment|prescription)\b|"
        r"\b\d+\s?(?:mg|mcg|ml)\b|\bside effects (?:are|include|were)\b|"
        r"\b(?:it is|it's|this is|the drug is|the medication is)\s+"
        r"(?:completely |totally |perfectly )?safe\b|\bno (?:side effects|"
        r"risks?)\b|\byou (?:should|need to|must) (?:take|stop|switch)\b",
        re.I), False),
    ("schedule", re.compile(
        r"\b\d+\s+(?:clinic |in-person |study )?visits?\b|"
        r"\b(?:lasts?|takes?|runs? for|is)\s+(?:about |around |roughly )?"
        r"\d+\s+(?:weeks?|months?|years?)\b", re.I), True),
]

REASON_TEXT = {
    "amount": "states a payment or reimbursement amount",
    "odds": "states placebo odds or a percentage",
    "eligibility": "tells them whether they qualify or are enrolled",
    "medical": "gives medical direction or a safety assurance",
    "schedule": "states a visit count or study length",
}


def _norm(s):
    return re.sub(r"[\s,]+", "", (s or "").lower())


def check_draft(text, instruction=""):
    """Return the list of rule keys a draft breaks (empty means clean).

    Matches the coordinator's own instruction supplied are allowed for the
    rules marked so; eligibility promises and medical directives never are."""
    text = text or ""
    ask = _norm(instruction)
    broken = []
    for key, rx, allowed_if_instructed in _RULES:
        for m in rx.finditer(text):
            if allowed_if_instructed and ask and _norm(m.group(0)) in ask:
                continue
            broken.append(key)
            break
    return broken


def describe(broken):
    """One plain sentence for the coordinator about why a draft was refused."""
    parts = [REASON_TEXT.get(k, k) for k in broken]
    if not parts:
        return ""
    if len(parts) == 1:
        return f"It {parts[0]}."
    return "It " + ", ".join(parts[:-1]) + " and " + parts[-1] + "."
