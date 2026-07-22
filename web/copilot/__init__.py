"""BridgeMD Copilot - a grounded, right-rail assistant for the study team.

Design stance (see the chat architecture discussion):
  * It reasons over the workflow, NOT patient identities. Everything runs on
    de-identified, coded operational data unless a lead was already revealed to
    the acting user.
  * Every tool is scoped by ``user_id`` to the studies that user has claimed, so
    the assistant can never surface another site's data.
  * Read tools answer automatically; write intents only ever DRAFT (never send),
    so a human stays in the loop.
  * Answers are grounded: they cite the record they came from and refuse when the
    data isn't there. When ``LLM_API_KEY`` is unset the assistant still works via
    a deterministic responder (so the public demo runs with zero PHI).

Public surface:
  * ``register(app)`` - attach the ``/app/copilot/ask`` endpoint to the Flask app.
  * ``answer(user_id, query, context)`` - the orchestrator entry point.
"""

from .agent import answer  # noqa: F401
from .web import register  # noqa: F401

__all__ = ["answer", "register"]
