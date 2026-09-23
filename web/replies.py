"""Replies to our study-team letters, read by the system.

When a clinic answers a handoff email the answer lands at an address the app
receives (Resend inbound on reply.bridgemd.health) and Resend posts it to
/hooks/resend. This module decides what to do with it:

  * Every reply is forwarded to the operator's own inbox with the original
    sender as Reply-To, so a person still sees everything and can answer
    from Gmail as before.
  * An automatic reply (out of office) is read for the people it names
    ("please contact rps@linear.org.au") and the application is re-sent to
    them, the same letter, so an applicant is never parked behind someone's
    leave. What was done is written on the lead's timeline.
  * A human reply is logged on the lead's timeline as the study team having
    answered.

Nothing here decides eligibility or writes to the applicant. Addresses are
filtered the same way handoff recipients are (db.is_placeholder_site_email),
so a legal desk or a no-reply mailbox is never a forward target.
"""
import re

import db
import mailer

AUTO_SUBJECT_MARKERS = (
    "automatic reply", "auto reply", "auto-reply", "autoreply", "out of office",
    "out of the office", "ooo:", "away from the office", "on leave",
    "autosvar", "réponse automatique", "abwesenheit",
)
_REPLY_PREFIX = re.compile(
    r"^\s*(?:(?:re|fw|fwd|aw|wg|sv|tr|automatic reply|auto reply|auto-reply|"
    r"autoreply|out of office|ooo)\s*:\s*)+", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?:\+?\d[\d\s().-]{6,}\d)")
_UNTIL = re.compile(
    r"\b(?:until|till|through|back on|returning(?: on)?|return(?:s|ing)? on)\s+"
    r"([A-Za-z]{3,9}\.? \d{1,2}(?:st|nd|rd|th)?(?:,? \d{4})?|"
    r"\d{1,2}(?:st|nd|rd|th)? [A-Za-z]{3,9}\.?(?:,? \d{4})?|"
    r"\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?)", re.I)


def address_of(header_value):
    """'Peter Schrader <pschrader@linear.org.au>' -> 'pschrader@linear.org.au'."""
    m = _EMAIL.search(header_value or "")
    return m.group(0).lower() if m else ""


def name_of(header_value):
    v = (header_value or "").strip()
    if "<" in v:
        return v.split("<", 1)[0].strip().strip('"')
    return ""


def is_auto_reply(subject, headers=None):
    """Out-of-office and other machine replies, by header first (RFC 3834),
    then by the subject line."""
    h = {str(k).lower(): str(v).lower() for k, v in (headers or {}).items()}
    if h.get("auto-submitted", "no") not in ("", "no"):
        return True
    if h.get("x-auto-response-suppress") or h.get("x-autoreply") or \
            h.get("x-autorespond"):
        return True
    if h.get("precedence") in ("auto_reply", "bulk", "junk"):
        return True
    s = (subject or "").lower()
    return any(m in s for m in AUTO_SUBJECT_MARKERS)


def subject_core(subject):
    """Our original subject, with the reply and auto-reply prefixes removed."""
    return _REPLY_PREFIX.sub("", subject or "").strip()


def extract_emails(text):
    return [m.lower() for m in _EMAIL.findall(text or "")]


def extract_phones(text):
    out = []
    for m in _PHONE.findall(text or ""):
        digits = re.sub(r"\D", "", m)
        if 7 <= len(digits) <= 15:
            out.append(" ".join(m.split()))
    return out


def alternate_contacts(text, exclude=()):
    """People the reply says to contact instead: real mailboxes, never the
    sender's own, never ours, never a legal or no-reply desk. Order kept,
    duplicates dropped."""
    ex = {e.strip().lower() for e in exclude if e}
    ex |= {mailer.HELLO_EMAIL.lower(), mailer.address_of_from().lower()}
    out = []
    for e in extract_emails(text):
        if e in ex or e in out:
            continue
        if db.is_placeholder_site_email(e):
            continue
        out.append(e)
    return out


def leave_until(text):
    m = _UNTIL.search(text or "")
    return m.group(1).strip() if m else ""


def _plain(msg):
    text = (msg.get("text") or "").strip()
    if text:
        return text
    html = msg.get("html") or ""
    html = re.sub(r"<(br|/p|/div|/li|/tr)\s*/?>", "\n", html, flags=re.I)
    return re.sub(r"<[^>]+>", " ", html).strip()


