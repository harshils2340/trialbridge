"""Email helpers for coordinator notifications.

Sending is optional: if SMTP isn't configured via environment variables the
app falls back to a prefilled mailto: link, so the referral loop still works
with zero infrastructure. Set these to enable real sending:

    SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS,
    SMTP_FROM (defaults to SMTP_USER), SMTP_TLS ("1" default)
"""
import os
import smtplib
import ssl
from email.message import EmailMessage


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
        "Sent via TrialBridge.",
    ]
    return subject, "\n".join(lines)


def build_candidate_message(lead, link):
    """Notify a study site that a new de-identified candidate is waiting.
    Contains NO contact details - the site reveals those only after accepting
    via the secure link."""
    nct = lead["nct"] or "your study"
    subject = f"New candidate for {nct} - TrialBridge"
    lines = [
        "Hello,",
        "",
        "A patient has applied and may be eligible for your study. They are "
        "de-identified until you accept them.",
        "",
        f"Study: {lead['title'] or nct}",
    ]
    if lead["nct"]:
        lines.append(f"NCT: {lead['nct']}")
    if lead["condition"]:
        lines.append(f"Condition: {lead['condition']}")
    if lead["location"]:
        lines.append(f"Region: {lead['location']}")
    lines += [
        "",
        "Review the candidate and accept or decline (no login required):",
        link,
        "",
        "If you accept, the patient's consented contact details are unlocked so "
        "you can invite them to a screening visit.",
        "",
        "Sent via TrialBridge.",
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
        "See your applications and status any time here:",
        link,
        "",
        "This isn't medical advice and you can talk to your own doctor first.",
        "",
        "Sent via TrialBridge.",
    ]
    return subj, "\n".join(lines)


def build_alert_message(alert, new_matches, link):
    """Notify a patient that new trials matched their saved interest.
    new_matches: list of (nct, title)."""
    what = alert["label"] or alert["condition"] or alert["intervention"] or "your interests"
    n = len(new_matches)
    subject = (f"{n} new clinical trial{'s' if n != 1 else ''} matching {what}")
    lines = [
        "Hi,",
        "",
        f"{n} new recruiting trial{'s' if n != 1 else ''} just matched your "
        f"saved interest ({what})"
        + (f" near {alert['location']}" if alert["location"] else "") + ":",
        "",
    ]
    for nct, title in new_matches[:10]:
        lines.append(f"  - {title or nct} ({nct})")
    if n > 10:
        lines.append(f"  ...and {n - 10} more.")
    lines += [
        "",
        "See them and ask to be contacted here:",
        link,
        "",
        "You're getting this because you set up a trial alert on TrialBridge. "
        "Manage or turn off alerts from the link above.",
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
        "You can review this application any time at:",
        apps_link,
        "",
        "This isn't medical advice and you can talk to your own doctor first.",
        "",
        "Sent via TrialBridge.",
    ]
    return subject, "\n".join(lines)


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

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_addr
    msg.set_content(body)
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
