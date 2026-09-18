"""Email helpers for coordinator notifications.

Sending is optional: if SMTP isn't configured via environment variables the
app falls back to a prefilled mailto: link, so the referral loop still works
with zero infrastructure. Set these to enable real sending:

    SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS,
    SMTP_FROM (defaults to SMTP_USER), SMTP_TLS ("1" default)
"""
import html
import json
import os
import smtplib
import ssl
from email.message import EmailMessage
try:
    from copy_sanitize import sanitize_copy
except ImportError:  # imported outside the app, without the repo root on sys.path
    import pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
    from copy_sanitize import sanitize_copy


def smtp_configured():
    return bool(os.environ.get("SMTP_HOST"))


def build_message(ref, link):
    """Return (subject, body) for a coordinator notification email."""
    subject = f"Trial referral - {ref['nct']} ({ref['patient_label']})"
    lines = [
        "Hello,",
        "",
        f"A physician has referred a patient who may be eligible for your study "
        f"{ref['nct']}.",
        "",
        f"Study: {ref['title']}",
        f"Site: {ref['site'] or 'see ClinicalTrials.gov'}",
        f"Patient reference: {ref['patient_label']}",
    ]
    if ref["verdict"]:
        v = ref["verdict"].replace("_", " ")
        lines.append(f"Screening: {v}" + (f" (score {ref['score']})"
                                          if ref["score"] else ""))
    if ref["consent"] and (ref["patient_name"] or ref["patient_contact"]):
        lines += ["", "Patient consented to be contacted:",
                  f"  Name: {ref['patient_name'] or 'n/a'}",
                  f"  Contact: {ref['patient_contact'] or 'n/a'}"]
    else:
        lines += ["", "The patient has not released contact details; please "
                  "coordinate the referral back through the referring physician."]
    if ref["patient_summary"]:
        lines += ["", "De-identified clinical summary:", ref["patient_summary"]]
    lines += [
        "",
        "Please confirm receipt and update the referral status here:",
        link,
        "",
        "Sent via BridgeMD.",
    ]
    return subject, "\n".join(lines)


FINDER_URL = "https://bridgemd.health/"
BRAND_HOME = "https://bridgemd.health"
# PNG mark (email clients often skip SVG). Already served on production.
LOGO_URL = "https://bridgemd.health/static/apple-touch-icon.png"

# Every email is from a person, not a robot. Replies go to a real inbox the
# founder reads, and the footer puts a face and a LinkedIn page on it so a
# coordinator who has never heard of BridgeMD can see who is writing.
HELLO_EMAIL = "hello@bridgemd.health"
FOUNDER_NAME = "Harshil Shah"
FOUNDER_TITLE = "Founder, BridgeMD. Student at the University of Waterloo."
LINKEDIN_URL = os.environ.get(
    "LINKEDIN_URL", "https://www.linkedin.com/company/bridgemd/").strip()
LINKEDIN_LOGO_URL = "https://bridgemd.health/static/linkedin.png"


def from_header():
    """Sender for every outbound email. MAIL_FROM overrides; otherwise the
    founder at the shared inbox, never a no-reply address."""
    return (os.environ.get("MAIL_FROM") or "").strip() or \
        f"{FOUNDER_NAME} at BridgeMD <{HELLO_EMAIL}>"


def reply_to_header():
    return (os.environ.get("MAIL_REPLY_TO") or "").strip() or HELLO_EMAIL


def _first_name(lead):
    return (_lead_text(lead, "name") or "there").split()[0]


def _condition_phrase(lead):
    c = _lead_text(lead, "condition").strip().rstrip(".")
    # Mid-sentence, a condition reads as a plain noun ("alcohol use disorder
    # study"); an acronym or proper noun ("COVID", "Crohn's") keeps its case.
    if len(c) > 1 and c[0].isupper() and c[1].islower():
        c = c[0].lower() + c[1:]
    return f"{c} study" if c else "clinical trial"


def _place(lead):
    return _lead_text(lead, "location").strip()


