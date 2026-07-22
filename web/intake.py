"""Layer 2 - Part 1: multi-source applicant intake (the ATS capture layer).

Applicants reach a site from many places: their own inbox, ClinicalTrials.gov
inquiries, ad lead forms, physician referrals, walk-ins. This module lets all of
them land in ONE per-trial queue WITHOUT the site changing how patients apply.
Each claimed study has a unique inbound email address (db.intake_addresses); a
site auto-forwards applicant mail there, we match it to an existing applicant (or
create one), thread it into the existing per-lead messages inbox, and run an AI
pre-screen against the study's criteria.

Design notes:
- Every function degrades gracefully. The inbound webhook must never 500 on a
  malformed payload or an LLM outage, or we silently drop a real applicant.
- Pre-screen is DECISION SUPPORT only. It surfaces a verdict + the reason, and a
  human always decides - we never auto-accept or auto-reject (compliance + trust).

Compliance: inbound mail carries PHI. Address resolution is scoped to the owning
user+nct, and a BAA with the inbound-email provider is required before production
(see matcher/COMPLIANCE.md). We store only what the applicant volunteered.
KPI: speeds contacted -> screened (nothing rots in a personal inbox).
"""
import email.utils
import json
import os

import db
import match_trials as mt

# Full inbound address is <local-part>@<INTAKE_EMAIL_DOMAIN>. The provider's MX
# for this subdomain points at our inbound webhook.
INTAKE_EMAIL_DOMAIN = os.environ.get("INTAKE_EMAIL_DOMAIN", "intake.bridgemd.com")

MAX_BODY_CHARS = 8000


def full_intake_address(local_part):
    """Bare token -> full email a site forwards to."""
    lp = (local_part or "").strip().lower()
    if "@" in lp:
        return lp
    return f"{lp}@{INTAKE_EMAIL_DOMAIN}"


def parse_from(raw):
    """'Jane Doe <jane@x.com>' -> ('Jane Doe', 'jane@x.com'). Tolerates a bare
    address or empty input."""
    name, addr = email.utils.parseaddr(raw or "")
    return (name or "").strip(), (addr or "").strip().lower()


def _recipient_local_part(payload):
    """Pull the intake address token out of whatever 'to' field the provider
    sends (SendGrid uses 'to', Postmark 'ToFull', Mailgun 'recipient')."""
    for key in ("to", "recipient", "To", "envelope_to"):
        val = payload.get(key)
        if val:
            _n, addr = parse_from(val if isinstance(val, str) else str(val))
            if addr:
                return addr.split("@", 1)[0]
    return ""


def handle_inbound_email(payload):
    """Ingest one inbound email into the right trial's queue.

    `payload` is a provider-agnostic dict with any of: to/recipient, from/From,
    subject/Subject, text/body/TextBody. Returns a summary dict; never raises.
    """
    try:
        local = _recipient_local_part(payload)
        addr = db.resolve_intake_address(local)
        if not addr:
            # Unknown or disabled address: refuse rather than guess a trial.
            return {"ok": False, "reason": "unknown_intake_address",
                    "address": local}

        sender_name, sender_email = parse_from(
            payload.get("from") or payload.get("From") or "")
        subject = (payload.get("subject") or payload.get("Subject")
                   or "").strip()
        text = (payload.get("text") or payload.get("body")
                or payload.get("TextBody") or "")
        body = (subject + "\n\n" + text).strip()[:MAX_BODY_CHARS] or "(no content)"

        # notes are only applied when a NEW lead is created; match_or_create_lead
        # ignores them when threading onto an existing applicant.
        lead_id, token, created = db.match_or_create_lead(
            addr["nct"], email=sender_email, name=sender_name,
            notes=(text or "")[:2000], source="email_intake")

        # Thread the message into the existing per-lead inbox as a patient message.
        db.add_message(lead_id, "patient", body)

        prescreen = None
        try:
            prescreen = prescreen_lead(lead_id)
        except Exception:
            prescreen = None

        return {"ok": True, "lead_id": lead_id, "token": token,
                "created": created, "nct": addr["nct"],
                "prescreen": prescreen}
    except Exception as e:  # pragma: no cover - defensive: never break the webhook
        return {"ok": False, "reason": "error", "detail": str(e)[:200]}


