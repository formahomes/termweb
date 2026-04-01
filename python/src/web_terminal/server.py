# ABOUTME: Serves a browser terminal page and backs each session with a local PTY shell.
# ABOUTME: Streams terminal I/O over WebSocket and accepts session management over HTTP.

import argparse
import base64
import hashlib
import json
import os
import pty
import select
import signal
import socket
import subprocess
import termios
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse

try:
    import fcntl
    import struct
except ImportError as error:
    raise RuntimeError("Terminal resizing requires fcntl and struct support") from error

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_WS_PORT_OFFSET = 1
DEFAULT_COLS = 120
DEFAULT_ROWS = 32
DEFAULT_OUTPUT_TIMEOUT = 0.25
DEFAULT_READ_SIZE = 4096
PROCESS_EXIT_TIMEOUT = 1.0
TERMINAL_PAGE = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Saibai Terminal</title>
    <link
      rel="stylesheet"
      href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css"
    />
    <style>
      :root {
        color-scheme: dark;
        --page: #0b1020;
        --panel: #11182d;
        --border: #24304e;
        --text: #dbe5ff;
        --muted: #8ea1c9;
        --app-height: 100dvh;
        --app-width: 100vw;
      }

      * {
        box-sizing: border-box;
      }

      body {
        margin: 0;
        min-height: var(--app-height);
        background:
          radial-gradient(circle at top, rgba(76, 112, 255, 0.18), transparent 30%),
          linear-gradient(180deg, #11162a 0%, var(--page) 65%);
        color: var(--text);
        font-family: "SFMono-Regular", "Menlo", "Monaco", monospace;
        overflow: hidden;
      }

      .shell {
        display: grid;
        grid-template-rows: auto minmax(0, 1fr);
        min-height: var(--app-height);
        height: var(--app-height);
        padding: 10px;
        gap: 8px;
        width: min(100%, var(--app-width));
      }

      .shell__header {
        display: grid;
        gap: 8px;
        padding: 8px 10px;
        background: rgba(17, 24, 45, 0.92);
        border: 1px solid var(--border);
        border-radius: 12px;
        backdrop-filter: blur(12px);
      }

      .shell__bar {
        display: grid;
        grid-template-columns: auto minmax(0, 1fr) auto;
        gap: 8px;
        align-items: center;
      }

      .shell__title {
        font-size: 11px;
        letter-spacing: 0.06em;
        text-transform: uppercase;
      }

      .shell__status {
        color: var(--muted);
        font-size: 10px;
        text-align: center;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .shell__menu {
        display: grid;
        gap: 8px;
      }

      .shell__menu[hidden] {
        display: none;
      }

      .shell__controls {
        display: flex;
        gap: 10px;
        align-items: center;
        flex-wrap: wrap;
      }

      .shell__select {
        min-width: 132px;
        border: 1px solid var(--border);
        background: rgba(10, 15, 30, 0.94);
        color: var(--text);
        border-radius: 8px;
        padding: 6px 8px;
        font: inherit;
        font-size: 11px;
      }

      .shell__terminal {
        display: flex;
        flex-direction: column;
        min-height: 0;
        padding: 8px;
        background: rgba(10, 15, 30, 0.94);
        border: 1px solid var(--border);
        border-radius: 14px;
        box-shadow: 0 24px 80px rgba(0, 0, 0, 0.45);
        overflow: hidden;
      }

      .shell__keys {
        display: flex;
        gap: 6px;
        flex-wrap: wrap;
      }

      .shell__key {
        border: 1px solid var(--border);
        background: rgba(17, 24, 45, 0.92);
        color: var(--text);
        border-radius: 999px;
        padding: 6px 10px;
        font: inherit;
        font-size: 11px;
        line-height: 1;
      }

      .shell__key.is-active {
        background: rgba(76, 112, 255, 0.28);
        border-color: rgba(154, 176, 255, 0.6);
      }

      .shell__key--menu {
        min-width: 58px;
      }

      #terminal {
        width: 100%;
        flex: 1 1 auto;
        min-height: 0;
        overflow: hidden;
      }
    </style>
  </head>
  <body>
    <main class="shell">
      <header class="shell__header">
        <div class="shell__bar">
          <div class="shell__title">Saibai Remote Terminal</div>
          <div class="shell__status" id="status">Connecting…</div>
          <button class="shell__key shell__key--menu" id="menu-toggle" type="button">Menu</button>
        </div>
        <div class="shell__menu" hidden>
          <div class="shell__controls">
            <select class="shell__select" id="session-picker"></select>
            <button class="shell__key" id="connect-session" type="button">Connect</button>
            <button class="shell__key" id="new-session" type="button">New</button>
            <button class="shell__key" id="close-session" type="button">Close</button>
          </div>
          <div class="shell__keys">
            <button class="shell__key" data-key="ctrl" type="button">Ctrl</button>
            <button class="shell__key" data-key="esc" type="button">Esc</button>
            <button class="shell__key" data-key="tab" type="button">Tab</button>
            <button class="shell__key" data-key="up" type="button">Up</button>
            <button class="shell__key" data-key="down" type="button">Down</button>
            <button class="shell__key" data-key="left" type="button">Left</button>
            <button class="shell__key" data-key="right" type="button">Right</button>
          </div>
        </div>
      </header>
      <section class="shell__terminal">
        <div id="terminal"></div>
      </section>
    </main>
    <script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js"></script>
    <script>
      const statusNode = document.getElementById("status");
      const terminalNode = document.getElementById("terminal");
      const menuToggleNode = document.getElementById("menu-toggle");
      const menuPanelNode = document.querySelector(".shell__menu");
      const sessionPickerNode = document.getElementById("session-picker");
      const connectSessionNode = document.getElementById("connect-session");
      const newSessionNode = document.getElementById("new-session");
      const closeSessionNode = document.getElementById("close-session");
      const keyButtons = Array.from(document.querySelectorAll("[data-key]"));
      const SESSION_STORAGE_KEY = "saibai-terminal-session";
      let sessionId = null;
      let sessionList = [];
      let ws = null;
      let ctrlArmed = false;
      let menuExpanded = false;
      const buttonInputs = {
        esc: "\\x1b",
        tab: "\\t",
        up: "\\x1b[A",
        down: "\\x1b[B",
        left: "\\x1b[D",
        right: "\\x1b[C"
      };

      function setStatus(message) {
        statusNode.textContent = message;
      }

      function setMenuExpanded(expanded) {
        menuExpanded = expanded;
        menuPanelNode.hidden = !expanded;
        menuToggleNode.classList.toggle("is-active", expanded);
        menuToggleNode.textContent = expanded ? "Hide" : "Menu";
      }

      function updateViewportMetrics() {
        const viewport = window.visualViewport;
        const height = viewport ? viewport.height : window.innerHeight;
        const width = viewport ? viewport.width : window.innerWidth;
        document.documentElement.style.setProperty("--app-height", height + "px");
        document.documentElement.style.setProperty("--app-width", width + "px");
      }

      function createBasicTerminal() {
        const outputNode = document.createElement("pre");
        const inputNode = document.createElement("textarea");
        outputNode.style.margin = "0";
        outputNode.style.whiteSpace = "pre-wrap";
        outputNode.style.wordBreak = "break-word";
        outputNode.style.minHeight = "100%";
        outputNode.style.color = "#dbe5ff";
        outputNode.style.fontSize = "12px";
        inputNode.setAttribute("aria-label", "Basic terminal input");
        inputNode.style.position = "absolute";
        inputNode.style.opacity = "0";
        inputNode.style.pointerEvents = "none";
        inputNode.style.height = "1px";
        inputNode.style.width = "1px";
        terminalNode.style.position = "relative";
        terminalNode.style.overflow = "auto";
        terminalNode.replaceChildren(outputNode, inputNode);

        const listeners = [];
        const terminal = {
          cols: 120,
          rows: 32,
          open() {
            outputNode.focus?.();
          },
          loadAddon() {},
          write(data) {
            outputNode.textContent += data;
            terminalNode.scrollTop = terminalNode.scrollHeight;
          },
          writeln(data) {
            terminal.write(data + "\\n");
          },
          clear() {
            outputNode.textContent = "";
          },
          onData(listener) {
            listeners.push(listener);
          },
          fit() {
            const width = Math.max(terminalNode.clientWidth - 16, 320);
            const height = Math.max(terminalNode.clientHeight - 16, 160);
            terminal.cols = Math.max(Math.floor(width / 9), 20);
            terminal.rows = Math.max(Math.floor(height / 18), 8);
          }
        };

        function emit(data) {
          listeners.forEach((listener) => listener(data));
        }

        function focusInput() {
          inputNode.focus();
        }

        terminalNode.addEventListener("mousedown", focusInput);
        window.addEventListener("load", focusInput);

        inputNode.addEventListener("input", () => {
          if (inputNode.value) {
            emit(inputNode.value);
            inputNode.value = "";
          }
        });

        inputNode.addEventListener("keydown", (event) => {
          const keys = {
            Enter: "\\r",
            Backspace: "\\x7f",
            Tab: "\\t",
            Escape: "\\x1b",
            ArrowUp: "\\x1b[A",
            ArrowDown: "\\x1b[B",
            ArrowRight: "\\x1b[C",
            ArrowLeft: "\\x1b[D"
          };
          if (event.ctrlKey && event.key === "c") {
            event.preventDefault();
            emit("\\x03");
            return;
          }
          if (event.ctrlKey && event.key === "d") {
            event.preventDefault();
            emit("\\x04");
            return;
          }
          if (event.ctrlKey && event.key === "l") {
            event.preventDefault();
            emit("\\x0c");
            return;
          }
          if (keys[event.key]) {
            event.preventDefault();
            emit(keys[event.key]);
          }
        });

        return terminal;
      }

      function createTerminal() {
        if (typeof window.Terminal === "function") {
          const terminal = new Terminal({
            cursorBlink: true,
            fontSize: 12,
            theme: {
              background: "#0a0f1e",
              foreground: "#dbe5ff",
              cursor: "#9ab0ff",
              black: "#101828",
              brightBlack: "#51607b"
            }
          });
          let fitAddon = null;
          if (window.FitAddon && typeof window.FitAddon.FitAddon === "function") {
            fitAddon = new FitAddon.FitAddon();
            terminal.loadAddon(fitAddon);
          }
          terminal.open(terminalNode);
          terminal.fit = () => {
            if (fitAddon) {
              fitAddon.fit();
              return;
            }
            const width = Math.max(terminalNode.clientWidth - 16, 320);
            const height = Math.max(terminalNode.clientHeight - 16, 160);
            const cols = Math.max(Math.floor(width / 9), 20);
            const rows = Math.max(Math.floor(height / 18), 8);
            terminal.resize(cols, rows);
          };
          terminal.clientName = fitAddon ? "xterm" : "xterm (manual sizing)";
          return terminal;
        }
        const terminal = createBasicTerminal();
        terminal.open(terminalNode);
        terminal.clientName = "basic terminal";
        return terminal;
      }

      const terminal = createTerminal();
      updateViewportMetrics();
      setMenuExpanded(false);

      async function sendJson(url, method, payload) {
        const response = await fetch(url, {
          method,
          headers: { "Content-Type": "application/json" },
          body: payload ? JSON.stringify(payload) : undefined
        });
        if (!response.ok) {
          throw new Error("Request failed: " + response.status);
        }
        return await response.json();
      }

      function sendInput(data) {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(data);
        }
      }

      function setCtrlButtonState() {
        keyButtons.forEach((button) => {
          if (button.dataset.key === "ctrl") {
            button.classList.toggle("is-active", ctrlArmed);
          }
        });
      }

      function useCtrlModifier(data) {
        if (!ctrlArmed || !data || data.length !== 1) {
          return data;
        }
        ctrlArmed = false;
        setCtrlButtonState();
        const code = data.toUpperCase().charCodeAt(0);
        if (code >= 64 && code <= 95) {
          return String.fromCharCode(code - 64);
        }
        return data;
      }

      async function listSessions() {
        const payload = await sendJson("/api/sessions", "GET");
        sessionList = payload.sessions;
        renderSessionPicker();
        return sessionList;
      }

      function renderSessionPicker() {
        const selectedSessionId = sessionId || window.localStorage.getItem("saibai-terminal-session") || "";
        sessionPickerNode.replaceChildren();
        if (sessionList.length === 0) {
          const option = document.createElement("option");
          option.value = "";
          option.textContent = "No sessions";
          sessionPickerNode.appendChild(option);
          sessionPickerNode.disabled = true;
          return;
        }
        sessionPickerNode.disabled = false;
        sessionList.forEach((session) => {
          const option = document.createElement("option");
          option.value = session.session_id;
          const state = session.closed ? "closed" : "open";
          option.textContent = session.session_id.slice(0, 8) + " (" + state + ")";
          if (session.session_id === selectedSessionId) {
            option.selected = true;
          }
          sessionPickerNode.appendChild(option);
        });
      }

      async function createSession() {
        const payload = await sendJson("/api/sessions", "POST");
        await listSessions();
        await connectToSession(payload.session_id);
      }

      async function resizeTerminal() {
        if (!sessionId) {
          return;
        }
        terminal.fit();
        const cols = Math.max(terminal.cols, 20);
        const rows = Math.max(terminal.rows, 8);
        await sendJson("/api/sessions/" + sessionId + "/resize", "POST", {
          cols,
          rows
        });
      }

      function openWebSocket(wsSessionId) {
        var protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        var url = protocol + "//" + window.location.host + "/api/sessions/" + wsSessionId + "/ws";
        var socket = new WebSocket(url);
        socket.onmessage = function(event) {
          terminal.write(event.data);
        };
        socket.onclose = function() {
          if (sessionId === wsSessionId) {
            setStatus("Disconnected");
            terminal.writeln("");
            terminal.writeln("[terminal disconnected]");
            listSessions().catch(function(error) { console.error(error); });
          }
        };
        socket.onerror = function(error) {
          console.error("WebSocket error:", error);
        };
        return socket;
      }

      async function connectToSession(nextSessionId) {
        if (!nextSessionId) {
          return;
        }
        if (ws) {
          ws.onclose = null;
          ws.close();
          ws = null;
        }
        sessionId = nextSessionId;
        ctrlArmed = false;
        setCtrlButtonState();
        if (typeof terminal.clear === "function") {
          terminal.clear();
        }
        terminal.fit();
        await resizeTerminal();
        window.localStorage.setItem("saibai-terminal-session", sessionId);
        renderSessionPicker();
        ws = openWebSocket(sessionId);
        setStatus("Connected via " + terminal.clientName);
      }

      async function closeSelectedSession() {
        const targetSessionId = sessionPickerNode.value || sessionId;
        if (!targetSessionId) {
          return;
        }
        await sendJson("/api/sessions/" + targetSessionId, "DELETE");
        if (sessionId === targetSessionId) {
          if (ws) {
            ws.onclose = null;
            ws.close();
            ws = null;
          }
          sessionId = null;
          window.localStorage.removeItem("saibai-terminal-session");
          if (typeof terminal.clear === "function") {
            terminal.clear();
          }
          setStatus("Session closed");
        }
        await listSessions();
      }

      terminal.onData((data) => {
        if (!sessionId) {
          return;
        }
        sendInput(useCtrlModifier(data));
      });

      keyButtons.forEach((button) => {
        button.addEventListener("click", () => {
          const key = button.dataset.key;
          if (key === "ctrl") {
            ctrlArmed = !ctrlArmed;
            setCtrlButtonState();
            return;
          }
          if (!sessionId) {
            return;
          }
          if (buttonInputs[key]) {
            sendInput(buttonInputs[key]);
          }
        });
      });

      connectSessionNode.addEventListener("click", () => {
        connectToSession(sessionPickerNode.value).catch((error) => {
          setStatus("Connection failed");
          console.error(error);
        });
      });

      menuToggleNode.addEventListener("click", () => {
        setMenuExpanded(!menuExpanded);
        updateViewportMetrics();
        resizeTerminal().catch((error) => console.error(error));
      });

      newSessionNode.addEventListener("click", () => {
        createSession().catch((error) => {
          setStatus("Connection failed");
          console.error(error);
        });
      });

      closeSessionNode.addEventListener("click", () => {
        closeSelectedSession().catch((error) => {
          setStatus("Close failed");
          console.error(error);
        });
      });

      window.addEventListener("resize", () => {
        updateViewportMetrics();
        resizeTerminal().catch((error) => console.error(error));
      });

      if (window.visualViewport) {
        window.visualViewport.addEventListener("resize", () => {
          updateViewportMetrics();
          resizeTerminal().catch((error) => console.error(error));
        });
        window.visualViewport.addEventListener("scroll", () => {
          updateViewportMetrics();
        });
      }

      window.addEventListener("beforeunload", () => {
        if (ws) {
          ws.onclose = null;
          ws.close();
          ws = null;
        }
      });

      async function initializePage() {
        await listSessions();
        const preferredSessionId = window.localStorage.getItem("saibai-terminal-session");
        const availableSessionId = preferredSessionId && sessionList.some((session) => session.session_id === preferredSessionId)
          ? preferredSessionId
          : (sessionList[0] && sessionList[0].session_id);
        if (availableSessionId) {
          await connectToSession(availableSessionId);
          return;
        }
        await createSession();
      }

      initializePage().catch((error) => {
        setStatus("Connection failed");
        terminal.writeln("[unable to start terminal]");
        console.error(error);
      });
    </script>
  </body>