def human_subject(kind, lead):
    """Subject lines that read like a person wrote them: what the trial is,
    in plain words, and where. No registry codes, no brand tags."""
    cond = _condition_phrase(lead)
    place = _place(lead)
    first = _first_name(lead)
    if kind == "candidate":
        who = f"{first} in {place}" if place else first
        return f"{who} applied to your {cond}"
    if kind == "apply_confirmation":
        return f"Your application to the {cond}"
    if kind == "founder_connect":
        return f"How to reach the {cond} team directly"
    if kind == "clinic_connect":
        return f"Messaging the {cond} team"
    if kind == "dm_to_site":
        return f"{first} sent you a message about the {cond}"
    if kind == "dm_to_patient":
        return f"The {cond} team replied to you"
    if kind == "owner":
        return f"New application: {cond}" + (f" in {place}" if place else "")
    return f"About your {cond}"


def age_from_dob(dob):
    """Whole years from a YYYY-MM-DD date of birth, or "" when unparseable."""
    try:
        y, m, d = (int(x) for x in (dob or "").strip().split("-"))
        import datetime as _dt
        born = _dt.date(y, m, d)
        today = _dt.date.today()
        if born > today:
            return ""
        return str(today.year - born.year
                   - ((today.month, today.day) < (born.month, born.day)))
    except (ValueError, TypeError):
        return ""


def _dob_line(lead):
    dob = _lead_text(lead, "dob")
    age = _lead_text(lead, "age")
    if dob:
        a = age_from_dob(dob) or age
        return f"{dob}" + (f" (age {a})" if a else "")
    return f"age {age}" if age else ""


def _lead_text(lead, key):
    try:
        val = lead[key]
    except (KeyError, IndexError, TypeError):
        return ""
    return ("" if val is None else str(val)).strip()


def _answers_block(lead):
    """The applicant's own answers, question by question, plus any the study
    would want a second look at. Plain text a coordinator can read in one
    pass and type into their own system."""
    raw = _lead_text(lead, "screener")
    try:
        scr = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        scr = {}
    if not isinstance(scr, dict):
        scr = {}
    labels = {"travel": "Can travel to the study site for visits",
              "other_trial": "Currently in another clinical trial",
              "pregnancy": "Pregnant or planning to become pregnant",
              "consent_capable": "Can give their own informed consent"}
    lines = []
    for q, a in scr.items():
        if q.startswith("_"):
            continue
        label = labels.get(q, q)
        ans = str(a).strip().capitalize()
        lines.append(f"- {label} {ans}" if label.endswith("?") else f"- {label}: {ans}")
    flags = scr.get("_flags") or []
    if flags:
        lines.append("")
        lines.append("Answers worth a second look:")
        lines += [f"- {f}" for f in flags]
    return lines


def _eligibility_block(lead):
    raw = _lead_text(lead, "eligibility")
    try:
        elig = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        elig = {}
    if not isinstance(elig, dict):
        return []
    lines = []
    for key, label in (("met", "Looks met"), ("unknown", "Still to confirm"),
                       ("not_met", "May not be met")):
        items = [str(x).strip() for x in (elig.get(key) or []) if str(x).strip()]
        if items:
            lines.append(f"{label}:")
            lines += [f"- {x}" for x in items[:8]]
    return lines


