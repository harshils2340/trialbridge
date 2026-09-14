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


def _lead_text(lead, key):
    try:
        val = lead[key]
    except (KeyError, IndexError, TypeError):
        return ""
    return ("" if val is None else str(val)).strip()


def build_candidate_message(lead, link, clinic=None):
    """Notify study contacts that someone applied. Applicant email is in the
    body so they can write back. The applicant is never on To/CC."""
    nct = _lead_text(lead, "nct") or "your study"
    clinic = clinic or {}
    facility = (clinic.get("facility") or "").strip()
    if not facility:
        facility = _lead_text(lead, "site")
    where = facility or "this study"
    applicant_email = _lead_text(lead, "email")
    applicant_name = _lead_text(lead, "name")
    subject = f"New applicant for {nct} - BridgeMD"
    lines = [
        "Hello,",
        "",
        f"A patient applied on BridgeMD for {where}. They asked to be contacted "
        "about this study. Reply to them directly at the address below. They "
        "are not copied on this email.",
        "",
        f"Study: {_lead_text(lead, 'title') or nct}",
    ]
    if _lead_text(lead, "nct"):
        lines.append(f"NCT: {_lead_text(lead, 'nct')}")
    if _lead_text(lead, "condition"):
        lines.append(f"Condition: {_lead_text(lead, 'condition')}")
    if _lead_text(lead, "location"):
        lines.append(f"Patient area: {_lead_text(lead, 'location')}")
    if facility:
        lines.append(f"Listed site: {facility}")
    lines += ["", "Applicant"]
    if applicant_name:
        lines.append(f"Name: {applicant_name}")
    lines.append(f"Email: {applicant_email or 'not provided'}")
    if link:
        lines += [
            "",
            "Full application (no login required):",
            link,
        ]
    lines += [
        "",
        "Sent via BridgeMD.",
        f"Find recruiting trials near you: {FINDER_URL}",
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
        f"<a href=\"{FINDER_URL}\" style=\"color:#1257b0;font-weight:700;"
        "text-decoration:none;\">Find a trial that fits</a>"
        f"<div style=\"margin-top:4px;\"><a href=\"{FINDER_URL}\" "
        f"style=\"color:#1257b0;\">{FINDER_URL}</a></div>"
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
    subject = f"New application: {nct} - BridgeMD"
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


def build_apply_confirmation(lead, link):
    """Warm, job-application-style confirmation sent to the applicant right after
    they apply. Sets expectations (someone will respond) and respects their time."""
    title = lead["title"] or lead["nct"] or "a clinical trial"
    subj_title = title if len(title) <= 60 else title[:57].rstrip() + "..."
    subject = f"We got your application - {subj_title}"
    lines = [
        f"Hi {lead['name'] or 'there'},",
        "",
        "Thanks for applying - your application was received and sent to the "
        "study team. We know your time matters, so here's exactly what happens "
        "next:",
        "",
        f"Trial: {title}",
    ]
    if lead["nct"]:
        lines.append(f"Reference number: {lead['nct']}")
    lines += [
        "",
        "What's next:",
        "  - The study team reviews your application.",
        "  - Someone will respond shortly - typically within a few business days.",
        "  - You'll hear from us here and in your BridgeMD account either way.",
        "",
        "You don't need to do anything right now. You can check your status or "
        "message the study team any time here (no sign-in):",
        link,
        "",
        "This isn't medical advice and you can talk to your own doctor first.",
        "",
        "Sent via BridgeMD.",
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
    title = lead["title"] or lead["nct"] or "your clinical trial application"
    if to == "patient":
        subject = f"New message from the study team - {lead['nct'] or 'your application'}"
        opener = (f"Hi {lead['name'] or 'there'},\n\nThe study team sent you a "
                  f"message about {title}:")
        reply = "Reply to the study team here (no sign-in needed):"
    else:
        subject = f"New message from an applicant - {lead['nct'] or 'application'}"
        opener = f"An applicant sent a message about {title}:"
        reply = "Reply here:"
    lines = [opener, "", f"  \"{body.strip()}\"", "", reply, link]
    if to == "patient" and clinic_contacts:
        lines += ["", "You can also reach the study clinic directly:"]
        for c in clinic_contacts[:4]:
            if not isinstance(c, dict):
                continue
            bit = "  - "
            if c.get("facility"):
                bit += f"{c['facility']} "
            if c.get("email"):
                bit += c["email"]
            if bit.strip() != "-":
                lines.append(bit.rstrip())
    lines += ["", "Sent via BridgeMD."]
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
    sender = os.environ.get("SMTP_FROM", user)
    use_tls = os.environ.get("SMTP_TLS", "1") == "1"

    # Coordinators asked for no em dashes anywhere, and an email is the copy
    # that leaves the building.
    subject = sanitize_copy(subject or "")
    body = sanitize_copy(body or "")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
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
