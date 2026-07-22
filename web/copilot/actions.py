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
from . import tools

# A generic, neutral reminder used for the "nudge everyone stuck in screening"
# bulk action. Kept truthful and non-coercive (compliance).
BULK_REMINDER_TEXT = ("Friendly reminder to book your screening visit using the "
                      "link we sent - let me know if you need another time.")


def build_message_proposal(user_id, lead_id, intent="check_in"):
    """Propose sending a drafted follow-up to one applicant."""
    d = tools.message_draft(user_id, lead_id, intent)
    if not d:
        return None
    if not d["revealed"]:
        return {"blocked": "You can message an applicant once they've been "
                           "accepted (revealed)."}
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
