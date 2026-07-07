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
