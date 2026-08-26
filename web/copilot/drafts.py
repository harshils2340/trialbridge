"""One drafting service for every "Bridget writes something a human will send".

Before this module the same idea lived in three places: a keyword ladder in
``app.py`` for the marketing inbox, a second ladder in ``copilot/tools.py`` for
applicant threads, and the proposal copy in ``copilot/actions.py``. They drifted
in tone and in what they were willing to claim. Everything now routes here.

Two layers, in this order:

  1. a DETERMINISTIC ladder per intent - reads the actual inbound message and
     answers what was asked. This is the product when ``LLM_API_KEY`` is unset
     (demo, local dev, and any outage), so it has to be good on its own;
  2. an optional LLM pass that rewrites the deterministic draft in a warmer
     voice, under a system prompt that forbids adding any fact the ladder did
     not already state.

The compliance rule the ladders encode (see COMPLIANCE.md): a draft must never
assert a study-specific fact a coordinator has to confirm - reimbursement
amounts, placebo odds, visit counts, eligibility outcomes. It offers to find out
instead. A rushed "send as-is" must not be able to state an unapproved claim.
Bridget drafts; a person edits and sends. Nothing here sends anything.
"""

import match_trials as mt

from copy_sanitize import sanitize_copy

INTENTS = ("reply", "applicant_reply", "check_in", "booking", "reschedule",
           "thanks", "blast", "note_summary")

# Rewrites the deterministic draft, never replaces its content. The explicit
# "add no new facts" rule is what keeps the LLM path as compliant as the ladder.
SYSTEM_PROMPT = (
    "You are Bridget, drafting a short message for a clinical-trial study "
    "coordinator to review, edit, and send to a study participant or applicant. "
    "You will be given a DRAFT that is already correct and compliant, plus the "
    "context it was written from. Rewrite the draft so it reads naturally and "
    "warmly. Rules you must follow:\n"
    "1. Do NOT add any fact that is not already in the draft. Never state "
    "payment or reimbursement amounts, placebo odds, side effects, visit counts, "
    "study length, or whether someone is eligible - if the draft offers to find "
    "something out, keep it as an offer.\n"
    "2. Never give medical advice and never tell anyone they do or do not "
    "qualify.\n"
    "3. Keep it to 2-3 short sentences. No greeting line if the draft has one "
    "already, no signature, no subject line.\n"
    "4. Plain warm English. No marketing language, no pressure, no urgency.\n"
    "5. Never use em dashes (the long dash character). Use commas, periods, "
    "or hyphens instead.\n"
    "Return only the rewritten message."
)

# Appended only when the coordinator typed an instruction. Their instruction is
# the point of the message, so it has to be able to introduce content the
# deterministic draft did not have - they wrote it, and they read and send the
# result. What it cannot do is unlock the safety rules or invite the model to
# supply facts of its own.
ASK_RULES = (
    "\n5. The COORDINATOR'S INSTRUCTION below is what they want this message to "
    "say. Follow it and rewrite the draft to carry it out. A fact the "
    "instruction states explicitly may appear in the message - the coordinator "
    "is the source of it and reviews the result before sending. Never add a "
    "fact of your own on top, and rules 2 and 4 hold no matter what the "
    "instruction asks."
)


# --------------------------------------------------------------------------- #
# Deterministic ladders
# --------------------------------------------------------------------------- #
# Marketing-inbox ladder: a first-contact inquiry, usually from an ad or a
# ClinicalTrials.gov listing. Ordered most-specific -> least.
_REPLY_LADDER = [
    (("evening", "weekend", "after work", "what time", "times", "schedule",
      "appointment", "availability", "available", "when can"),
     "Happy to work around your schedule for a brief screening call. "
     "What days and times generally work best for you?"),
    (("travel", "mileage", "parking", "reimburse", "compensat", " paid",
      "payment", "stipend", "cost", "expense", "gas"),
     "Good question on visit costs. Let me confirm exactly what this study "
     "covers and I'll follow up with the specifics."),
    (("qualify", "eligible", "eligibility", "criteria", "requirement",
      "do i fit", "am i able", "right fit"),
     "To see whether this study is a fit, we do a short pre-screening. Could we "
     "set up a quick call to walk through a few questions?"),
    (("safe", "placebo", "side effect", "risk", "danger", "guinea pig"),
     "Those are important questions. The study team can walk you through "
     "safety, what to expect, and how the study is designed on a short call. "
     "Would that be helpful?"),
    (("how long", "how many visit", "duration", "time commitment", "how often",
      "last for", "how many weeks", "how many months"),
     "Good question on the time commitment. Let me confirm the visit schedule "
     "and overall length for this study and I'll lay it out for you."),
    (("where is", "where are", "location", "how far", "address", "near me",
      "drive", "remote", "virtual", "online", "from home", "telehealth",
      "in person"),
     "Good question on location and visits. Let me confirm where this study "
     "runs and whether any parts can be done remotely, and I'll follow up with "
     "the details."),
    (("privacy", "private", "confidential", "who sees", "who will see", "spam",
      "share my", "sell my", "my data", "my information", "my info",
      "personal information"),
     "Your privacy matters here. Your information is only used to see if this "
     "study could be a fit and to connect you with the study team, and it's "
     "never sold. Happy to answer any specific concern."),
    # Voluntariness and the right to withdraw are universal informed-consent
    # principles (Common Rule / GCP), not a study-specific claim, so this is
    # safe to state directly rather than deferring.
    (("withdraw", "drop out", "opt out", "change my mind", "leave the study",
      "back out", "pull out"),
     "Taking part is completely voluntary and you can stop at any time, for any "
     "reason. I'm happy to walk through what that involves whenever you'd like."),
]

