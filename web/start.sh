#!/usr/bin/env bash
# Render start command: `bash start.sh` (rootDir web). See LITESTREAM.md.
#
# The live SQLite file is backed up continuously to an R2 bucket by Litestream. Without
# a persistent disk the file is gone after every deploy or restart, so this brings it
# back before the app starts. The app then runs under Litestream, so Render's stop
# signal reaches both: gunicorn finishes its requests first, then Litestream uploads
# the last writes before the instance goes away.
set -euo pipefail
cd "$(dirname "$0")"
CONFIG="${LITESTREAM_CONFIG:-litestream.yml}"

if [ ! -f "$DB_PATH" ]; then
  mkdir -p "$(dirname "$DB_PATH")"
  OPS_KEY="${OPS_READINESS_KEY:-${ALERTS_CRON_KEY:-}}"
  # During a deploy the previous instance is still serving. Ask it to hold new writes
  # and wait while its Litestream uploads what it already has, so the restore below is
  # complete. No answer means there is no previous instance: restore straight away.
  if [ -n "${RENDER_EXTERNAL_URL:-}" ] && [ -n "$OPS_KEY" ]; then
    wait_s=$(curl -fsS -m 10 -X POST -H "X-Ops-Key: $OPS_KEY" "$RENDER_EXTERNAL_URL/ops/handoff" 2>/dev/null \
      | python3 -c 'import json,sys; print(int(json.load(sys.stdin).get("wait", 0)))' 2>/dev/null) || wait_s=0
    if [ "${wait_s:-0}" -gt 0 ]; then
      echo "handoff: previous instance is holding writes, waiting ${wait_s}s for its last upload"
      sleep "$wait_s"
    else
      echo "handoff: no previous instance answered, restoring now"
    fi
  fi
  # No -if-replica-exists: a missing or unreachable backup has to stop the boot. Starting
  # on a new empty database would let Litestream back up the empty one as the latest.
  if ! ./litestream restore -config "$CONFIG" "$DB_PATH"; then
    echo "restore failed, not starting"
    if [ "${wait_s:-0}" -gt 0 ]; then
      curl -fsS -m 10 -X POST -H "X-Ops-Key: $OPS_KEY" "$RENDER_EXTERNAL_URL/ops/handoff?cancel=1" >/dev/null 2>&1 || true
    fi
    exit 1
  fi
  echo "restore: done"
fi

exec ./litestream replicate -config "$CONFIG" -exec "gunicorn -c gunicorn.conf.py app:app --workers 1 --threads 8 --max-requests 300 --max-requests-jitter 60 --timeout 120 --graceful-timeout 15 --bind 0.0.0.0:${PORT}"
