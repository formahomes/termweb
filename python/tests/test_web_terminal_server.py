# ABOUTME: Integration tests for a browser terminal server backed by a local PTY shell.
# ABOUTME: Verifies the HTML client, session lifecycle, shell input, and streamed output.

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from web_terminal.server import DEFAULT_HOST, WebTerminalServer

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

    yield server, port

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
    assert "let pendingInput = \"\";" in body
    assert "const INPUT_FLUSH_DELAY_MS = 16;" in body
    assert "function scheduleInputFlush()" in body
    assert "pendingInput += data;" in body
    assert "flushInputSoon = window.setTimeout(flushPendingInput, INPUT_FLUSH_DELAY_MS);" in body
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
    assert "function queueInput(data)" in body
    assert "let ctrlArmed = false;" in body
    assert "let menuExpanded = false;" in body
    assert "function setMenuExpanded(expanded)" in body
    assert "const buttonInputs = {" in body
    assert "window.localStorage.getItem(\"saibai-terminal-session\")" in body
    assert 'navigator.sendBeacon("/api/sessions/" + sessionId + "/close")' not in body
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

    _, sessions_payload = http_request(f"{base_url}/api/sessions")
    assert sessions_payload["sessions"] == []

    _, session_payload = http_request(f"{base_url}/api/sessions", method="POST")
    session_id = session_payload["session_id"]

    _, sessions_payload = http_request(f"{base_url}/api/sessions")
    assert [session["session_id"] for session in sessions_payload["sessions"]] == [session_id]
    assert sessions_payload["sessions"][0]["closed"] is False

    http_request(f"{base_url}/api/sessions/{session_id}", method="DELETE")

    _, sessions_payload = http_request(f"{base_url}/api/sessions")
    assert sessions_payload["sessions"] == []


def test_missing_session_returns_not_found(terminal_server):
    server, port = terminal_server
    base_url = f"http://{server.host}:{port}"

    with pytest.raises(urllib.error.HTTPError) as error:
        http_request(f"{base_url}/api/sessions/missing/output?cursor=0")

    assert error.value.code == 404
