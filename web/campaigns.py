"""Layer 2 - Part 2: recruitment campaigns (the AI "agency", wired to the funnel).

A study team drafts a campaign for a channel (Meta, Google, Reddit, campus,
email). This module drafts the ad creative with AI - but the draft is NEVER
publishable on its own: recruitment materials must be IRB/REB-approved and
truthful before a campaign can go live. Persistence + the activation gate live in
db.py (create_campaign / set_campaign_creative / approve_campaign /
set_campaign_status); this module is the AI + the compliance-shaped copy.

Because campaigns feed applicants into the SAME lead pipeline (leads.campaign_id),
db.campaign_performance() can report cost per *enrolled* patient - not vanity
clicks. That closed loop (ad -> applicant -> enrolled) is the thing generic
recruitment agencies can't see. KPI: found -> contacted.

Compliance (hard gate, see matcher/COMPLIANCE.md): generated copy states neutral,
truthful facts only - no efficacy/benefit claims, no sponsor recruitment claims,
no payment promises. A human must confirm IRB/REB approval before it runs.
"""
import json

import match_trials as mt

# Channel-specific shaping so the same facts read naturally per surface.
_CHANNEL_HINT = {
    "meta": "a Facebook/Instagram ad: 1 short headline, 2-3 sentence body.",
    "google": "a Google search ad: 3 punchy headlines (<=30 chars) joined by ' | '"
              " as the headline, and 1-2 short description lines as the body.",
    "reddit": "a plain, non-salesy Reddit post: honest, community-friendly tone.",
    "campus": "a campus flyer blurb: friendly, aimed at students/staff.",
    "email": "a short outreach email: subject as headline, 3-4 sentence body.",
    "other": "a short, neutral recruitment notice.",
}

_CREATIVE_SYSTEM = (
    "You draft NEUTRAL, TRUTHFUL clinical-trial recruitment copy that a research "
    "ethics board (IRB/REB) would be willing to approve. HARD RULES: state only "
    "plain facts about who the study is looking for and what participation "
    "involves; do NOT claim or imply benefit, efficacy, safety, or a cure; do NOT "
    "promise payment, treatment, or enrollment; no urgency/pressure tactics; no "
    "superlatives. This copy is a DRAFT that a human must review and get "
    "IRB/REB-approved before use. Return strict JSON: "
    '{"headline": string, "body": string, "landing_copy": string}. '
    "landing_copy is 2-4 short sentences for a landing page."
)


def _fallback_creative(trial):
    title = (trial or {}).get("title") or "a clinical research study"
    cond = (trial or {}).get("condition") or ""
    who = f" for people with {cond}" if cond else ""
    return {
        "headline": f"Volunteers sought{who}",
        "body": (f"A research team is enrolling participants in {title}. "
                 "If you're interested, you can find out whether you may be "
                 "eligible - no obligation."),
        "landing_copy": (f"{title} is currently seeking volunteers{who}. "
                         "Answer a few short questions to see if you may be "
                         "eligible. A study team member will follow up. This is "
                         "research, not treatment, and participation is voluntary."),
    }


def generate_creative(trial, channel="other"):
    """Draft {headline, body, landing_copy} for a channel. Always returns
    reviewable copy (LLM when available, else a safe template). The result is a
    DRAFT: the caller stores it with irb_approved=0 and a human must approve it
    before the campaign can go active."""
    ch = (channel or "other").strip().lower()
    if ch not in _CHANNEL_HINT:
        ch = "other"
    if not mt.LLM_API_KEY:
        return _fallback_creative(trial)
    t = trial or {}
    user = (
        f"STUDY TITLE: {t.get('title') or 'n/a'}\n"
        f"CONDITION: {t.get('condition') or 'n/a'}\n"
        f"SUMMARY: {(t.get('brief_summary') or t.get('summary') or '')[:1200]}\n"
        f"LOCATION: {t.get('location') or 'n/a'}\n\n"
        f"Write {_CHANNEL_HINT[ch]}\n"
        "Return the JSON."
    )
    try:
        raw = mt.llm_chat(_CREATIVE_SYSTEM, user)
        data = mt._extract_json(raw)
        out = {
            "headline": str(data.get("headline") or "").strip(),
            "body": str(data.get("body") or "").strip(),
            "landing_copy": str(data.get("landing_copy") or "").strip(),
        }
        if not out["headline"] or not out["body"]:
            return _fallback_creative(trial)
        return out
    except Exception:
        return _fallback_creative(trial)