def build_candidate_message(lead, link=None, clinic=None, reach=0):
    """The handoff to a study team, written as a letter from the founder.

    Everything the applicant told us is in the body, in order, so a
    coordinator can screen and enroll them from the email alone. There is no
    application link: the email is the application. The applicant is never on
    To/CC; their contact details are the point of the handoff and they
    consented to it. No eligibility claims: their answers are labelled as
    self-reported."""
    _ = link
    clinic = clinic or {}
    facility = (clinic.get("facility") or "").strip() or _lead_text(lead, "site")
    first = _first_name(lead)
    subject = human_subject("candidate", lead)
    place = _place(lead)
    who = f"{_lead_text(lead, 'name') or 'Someone'}"
    if place:
        who += f", in {place},"
    lines = [
        "Hi,",
        "",
        f"I'm {FOUNDER_NAME}, the founder of BridgeMD. I'm a student at the "
        "University of Waterloo, and BridgeMD is a not-for-profit project with "
        "one job: connecting people who want to join a clinical trial with the "
        "team running it. We don't charge you or the applicant, we don't sell "
        "anything, and we don't need anything from you.",
        "",
        f"{who} applied to your study on BridgeMD and asked to be contacted. "
        "Everything they told us is below, so you can screen and enroll them "
        "from this email. Please reach out to them directly. They are not "
        "copied here.",
        "",
        "STUDY",
        f"{_lead_text(lead, 'title') or 'your study'}",
    ]
    if _lead_text(lead, "nct"):
        lines.append(f"Registry number: {_lead_text(lead, 'nct')}")
    if facility:
        lines.append(f"Site they chose: {facility}")
    lines += ["", "APPLICANT",
              f"Name: {_lead_text(lead, 'name') or 'not given'}",
              f"Email: {_lead_text(lead, 'email') or 'not given'}",
              f"Phone: {_lead_text(lead, 'phone') or 'not given'}"]
    dob = _dob_line(lead)
    if dob:
        lines.append(f"Date of birth: {dob}" if _lead_text(lead, "dob")
                     else f"Age: {_lead_text(lead, 'age')}")
    if _lead_text(lead, "sex"):
        lines.append(f"Sex: {_lead_text(lead, 'sex').capitalize()}")
    if place:
        lines.append(f"Location: {place}")
    if _lead_text(lead, "condition"):
        lines.append(f"Condition: {_lead_text(lead, 'condition')}")
    if _lead_text(lead, "created_at"):
        lines.append(f"Applied: {_lead_text(lead, 'created_at')}")
    answers = _answers_block(lead)
    if answers:
        lines += ["", "THEIR ANSWERS (self-reported, not verified)"] + answers
    elig = _eligibility_block(lead)
    if elig:
        lines += ["", "ELIGIBILITY READ FROM THEIR ANSWERS (not verified)"] + elig
    notes = _lead_text(lead, "notes").strip()
    if notes:
        lines += ["", "IN THEIR OWN WORDS", notes]
    summary = _lead_text(lead, "record_summary").strip()
    if summary:
        lines += ["", "FROM RECORDS THEY CONNECTED", summary]
    reach_line = ""
    try:
        n = int(reach or 0)
    except (TypeError, ValueError):
        n = 0
    if n >= 5:
        reach_line = (f"So far {n} people have applied to trials through "
                      "BridgeMD. ")
    lines += [
        "",
        reach_line + "If this was useful, or if there is any reason you can't "
        "act on it, just reply to this email and tell me. I read every reply, "
        "and it is how we make this better.",
        "",
        FOUNDER_NAME,
        FOUNDER_TITLE,
        f"LinkedIn: {LINKEDIN_URL}",
        HELLO_EMAIL,
    ]
    return subject, "\n".join(lines)


