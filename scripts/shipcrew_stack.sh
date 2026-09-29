#!/usr/bin/env bash
# Boot an isolated omnigent server + host for shipcrew work (no Vite).
#
# Usage: scripts/shipcrew_stack.sh {start|stop|status} [STATE_DIR] [PORT]
# State (DB, config, logs, pidfiles) lives in STATE_DIR so the stack never
# touches ~/.omnigent. Telemetry is forced off (env + config.yaml).
set -euo pipefail

cmd="${1:-start}"
STATE_DIR="$(realpath -m "${2:-${SHIPCREW_STATE_DIR:-$PWD/.shipcrew-state}}")"
PORT="${3:-${SHIPCREW_PORT:-16767}}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
URL="http://127.0.0.1:${PORT}"

export OMNIGENT_DATA_DIR="$STATE_DIR/data"
export OMNIGENT_CONFIG_HOME="$STATE_DIR/config"
export OMNIGENT_DATABASE_URI="sqlite:///$STATE_DIR/data/chat.db"
export OMNIGENT_URL="$URL"
export OMNIGENT_ANALYTICS=0 OMNIGENT_DISABLE_TELEMETRY=true DO_NOT_TRACK=1
export OMNIGENT_NO_UPDATE_CHECK=1
# Launched from inside Claude Code, these leak into every spawned `claude`
# (child-session mode, a foreign session id); agents must start clean.
# ANTHROPIC_API_KEY is dropped so `claude` uses the subscription login.
for v in $(env | grep -oE '^(CLAUDECODE|CLAUDE_[A-Z_]+|ANTHROPIC_API_KEY)=' | tr -d =); do
  unset "$v"
done

start() {
  mkdir -p "$OMNIGENT_DATA_DIR" "$OMNIGENT_CONFIG_HOME" "$STATE_DIR/logs"
  grep -qs '^telemetry: false' "$OMNIGENT_CONFIG_HOME/config.yaml" \
    || printf 'telemetry: false\n' >>"$OMNIGENT_CONFIG_HOME/config.yaml"
  cd "$REPO_ROOT"
  setsid uv run --no-sync omnigent --log-to-stderr server --no-open \
    --host 127.0.0.1 --port "$PORT" \
    --database-uri "$OMNIGENT_DATABASE_URI" \
    --artifact-location "$OMNIGENT_DATA_DIR/artifacts" \
    >"$STATE_DIR/logs/server.log" 2>&1 &
  echo $! >"$STATE_DIR/server.pid"
  for _ in $(seq 1 120); do
    curl -fsS "$URL/health" >/dev/null 2>&1 && break
    sleep 0.5
  done
  curl -fsS "$URL/health" >/dev/null || { echo "server failed; see $STATE_DIR/logs/server.log"; exit 1; }
  setsid uv run --no-sync omnigent --log-to-stderr host --server "$URL" --no-open --non-interactive \
    >"$STATE_DIR/logs/host.log" 2>&1 &
  echo $! >"$STATE_DIR/host.pid"
  echo "server $URL (pid $(cat "$STATE_DIR/server.pid")), host pid $(cat "$STATE_DIR/host.pid")"
  echo "logs: $STATE_DIR/logs"
}

stop() {
  for p in host server; do
    f="$STATE_DIR/$p.pid"
    [[ -f $f ]] || continue
    # setsid made each process a group leader: kill the whole group
    kill -TERM -- "-$(cat "$f")" 2>/dev/null || true
    rm -f "$f"
  done
  echo "stopped"
}

case "$cmd" in
  start) start ;;
  stop) stop ;;
  status) curl -fsS "$URL/health" && echo ;;
  *) echo "usage: $0 {start|stop|status} [STATE_DIR] [PORT]"; exit 2 ;;
esac