</html>
"""


class TerminalSession:
    """A single PTY-backed shell session."""

    def __init__(self, shell: str, cwd: str, cols: int, rows: int):
        self.shell = shell
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.session_id = uuid.uuid4().hex
        self.created_at = time.time()
        self._buffer = ""
        self._closed = False
        self._lock = threading.Lock()
        self._output_ready = threading.Condition(self._lock)
        self._master_fd, slave_fd = pty.openpty()
        self._set_winsize(cols, rows)
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("LANG", "en_US.UTF-8")
        env.setdefault("LC_ALL", "en_US.UTF-8")
        def configure_child_terminal() -> None:
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        self._process = subprocess.Popen(
            [shell],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=env,
            close_fds=True,
            preexec_fn=configure_child_terminal,
        )
        os.close(slave_fd)
        self._reader = threading.Thread(target=self._read_output, daemon=True)
        self._reader.start()

    def read(self, cursor: int, timeout: float) -> Dict[str, object]:
        with self._output_ready:
            if cursor < 0:
                cursor = 0
            deadline = time.monotonic() + max(timeout, 0.0)
            while not self._closed and cursor >= len(self._buffer):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._output_ready.wait(remaining)
            if cursor > len(self._buffer):
                cursor = len(self._buffer)
            data = self._buffer[cursor:]
            return {
                "session_id": self.session_id,
                "cursor": len(self._buffer),
                "data": data,
                "closed": self._closed,
            }

    def write(self, data: str) -> None:
        if not data:
            return
        with self._lock:
            if self._closed:
                raise RuntimeError("Session is closed")
        os.write(self._master_fd, data.encode("utf-8"))

    def resize(self, cols: int, rows: int) -> Dict[str, int]:
        cols = max(int(cols), 20)
        rows = max(int(rows), 8)
        with self._lock:
            if self._closed:
                raise RuntimeError("Session is closed")
            self.cols = cols
            self.rows = rows
            self._set_winsize(cols, rows)
        return {"cols": cols, "rows": rows}

    def close(self) -> None:
        with self._output_ready:
            if self._closed:
                return
            self._closed = True
            self._output_ready.notify_all()
        try:
            os.close(self._master_fd)
        except OSError:
            pass
        if self._process.poll() is None:
            try:
                os.killpg(self._process.pid, signal.SIGTERM)
            except OSError:
                self._process.terminate()
            try:
                self._process.wait(timeout=PROCESS_EXIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self._process.pid, signal.SIGKILL)
                except OSError:
                    self._process.kill()
                self._process.wait(timeout=PROCESS_EXIT_TIMEOUT)

    def info(self) -> Dict[str, object]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "cols": self.cols,
                "rows": self.rows,
                "cwd": self.cwd,
                "shell": self.shell,
                "created_at": self.created_at,
                "closed": self._closed,
            }

    def _read_output(self) -> None:
        try:
            while True:
                try:
                    ready, _, _ = select.select([self._master_fd], [], [], 0.1)
                except OSError:
                    break
                if not ready:
                    if self._process.poll() is not None:
                        break
                    continue
                try:
                    chunk = os.read(self._master_fd, DEFAULT_READ_SIZE)
                except OSError:
                    break
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                with self._output_ready:
                    self._buffer += text
                    self._output_ready.notify_all()
        finally:
            with self._output_ready:
                self._closed = True
                self._output_ready.notify_all()

    def _set_winsize(self, cols: int, rows: int) -> None:
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, size)


WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_OP_TEXT = 0x1
WS_OP_CLOSE = 0x8
WS_OP_PING = 0x9
WS_OP_PONG = 0xA


def ws_accept_key(client_key: str) -> str:
    """Compute the Sec-WebSocket-Accept header value."""
    digest = hashlib.sha1((client_key.strip() + WS_MAGIC).encode()).digest()
    return base64.b64encode(digest).decode()


def ws_read_frame(reader) -> Optional[tuple]:
    """Read one WebSocket frame and return (opcode, payload_bytes) or None on EOF."""
    header = _ws_read_exact(reader, 2)
    if header is None:
        return None
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F
    if length == 126:
        ext = _ws_read_exact(reader, 2)
        if ext is None:
            return None
        length = int.from_bytes(ext, "big")
    elif length == 127:
        ext = _ws_read_exact(reader, 8)
        if ext is None:
            return None
        length = int.from_bytes(ext, "big")
    mask_key = _ws_read_exact(reader, 4) if masked else None
    if masked and mask_key is None:
        return None
    payload = _ws_read_exact(reader, length) if length > 0 else b""
    if payload is None:
        return None
    if masked and mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def ws_send_frame(sock, opcode: int, payload: bytes) -> None:
    """Send a WebSocket frame (server-to-client, unmasked)."""
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + length.to_bytes(2, "big")
    else:
        header += bytes([127]) + length.to_bytes(8, "big")
    sock.sendall(header + payload)


def _ws_read_exact(sock, count: int) -> Optional[bytes]:
    """Read exactly count bytes from a socket, or return None on EOF."""
    buf = b""
    while len(buf) < count:
        try:
            chunk = sock.recv(count - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def ws_relay(sock, reader, session: "TerminalSession") -> None:
    """Relay data between a WebSocket and a PTY session until either side closes."""
    cursor = 0

    def send_output():
        nonlocal cursor
        while not session._closed:
            result = session.read(cursor=cursor, timeout=0.5)
            cursor = result["cursor"]
            if result["data"]:
                try:
                    ws_send_frame(sock, WS_OP_TEXT, result["data"].encode("utf-8"))
                except OSError:
                    return
            if result["closed"]:
                try:
                    ws_send_frame(sock, WS_OP_CLOSE, b"")
                except OSError:
                    pass
                return

    output_thread = threading.Thread(target=send_output, daemon=True)
    output_thread.start()

    try:
        while True:
            frame = ws_read_frame(reader)
            if frame is None:
                break
            opcode, payload = frame
            if opcode == WS_OP_TEXT:
                session.write(payload.decode("utf-8", errors="replace"))
            elif opcode == WS_OP_PING:
                ws_send_frame(sock, WS_OP_PONG, payload)
            elif opcode == WS_OP_CLOSE:
                try:
                    ws_send_frame(sock, WS_OP_CLOSE, b"")
                except OSError:
                    pass
                break
    except OSError:
        pass
    finally:
        output_thread.join(timeout=2.0)


def ws_handle_connection(conn, service, data=None):
    """Handle a raw WebSocket connection: handshake then relay to a session."""
    try:
        if data is None:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
        request_line = data.split(b"\r\n")[0].decode()
        path = request_line.split(" ")[1] if " " in request_line else ""
        headers = {}
        for line in data.decode().split("\r\n")[1:]:
            if ": " in line:
                key, value = line.split(": ", 1)
                headers[key.lower()] = value
        parts = path.strip("/").split("/")
        if len(parts) < 4 or parts[0] != "api" or parts[1] != "sessions" or parts[3] != "ws":
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        session_id = parts[2]
        client_key = headers.get("sec-websocket-key", "")
        if not client_key:
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        try:
            session = service._get_session(session_id)
        except KeyError:
            conn.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")
            return
        accept = ws_accept_key(client_key)
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode()
        conn.sendall(response)
        ws_relay(conn, conn, session)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def ws_serve_forever(host, port, service):
    """Accept WebSocket connections on a dedicated socket."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(8)
    try:
        while True:
            conn, _addr = srv.accept()
            thread = threading.Thread(
                target=ws_handle_connection, args=(conn, service), daemon=True
            )
            thread.start()
    except OSError:
        pass
    finally:
        srv.close()