def branded_html(body):
    """HTML wrapper with the BridgeMD mark and finder link. Used as the
    multipart alternative so Gmail/Outlook show branding, not only plain text."""
    escaped = html.escape(body or "").replace("\n", "<br>\n")
    return (
        "<!doctype html><html><body style=\"margin:0;padding:0;background:#f4f7fb;"
        "font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;"
        "color:#12122b;\">"
        "<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" "
        "cellspacing=\"0\" style=\"background:#f4f7fb;padding:24px 12px;\">"
        "<tr><td align=\"center\">"
        "<table role=\"presentation\" width=\"560\" cellpadding=\"0\" "
        "cellspacing=\"0\" style=\"background:#ffffff;border-radius:12px;"
        "padding:28px 28px 22px;max-width:560px;\">"
        "<tr><td>"
        f"<a href=\"{FINDER_URL}\" style=\"text-decoration:none;color:#1257b0;\">"
        f"<img src=\"{LOGO_URL}\" width=\"40\" height=\"40\" alt=\"BridgeMD\" "
        "style=\"display:block;border:0;border-radius:9px;\">"
        "</a>"
        "<div style=\"font-size:20px;font-weight:800;letter-spacing:-0.3px;"
        "color:#12122b;margin:10px 0 4px;\">BridgeMD</div>"
        f"<div style=\"font-size:13px;margin:0 0 20px;\">"
        f"<a href=\"{FINDER_URL}\" style=\"color:#1257b0;text-decoration:none;"
        f"font-weight:600;\">{FINDER_URL}</a></div>"
        f"<div style=\"font-size:15px;line-height:1.55;color:#1a1a2e;\">{escaped}</div>"
        "<div style=\"margin-top:28px;padding-top:16px;border-top:1px solid #e4eaf2;"
        "font-size:13px;line-height:1.5;color:#5b6475;\">"
        "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\"><tr>"
        f"<td style=\"padding-right:10px;\"><a href=\"{LINKEDIN_URL}\">"
        f"<img src=\"{LINKEDIN_LOGO_URL}\" width=\"28\" height=\"28\" "
        "alt=\"LinkedIn\" style=\"display:block;border:0;border-radius:6px;\">"
        "</a></td><td>"
        f"<div style=\"color:#12122b;font-weight:700;\">{FOUNDER_NAME}</div>"
        f"<div>{html.escape(FOUNDER_TITLE)}</div>"
        f"<div><a href=\"{LINKEDIN_URL}\" style=\"color:#1257b0;\">LinkedIn</a>"
        f" &middot; <a href=\"mailto:{HELLO_EMAIL}\" style=\"color:#1257b0;\">"
        f"{HELLO_EMAIL}</a></div>"
        "</td></tr></table>"
        "</div></td></tr></table></td></tr></table></body></html>"
    )


def build_owner_new_application(lead, link, inbox=""):
    """Internal heads-up to the operator that a new application came in, so they
    can act on it (forward to the study team) fast. De-identified on purpose:
    NO name, email, phone, or clinical notes in the email itself, those live
    behind the secure record link. Includes triage context so the operator can
    prioritise before clicking through."""
    def _lget(key, default=""):
        try:
            v = lead[key]
        except (KeyError, IndexError, TypeError):
            return default
        return v if v not in (None, "") else default

    nct = _lget("nct") or "a study"
    subject = human_subject("owner", lead)
    src = (_lget("source", "web")).replace("_", " ")
    lines = [
        "A new application was just submitted on BridgeMD.",
        "",
        f"Study: {_lget('title') or nct}",
    ]
    if _lget("nct"):
        lines.append(f"NCT: {_lget('nct')}")
    if _lget("condition"):
        lines.append(f"Condition: {_lget('condition')}")
    if _lget("location"):
        lines.append(f"Region: {_lget('location')}")
    lines.append(f"Source: {src}")

    # Triage context (still de-identified): what the operator needs to decide
    # how urgent this is, without any PII.
    contact = []
    if _lget("email"):
        contact.append("email")
    if _lget("phone"):
        contact.append("phone")
    lines.append(f"Contact on file: {', '.join(contact) or 'none'}")

    verdict = ""
    try:
        elig = json.loads(_lget("eligibility") or "{}")
        if isinstance(elig, dict):
            verdict = (elig.get("verdict") or "").strip()
    except (ValueError, TypeError):
        pass
    if verdict:
        lines.append(f"Pre-screen: {verdict}")

    try:
        scr = json.loads(_lget("screener") or "{}")
        flags = scr.get("_flags") if isinstance(scr, dict) else None
        if flags:
            lines.append(f"Flags: {len(flags)} to review")
    except (ValueError, TypeError):
        pass

    if _lget("records_connected"):
        lines.append("Records: connected")

    lines += [
        "",
        "Open this applicant (contact + answers, behind login):",
        link,
    ]
    if inbox:
        lines += ["", "All applications (inbox):", inbox]
    lines += [
        "",
        "You're getting this because you're the BridgeMD operator.",
        "Sent via BridgeMD.",
    ]
    return subject, "\n".join(lines)


