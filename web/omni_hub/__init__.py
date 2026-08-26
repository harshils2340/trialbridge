"""Omni: a prompt-configured, template-driven intake inbox, served at /omni.

Describe your business and where leads come from; Omni interviews you, builds
an inbox from a spec (connectors, fields, stages, views, rules, tone, approval
policy, guardrails), seeds it with realistic conversations, and then works the
inbox with you: extracting fields, drafting replies, proposing actions and
setup changes that a person confirms.

The package is self-contained: its own ``om_*`` tables (schema.py), its own
routes (routes.py) registered with the same ``register(app)`` closure pattern
the copilot uses, and no dependency on ``g.user``. It reuses the app's DB
connection, the em dash sanitizer, and the LLM plumbing in match_trials.

Public surface:
  * ``register(app)``: create the schema and attach the /omni routes.
  * ``OMNI_BRAND``: the one string to change for a rebrand.
"""

import os as _os
import sys as _sys

# copy_sanitize and match_trials live at the repo root. app.py puts the root on
# sys.path; standalone tools (the seed checker, evals) import this package
# directly, so make the root importable here too, appended so the app's own
# ordering is untouched.
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
if _ROOT not in _sys.path:
    _sys.path.append(_ROOT)

OMNI_BRAND = "Omni"


def register(app):
    from . import schema
    from . import routes

    schema.init_schema()
    routes.register(app)
    app.jinja_env.globals["OMNI_BRAND"] = OMNI_BRAND


__all__ = ["register", "OMNI_BRAND"]
