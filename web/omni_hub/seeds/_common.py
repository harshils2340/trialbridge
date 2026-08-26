"""Helpers shared by every template's seed file.

Everything here is fictional by construction: 555 phone numbers, example.com
addresses, invented business names. Seed bodies are sanitized again at seed
time, but the rule is that no string in a seed file contains an em dash.
"""
import random

FIRST_NAMES = [
    "Priya", "Marcus", "Dana", "Tom", "Lena", "Jordan", "Ana", "Chris", "Beatrice",
    "Sam", "Ruth", "Miguel", "Hannah", "Teresa", "Devon", "Aisha", "Noah", "Grace",
    "Omar", "Elena", "Victor", "Nadia", "Felix", "Ingrid", "Kwame", "Sofia", "Leo",
    "Maya", "Rohan", "Claire", "Yusuf", "Wren", "Diego", "Harper", "Amara", "Theo",
]
LAST_NAMES = [
    "Natarajan", "Bell", "Whitfield", "Reyes", "Okafor", "Kim", "Sousa", "Doyle",
    "Lang", "Patel", "Adler", "Torres", "Cole", "Okonkwo", "Marsh", "Ferreira",
    "Lindqvist", "Nakamura", "Haddad", "Brennan", "Castillo", "Osei", "Varga",
    "Delgado", "Moreau", "Ivers", "Quinn", "Sato", "Abara", "Rinaldi",
]


def phone(seed):
    r = random.Random(seed)
    area = r.choice(["312", "773", "630", "847", "708", "224", "331"])
    return f"+1 ({area}) 555-{r.randint(100, 199):03d}{r.randint(0, 9)}"


def email(name, domain="example.com"):
    return name.lower().replace(" ", ".") + "@" + domain


def msg(kind, ago, body, author=""):
    """kind: inbound | outbound | note. ago: minutes before now."""
    return {"kind": kind, "ago": int(ago), "body": body, "author": author}


def conv(key, source, name, handle, subject, messages, stage="new", fields=None,
         extra_fields=None, drafts=None, priority="normal", unread=None,
         assignee="", status="open", tricky=""):
    """One seed conversation. fields: {key: (value, confidence, quote)}."""
    if unread is None:
        unread = messages[-1]["kind"] == "inbound" and status == "open"
    return {
        "key": key, "source": source,
        "contact": {"name": name, "handle": handle},
        "subject": subject, "messages": messages, "stage": stage,
        "fields": fields or {}, "extra_fields": extra_fields or {},
        "drafts": drafts or {}, "priority": priority, "unread": bool(unread),
        "assignee": assignee, "status": status, "tricky": tricky,
    }


def v(value, confidence=0.9, quote=""):
    return (value, confidence, quote)


UNKNOWN = (None, 0, "")