def build_coordinator_forward(lead, elig=None, screener=None, flags=None,
                              to_name="", sender_name=""):
    """Draft the operator sends to a study coordinator to hand off a patient who
    applied and consented to be contacted. The patient is inbound and has
    consented to the study team reaching them, so their own contact details are
    included on purpose - that is the handoff. Pre-screen is patient-reported and
    labelled as not verified. No recruitment claims, no payment: a neutral
    handoff the coordinator can act on."""
    elig = elig or {}
    flags = flags or []

    def _lget(k, d=""):
        try:
            v = lead[k]
        except (KeyError, IndexError, TypeError):
            return d
        return v if v not in (None, "") else d

    nct = _lget("nct")
    title = _lget("title") or nct or "your study"
    subject = f"Patient interested in {nct or 'your study'}"
    if _lget("condition"):
        subject += f" ({_lget('condition')})"

    greeting = f"Hi {to_name.strip()}," if to_name.strip() else "Hi,"
    contact_bits = []
    if _lget("email"):
        contact_bits.append(_lget("email"))
    if _lget("phone"):
        contact_bits.append(_lget("phone"))

    study_line = f"Study: {title}"
    if nct and nct not in title:
        study_line += f" ({nct})"

    lines = [
        greeting,
        "",
        ("A patient found " + (f"this study ({nct})" if nct else "your study") +
         " on BridgeMD and asked to be contacted about taking part. They've "
         "consented to your team reaching out using the details below."),
        "",
        study_line,
        f"Patient: {_lget('name') or 'Applicant'}",
    ]
    if contact_bits:
        lines.append("Contact: " + "  |  ".join(contact_bits))
    reported = []
    if _lget("age"):
        reported.append(f"age {_lget('age')}")
    if _lget("sex"):
        reported.append(_lget("sex"))
    if reported:
        lines.append("Reported: " + ", ".join(reported))

    verdict = (elig.get("verdict") or "").strip()
    if verdict:
        lines += ["", f"Pre-screen (patient-reported, not verified): {verdict}"]
    if elig.get("met"):
        lines.append("Appears to meet: " + "; ".join(elig["met"][:6]))
    if elig.get("unknown"):
        lines.append("To confirm: " + "; ".join(elig["unknown"][:6]))
    if elig.get("not_met"):
        lines.append("Possible barriers: " + "; ".join(elig["not_met"][:6]))
    if flags:
        lines.append("Flags to review: " + "; ".join(str(x) for x in flags[:6]))

    lines += [
        "",
        ("BridgeMD doesn't access medical records - the above is what the patient "
         "reported when they applied. If your team isn't the right contact for "
         "recruitment, let me know who is and I'll route it there."),
        "",
        "Thanks,",
        sender_name or "BridgeMD",
    ]
    return subject, "\n".join(lines)


_APPLICANT_COPY = {
    "accepted": ("A study team wants to move forward with your application",
                 "Good news - a study team reviewed your application and would "
                 "like to move forward. They may reach out using the contact "
                 "details you provided to arrange a screening visit."),
    "declined": ("Update on your trial application",
                 "Thanks for applying. This study wasn't a match this time. You "
                 "can explore other trials that may fit you better."),
    "screening": ("Your trial application: screening visit",
                  "A study team is arranging a screening visit for your "
                  "application. They'll confirm the details with you directly."),
    "enrolled": ("Your trial application: enrolled",
                 "Your application has advanced to enrolled. The study team will "
                 "guide you through the next steps."),
}


def build_applicant_message(lead, kind, link):
    """Notify the applicant that their application status changed."""
    subj, body = _APPLICANT_COPY.get(kind, _APPLICANT_COPY["accepted"])
    title = lead["title"] or lead["nct"] or "a clinical trial"
    lines = [
        f"Hi {lead['name'] or 'there'},",
        "",
        f"{body}",
        "",
        f"Trial: {title}",
        "",
        "See this application and message the study team here (no sign-in):",
        link,
        "",
        "This isn't medical advice and you can talk to your own doctor first.",
        "",
        "Sent via BridgeMD.",
    ]
    return subj, "\n".join(lines)


