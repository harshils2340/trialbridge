"""Approval policy: which category a message falls in, and whether the spec
lets the agent send that category on its own. Distress always waits for a
person, whatever the spec says."""
import re

CATEGORIES = ("routine", "pricing", "medical_legal", "distress")

_DISTRESS = ("suicid", "kill myself", "hurt myself", "end it all", "don't want to be here",
             "dont want to be here", "not safe", "self harm", "self-harm", "overdose",
             "emergency", "911", "bleeding", "can't breathe", "cant breathe", "chest pain",
             "no heat", "gas smell", "flooding", "fell today", "fall today", "unresponsive",
             "stop texting", "unsubscribe", "remove me", "stop contacting")
_PRICING = ("how much", "price", "pricing", "cost", "fee", "fees", "charge", "rate",
            "rates", "per session", "per unit", "insurance", "in network", "out of network",
            "copay", "sliding scale", "deposit", "rent", "monthly", "payment plan",
            "stipend", "reimburse", "compensation", "worth", "settlement", "financing",
            "tuition", "salary", "pay rate")
_MEDICAL_LEGAL = ("do i qualify", "am i eligible", "eligible", "qualify", "diagnos",
                  "dosage", "side effect", "placebo", "should i sue", "my rights",
                  "is it too late", "statute", "legal advice", "do i have a case",
                  "is it covered", "will i be approved", "guarantee", "sponsor my visa",
                  "immigration", "prescri", "symptom", "medication", "cancer",
                  "report says", "results mean")


def categorize(inbound_text="", instruction="", extracted=None):
    text = f"{inbound_text or ''} {instruction or ''}".lower()
    urgency = None
    if extracted:
        rec = extracted.get("urgency") or {}
        urgency = rec.get("value") if isinstance(rec, dict) else rec
    if any(k in text for k in _DISTRESS) or str(urgency).lower() in ("high", "urgent"):
        return "distress"
    if any(re.search(r"\b" + re.escape(k) + r"\b", text) for k in _PRICING):
        return "pricing"
    if any(k in text for k in _MEDICAL_LEGAL):
        return "medical_legal"
    return "routine"


def decide(spec, category):
    """'auto_send' or 'review'."""
    if category == "distress":
        return "review"
    pol = (spec or {}).get("approval_policy") or {}
    return "auto_send" if pol.get(category) == "auto_send" else "review"


def category_words(category):
    return {"routine": "a routine reply", "pricing": "a pricing question",
            "medical_legal": "a question that needs a professional",
            "distress": "something urgent or sensitive"}.get(category, category)
