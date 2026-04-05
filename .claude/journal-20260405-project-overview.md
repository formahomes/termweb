# 2026-04-05 — tmux backend migration

## Session summary
Migrated TerminalSession from direct PTY to tmux-backed sessions for restart survival. Three approaches tried for I/O relay:

1. **PTY + `tmux attach-session`** (failed) — nested terminal: tmux sends DA queries, xterm.js responds, responses leak into shell as garbage input. Tried filtering queries on both input and output sides but arrow keys and cursor responses are indistinguishable from real user input.

2. **tmux control mode** (`tmux -C attach`) (failed) — structured protocol avoids nested terminal, but `%output` events don't reliably stream all terminal output. Shell prompts and command responses were missing.

3. **`tmux pipe-pane` + `tmux send-keys -H`** (working) — pipe-pane streams raw shell output to a named FIFO. send-keys -H sends hex-encoded input. No nested terminal, no query leaking, full output.

## Key issues encountered
- **launchd environment**: tmux server inherits launchd's minimal PATH. oh-my-zsh blocks in `open()` trying to source files on external volumes. Fix: pass full PATH/HOME env to `tmux new-session`.
- **tmux binary not found**: launchd PATH doesn't include `/opt/homebrew/bin`. Fix: `shutil.which` with Homebrew fallback paths.
- **Local echo**: attempted but shell readline redraws make duplicate suppression impossible (same conclusion as the WebSocket migration session).
- **NEVER run `tmux kill-server`** during restarts — it kills all sessions. The start script properly detaches without killing.

## Input batching
Added 10ms accumulation window for keystrokes to reduce subprocess overhead (one `tmux send-keys` call per batch instead of per keystroke).

## Current architecture
- `_create_tmux_session()`: `tmux new-session -d` with proper env
- `_start_output_pipe()`: creates FIFO, `tmux pipe-pane -O` to stream output
- `write()`: batches input, flushes via `tmux send-keys -H`
- `resize()`: `tmux resize-window`
- `detach()`: stops pipe-pane, closes FIFO, leaves tmux session alive
- `close()`: detach + `tmux kill-session`
- `recover()`: queries tmux for size, sets up fresh pipe-pane

---

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