def build_apply_confirmation(lead, link, clinic_contacts=None):
    """BridgeMD emails the applicant their thread. They reply there, not by
    writing the clinic themselves. clinic_contacts is unused (kept for callers)."""
    _ = clinic_contacts
    title = lead["title"] or lead["nct"] or "a clinical trial"
    subject = human_subject("apply_confirmation", lead)
    lines = [
        f"Hi {lead['name'] or 'there'},",
        "",
        "Thanks for applying. Your application was sent to the study team. "
        "Message them here - no account needed. They will write you back on "
        "this same thread.",
        "",
        f"Trial: {title}",
    ]
    if lead["nct"]:
        lines.append(f"Reference number: {lead['nct']}")
    lines += [
        "",
        "Open your application:",
        link,
        "",
        "This isn't medical advice and you can talk to your own doctor first.",
        "",
        "Sent via BridgeMD.",
    ]
    return subject, "\n".join(lines)


def build_clinic_connect_message(lead, link, clinic_contacts=None):
    """BridgeMD emails the applicant their thread. No clinic address to write."""
    _ = clinic_contacts
    title = lead["title"] or lead["nct"] or "a clinical trial"
    subject = human_subject("clinic_connect", lead)
    lines = [
        f"Hi {lead['name'] or 'there'},",
        "",
        "Your application is with the study team. Please disregard any "
        "earlier email asking you to pick a screening time - that was sent "
        "by mistake and the link does not work.",
        "",
        "Message the study team here. No sign-in. They will reply on this "
        "same thread.",
        "",
        f"Trial: {title}",
    ]
    if lead["nct"]:
        lines.append(f"Reference number: {lead['nct']}")
    lines += [
        "",
        "Open your application:",
        link,
        "",
        "This isn't medical advice and you can talk to your own doctor first.",
        "",
        "Sent via BridgeMD.",
    ]
    return subject, "\n".join(lines)


def build_founder_connect_message(lead, sites, central, link):
    """A personal note from the founder with the study's own public contacts,
    so a new platform's reply lag never slows an applicant down. Contacts come
    straight from the study's ClinicalTrials.gov listing: phone numbers and
    recruitment inboxes, no eligibility claims, no amounts (COMPLIANCE.md)."""
    first = (lead["name"] or "there").split()[0]
    title = lead["title"] or lead["nct"] or "the study you applied to"
    nct = lead["nct"] or ""
    subject = human_subject("founder_connect", lead)
    lines = [
        f"Hi {first},",
        "",
        "I'm Harshil, the founder of BridgeMD. Your application for "
        f"{title} was sent to the study team. We're a new platform, so study "
        "teams can take longer to reply here, and I don't want that to slow "
        "you down.",
        "",
        "Here are the study's own contacts, straight from its public listing. "
        "Calling is usually the fastest way to get screened:",
        "",
    ]
    for site in sites:
        bits = [b for b in (site.get("phone"), site.get("email")) if b]
        where = ", ".join(b for b in (site.get("facility"), site.get("city")) if b)
        if where and bits:
            lines.append(f"- {where}: {' or '.join(bits)}")
    for c in central:
        bits = [b for b in (c.get("phone"), c.get("email")) if b]
        if bits:
            lines.append(f"- Study information line: {' or '.join(bits)}")
    lines += [
        "",
        f"When you call, give them the study number {nct} and say you applied "
        "through BridgeMD." if nct else
        "When you call, say you applied through BridgeMD.",
        "",
        "You can also keep messaging the study team on your thread:",
        link,
        "",
        "This isn't medical advice and you can talk to your own doctor first.",
        "",
        "Harshil Shah",
        "Founder, BridgeMD",
    ]
    return subject, "\n".join(lines)


def build_alert_message(alert, new_matches, link):
    """Notify a patient with a concise, useful weekly digest.
    `new_matches` accepts either [(nct, title)] or [{"nct","title"}, ...]."""
    what = alert["label"] or alert["condition"] or alert["intervention"] or "your interests"
    rows = []
    for m in new_matches or []:
        if isinstance(m, dict):
            nct = (m.get("nct") or "").strip()
            title = (m.get("title") or nct).strip()
        else:
            try:
                nct, title = m
            except Exception:
                continue
            nct = (nct or "").strip()
            title = (title or nct).strip()
        if nct:
            rows.append({"nct": nct, "title": title})
    n = len(rows)
    subject = f"BridgeMD weekly trial update: {n} new {what} match{'es' if n != 1 else ''}"
    lines = [
        "Hi there,",
        "",
        f"Here are the strongest new recruiting trial match{'es' if n != 1 else ''} "
        f"for your saved alert: {what}"
        + (f" near {alert['location']}." if alert["location"] else "."),
        "",
        "Top new matches:",
        "",
    ]
    for i, row in enumerate(rows, 1):
        nct = row["nct"]
        title = row["title"]
        lines += [
            f"{i}) {title}",
            f"   NCT: {nct}",
            f"   Details: https://clinicaltrials.gov/study/{nct}",
            "",
        ]
    lines += [
        "Review these and apply from your alerts page:",
        link,
        "",
        "We keep this to a weekly cadence and only include high-signal new matches.",
        "You're receiving this because you created a trial alert on BridgeMD.",
        "You can manage or turn alerts off from the link above.",
    ]
    return subject, "\n".join(lines)


