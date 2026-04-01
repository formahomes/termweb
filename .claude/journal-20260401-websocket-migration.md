# 2026-04-01 — WebSocket migration: replacing HTTP polling for terminal I/O

## 2026-04-01 Update: Root cause found for browser disconnect
The WS_MAGIC GUID was wrong — `258EAFA5-E914-47DA-95CA-5AB5A60AD65C` instead of the RFC 6455 value `258EAFA5-E914-47DA-95CA-C5AB0DC85B11`. Browser computed the expected accept key with the real GUID, server computed it with the wrong one, so the browser rejected the handshake and dropped the connection after 101. Tests passed because both sides used the same wrong constant. Fixed in commit 728f74d.

## Goal
Make terminal input feel instant on slow (cell) connections. Previously, keystrokes were only visible after the server echoed them back through a 250ms HTTP polling loop.

## Approach 1: Local echo (abandoned)
- Echoed printable characters client-side immediately, consumed duplicates from server output
- **Problem 1**: Shell uses readline-style redraws (backspace + rewrite), not bare character echo. Character-matching approach produced wrong output ("pwp" instead of "pwd")
- **Problem 2**: Switched to erase-and-let-server-redraw approach, but Steve correctly pointed out this was a hack — the real issue was the synchronous HTTP polling architecture
- **Lesson**: Don't try to predict shell echo behavior. The PTY echo model is fundamentally tied to the server.

## Approach 2: WebSocket (current)
Replaced HTTP polling + batched POST input with WebSocket for terminal I/O. Session management (create, list, close, resize) stays on HTTP.

### BaseHTTPRequestHandler is hostile to WebSocket
This consumed most of the session. The core problem: `BaseHTTPRequestHandler` wraps the socket in buffered file objects (`rfile`/`wfile` via `socket.makefile()`), and these are fundamentally incompatible with raw socket operations needed for WebSocket relay.

**Failures in sequence:**
1. **Wrote 101 via `self.wfile`, relay read via `self.rfile`** — `rfile.read(2)` returned empty (0 bytes). The BufferedReader's internal state was at EOF after consuming headers with no body following.
2. **Created fresh unbuffered reader via `makefile("rb", 0)`** — Same EOF issue. The unbuffered reader still returned 0 bytes.
3. **Used `os.dup()` + `socket.fromfd()`** — `socket.fromfd()` internally calls `dup()` again, creating a double-dup. The intermediate FD leaked. `recv()` returned EOF. `sendall()` got "Bad file descriptor".
4. **Closed `rfile`, used `self.connection` directly** — `rfile.close()` worked (doesn't close the underlying socket per Python docs). `recv()` on `self.connection` STILL returned 0. My local test showed `rfile.close()` + `recv()` works fine on a simple socket, but it failed in the actual browser flow.
5. **Used `send_response()` + `end_headers()` (handler's own methods)** — 101 response was sent correctly (verified by capturing exact bytes, accept key matches RFC 6455 test vector). Browser received 101 (confirmed in Firefox Network tab). But browser immediately closed the TCP connection after receiving it. `recv()` returned 0.
6. **Separate WebSocket port (port+1)** — Clean raw socket server, no BaseHTTPRequestHandler involvement. Worked perfectly from Python. Firefox refused to connect — likely cross-port restriction from HTTP page.
7. **Intercept in `setup()` before buffered I/O created (current approach)** — Override `setup()` in the request handler. `MSG_PEEK` to detect WebSocket upgrades. If WebSocket, consume the data and handle on the raw socket before `rfile`/`wfile` are ever created. If HTTP, call `super().setup()` for normal handling. **This works in tests. Awaiting browser confirmation.**

### Key technical insights
- `socket.makefile()` creates a BufferedReader that does large `recv()` calls internally. After consuming HTTP headers, the BufferedReader may mark the stream as EOF even though the socket is still open.
- You CANNOT mix `makefile()` file objects with direct `socket.recv()`/`sendall()` calls reliably. Python docs warn about this but it's easy to miss.
- `socket.fromfd()` calls `dup()` internally — don't `os.dup()` before passing to it.
- Firefox may block WebSocket connections to a different port than the page origin (observed but not 100% confirmed as the cause).
- The `websocket-client` Python library v1.8.0 uses the correct RFC 6455 GUID (`258EAFA5-E914-47DA-95CA-C5AB0DC85B11`). Our code had a wrong GUID (`258EAFA5-E914-47DA-95CA-5AB5A60AD65C`) — this was the root cause of browser disconnects. Tests used raw sockets that imported the same wrong constant, masking the bug.

### Server architecture (current)
- `TerminalRequestHandler.setup()`: peeks at incoming data, intercepts WebSocket upgrades before BaseHTTPRequestHandler creates buffered I/O
- `ws_handle_connection()`: handles raw WebSocket handshake + relay on the clean socket
- `ws_relay()`: two threads — one reads WebSocket frames and writes to PTY, one reads PTY output and sends WebSocket frames
- `ws_serve_forever()`: standalone socket server (still exists, used by the separate-port approach, could be removed)
- HTTP endpoints for session management unchanged

### Client architecture (current)
- `sendInput(data)`: sends via `ws.send(data)` — no batching, no delay
- `openWebSocket()`: creates WebSocket, `onmessage` writes to terminal, `onclose` shows disconnect
- No local echo — WebSocket latency should be low enough

## Status
Tests all pass (8/8). Wrong WS_MAGIC GUID fixed (commit 728f74d). Awaiting Steve's browser confirmation.