# Coordinator instructions, typed into the composer ("offer a screening call
# next week", "ask when they're free"). Matched against what the COORDINATOR
# typed rather than against the inbound message, so the draft carries out the
# instruction instead of answering the topic. Every body reads correctly after
# "Hi <name>, " and, like the ladders above, states no study-specific fact.
_ASK_LADDER = [
    (("screening call", "screening visit", "book", "schedule", "set up a call",
      "phone call", "call them", "call her", "call him", "get them on the phone",
      "hop on a call", "meet"),
     "I'd love to set up a short screening call to walk through a few "
     "questions. What days and times generally work best for you?"),
    (("best time", "when are they free", "when they are free", "availability",
      "available", "what time", "reach them", "good number", "phone number"),
     "what's the best time and number to reach you? I'll work around your "
     "schedule."),
    (("still interested", "follow up", "following up", "check in", "checking in",
      "nudge", "haven't heard", "no reply", "no response", "bump"),
     "just checking in to see whether you're still interested. Happy to pick up "
     "wherever we left off - reply here any time."),
    (("what to expect", "next step", "what happens", "walk through", "process",
      "overview", "explain"),
     "here's how this usually goes: a short call to talk through a few "
     "questions, and if it looks like a fit, the study team walks you through "
     "the details before anything is decided. Happy to answer questions at any "
     "point along the way."),
    (("thank", "appreciate", "grateful"),
     "thank you for taking the time on this - I really appreciate it. I'm here "
     "if anything else comes up."),
    (("not a fit", "decline", "does not qualify", "doesn't qualify", "turn down",
      "reject", "close it out", "close this out"),
     "thank you for your interest, and for the time you've already put into "
     "this. I'll follow up if something changes or if another study looks like "
     "a better fit."),
    (("record", "document", "paperwork", "medication list", "med list",
      "insurance", "referral letter"),
     "could you have your current medication list and any recent records handy "
     "before we talk? It makes the screening conversation much quicker."),
    (("apolog", "sorry", "delay", "late", "slow to"),
     "apologies for the slow reply, and thank you for your patience. I'm "
     "picking this back up now."),
]

_REPLY_FALLBACK = ("I can help with the next step. Could you confirm the best "
                   "time for a brief screening call this week?")

# Applicant-thread ladder: an ongoing conversation with someone already in the
# pipeline, so the shapes are different - they're confirming a time, moving one,
# asking a question, or saying thanks. Reschedule beats availability so "can we
# move it, mornings are better" reads as a reschedule, not a confirmation.
_APPLICANT_LADDER = [
    (("reschedul", "can't make", "cant make", "cannot make", "won't make",
      "wont make", "move it", "another time", "postpone", "push it",
      "different day", "can we change", "not going to make", "need to change"),
     "No problem at all, {who} - we can reschedule. What days or times work "
     "best for you over the next week or two? I'll get you booked back in."),
    (("morning", "afternoon", "evening", "tomorrow", "monday", "tuesday",
      "wednesday", "thursday", "friday", "saturday", "sunday", "next week",
      "this week", "works for me", "work for me", "works best", "i'm free",
      "im free", "i'm available", "im available", "i am available",
      "available on", "i can do", "how about", "sounds good", "that works"),
     "That works, {who} - thanks for letting me know. I'll hold that time and "
     "send you a calendar invite with the visit details. Talk soon."),
    (("?", "how long", "how much", "do i need", "should i", "can i", "is there",
      "are there", "will i", "need to bring", "what time", "where is",
      "where do", "cost", "paid", "compensat", "insurance", "eligible"),
     "Good question, {who}. Happy to walk you through it - I'll lay out exactly "
     "what to expect, and let me know if anything is still unclear."),
    (("thank you", "thanks", "appreciate", "perfect", "great, ", "awesome",
      "will do", "see you", "confirmed", "got it"),
     "You're very welcome, {who}! I'm here if anything else comes up - just "
     "reply here anytime."),
]

_APPLICANT_FALLBACK = ("Thanks for getting back to me, {who}. I'll take a look "
                       "and follow up with next steps shortly - reach out here "
                       "if any questions come up in the meantime.")