def _patient_summary(lead):
    """De-identified summary of what the applicant volunteered, for the matcher.
    Name/contact are omitted - eligibility never depends on them."""
    bits = []
    if lead["age"]:
        bits.append(f"Age: {lead['age']}")
    if lead["sex"]:
        bits.append(f"Sex: {lead['sex']}")
    if lead["condition"]:
        bits.append(f"Reason for interest: {lead['condition']}")
    if lead["record_summary"]:
        bits.append(f"Records: {lead['record_summary']}")
    if lead["notes"]:
        bits.append(f"Notes: {lead['notes']}")
    return "\n".join(bits) or "(no details volunteered yet)"


def _trial_for_lead(lead, trial=None):
    """Build the trial dict mt.llm_match expects. Prefer a caller-supplied trial
    (e.g. the CT.gov record the app already loaded); else fall back to a
    site-posted study's stored eligibility text. Returns None if we have no
    criteria to screen against."""
    if trial and (trial.get("criteria") or trial.get("eligibility")):
        return {
            "title": trial.get("title") or lead["title"] or "",
            "nctId": trial.get("nctId") or lead["nct"] or "",
            "phase": trial.get("phase") or "",
            "sex": trial.get("sex") or "ALL",
            "minAge": trial.get("minAge") or "",
            "maxAge": trial.get("maxAge") or "",
            "healthyVolunteers": trial.get("healthyVolunteers") or "",
            "criteria": trial.get("criteria") or trial.get("eligibility") or "",
        }
    posted = db.get_site_posted_study_by_nct(lead["nct"]) if lead["nct"] else None
    if posted and posted["eligibility"]:
        return {
            "title": posted["title"] or lead["title"] or "",
            "nctId": posted["nct"], "phase": posted["phase"] or "",
            "sex": "ALL", "minAge": "", "maxAge": "", "healthyVolunteers": "",
            "criteria": posted["eligibility"],
        }
    return None


def prescreen_lead(lead_id, trial=None):
    """Run the AI eligibility read for a captured applicant and persist it.

    Reuses the same matcher the consumer side uses (mt.llm_match) so a lead from
    ANY source gets the same verdict + reasons. Stores a structured eligibility
    read the queue can render ("excluded: history of seizures"). Returns the
    verdict dict, or a neutral 'needs_review' result when we can't screen (no
    criteria on file, or the LLM is unavailable). Never raises."""
    lead = db.get_lead(lead_id)
    if not lead:
        return None
    neutral = {"verdict": "needs_review", "score": 0, "met": [], "not_met": [],
               "unknown": [], "rationale": "No criteria on file to screen against."}
    trial_dict = _trial_for_lead(lead, trial=trial)
    if not trial_dict or not mt.LLM_API_KEY:
        db.set_lead_prescreen(lead_id, json.dumps(neutral), "")
        return neutral
    try:
        result = mt.llm_match(_patient_summary(lead), trial_dict)
    except Exception:
        db.set_lead_prescreen(lead_id, json.dumps(neutral), "")
        return neutral
    db.set_lead_prescreen(lead_id, json.dumps(result), "")
    return result


# Bounded, compliance-safe outreach: neutral, factual, no efficacy/benefit or
# sponsor claims (those need IRB/REB approval). The model drafts; a human sends.
_OUTREACH_SYSTEM = (
    "You are a clinical research coordinator writing a SHORT, warm first-contact "
    "message to someone who expressed interest in a study. Rules: be plain and "
    "human; thank them for their interest; say the study team will help them see "
    "if they may be eligible; invite them to reply or book a quick screening "
    "call. Do NOT promise enrollment, payment, treatment benefit, or make any "
    "medical or recruitment claim. No PHI beyond the first name. Keep it under "
    "90 words. Return only the message text."
)


def draft_outreach(lead_id, trial=None):
    """Draft (do NOT send) a first-contact message for a captured applicant.
    Returns a string the coordinator can edit and send. Falls back to a safe
    template if the LLM is unavailable."""
    lead = db.get_lead(lead_id)
    if not lead:
        return ""
    first = (lead["name"] or "").split(" ")[0].strip() or "there"
    study = lead["title"] or lead["condition"] or "the study you asked about"
    fallback = (
        f"Hi {first}, thanks for your interest in {study}. Our study team would "
        "love to help you find out whether you may be eligible. Could you reply "
        "here, or book a short screening call at your convenience? Happy to "
        "answer any questions."
    )
    if not mt.LLM_API_KEY:
        return fallback
    user = (f"APPLICANT FIRST NAME: {first}\nSTUDY: {study}\n"
            "Write the first-contact message.")
    try:
        return (mt.llm_chat(_OUTREACH_SYSTEM, user) or "").strip() or fallback
    except Exception:
        return fallback
