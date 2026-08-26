"""Thin wrappers over match_trials' LLM plumbing. Both return None when there
is no key or anything fails, so callers fall back deterministically and never
raise because of the model. Every string that comes back is sanitized."""
import json
import time
import urllib.request

import match_trials as mt
from copy_sanitize import sanitize_copy


def available():
    return bool(getattr(mt, "LLM_API_KEY", ""))


def chat_text(system, user, retries=2):
    if not available():
        return None
    try:
        return mt.llm_chat(system, user, retries=retries)
    except Exception:
        return None


def _clean(obj):
    if isinstance(obj, str):
        return sanitize_copy(obj)
    if isinstance(obj, list):
        return [_clean(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    return obj


def chat_json(system, user, max_tokens=700, retries=2, timeout=60):
    """JSON-mode call (same request shape as match_trials.prescreen_questions).
    Returns a dict or None."""
    if not available():
        return None
    body = json.dumps({
        "model": mt.LLM_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": max_tokens,
    }).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                f"{mt.LLM_BASE_URL}/chat/completions", data=body,
                headers={"Authorization": f"Bearer {mt.LLM_API_KEY}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.load(r)
            raw = resp["choices"][0]["message"]["content"]
            data = mt._extract_json(raw)
            return _clean(data) if isinstance(data, dict) else None
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None
