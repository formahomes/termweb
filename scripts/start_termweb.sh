#!/bin/zsh
# ABOUTME: Starts the Termweb browser terminal as a launchd user service.
# ABOUTME: Copies the server file into a runtime path and replaces the active listener on port 8765.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="$HOME/.termweb-runtime"
RUNTIME_FILE="$RUNTIME_DIR/web_terminal_server.py"
STATIC_DIR="$PROJECT_ROOT/python/src/web_terminal/static"
STDOUT_LOG="/tmp/termweb-web-terminal.out"
STDERR_LOG="/tmp/termweb-web-terminal.err"
SERVICE_LABEL="com.termweb.web-terminal"
PORT="8765"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"

if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "error: python3 not found; set PYTHON_BIN to a valid interpreter (got '$PYTHON_BIN')" >&2
  exit 1
fi

mkdir -p "$RUNTIME_DIR"
cp "$PROJECT_ROOT/python/src/web_terminal/server.py" "$RUNTIME_FILE"

launchctl remove "$SERVICE_LABEL" >/dev/null 2>&1 || true

# Kill any process still holding the port from a previous run
STALE_PID="$(lsof -ti :"$PORT" 2>/dev/null || true)"
if [[ -n "$STALE_PID" ]]; then
  kill $STALE_PID 2>/dev/null || true
  for i in {1..10}; do
    lsof -ti :"$PORT" >/dev/null 2>&1 || break
    sleep 0.2
  done
fi

: > "$STDOUT_LOG"
: > "$STDERR_LOG"

launchctl submit \
  -l "$SERVICE_LABEL" \
  -o "$STDOUT_LOG" \
  -e "$STDERR_LOG" \
  -- "$PYTHON_BIN" "$RUNTIME_FILE" --host 0.0.0.0 --port "$PORT" --static-dir "$STATIC_DIR"

sleep 1

echo "Service: $SERVICE_LABEL"
echo "Port: $PORT"
echo "Log: $STDERR_LOG"
