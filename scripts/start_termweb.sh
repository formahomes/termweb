#!/bin/zsh
# ABOUTME: Starts the Termweb browser terminal as a launchd user service.
# ABOUTME: Copies the server file into a runtime path and replaces the active listener on port 8765.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="$HOME/.termweb-runtime"
RUNTIME_FILE="$RUNTIME_DIR/web_terminal_server.py"
SESSION_METADATA_FILE="$RUNTIME_DIR/sessions.json"
STATIC_DIR="$PROJECT_ROOT/python/src/web_terminal/static"
STDOUT_LOG="/tmp/termweb-web-terminal.out"
STDERR_LOG="/tmp/termweb-web-terminal.err"
SERVICE_LABEL="com.termweb.web-terminal"
PORT="8765"
POLL_INTERVAL_SECONDS="0.2"
STOP_ATTEMPTS=25
START_ATTEMPTS=25
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"

if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "error: python3 not found; set PYTHON_BIN to a valid interpreter (got '$PYTHON_BIN')" >&2
  exit 1
fi

mkdir -p "$RUNTIME_DIR"

snapshot_existing_sessions() {
  local snapshot_path
  snapshot_path="$(mktemp "$RUNTIME_DIR/.sessions-api.XXXXXX")"
  if curl --fail --silent --max-time 2 "http://127.0.0.1:$PORT/api/sessions" > "$snapshot_path"; then
    "$PYTHON_BIN" - "$SESSION_METADATA_FILE" "$snapshot_path" <<'PY'
import json
import os
import sys
import tempfile

metadata_path, snapshot_path = sys.argv[1:]
try:
    with open(snapshot_path, encoding="utf-8") as stream:
        payload = json.load(stream)
except (OSError, json.JSONDecodeError):
    raise SystemExit(0)

try:
    with open(metadata_path, encoding="utf-8") as stream:
        records = json.load(stream)
except (FileNotFoundError, OSError, json.JSONDecodeError):
    records = {}
if not isinstance(records, dict):
    records = {}

for session in payload.get("sessions", []):
    session_id = session.get("session_id")
    if not isinstance(session_id, str):
        continue
    record = records.get(session_id, {})
    if not isinstance(record, dict):
        record = {}
    for key in ("label", "port", "cwd", "repo_path", "worktree_path", "created_at"):
        if key in session:
            record[key] = session[key]
    records[session_id] = record

parent = os.path.dirname(metadata_path)
fd, temporary_path = tempfile.mkstemp(prefix=".sessions-migrate.", dir=parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        fd = None
        json.dump(records, stream, indent=2)
        stream.write("\n")
    os.replace(temporary_path, metadata_path)
finally:
    if fd is not None:
        os.close(fd)
    try:
        os.unlink(temporary_path)
    except FileNotFoundError:
        pass
PY
  fi
  rm -f "$snapshot_path"
}

snapshot_existing_sessions

launchctl remove "$SERVICE_LABEL" >/dev/null 2>&1 || true

wait_for_port_free() {
  local attempt=0
  while (( attempt < STOP_ATTEMPTS )); do
    if ! lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep "$POLL_INTERVAL_SECONDS"
    (( attempt += 1 ))
  done
  return 1
}

# Ask the running server to detach cleanly before using its runtime file.
STALE_PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "$STALE_PIDS" ]]; then
  kill -TERM ${(f)STALE_PIDS} 2>/dev/null || true
  if ! wait_for_port_free; then
    STALE_PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "$STALE_PIDS" ]]; then
      kill -KILL ${(f)STALE_PIDS} 2>/dev/null || true
    fi
    if ! wait_for_port_free; then
      echo "error: port $PORT did not become available" >&2
      exit 1
    fi
  fi
fi

cp "$PROJECT_ROOT/python/src/web_terminal/server.py" "$RUNTIME_FILE"
: > "$STDOUT_LOG"
: > "$STDERR_LOG"

launchctl submit \
  -l "$SERVICE_LABEL" \
  -o "$STDOUT_LOG" \
  -e "$STDERR_LOG" \
  -- "$PYTHON_BIN" "$RUNTIME_FILE" --host 0.0.0.0 --port "$PORT" --static-dir "$STATIC_DIR"

HEALTH_URL="http://127.0.0.1:$PORT/api/sessions"
healthy=0
for ((attempt = 0; attempt < START_ATTEMPTS; attempt += 1)); do
  if curl --fail --silent --show-error --max-time 1 "$HEALTH_URL" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep "$POLL_INTERVAL_SECONDS"
done

if (( healthy == 0 )); then
  echo "error: Termweb did not become ready; see $STDERR_LOG" >&2
  exit 1
fi

echo "Service: $SERVICE_LABEL"
echo "Port: $PORT"
echo "Log: $STDERR_LOG"
