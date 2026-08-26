"""Reply drafting: a canned seed draft when there is one and no instruction,
otherwise the template's keyword ladder, optionally polished by the model with
the workspace's tone, guardrails and playbook. Nothing here sends anything."""
import re

from copy_sanitize import sanitize_copy

from . import llm
from . import policy
from .. import seeds

_FORBIDDEN_INSTRUCTIONS = (
    ("tell them they qualify", "I cannot say whether someone qualifies; that is for a professional to decide."),
    ("tell them they are eligible", "I cannot say whether someone is eligible; that is for a professional to decide."),
    ("guarantee", "I cannot promise an outcome."),
    ("promise", "I cannot promise an outcome."),
    ("how much they will get", "I cannot estimate what a case is worth."),
    ("quote a price", "I cannot quote prices; the playbook says to offer a call instead."),
)


def _first_name(conv):
    name = ((conv.get("contact") or {}).get("name") or conv.get("contact_name") or "").strip()
    if not name or name.lower() in ("unknown", "unknown caller"):
        return "there"
    return name.split()[0]


def _pick(ladder, text):
    low = (text or "").lower()
    for keywords, copy_ in ladder:
        if any(k in low for k in keywords):
            return copy_
    return None


def _last_inbound(messages):
    for m in reversed(messages or []):
        if m.get("kind") == "inbound":
            return m.get("body") or ""
    return ""


_INTENT_WORDS = [
    ("booking", ("booking link", "book", "schedule link", "link to book", "calendar")),
    ("check_in", ("check in", "checking in", "follow up", "still interested", "nudge")),
    ("thanks", ("thank them", "say thanks", "thank you note")),
    ("decline", ("decline", "not a fit", "turn down", "can't take", "cannot take", "refer them out")),
    ("ask_missing", ("ask for details", "ask what happened", "ask for", "details we need", "ask them for")),
]


def intent_from_instruction(instruction):
    low = (instruction or "").lower()
    for intent, words in _INTENT_WORDS:
        if any(w in low for w in words):
            return intent
    return "reply"


def polish_system(spec):
    tone = (spec.get("tone") or {}).get("voice", "warm")
    agent = (spec.get("agent") or {}).get("name", "Omni")
    biz = (spec.get("business") or {}).get("name", "the business")
    lines = [
        f"You are {agent}, drafting a short reply for {biz} that a person will review, edit, and send.",
        "You are given a DRAFT that is already correct and safe. Rewrite it so it reads naturally in a "
        f"{tone} voice. Rules:",
        "1. Do not add any fact not already in the draft or the playbook. If the draft offers to find something out, keep it as an offer.",
    ]
    n = 2
    for g in spec.get("guardrails") or []:
        if g.get("enforce") in ("never_promise", "escalate"):
            lines.append(f"{n}. {g.get('text')}")
            n += 1
    lines.append(f"{n}. Keep it to 2 to 4 short sentences. No signature, no subject line.")
    lines.append(f"{n + 1}. Never use em dashes. Use commas, periods, or hyphens.")
    if spec.get("playbook"):
        lines.append("PLAYBOOK (house style and standing facts you may use verbatim):")
        lines.append(spec["playbook"])
    lines.append("Return only the rewritten message.")
    return "\n".join(lines)


def compose(spec, template, conv, messages, instruction="", intent=None):
    """Returns {text, intent, category, policy, blocked, source}."""
    template = template or seeds.get(spec.get("template_key"))
    ladders = template.get("ladders") or {}
    who = _first_name(conv)
    signoff = (spec.get("tone") or {}).get("signoff", "")
    last = _last_inbound(messages)
    category = policy.categorize(last, instruction, conv.get("f"))
    mode = policy.decide(spec, category)

    low = (instruction or "").lower()
    for phrase, reason in _FORBIDDEN_INSTRUCTIONS:
        if phrase in low:
            return {"text": "", "intent": "reply", "category": category, "policy": "review",
                    "blocked": reason, "source": "guardrail"}

    intent = intent or intent_from_instruction(instruction)
    text, source = None, "ladder"

    if intent == "reply" and not instruction and conv.get("seed_key"):
        seed = seeds.seed_conversation(spec.get("template_key"), conv["seed_key"])
        if seed and (seed.get("drafts") or {}).get("reply"):
            text, source = seed["drafts"]["reply"], "canned"

    if text is None:
        if intent in ("check_in", "booking", "thanks", "decline", "ask_missing"):
            text = ladders.get(intent) or ladders.get("fallback") or ""
        else:
            text = _pick(ladders.get("reply") or [], f"{last} {instruction}") \
                or ladders.get("fallback") or "Thanks for reaching out. What days and times work for a quick call?"
        text = text.replace("{who}", who).replace("{signoff}", signoff)
        if who == "there":
            text = re.sub(r"\bHi there,\s*", "Hi, ", text)
            text = text.replace(", there.", ".").replace(", there,", ",")
        if instruction and llm.available():
            user = (f"DRAFT:\n{text}\n\nTHEIR LAST MESSAGE:\n{last[:1200]}\n\n"
                    f"INSTRUCTION FROM THE PERSON SENDING IT:\n{instruction[:400]}\n\n"
                    "Rewrite the draft to follow the instruction without adding facts.")
            out = llm.chat_text(polish_system(spec), user)
            if out and 0 < len(out) <= 1200:
                text, source = out.strip(), "llm"
    text = sanitize_copy(text)
    return {"text": text, "intent": intent, "category": category, "policy": mode,
            "blocked": None, "source": source}
