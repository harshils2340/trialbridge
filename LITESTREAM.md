# Dropping the Starter plan without losing real patient data

BridgeMD was on Render's Starter plan ($7/month flat) only because a Render Disk, used to
keep the SQLite database across redeploys, requires a paid plan. The app itself barely
uses the compute: `/alerts/run` and `/reminders/run` are hit every 30 minutes by two tiny
cron jobs, and the owner rarely redeploys. The $7 was buying disk persistence, not CPU.

[Litestream](https://litestream.io) replicates a SQLite file continuously to any
S3-compatible bucket and restores the latest snapshot before the app starts, with zero
change to how the app reads or writes the file. `db.py`, all 12,000-some lines of it, and
every other module that touches `DB_PATH`, is untouched. That is the whole point: this
database holds real people's clinical-trial applications, and rewriting a database layer
that size to a different SQL dialect under time pressure is how real data gets lost. This
instead keeps the exact same SQLite file and queries, and only changes where the
durability comes from.

## Rollout, in two phases, with a real check between them

**Phase 1, already in this branch, zero risk.** The Starter plan and its disk stay
exactly as they are. `litestream.yml` and the build/start command changes in
`render.yaml` make the app also stream every write to a free bucket, alongside the disk
that's already working. Nothing about the live app changes behaviorally. This can sit
here for a few days with no downside.

**Verify before doing anything else.** Run `python verify_litestream_restore.py` (from a
shell with the deployed env vars, e.g. `render ssh bridgemd`). It restores the bucket's
latest snapshot to a temp file and compares row counts against the live database on the
tables that matter (`users`, `patient_users`, `leads`, `lead_events`), the same tables
`restore_drill.py` already checks for a local backup. It must say `PASS` with matching
counts, more than once, on different days, before phase 2.

**Phase 2, only after that.** In `render.yaml`: change `plan: starter` to `plan: free`
and delete the `disk:` block. Nothing else changes; the comments in the file mark exactly
these two edits. On the next deploy, Litestream restores the latest snapshot into
`DB_PATH` before gunicorn starts, so the app comes up with the same data it had before,
now on the free compute tier with no disk. The $7/month goes away.

## What you need to provide (I can't create this for you)

A free S3-compatible bucket and an API key scoped to it. Cloudflare R2 is the natural
choice: 10 GB free storage, no egress fee, comfortably larger than this database. Create
the bucket, create an API token scoped to only that bucket, and set four values in the
Render dashboard's Environment tab for the `bridgemd` service:

- `LITESTREAM_BUCKET` — the bucket name
- `LITESTREAM_ENDPOINT` — `https://<account-id>.r2.cloudflarestorage.com`
- `LITESTREAM_ACCESS_KEY_ID` / `LITESTREAM_SECRET_ACCESS_KEY` — from the API token

Once those are set and this branch is deployed, phase 1 is live. Ping me (or whichever
session is working on this) to run the verification and move to phase 2.