def build_schedule_message(lead, schedule_url, apps_link):
    """Tell the applicant a study team invited them to book their screening call."""
    title = lead["title"] or lead["nct"] or "a clinical trial"
    subject = f"Book your screening call - {lead['nct'] or 'your trial application'}"
    lines = [
        f"Hi {lead['name'] or 'there'},",
        "",
        "Good news - a study team wants to move forward and invited you to book "
        "a screening call, the first quick step to see if you qualify.",
        "",
        f"Trial: {title}",
        "",
        "Pick a time that works for you here:",
        schedule_url,
        "",
        "You can review this application any time here (no sign-in):",
        apps_link,
        "",
        "This isn't medical advice and you can talk to your own doctor first.",
        "",
        "Sent via BridgeMD.",
    ]
    return subject, "\n".join(lines)


def build_schedule_sms(lead, schedule_url):
    """Short SMS for self-scheduling invites."""
    trial = lead["nct"] or "your trial application"
    return ("BridgeMD: A study team invited you to book your screening call for "
            f"{trial}. Book here: {schedule_url}")


def build_dm_message(lead, body, link, to="patient", clinic_contacts=None):
    """A new chat message notification. `to` is who receives the email."""
    _ = clinic_contacts
    title = lead["title"] or lead["nct"] or "your clinical trial application"
    if to == "patient":
        subject = human_subject("dm_to_patient", lead)
        opener = (f"Hi {lead['name'] or 'there'},\n\nThe study team sent you a "
                  f"message about {title}:")
        lines = [opener, "", f"  \"{body.strip()}\"", "",
                 "Reply here (no sign-in needed):", link,
                 "", "Sent via BridgeMD."]
        return subject, "\n".join(lines)
    # To the study team: the message and the person's contact details are in
    # the email itself. They reply to the applicant directly, no link.
    subject = human_subject("dm_to_site", lead)
    contact = [c for c in (_lead_text(lead, "email"), _lead_text(lead, "phone")) if c]
    lines = [
        f"{_lead_text(lead, 'name') or 'An applicant'} sent a message about "
        f"{title}:",
        "", f"  \"{body.strip()}\"", "",
        "Reply to them directly: " + (" or ".join(contact) or "no contact given"),
    ]
    if _lead_text(lead, "nct"):
        lines.append(f"Registry number: {_lead_text(lead, 'nct')}")
    lines += ["", FOUNDER_NAME, FOUNDER_TITLE, HELLO_EMAIL]
    return subject, "\n".join(lines)


def build_dm_sms(lead, link, to="patient"):
    """Short SMS for new direct-message notifications."""
    if to == "patient":
        trial = lead["nct"] or "your application"
        return (f"BridgeMD: New message from the study team about {trial}. "
                f"Reply here: {link}")
    trial = lead["nct"] or "application"
    return f"BridgeMD: New applicant message about {trial}. Reply here: {link}"


def _prep_lines(prep):
    """Normalize a prep checklist (string with one item per line, or a list)
    into email lines under a clear 'Please bring / prepare' heading."""
    if not prep:
        return []
    if isinstance(prep, str):
        items = [p.strip() for p in prep.splitlines() if p.strip()]
    else:
        items = [str(p).strip() for p in prep if str(p).strip()]
    if not items:
        return []
    out = ["", "Please bring / prepare before you come:"]
    out += [f"  - {it}" for it in items]
    return out


