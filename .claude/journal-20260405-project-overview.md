# 2026-04-05 — Project overview and orientation

## What termweb is
Browser-based remote terminal service. Access a shell from phone/browser over HTTP + WebSocket. Single Python server, no frameworks. Deployed as a macOS launchd user service on port 8765.

## Codebase structure
- `python/src/web_terminal/server.py` (~1260 lines) — the entire server: inline HTML/JS client (xterm.js + basic fallback), HTTP REST for session mgmt, WebSocket for terminal I/O, PTY-backed shell sessions
- `python/tests/test_web_terminal_server.py` — 8 integration tests, real PTYs, raw socket WebSocket client, no mocks
- `python/tests/test_project_files.py` — checks start script and README exist/parse
- `python/tests/debug_websocket_firefox.py` — Selenium debug script for browser WS testing
- `scripts/start_termweb.sh` — copies server to `~/.termweb-runtime/`, registers launchd service (`com.termweb.web-terminal`)

## Architecture notes
- `TerminalSession`: wraps `pty.openpty()` + `subprocess.Popen`, threaded reader accumulates output buffer, supports read/write/resize/close
- `TerminalRequestHandler`: extends `BaseHTTPRequestHandler`, overrides `setup()` to intercept WebSocket upgrades via `MSG_PEEK` before buffered I/O is created
- `ws_handle_connection()` / `ws_relay()`: raw WebSocket handshake + two-thread relay (WS→PTY, PTY→WS)
- `ws_serve_forever()`: standalone socket server on port+1 (leftover from abandoned approach, could be removed but still wired up in `serve_forever()`)
- Client JS: xterm.js with FitAddon, falls back to basic `<pre>`+`<textarea>` terminal. Session picker with localStorage persistence. Ctrl modifier, special key buttons for mobile.

## Key constants
- `WS_MAGIC` = RFC 6455 GUID (was wrong, fixed in 728f74d)
- Default port 8765, WS port offset +1
- Default terminal 120x32

## Current state (2026-04-05)
- Branch: `wip/termweb-init`, clean
- All 8 tests passing
- WebSocket approach works in tests, awaiting Steve's browser confirmation
- `ws_serve_forever` on port+1 is still wired up in `serve_forever()` even though the `setup()` intercept handles WS on the same port — potential dead code