_VARIANTS_SYSTEM = _CREATIVE_SYSTEM.replace(
    'Return strict JSON: {"headline": string, "body": string, "landing_copy": string}. '
    "landing_copy is 2-4 short sentences for a landing page.",
    "Return strict JSON: {\"variants\": [{\"headline\": string, \"body\": string}]} "
    "with DISTINCT angles (e.g. one factual, one community-friendly, one concise). "
    "Every variant must obey the hard rules above."
)


def generate_variants(trial, channel="other", n=3):
    """Draft up to `n` DISTINCT creative options for a channel so the site can
    pick what fits where they post. Returns a list of {headline, body}; always at
    least one (falls back to the safe template). All drafts are unapproved."""
    n = max(1, min(5, int(n or 3)))
    fb = _fallback_creative(trial)
    fallback_list = [{"headline": fb["headline"], "body": fb["body"]}]
    ch = (channel or "other").strip().lower()
    if ch not in _CHANNEL_HINT:
        ch = "other"
    if not mt.LLM_API_KEY:
        return fallback_list
    t = trial or {}
    user = (
        f"STUDY TITLE: {t.get('title') or 'n/a'}\n"
        f"CONDITION: {t.get('condition') or 'n/a'}\n"
        f"SUMMARY: {(t.get('brief_summary') or t.get('summary') or '')[:1200]}\n"
        f"LOCATION: {t.get('location') or 'n/a'}\n\n"
        f"Write {n} variants of {_CHANNEL_HINT[ch]}\nReturn the JSON."
    )
    try:
        data = mt._extract_json(mt.llm_chat(_VARIANTS_SYSTEM, user))
        items = data.get("variants") if isinstance(data, dict) else data
        out = []
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            h = str(it.get("headline") or "").strip()
            b = str(it.get("body") or "").strip()
            if h and b:
                out.append({"headline": h, "body": b})
            if len(out) >= n:
                break
        return out or fallback_list
    except Exception:
        return fallback_list


def placement_share_payload(campaign, placement, base_url=""):
    """A ready-to-post bundle for one placement: the copy to paste + the trackable
    link to include. Sites post manually wherever they like; the /go/<token> link
    is what ties resulting applicants back to this exact placement.

    `campaign` and `placement` are db rows (or dicts). Returns {label, channel,
    url, copy}."""
    def _get(row, key, default=""):
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return (row or {}).get(key, default) if hasattr(row, "get") else default

    token = _get(placement, "track_token")
    url = f"{(base_url or '').rstrip('/')}/go/{token}"
    headline = _get(campaign, "headline") or "Research volunteers sought"
    body = _get(campaign, "body")
    label = _get(placement, "label")
    channel = _get(placement, "channel") or _get(campaign, "channel")
    parts = [headline]
    if body:
        parts.append(body)
    parts.append(f"See if you may be eligible: {url}")
    return {"label": label, "channel": channel, "url": url,
            "copy": "\n\n".join(p for p in parts if p)}


def can_activate(campaign):
    """A campaign may go live only once a human has confirmed its creative is
    IRB/REB-approved. Mirrors the gate in db.set_campaign_status so callers can
    check before offering an 'activate' action."""
    if not campaign:
        return False
    try:
        return bool(int(campaign["irb_approved"] or 0))
    except (KeyError, TypeError, ValueError):
        return bool((campaign or {}).get("irb_approved"))