def build_visit_message(lead, when, location, link, invite_url="", prep=""):
    """Confirmation that a screening/visit was booked."""
    title = lead["title"] or lead["nct"] or "your clinical trial"
    subject = f"Visit booked - {lead['nct'] or 'your trial application'}"
    lines = [
        f"Hi {lead['name'] or 'there'},",
        "",
        f"The study team booked a visit for {title}:",
        "",
        f"  When: {when}",
    ]
    if location:
        lines.append(f"  Where: {location}")
    lines += _prep_lines(prep)
    if invite_url:
        lines += ["", "Add to calendar (.ics):", invite_url]
    lines += [
        "",
        "We'll remind you before it. See details any time here:",
        link,
        "",
        "If the time doesn't work, reply to the study team from your applications "
        "page and they'll reschedule.",
        "",
        "Sent via BridgeMD.",
    ]
    return subject, "\n".join(lines)


def build_reminder_message(lead, when, location, link, prep=""):
    """Reminder sent shortly before an upcoming visit."""
    title = lead["title"] or lead["nct"] or "your clinical trial"
    subject = f"Reminder: your visit is coming up - {lead['nct'] or 'trial'}"
    lines = [
        f"Hi {lead['name'] or 'there'},",
        "",
        f"A quick reminder about your upcoming visit for {title}:",
        "",
        f"  When: {when}",
    ]
    if location:
        lines.append(f"  Where: {location}")
    lines += _prep_lines(prep)
    lines += [
        "",
        "Showing up to this visit is the most important step - it's how the team "
        "confirms you can join. See details or message the team here:",
        link,
        "",
        "Sent via BridgeMD.",
    ]
    return subject, "\n".join(lines)


def build_reminder_sms(lead, when, location, link):
    """Short SMS sent before an upcoming visit."""
    trial = lead["nct"] or "your trial"
    msg = f"BridgeMD reminder: your {trial} visit is on {when}"
    if location:
        msg += f" at {location}"
    return f"{msg}. Details: {link}"


def build_nudge_message(lead, link):
    """Gentle check-in for an applicant who's gone quiet mid-process."""
    title = lead["title"] or lead["nct"] or "your clinical trial application"
    subject = f"Still interested? - {lead['nct'] or 'your trial application'}"
    lines = [
        f"Hi {lead['name'] or 'there'},",
        "",
        f"Just checking in on {title}. Your application is still active and the "
        "study team can move it forward whenever you're ready.",
        "",
        "If you're still interested, open your application here - and message the "
        "team with any questions:",
        link,
        "",
        "If your situation changed, you can withdraw from the same page. No "
        "pressure either way.",
        "",
        "Sent via BridgeMD.",
    ]
    return subject, "\n".join(lines)


def build_nudge_sms(lead, link):
    """Short SMS re-engagement nudge."""
    trial = lead["nct"] or "your trial application"
    return ("BridgeMD: Your application is still active for "
            f"{trial}. Continue or message the team: {link}")


def send_email(to_addr, subject, body):
    """Send via SMTP. Returns (ok, message)."""
    if not smtp_configured():
        return False, "SMTP is not configured."
    if not to_addr:
        return False, "No coordinator email address."
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    pw = os.environ.get("SMTP_PASS", "")
    sender = from_header()
    use_tls = os.environ.get("SMTP_TLS", "1") == "1"

    # Coordinators asked for no em dashes anywhere, and an email is the copy
    # that leaves the building.
    subject = sanitize_copy(subject or "")
    body = sanitize_copy(body or "")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["Reply-To"] = reply_to_header()
    msg["To"] = to_addr
    # Applicant is never Cc/Bcc. Their address, if any, lives in the body.
    msg.set_content(body)
    msg.add_alternative(branded_html(body), subtype="html")
    try:
        with smtplib.SMTP(host, port, timeout=20) as s:
            if use_tls:
                s.starttls(context=ssl.create_default_context())
            if user:
                s.login(user, pw)
            s.send_message(msg)
        return True, "Email sent."
    except Exception as e:  # noqa: BLE001
        return False, f"Couldn't send email: {e}"
