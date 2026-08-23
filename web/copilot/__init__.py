"""BridgeMD Copilot - a grounded, right-rail assistant for the study team.

Design stance (see the chat architecture discussion):
  * It reasons over the workflow, NOT patient identities. Everything runs on
    de-identified, coded operational data unless a lead was already revealed to
    the acting user.
  * Every tool is scoped by ``user_id`` to the studies that user has claimed, so
    the assistant can never surface another site's data.
  * Read tools answer automatically. Write intents (send a message, send a
    booking link, remind everyone stuck) never fire on their own: the assistant
    builds a PROPOSED action, the human reviews/edits it, and only an explicit
    confirm executes it - reusing the same helpers as the manual UI, re-scoped to
    the team, logged, and idempotent (see ``actions.py`` + ``/app/copilot/act``).
  * Answers are grounded: they cite the record they came from and refuse when the
    data isn't there. When ``LLM_API_KEY`` is unset the assistant still works via
    a deterministic responder (so the public demo runs with zero PHI).

Public surface:
  * ``register(app)`` - attach the ``/app/copilot/ask`` endpoint to the Flask app.
  * ``answer(user_id, query, context)`` - the orchestrator entry point.
  * ``actions`` - proposal builders + confirm-time validation for write actions.
  * ``drafts`` - the single drafting service every "Bridget writes this"
    surface calls (inbox replies, applicant nudges, blast bodies, notes).
"""

from . import actions  # noqa: F401
from . import digest  # noqa: F401
from . import drafts  # noqa: F401
from .agent import answer  # noqa: F401
from .web import register  # noqa: F401

__all__ = ["answer", "register", "actions", "digest", "drafts"]
