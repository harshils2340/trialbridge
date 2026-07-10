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
        "Sent via BridgeMD.",
    ]
    return subject, "\n".join(lines)


def build_candidate_message(lead, link):
    """Notify a study site that a new de-identified candidate is waiting.
    Contains NO contact details - the site reveals those only after accepting
    via the secure link."""
    nct = lead["nct"] or "your study"
    subject = f"New candidate for {nct} - BridgeMD"
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
        "Sent via BridgeMD.",
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
        "Sent via BridgeMD.",
    ]
    return subj, "\n".join(lines)


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
        "You can review this application any time at:",
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


def build_dm_message(lead, body, link, to="patient"):
    """A new chat message notification. `to` is who receives the email."""
    title = lead["title"] or lead["nct"] or "your clinical trial application"
    if to == "patient":
        subject = f"New message from the study team - {lead['nct'] or 'your application'}"
        opener = (f"Hi {lead['name'] or 'there'},\n\nThe study team sent you a "
                  f"message about {title}:")
    else:
        subject = f"New message from an applicant - {lead['nct'] or 'application'}"
        opener = f"An applicant sent a message about {title}:"
    lines = [opener, "", f"  \"{body.strip()}\"", "",
             "Reply here:", link, "", "Sent via BridgeMD."]
    return subject, "\n".join(lines)


def build_dm_sms(lead, link, to="patient"):
    """Short SMS for new direct-message notifications."""
    if to == "patient":
        trial = lead["nct"] or "your application"
        return (f"BridgeMD: New message from the study team about {trial}. "
                f"Reply here: {link}")
    trial = lead["nct"] or "application"
    return f"BridgeMD: New applicant message about {trial}. Reply here: {link}"


def build_visit_message(lead, when, location, link, invite_url=""):
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


def build_reminder_message(lead, when, location, link):
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
