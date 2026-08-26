"""Strip em dashes from user-facing copy and AI output.

Coordinators asked for zero em dashes in product text. All LLM responses and
drafts pass through ``sanitize_copy``; user-visible strings in the repo should
not contain \\u2014 or &mdash; to begin with.
"""
from __future__ import annotations

import re

EM_DASH = "\u2014"
EM_DASH_HTML = "&mdash;"
EM_DASH_ENTITY = "&#8212;"
EM_DASH_HEX_ENTITY = "&#x2014;"

# Spaced em dash -> comma; tight em dash -> hyphen.
_SPACED_EM = re.compile(r"\s*\u2014\s*")
_TIGHT_EM = re.compile(r"\u2014")


def sanitize_copy(text):
    """Replace em dashes with commas or hyphens. Safe on None/non-str."""
    if text is None:
        return text
    if not isinstance(text, str):
        return text
    if (EM_DASH not in text and EM_DASH_HTML not in text
            and EM_DASH_ENTITY not in text and EM_DASH_HEX_ENTITY not in text):
        return text
    s = (text.replace(EM_DASH_HTML, EM_DASH).replace(EM_DASH_ENTITY, EM_DASH)
         .replace(EM_DASH_HEX_ENTITY, EM_DASH))
    s = _SPACED_EM.sub(", ", s)
    s = _TIGHT_EM.sub("-", s)
    s = re.sub(r",\s*,+", ",", s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r",\s+\.", ".", s)
    return s


def contains_em_dash(text) -> bool:
    if not isinstance(text, str):
        return False
    return (EM_DASH in text or EM_DASH_HTML in text or EM_DASH_ENTITY in text
            or EM_DASH_HEX_ENTITY in text)