# Single-applicant nudges the copilot proposes. These are not replies to
# anything, so they don't read the inbound message.
_NUDGES = {
    "check_in": ("Hi {who}, just checking in on your interest in {study}. "
                 "Happy to answer anything that's still open - reply here "
                 "anytime and I'll get back to you."),
    "booking": ("Hi {who}, a friendly reminder to book your screening visit "
                "using the link we sent. Let me know if none of those times "
                "work and I'll find another."),
    "reschedule": ("Hi {who}, no problem rescheduling. What days or times work "
                   "best for you over the next couple of weeks? I'll get you "
                   "booked back in."),
    "thanks": ("Thanks so much, {who} - really appreciate you taking the time. "
               "I'm here if anything else comes up."),
}


def _pick(ladder, text, fallback):
    t = (text or "").lower()
    for hints, body in ladder:
        if any(h in t for h in hints):
            return body
    return fallback


def _deterministic(intent, ctx):
    """The always-available draft. Every branch is safe to send as-is."""
    who = (ctx.get("first") or "there").strip() or "there"
    study = (ctx.get("study") or "the study").strip() or "the study"
    inbound = ctx.get("inbound") or ""

    if intent == "reply":
        # An instruction beats the inbound message. The coordinator is reading
        # the same thread and has just said what they want said, so answering
        # the topic instead of the instruction would be ignoring them.
        ask = (ctx.get("ask") or "").strip()
        if ask:
            body = _pick(_ASK_LADDER, ask, "")
            if body:
                return f"Hi {who}, " + body
        opener = f"Hi {who}, thanks for reaching out about {study}. "
        return opener + _pick(_REPLY_LADDER, inbound, _REPLY_FALLBACK)

    if intent == "applicant_reply":
        return _pick(_APPLICANT_LADDER, inbound, _APPLICANT_FALLBACK).format(
            who=who, study=study)

    if intent in _NUDGES:
        return _NUDGES[intent].format(who=who, study=study)

    if intent == "blast":
        # A blast goes to many people at once, so it must not reference anything
        # personal and must not imply the recipient specifically is eligible.
        return (f"Quick update from the study team about {study}. Reply here if "
                "you have any questions or if anything has changed on your end - "
                "we're happy to help.")

    if intent == "note_summary":
        return _thread_note(ctx)

    return _REPLY_FALLBACK


def _thread_note(ctx):
    """A team-only summary of a thread, built from counts and the last message
    rather than from anything interpretive. Never speculates about eligibility."""
    msgs = ctx.get("messages") or []
    inbound = sum(1 for m in msgs if m.get("inbound"))
    outbound = len(msgs) - inbound
    last = (ctx.get("inbound") or "").strip()
    who = (ctx.get("first") or "This applicant").strip()
    bits = [f"{who}: {len(msgs)} message" + ("" if len(msgs) == 1 else "s")
            + f" ({inbound} from them, {outbound} from us)."]
    if ctx.get("stage"):
        bits.append(f"Currently at {ctx['stage']}.")
    if last:
        snippet = last if len(last) <= 160 else last[:157].rstrip() + "..."
        bits.append(f"Their last message: “{snippet}”")
    if ctx.get("waiting"):
        bits.append("Waiting on us to reply.")
    return " ".join(bits)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def draft(intent, ctx=None):
    """Return a ready-to-review message for ``intent``.

    ``ctx`` keys (all optional): first, study, inbound, stage, messages,
    audience_summary, count. Never raises - any LLM problem falls back to the
    deterministic draft, which is always a complete, sendable message."""
    ctx = ctx or {}
    if intent not in INTENTS:
        intent = "reply"
    base = _deterministic(intent, ctx)
    out = _polish(base, intent, ctx) or base
    return sanitize_copy(out)


def _polish(base, intent, ctx):
    """Optional LLM rewrite of a draft that is already correct. Returns None on
    no key, any failure, or output that looks wrong, so the caller keeps the
    deterministic text."""
    if not mt.LLM_API_KEY:
        return None
    ask = (ctx.get("ask") or "").strip()
    try:
        user = (f"INTENT: {intent}\n"
                f"RECIPIENT FIRST NAME: {ctx.get('first') or 'unknown'}\n"
                f"STUDY: {ctx.get('study') or 'unknown'}\n"
                f"THEIR LAST MESSAGE: {(ctx.get('inbound') or '(none)')[:1200]}\n"
                + (f"COORDINATOR'S INSTRUCTION: {ask[:400]}\n" if ask else "")
                + f"\nDRAFT TO REWRITE:\n{base}\n\n"
                "Rewrite it now, following your rules.")
        out = (mt.llm_chat(SYSTEM_PROMPT + (ASK_RULES if ask else ""),
                           user) or "").strip()
        # A refusal, an empty answer, or a wall of text means something went
        # sideways - fall back rather than surface it to a coordinator.
        if not out or len(out) > 1200:
            return None
        return sanitize_copy(out)
    except Exception:
        return None
