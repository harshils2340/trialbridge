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

**Phase 1, done (live since Oct 5 2026).** Starter plan and disk unchanged, Litestream
streaming every write to the R2 bucket `outset`, path `bridgemd`. Its logs show snapshots
and WAL segments uploading; the only errors are occasional `checkpoint: database is locked`,
which Litestream retries on the next sync.

**How it runs without a disk (`web/start.sh`, the service's start command).**
- *Boot:* if `DB_PATH` is missing, restore it from R2 first. A missing or unreachable
  backup stops the boot instead of starting on an empty database, which Litestream would
  then back up as the newest copy.
- *Deploys:* without a disk Render starts the new instance while the old one still serves,
  so the new one first POSTs `/ops/handoff` to the old one. The old one holds every write
  (POST and friends get a 307 back to the same URL after 10 seconds, which the browser
  replays to whichever instance is live; the alert/reminder sweeps get a 503) and the new
  one waits 75 seconds, one sync interval plus upload time, before restoring. Reads keep
  working throughout. Writes made inside a GET on the old instance during those seconds
  (search cache, page analytics) are the only thing not carried over.
- *Stops:* gunicorn runs under `litestream replicate -exec`, so Render's SIGTERM reaches
  both: gunicorn drains (15s graceful timeout) and Litestream then uploads the last writes.
  Only a hard crash (out of memory, kernel kill) can lose up to the 60-second sync interval.
- *Restore time:* a snapshot every 6 hours (`snapshot-interval`), so a boot replays at
  most 6 hours of changes on top of a ~15 MB snapshot.

All four paths were tested end to end locally with the same Litestream release and a file
replica: final upload on stop, handoff with a write made just before it, the 307 hold, and
a failed restore refusing to start and releasing the old instance.

**Verify before switching.** Run `python verify_litestream_restore.py` in the service's
shell (`render ssh trialbridge`, then `cd /opt/render/project/src/web`). It restores the
bucket's latest copy to a temp file and compares row counts on `users`, `patient_users`,
`leads` and `lead_events` with the live file. It must say `PASS`. Free instances have no
shell, so this is the last chance to run it; afterwards `/ops/readiness` (keyed) reports
the same counts under `infra.core_rows`.

**Phase 2, the switch** (dashboard, `trialbridge` service):
1. Settings, Start Command: `bash start.sh`. Deploy. Nothing changes yet: with the disk
   attached `DB_PATH` exists, so start.sh skips the restore.
2. Environment: `DB_PATH=/opt/render/project/src/web/data/bridgemd.db`. Deploy. This
   boot restores from R2 while the old file stays untouched on the disk, so it proves the
   restore with a way back (set `DB_PATH` back). Compare `/ops/readiness` counts.
3. Disks: delete `bridgemd-data`. Settings, Instance Type: Free.
4. Delete the `trialbridge` and `trialbridge-1` cron jobs; `.github/workflows/scheduler.yml`
   runs the same sweeps for free and pings `/healthz` every 10 minutes (needs the repo
   secret `ALERTS_CRON_KEY`).

**Free plan limits to keep in mind.** 750 instance hours a month per workspace covers this
one service running 24/7 (744 hours in a 31-day month), so a second always-on free service
would run the workspace out and Render suspends every free service until the 1st.
0.1 CPU instead of 0.5: typical use is about 0.015, but every request takes up to five times
longer. Bandwidth is billed the same on either plan.

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