class WebTerminalServer:
    """Threaded HTTP server that exposes PTY-backed terminal sessions."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.shell = shell or os.environ.get("SHELL") or "/bin/sh"
        self.cwd = cwd or str(Path.cwd())
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._running = False
        self._sessions: Dict[str, TerminalSession] = {}
        self._sessions_lock = threading.Lock()

    def is_running(self) -> bool:
        return self._running

    def serve_forever(self) -> None:
        class TerminalHTTPServer(ThreadingHTTPServer):
            daemon_threads = True

        self._httpd = TerminalHTTPServer((self.host, self.port), TerminalRequestHandler)
        self._httpd.service = self
        self.ws_port = self.port + DEFAULT_WS_PORT_OFFSET
        ws_thread = threading.Thread(
            target=ws_serve_forever, args=(self.host, self.ws_port, self), daemon=True
        )
        ws_thread.start()
        self._running = True
        try:
            self._httpd.serve_forever()
        finally:
            self._running = False
            self.close_all_sessions()
            self._httpd.server_close()

    def shutdown(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()

    def create_session(self) -> Dict[str, object]:
        session = TerminalSession(
            shell=self.shell,
            cwd=self.cwd,
            cols=DEFAULT_COLS,
            rows=DEFAULT_ROWS,
        )
        with self._sessions_lock:
            self._sessions[session.session_id] = session
        return session.info()

    def list_sessions(self) -> Dict[str, object]:
        with self._sessions_lock:
            sessions = [session.info() for session in self._sessions.values()]
        sessions.sort(key=lambda session: session["created_at"], reverse=True)
        return {"sessions": sessions}

    def read_output(self, session_id: str, cursor: int, timeout: float) -> Dict[str, object]:
        return self._get_session(session_id).read(cursor=cursor, timeout=timeout)

    def write_input(self, session_id: str, data: str) -> Dict[str, object]:
        self._get_session(session_id).write(data)
        return {"ok": True}

    def resize_session(self, session_id: str, cols: int, rows: int) -> Dict[str, int]:
        return self._get_session(session_id).resize(cols=cols, rows=rows)

    def close_session(self, session_id: str) -> Dict[str, object]:
        with self._sessions_lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            raise KeyError(session_id)
        session.close()
        return {"ok": True}

    def close_all_sessions(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def _get_session(self, session_id: str) -> TerminalSession:
        with self._sessions_lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session


class TerminalRequestHandler(BaseHTTPRequestHandler):
    """HTTP routes for the browser terminal."""

    server_version = "SaibaiTerminal/0.1"

    @property
    def service(self) -> WebTerminalServer:
        return self.server.service

    def setup(self):
        """Intercept WebSocket upgrades before BaseHTTPRequestHandler creates buffered I/O."""
        self.connection = self.request
        if self.timeout is not None:
            self.connection.settimeout(self.timeout)
        self._ws_request_data = None
        try:
            peeked = self.connection.recv(4096, socket.MSG_PEEK)
        except OSError:
            peeked = b""
        if b"Upgrade: websocket" in peeked or b"upgrade: websocket" in peeked:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = self.connection.recv(4096)
                if not chunk:
                    break
                data += chunk
            self._ws_request_data = data
        else:
            super().setup()

    def handle(self):
        if self._ws_request_data is not None:
            ws_handle_connection(self.connection, self.service, self._ws_request_data)
            return
        super().handle()

    def finish(self):
        if self._ws_request_data is not None:
            return
        super().finish()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(TERMINAL_PAGE)
            return
        if parsed.path == "/api/sessions":
            self._send_json(self.service.list_sessions())
            return
        if parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/output"):
            session_id = parsed.path.split("/")[3]
            query = parse_qs(parsed.query)
            cursor = int(query.get("cursor", ["0"])[0])
            timeout = float(query.get("timeout", [str(DEFAULT_OUTPUT_TIMEOUT)])[0])
            try:
                payload = self.service.read_output(session_id, cursor, timeout)
            except KeyError:
                self._send_error(HTTPStatus.NOT_FOUND, "Session not found")
                return
            self._send_json(payload)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "Route not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/sessions":
            payload = self.service.create_session()
            self._send_json(payload, status=HTTPStatus.CREATED)
            return
        if parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/input"):
            session_id = parsed.path.split("/")[3]
            payload = self._read_json()
            try:
                response = self.service.write_input(session_id, payload.get("data", ""))
            except KeyError:
                self._send_error(HTTPStatus.NOT_FOUND, "Session not found")
                return
            self._send_json(response)
            return
        if parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/resize"):
            session_id = parsed.path.split("/")[3]
            payload = self._read_json()
            try:
                response = self.service.resize_session(
                    session_id,
                    cols=payload.get("cols", DEFAULT_COLS),
                    rows=payload.get("rows", DEFAULT_ROWS),
                )
            except KeyError:
                self._send_error(HTTPStatus.NOT_FOUND, "Session not found")
                return
            self._send_json(response)
            return
        if parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/close"):
            session_id = parsed.path.split("/")[3]
            try:
                response = self.service.close_session(session_id)
            except KeyError:
                self._send_error(HTTPStatus.NOT_FOUND, "Session not found")
                return
            self._send_json(response)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "Route not found")

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/sessions/"):
            session_id = parsed.path.split("/")[3]
            try:
                response = self.service.close_session(session_id)
            except KeyError:
                self._send_error(HTTPStatus.NOT_FOUND, "Session not found")
                return
            self._send_json(response)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "Route not found")

    def log_message(self, format: str, *args) -> None:
        return

    def _read_json(self) -> Dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload: Dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the browser terminal server."""
    parser = argparse.ArgumentParser(description="Run a browser terminal server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--shell", default=None)
    parser.add_argument("--cwd", default=None)
    return parser


def main() -> None:
    """Run the web terminal server until interrupted."""
    args = build_argument_parser().parse_args()
    server = WebTerminalServer(
        host=args.host,
        port=args.port,
        shell=args.shell,
        cwd=args.cwd,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