def handle_received(msg, send_fn, owner_email, reach=0):
    """Act on one inbound message.

    msg: {id, from, to, subject, text, html, headers}. send_fn(to, subject,
    body, reply_to=None) -> bool. Returns a dict describing what happened, and
    is safe to call twice with the same message id (the second call is a
    no-op)."""
    sender = address_of(msg.get("from"))
    sender_name = name_of(msg.get("from"))
    subject = (msg.get("subject") or "").strip()
    body = _plain(msg)
    # Our own mail coming back (a forward we sent, a bounce notice from us)
    # must never be forwarded again.
    if sender and (sender == mailer.HELLO_EMAIL.lower()
                   or sender == mailer.address_of_from().lower()
                   or sender.endswith("@reply.bridgemd.health")):
        db.record_inbound_email(msg.get("id") or "", sender, subject, None,
                                "own", [])
        return {"ok": True, "ignored": "own address"}
    auto = is_auto_reply(subject, msg.get("headers"))
    kind = "auto_reply" if auto else "reply"
    # Claim the message before doing anything with it, so a redelivered
    # webhook can never send the application or the operator copy twice.
    if not db.record_inbound_email(msg.get("id") or "", sender, subject, None,
                                   kind, []):
        return {"ok": True, "duplicate": True}
    lead = db.find_lead_by_clinic_recipient(sender, subject_core(subject)) \
        if sender else None

    forwarded_to = []
    if auto and lead:
        alternates = alternate_contacts(body, exclude=[sender])
        when = leave_until(body)
        if alternates:
            letter_subject, letter = mailer.build_candidate_message(
                lead, None,
                clinic={"facility": db.lead_clinic_label(lead)},
                reach=reach)
            # Keep the subject the original recipient saw, so the person they
            # named sees the same thread, not a second one.
            for r in db.lead_clinic_notify(lead):
                if (r.get("email") or "").lower() == sender and r.get("subject"):
                    letter_subject = r["subject"]
                    break
            note = (f"Forwarded from {sender}, whose automatic reply"
                    + (f" says they are away until {when}" if when else "")
                    + " named you as the contact.")
            letter = letter.replace("Hi,\n\n", f"Hi,\n\n{note}\n\n", 1)
            for alt in alternates:
                if send_fn(alt, letter_subject, letter):
                    forwarded_to.append(alt)
            if forwarded_to:
                db.append_clinic_notify(lead["id"], [
                    {"email": a, "facility": db.lead_clinic_label(lead),
                     "source": "auto_reply", "subject": letter_subject}
                    for a in forwarded_to])
                db.add_lead_event(
                    lead["id"],
                    f"{sender} is away" + (f" until {when}" if when else "")
                    + "; application re-sent to " + ", ".join(forwarded_to))
        else:
            db.add_lead_event(
                lead["id"],
                f"Automatic reply from {sender}"
                + (f" (away until {when})" if when else "")
                + " named no other contact; operator notified")
    elif lead:
        db.add_lead_event(lead["id"], f"The study team replied from {sender}")

    db.update_inbound_email(msg.get("id") or "", lead["id"] if lead else None,
                            kind, forwarded_to)

    # The operator's copy: what this is, what was done, then the reply itself.
    context = []
    if lead:
        context.append(f"About {lead['name'] or 'an applicant'}'s application to "
                       f"{lead['title'] or lead['nct'] or 'a study'}.")
    if auto:
        context.append("This is an automatic reply."
                       + (f" Application re-sent to {', '.join(forwarded_to)}."
                          if forwarded_to else
                          " It named no other contact, so nothing was re-sent."))
    if auto:
        fwd_subject = f"Auto-reply from {sender_name or sender}: {subject_core(subject) or subject}"
    elif lead:
        fwd_subject = f"{sender_name or sender} replied: {subject_core(subject) or subject}"
    else:
        # Ordinary mail to the inbox, not about an application: pass it
        # through under its own subject.
        fwd_subject = subject or f"Email from {sender_name or sender}"
    fwd_body = "\n".join(context + ["", f"From: {msg.get('from') or sender}",
                                    "", body])
    copied = send_fn(owner_email, fwd_subject, fwd_body, reply_to=sender or None)
    return {"ok": True, "kind": kind, "lead_id": lead["id"] if lead else None,
            "forwarded_to": forwarded_to, "operator_copy": bool(copied),
            "operator": owner_email}
