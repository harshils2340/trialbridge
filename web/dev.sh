#!/usr/bin/env bash
# Keep-alive dev server for BridgeMD.
#
# Runs app.py under a supervisor loop so the local server NEVER stays down: if it
# crashes or exits for any reason, it is restarted automatically. Templates hot-
# reload (FLASK_DEBUG=1) so edits show on refresh without a restart.
#
#   Start (detached, survives closing the terminal):
#     nohup bash matcher/web/dev.sh >/tmp/bmd_dev_supervisor.log 2>&1 & disown
#   Start (foreground, Ctrl-C to stop):
#     bash matcher/web/dev.sh
#   Stop:
#     bash matcher/web/dev.sh stop
#
# Env overrides: PORT (default 5050), DB_PATH, SITE_DEMO, LOG.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/../.venv/bin/python"
PORT="${PORT:-5050}"
LOG="${LOG:-/tmp/bmd_dev_server.log}"
LOCK="${LOCK:-/tmp/bmd_dev_supervisor.pid}"

export PORT
export DB_PATH="${DB_PATH:-/tmp/bmd_dev.db}"
export SITE_DEMO="${SITE_DEMO:-0}"
export FLASK_DEBUG="${FLASK_DEBUG:-1}"

free_port() {
  local pids
  pids="$(lsof -ti tcp:"$PORT" 2>/dev/null || true)"
  [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
}

stop() {
  # Kill a running supervisor (recorded in LOCK) and free the port.
  if [ -f "$LOCK" ]; then
    local sp
    sp="$(cat "$LOCK" 2>/dev/null || true)"
    [ -n "$sp" ] && kill "$sp" 2>/dev/null || true
    rm -f "$LOCK"
  fi
  free_port
  echo "[dev.sh] stopped (port $PORT freed)"
}

if [ "${1:-}" = "stop" ]; then
  stop
  exit 0
fi

# Only one supervisor at a time.
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  echo "[dev.sh] supervisor already running (pid $(cat "$LOCK")). 'stop' first to restart."
  exit 0
fi
echo $$ > "$LOCK"

# Clean up on exit so a fresh start isn't blocked by a stale lock.
trap 'CHILD=${CHILD:-}; [ -n "$CHILD" ] && kill "$CHILD" 2>/dev/null; rm -f "$LOCK"; exit 0' INT TERM

free_port
echo "[dev.sh] supervising BridgeMD on http://127.0.0.1:$PORT  (log: $LOG)"

while true; do
  echo "[dev.sh] $(date '+%Y-%m-%d %H:%M:%S') starting server (port $PORT)..." >>"$LOG"
  "$PY" "$HERE/app.py" >>"$LOG" 2>&1 &
  CHILD=$!
  wait "$CHILD"
  code=$?
  echo "[dev.sh] $(date '+%Y-%m-%d %H:%M:%S') server exited (code $code); restarting in 1s" >>"$LOG"
  free_port
  sleep 1
done
