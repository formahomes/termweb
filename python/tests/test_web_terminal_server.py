# ABOUTME: Integration tests for a browser terminal server backed by a local PTY shell.
# ABOUTME: Verifies the HTML client, session lifecycle, shell input, and streamed output.

import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from web_terminal.server import DEFAULT_HOST, TMUX_SESSION_PREFIX, WS_MAGIC, WebTerminalServer

OUTPUT_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.05


def get_free_port():
    """Get an available TCP port for a test server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((DEFAULT_HOST, 0))
        return sock.getsockname()[1]


def http_request(url, method="GET", payload=None):
    """Send an HTTP request and decode a JSON response when present."""
    headers = {}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=OUTPUT_TIMEOUT_SECONDS) as response:
        body = response.read().decode("utf-8")
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return response.status, json.loads(body)
        return response.status, body


@pytest.fixture
def terminal_server():
    """Start a web terminal server on a free port for integration testing."""
    port = get_free_port()
    server = WebTerminalServer(
        host=DEFAULT_HOST,
        port=port,
        shell="/bin/sh",
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    deadline = time.time() + OUTPUT_TIMEOUT_SECONDS
    while time.time() < deadline:
        if server.is_running():
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    # Remember which sessions were recovered so we don't kill them on teardown
    recovered_ids = {s["session_id"] for s in server.list_sessions()["sessions"]}

    yield server, port

    # Only close sessions created during this test, detach recovered ones
    for session_info in server.list_sessions()["sessions"]:
        sid = session_info["session_id"]
        if sid not in recovered_ids:
            try:
                server.close_session(sid)
            except KeyError:
                pass
    server.detach_all_sessions()
    server.shutdown()
    server_thread.join(timeout=OUTPUT_TIMEOUT_SECONDS)


def read_until(url, session_id, expected_text, cursor=0):
    """Poll output until the terminal stream contains the requested text."""
    deadline = time.time() + OUTPUT_TIMEOUT_SECONDS
    while time.time() < deadline:
        output_url = (
            f"{url}/api/sessions/{session_id}/output?"
            + urllib.parse.urlencode({"cursor": cursor, "timeout": 0.2})
        )
        _, payload = http_request(output_url)
        cursor = payload["cursor"]
        if expected_text in payload["data"]:
            return payload, cursor
        time.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(f"Timed out waiting for terminal output: {expected_text}")


def test_root_serves_browser_client(terminal_server):
    server, port = terminal_server
    status, body = http_request(f"http://{server.host}:{port}/")

    assert status == 200
    assert "xterm" in body.lower()
    assert "/api/sessions" in body
    assert "https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js" in body
    assert "https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css" in body
    assert "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js" in body
    assert "https://cdn.jsdelivr.net/npm/xterm@5.5.0/lib/xterm.min.js" not in body
    assert "typeof window.Terminal === \"function\"" in body
    assert "basic terminal" in body.lower()
    assert "new WebSocket(" in body
    assert "ws.send(data)" in body
    assert "function sendInput(data)" in body
    assert "function openWebSocket(" in body
    assert 'data-key="ctrl"' in body
    assert 'data-key="esc"' in body
    assert 'data-key="tab"' in body
    assert 'data-key="up"' in body
    assert 'data-key="down"' in body
    assert 'data-key="left"' in body
    assert 'data-key="right"' in body
    assert 'id="session-picker"' in body
    assert 'id="new-session"' in body
    assert 'id="connect-session"' in body
    assert 'id="close-session"' in body
    assert 'id="menu-toggle"' in body
    assert 'class="shell__menu"' in body
    assert "let ctrlArmed = false;" in body
    assert "let menuExpanded = false;" in body
    assert "function setMenuExpanded(expanded)" in body
    assert "const buttonInputs = {" in body
    assert "window.localStorage.getItem(\"saibai-terminal-session\")" in body
    assert "100dvh" in body
    assert "function updateViewportMetrics()" in body
    assert "window.visualViewport" in body
    assert "document.documentElement.style.setProperty(\"--app-height\"" in body


def test_session_accepts_input_and_streams_output(terminal_server):
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    _, session_payload = http_request(f"{base_url}/api/sessions", method="POST")
    session_id = session_payload["session_id"]

    http_request(
        f"{base_url}/api/sessions/{session_id}/input",
        method="POST",
        payload={"data": "printf '__SAIBAI__\\n'\n"},
    )

    output_payload, _ = read_until(base_url, session_id, "__SAIBAI__")
    assert "__SAIBAI__" in output_payload["data"]


def test_session_exposes_controlling_tty(terminal_server):
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    _, session_payload = http_request(f"{base_url}/api/sessions", method="POST")
    session_id = session_payload["session_id"]

    http_request(
        f"{base_url}/api/sessions/{session_id}/input",
        method="POST",
        payload={
            "data": (
                "python3 -c \"handle = open('/dev/tty', 'rb'); "
                "print('__TTY__', handle.isatty()); handle.close()\" "
                "|| echo __TTY__ False\n"
                "echo __TTY_DONE__\n"
            )
        },
    )

    output_payload, _ = read_until(base_url, session_id, "__TTY__ True")
    assert "__TTY__ True" in output_payload["data"]


def test_resize_endpoint_returns_updated_size(terminal_server):
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    _, session_payload = http_request(f"{base_url}/api/sessions", method="POST")
    session_id = session_payload["session_id"]

    _, resize_payload = http_request(
        f"{base_url}/api/sessions/{session_id}/resize",
        method="POST",
        payload={"cols": 120, "rows": 40},
    )

    assert resize_payload["cols"] == 120
    assert resize_payload["rows"] == 40


def test_sessions_persist_until_closed(terminal_server):
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    _, before = http_request(f"{base_url}/api/sessions")
    initial_ids = {s["session_id"] for s in before["sessions"]}

    _, session_payload = http_request(f"{base_url}/api/sessions", method="POST")
    session_id = session_payload["session_id"]

    _, during = http_request(f"{base_url}/api/sessions")
    current_ids = {s["session_id"] for s in during["sessions"]}
    assert session_id in current_ids
    match = [s for s in during["sessions"] if s["session_id"] == session_id]
    assert match[0]["closed"] is False

    http_request(f"{base_url}/api/sessions/{session_id}", method="DELETE")

    _, after = http_request(f"{base_url}/api/sessions")
    remaining_ids = {s["session_id"] for s in after["sessions"]}
    assert session_id not in remaining_ids
    assert remaining_ids == initial_ids


def test_missing_session_returns_not_found(terminal_server):
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    with pytest.raises(urllib.error.HTTPError) as error:
        http_request(f"{base_url}/api/sessions/missing/output?cursor=0")

    assert error.value.code == 404


def ws_connect(host, port, path):
    """Open a raw WebSocket connection and return the socket."""
    sock = socket.create_connection((host, port), timeout=OUTPUT_TIMEOUT_SECONDS)
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(request.encode())
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("Connection closed during handshake")
        response += chunk
    header_block = response.split(b"\r\n\r\n")[0].decode()
    assert "101" in header_block.split("\r\n")[0]
    expected_accept = base64.b64encode(
        hashlib.sha1((key + WS_MAGIC).encode()).digest()
    ).decode()
    assert expected_accept in header_block
    return sock


def ws_send_text(sock, text):
    """Send a masked WebSocket text frame."""
    payload = text.encode("utf-8")
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    header = bytes([0x81])
    length = len(payload)
    if length < 126:
        header += bytes([0x80 | length])
    elif length < 65536:
        header += bytes([0x80 | 126]) + struct.pack("!H", length)
    else:
        header += bytes([0x80 | 127]) + struct.pack("!Q", length)
    sock.sendall(header + mask + masked)


def ws_recv_text(sock, timeout=OUTPUT_TIMEOUT_SECONDS):
    """Read WebSocket text frames until timeout, returning accumulated text."""
    sock.settimeout(timeout)
    collected = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            header = sock.recv(2)
        except socket.timeout:
            break
        if len(header) < 2:
            break
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", sock.recv(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", sock.recv(8))[0]
        payload = b""
        while len(payload) < length:
            chunk = sock.recv(length - len(payload))
            if not chunk:
                break
            payload += chunk
        if opcode == 0x1:
            collected += payload.decode("utf-8", errors="replace")
        elif opcode == 0x8:
            break
    return collected


def test_websocket_streams_terminal_io(terminal_server):
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    _, session_payload = http_request(f"{base_url}/api/sessions", method="POST")
    session_id = session_payload["session_id"]

    sock = ws_connect(server.host, port, f"/api/sessions/{session_id}/ws")
    try:
        ws_send_text(sock, "printf '__WS_OK__\\n'\n")
        deadline = time.time() + OUTPUT_TIMEOUT_SECONDS
        collected = ""
        while time.time() < deadline:
            chunk = ws_recv_text(sock, timeout=1.0)
            collected += chunk
            if "__WS_OK__" in collected:
                break
        assert "__WS_OK__" in collected
    finally:
        sock.close()


def test_client_html_uses_websocket(terminal_server):
    server, port = terminal_server
    status, body = http_request(f"http://{server.host}:{port}/")

    assert status == 200
    assert "function openWebSocket(" in body
    assert "socket.onmessage" in body
    assert "socket.onclose" in body


def test_session_backed_by_tmux(terminal_server):
    """Each session creates a tmux session with the expected prefix."""
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    _, session_payload = http_request(f"{base_url}/api/sessions", method="POST")
    session_id = session_payload["session_id"]

    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    )
    tmux_sessions = result.stdout.strip().splitlines()
    expected_name = TMUX_SESSION_PREFIX + session_id
    assert expected_name in tmux_sessions


def test_tmux_session_has_termweb_env_vars(terminal_server):
    """Tmux sessions have TERMWEB_SESSION_ID and TERMWEB_URL set."""
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    _, session_payload = http_request(f"{base_url}/api/sessions", method="POST")
    session_id = session_payload["session_id"]
    tmux_name = TMUX_SESSION_PREFIX + session_id

    result = subprocess.run(
        ["tmux", "show-environment", "-t", tmux_name],
        capture_output=True, text=True,
    )
    env_lines = result.stdout.strip().splitlines()
    env_dict = {}
    for line in env_lines:
        if "=" in line:
            key, _, value = line.partition("=")
            env_dict[key] = value

    assert env_dict.get("TERMWEB_SESSION_ID") == session_id
    assert env_dict.get("TERMWEB_URL") == f"http://{server.host}:{port}"


def test_session_creation_accepts_label(terminal_server):
    """Sessions can be created with a user-defined label."""
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    _, payload = http_request(
        f"{base_url}/api/sessions", method="POST",
        payload={"label": "my-feature"},
    )
    session_id = payload["session_id"]
    assert payload["label"] == "my-feature"

    _, sessions = http_request(f"{base_url}/api/sessions")
    match = [s for s in sessions["sessions"] if s["session_id"] == session_id]
    assert len(match) == 1
    assert match[0]["label"] == "my-feature"


def test_session_creation_generates_label(terminal_server):
    """Sessions without a label get an auto-generated one."""
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    _, payload = http_request(f"{base_url}/api/sessions", method="POST")
    assert "label" in payload
    assert len(payload["label"]) > 0


def test_session_creation_accepts_port(terminal_server):
    """Sessions can be created with a port number exposed as TERMWEB_PORT env var."""
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    _, payload = http_request(
        f"{base_url}/api/sessions", method="POST",
        payload={"port": 3001},
    )
    session_id = payload["session_id"]
    assert payload["port"] == 3001

    # Verify TERMWEB_PORT env var is available in the session
    http_request(
        f"{base_url}/api/sessions/{session_id}/input",
        method="POST",
        payload={"data": "printf 'PORT=%s\\n' \"$TERMWEB_PORT\"\n"},
    )
    output, _ = read_until(base_url, session_id, "PORT=3001")
    assert "PORT=3001" in output["data"]


def test_session_auto_assigns_port_from_4000(terminal_server):
    """Sessions without an explicit port get one auto-assigned starting at 4000."""
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    _, payload1 = http_request(f"{base_url}/api/sessions", method="POST")
    _, payload2 = http_request(f"{base_url}/api/sessions", method="POST")
    assert payload1["port"] >= 4000
    assert payload2["port"] >= 4000
    assert payload1["port"] != payload2["port"]


def test_session_explicit_port_skipped_by_auto(terminal_server):
    """Auto-assignment skips ports already used by other sessions."""
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    # Take port 4000 explicitly
    _, payload1 = http_request(
        f"{base_url}/api/sessions", method="POST",
        payload={"port": 4000},
    )
    assert payload1["port"] == 4000

    # Auto-assigned should skip 4000
    _, payload2 = http_request(f"{base_url}/api/sessions", method="POST")
    assert payload2["port"] >= 4001


def test_directory_listing_endpoint(terminal_server):
    """GET /api/paths returns subdirectories for a given prefix."""
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    _, payload = http_request(f"{base_url}/api/paths?prefix=/tmp")
    assert "paths" in payload
    assert isinstance(payload["paths"], list)


def test_dashboard_page_served(terminal_server):
    """GET /dashboard returns an HTML page with session management UI."""
    server, port = terminal_server
    status, body = http_request(f"http://{server.host}:{port}/dashboard")

    assert status == 200
    assert "session-list" in body
    assert "session-terminal" in body
    assert "xterm" in body.lower()


def test_notify_sets_session_status(terminal_server):
    """POST /api/sessions/{id}/notify updates the session status field."""
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    _, session_payload = http_request(f"{base_url}/api/sessions", method="POST")
    session_id = session_payload["session_id"]

    # Default status should be "idle"
    _, sessions = http_request(f"{base_url}/api/sessions")
    match = [s for s in sessions["sessions"] if s["session_id"] == session_id]
    assert match[0]["status"] == "idle"

    # Set to processing
    status, resp = http_request(
        f"{base_url}/api/sessions/{session_id}/notify",
        method="POST",
        payload={"event": "processing"},
    )
    assert status == 200
    assert resp["ok"] is True

    _, sessions = http_request(f"{base_url}/api/sessions")
    match = [s for s in sessions["sessions"] if s["session_id"] == session_id]
    assert match[0]["status"] == "processing"

    # Set to done
    http_request(
        f"{base_url}/api/sessions/{session_id}/notify",
        method="POST",
        payload={"event": "done"},
    )

    _, sessions = http_request(f"{base_url}/api/sessions")
    match = [s for s in sessions["sessions"] if s["session_id"] == session_id]
    assert match[0]["status"] == "done"


def test_notify_missing_session_returns_not_found(terminal_server):
    """POST /api/sessions/{id}/notify returns 404 for unknown sessions."""
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    with pytest.raises(urllib.error.HTTPError) as error:
        http_request(
            f"{base_url}/api/sessions/nonexistent/notify",
            method="POST",
            payload={"event": "done"},
        )

    assert error.value.code == 404


def test_notify_invalid_event_returns_bad_request(terminal_server):
    """POST /api/sessions/{id}/notify rejects unknown event types."""
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    _, session_payload = http_request(f"{base_url}/api/sessions", method="POST")
    session_id = session_payload["session_id"]

    with pytest.raises(urllib.error.HTTPError) as error:
        http_request(
            f"{base_url}/api/sessions/{session_id}/notify",
            method="POST",
            payload={"event": "invalid_event"},
        )

    assert error.value.code == 400


def test_sse_streams_notify_events(terminal_server):
    """GET /api/events streams SSE events when sessions are notified."""
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    _, session_payload = http_request(f"{base_url}/api/sessions", method="POST")
    session_id = session_payload["session_id"]

    # Connect to SSE endpoint in a background thread
    sse_events = []
    sse_connected = threading.Event()

    def read_sse():
        request = urllib.request.Request(f"{base_url}/api/events")
        request.add_header("Accept", "text/event-stream")
        try:
            with urllib.request.urlopen(request, timeout=OUTPUT_TIMEOUT_SECONDS) as response:
                sse_connected.set()
                buffer = ""
                while True:
                    chunk = response.read(1).decode("utf-8")
                    if not chunk:
                        break
                    buffer += chunk
                    if "\n\n" in buffer:
                        parts = buffer.split("\n\n")
                        for part in parts[:-1]:
                            if part.strip():
                                sse_events.append(part)
                        buffer = parts[-1]
        except Exception:
            sse_connected.set()

    sse_thread = threading.Thread(target=read_sse, daemon=True)
    sse_thread.start()
    sse_connected.wait(timeout=OUTPUT_TIMEOUT_SECONDS)
    time.sleep(0.2)  # Give SSE connection time to register

    # Send a notify event
    http_request(
        f"{base_url}/api/sessions/{session_id}/notify",
        method="POST",
        payload={"event": "processing"},
    )
    time.sleep(0.5)

    # Check that the SSE client received the event
    assert len(sse_events) >= 1
    last_event = sse_events[-1]
    assert "processing" in last_event
    assert session_id in last_event


def test_sessions_survive_server_restart(terminal_server):
    """Sessions created by one server are recoverable by a new server."""
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    _, session_payload = http_request(f"{base_url}/api/sessions", method="POST")
    session_id = session_payload["session_id"]

    # Send a marker command so we can verify state survives
    http_request(
        f"{base_url}/api/sessions/{session_id}/input",
        method="POST",
        payload={"data": "printf '__SURVIVE__\\n'\n"},
    )
    read_until(base_url, session_id, "__SURVIVE__")

    # Shut down the first server (sessions should stay alive in tmux)
    server.shutdown()

    # Start a second server on a new port
    port2 = get_free_port()
    server2 = WebTerminalServer(
        host=DEFAULT_HOST,
        port=port2,
        shell="/bin/sh",
    )
    server2_thread = threading.Thread(target=server2.serve_forever, daemon=True)
    server2_thread.start()

    deadline = time.time() + OUTPUT_TIMEOUT_SECONDS
    while time.time() < deadline:
        if server2.is_running():
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    try:
        base_url2 = f"http://{server2.host}:{port2}"

        # The old session should appear in the new server's session list
        _, sessions_payload = http_request(f"{base_url2}/api/sessions")
        recovered_ids = [s["session_id"] for s in sessions_payload["sessions"]]
        assert session_id in recovered_ids

        # Should be able to send new input to the recovered session
        http_request(
            f"{base_url2}/api/sessions/{session_id}/input",
            method="POST",
            payload={"data": "printf '__RECOVERED__\\n'\n"},
        )
        output_payload, _ = read_until(base_url2, session_id, "__RECOVERED__")
        assert "__RECOVERED__" in output_payload["data"]
    finally:
        try:
            server2.close_session(session_id)
        except KeyError:
            pass
        server2.detach_all_sessions()
        server2.shutdown()
        server2_thread.join(timeout=OUTPUT_TIMEOUT_SECONDS)
