"""Copilot ACTIONS - the "does work for you" layer.

The assistant never acts autonomously. It builds a PROPOSED action (persisted via
``db.create_copilot_action``) and returns it to the rail, where the human reviews
and confirms. Only then does ``/app/copilot/act`` execute it (see app.py), reusing
the same vetted send/schedule helpers as the manual UI.

This module owns two responsibilities:
  * ``build_*_proposal`` - turn an intent + context into a stored proposal.
  * ``load_valid`` - re-validate a token at confirm time (ownership, status,
    expiry). The action's target is read from the stored proposal, never from the
    client, so a confirmed action can't be redirected to another patient.
"""

import db

from . import drafts, tools

# A generic, neutral reminder used for the "nudge everyone stuck in screening"
# bulk action. Kept truthful and non-coercive (compliance).
BULK_REMINDER_TEXT = ("Friendly reminder to book your screening visit using the "
                      "link we sent - let me know if you need another time.")

# Re-consent nudge. Never states what changed or pressures - just that an updated
# form exists and will be reviewed together at the visit (IRB/ICF compliant).
RECONSENT_REMINDER_TEXT = (
    "Hi - there's an updated consent form for your study. At your next visit "
    "we'll go through what changed together and re-sign. Reply here anytime if "
    "you have questions before then.")


def build_message_proposal(user_id, lead_id, intent="check_in"):
    """Propose sending a drafted follow-up to one applicant."""
    d = tools.message_draft(user_id, lead_id, intent)
    if not d:
        return None
    if not d["revealed"]:
        return {"blocked": "You can message an applicant once they've been "
                           "accepted (revealed)."}
    if d.get("opted_out"):
        return {"blocked": "This applicant has opted out of messages, so I can't "
                           "send to them."}
    token = db.create_copilot_action(
        user_id, "send_message", lead_id,
        {"text": d["text"], "label": d["label"], "url": d["url"]})
    return {"kind": "send_message", "token": token, "target": d["label"],
            "text": d["text"], "url": d["url"], "editable": True,
            "confirm_label": "Send message"}


def build_booking_proposal(user_id, lead_id):
    """Propose sending the team's booking link to one applicant."""
    lead = tools.own_lead(user_id, lead_id)
    if not lead:
        return None
    url = (db.get_claim_schedule_url(user_id, lead["nct"])
           or db.get_site_calendar_url(user_id))
    lbl = tools.label(lead)
    if not url:
        return {"blocked": "Set your booking calendar in Settings first, then I "
                           "can send it with one click."}
    token = db.create_copilot_action(
        user_id, "send_booking_link", lead["id"],
        {"url": url, "label": lbl, "url_ref": tools._url(lead)})
    return {"kind": "send_booking_link", "token": token, "target": lbl,
            "url": url, "editable": False, "confirm_label": "Send booking link"}


def build_bulk_reminder_proposal(user_id, days=5):
    """Propose sending a booking reminder to everyone stuck in screening."""
    ids = tools.stuck_lead_ids(user_id, days)
    if not ids:
        return {"blocked": "No one is stuck in screening that I can message right "
                           "now."}
    token = db.create_copilot_action(
        user_id, "bulk_booking_reminder", None,
        {"lead_ids": ids, "text": BULK_REMINDER_TEXT, "count": len(ids)})
    return {"kind": "bulk_booking_reminder", "token": token,
            "target": f"{len(ids)} applicant(s) stuck in screening",
            "text": BULK_REMINDER_TEXT, "count": len(ids), "editable": True,
            "confirm_label": f"Send to {len(ids)}"}


def build_reconsent_reminder_proposal(user_id):
    """Propose a neutral re-consent nudge to everyone with an upcoming re-consent
    visit (revealed + not opted out). Grounded in the calendar."""
    ids = tools.reconsent_lead_ids(user_id)
    if not ids:
        return {"blocked": "No one has an upcoming re-consent visit I can message "
                           "right now."}
    token = db.create_copilot_action(
        user_id, "reconsent_reminder", None,
        {"lead_ids": ids, "text": RECONSENT_REMINDER_TEXT, "count": len(ids)})
    return {"kind": "reconsent_reminder", "token": token,
            "target": f"{len(ids)} participant(s) due for re-consent",
            "text": RECONSENT_REMINDER_TEXT, "count": len(ids), "editable": True,
            "confirm_label": f"Send to {len(ids)}"}


def load_valid(user_id, token):
    """Return (row, None) for a still-actionable proposal owned by this user, or
    (None, error_message). Marks expired proposals canceled."""
    row = db.get_copilot_action(token)
    if not row:
        return None, "That action link is invalid."
    if row["user_id"] != user_id:
        return None, "That action isn't yours."
    if row["status"] != "proposed":
        return None, "That action was already handled."
    if db.copilot_action_expired(row):
        db.mark_copilot_action(token, "canceled")
        return None, "That action expired - just ask me again."
    return row, None


def build_blast_proposal(user_id, nct="", stage="", tag="", idle_days=None,
                         text=""):
    """Propose messaging a filtered group inside one study.

    The audience is resolved by db.blast_audience - the same function the manual
    composer uses - so the assistant cannot reach anyone the UI would refuse to
    reach. The resolved lead ids are frozen into the proposal, so confirming
    later sends to exactly the people the coordinator was shown."""
    nct, mode, value, leads, err = tools.blast_targets(
        user_id, nct=nct, stage=stage, tag=tag, idle_days=idle_days)
    if err:
        return {"blocked": err}
    if not leads:
        return {"blocked": "Nobody in that study matches that description right "
                           "now."}
    label = db.blast_audience_label(mode, value, len(leads))
    body = (text or "").strip() or drafts.draft("blast", {
        "study": (leads[0]["title"] or nct), "audience_summary": label,
        "count": len(leads)})
    token = db.create_copilot_action(
        user_id, "blast", None,
        {"lead_ids": [l["id"] for l in leads], "text": body, "nct": nct,
         "mode": mode, "value": value, "label": label, "count": len(leads)})
    return {"kind": "blast", "token": token,
            "target": f"{len(leads)} applicant(s) - {label}",
            "text": body, "count": len(leads), "editable": True,
            "confirm_label": f"Send to {len(leads)}"}


def build_handoff_proposal(user_id, cover=""):
    """Propose handing this user's whole open queue to a teammate.

    Reassigning someone's entire workload is exactly the kind of thing that must
    not happen on a misheard sentence, so it goes through the same confirm step
    as a send - with the real count shown before anything moves."""
    want = (cover or "").strip().lower()
    members = [m for m in db.mentionable_members(user_id)
               if m["user_id"] != user_id]
    if not members:
        return {"blocked": "Add a teammate to your workspace first, then I can "
                           "hand your queue over."}
    match = None
    if want:
        hits = [m for m in members
                if want in m["handles"] or want in m["name"].lower()]
        if len(hits) == 1:
            match = hits[0]
    if match is None:
        names = ", ".join(m["name"] for m in members[:5])
        return {"blocked": f"Who should cover for you? Your teammates are: "
                           f"{names}."}
    n = db.open_queue_count(user_id)
    token = db.create_copilot_action(
        user_id, "handoff_coverage", None,
        {"cover_user_id": match["user_id"], "cover_name": match["name"],
         "count": n})
    return {"kind": "handoff_coverage", "token": token,
            "target": f"{match['name']} ({n} open conversation"
                      f"{'' if n == 1 else 's'})",
            "editable": False,
            "confirm_label": f"Hand off to {match['name'].split()[0]}"}
