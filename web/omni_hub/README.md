# Omni

The prompt-configured intake inbox at `/omni`. A visitor describes their
business and where leads come from; Omni interviews them, builds an inbox from
a spec, seeds it with realistic conversations, and then works the inbox with
them: extracting fields, drafting replies, proposing actions and setup changes
that a person confirms.

It runs inside the BridgeMD Flask app (same process, same `bridgemd.db`) and is
registered with two lines in `app.py`: `import omni_hub` and
`omni_hub.register(app)` after `db.init_db()`. It has its own `om_*` tables,
its own routes and templates, and never reads `g.user`: a workspace is bound to
a browser cookie (`om_owner`) and reachable by anyone with its link.

## How it fits together

```
seeds/<template>.py   one TEMPLATE per business type: the spec, the interview
                      question bank, detection signals, reply ladders, guardrails,
                      sample inputs for the paste box, 14 to 20 seed conversations
                      with canned extraction values and drafts
spec.py               the canonical spec (connectors, fields, stages, views, rules,
                      approval policy, tone, agent, playbook, guardrails);
                      normalize(), validate(), apply_patch(), describe_patch()
builder.py            the interview state machine and build/rebuild/materialize
ai/interview.py       prefill from the first prompt, the opener, the summary
ai/refine.py          plain words -> patch ops ("add a field for ...", "flag anyone ...")
extract.py            canned -> keyword -> model extraction with provenance
ai/extract_llm.py     the model rung (only for fields the other rungs left unknown)
rules.py              flag / set priority / move / auto reject, with match counts
ai/compose.py         reply drafting: canned, ladders, optional model polish
ai/policy.py          routine / pricing / medical_legal / distress -> auto_send | review
actions.py            propose -> persist -> re-validate -> confirm, once
ingest.py             pasted emails, transcripts and uploads become conversations
inbox.py, filters.py  the read side: views, counts, search, the filter AST
routes.py             everything under /omni
```

The spec is the source of truth. `om_fields`, `om_views`, `om_rules` and
`om_sources` are projections rebuilt by `builder.materialize()`. Every change,
whether typed into the top bar, saved on the Setup page or toggled on the Rules
tab, is a patch op applied through `spec.apply_patch()`, which is also where
protected fields (fair housing, employment law, immigration status) are refused
with the template's own reason.

## Running and testing

```
cd web
../.venv/bin/python test_omni_hub.py                       # engine, routes, rules, drafting, builder
../.venv/bin/python -m omni_hub.seeds._check              # validate every template
OMNI_BASE=http://127.0.0.1:5055 OMNI_OUT=/tmp/omni_shots \
  ../.venv/bin/python tools/omni_shots.py pi_law_firm      # render every screen, DOM checks, screenshots
../.venv/bin/python -m omni_hub.ai.evals                   # extraction against canned values (needs a key)
```

Everything works with no LLM key. With `LLM_API_KEY` set (OpenAI-compatible,
see `match_trials.py`), the model prefills the interview from the prompt,
rewrites the assistant's lines, extracts fields the keyword rung could not,
polishes drafts under the workspace playbook and guardrails, and parses setup
changes the regexes could not. Every model call has a deterministic fallback
and every model output is sanitized.

## Adding a template

Copy `seeds/pi_law_firm.py`, keep the structure, add the key to
`seeds.TEMPLATE_KEYS`, and run `python -m omni_hub.seeds._check <key>` until it
prints `[ok ]`. No em dashes anywhere; the checker and `test_copy_sanitize.py`
both fail on them.

## Renaming

`OMNI_BRAND` in `__init__.py` is the one string behind every "Omni" in the
templates. The agent's name inside a workspace lives in the spec
(`agent.name`) and is per business.

## Not yet real

Connections are simulated: "Connect" marks a source connected and seeds that
source's conversations. The paste box and file upload are real. Real
connectors would replace `seeds.seed_workspace` per source with an ingester
writing `om_conversations` and `om_messages`; the schema already carries
`meta_json` and `simulated` for that.
