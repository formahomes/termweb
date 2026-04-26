# ABOUTME: Serves a browser terminal page and backs each session with a local PTY shell.
# ABOUTME: Streams terminal I/O over WebSocket and accepts session management over HTTP.

import argparse
import base64
import hashlib
import json
import os
import queue
import select
import shutil
import socket
import subprocess
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_COLS = 120
DEFAULT_ROWS = 32
DEFAULT_OUTPUT_TIMEOUT = 0.25
DEFAULT_READ_SIZE = 4096
PROCESS_EXIT_TIMEOUT = 1.0
INPUT_BATCH_DELAY = 0.01
DEFAULT_TAIL_LINES = 2000
SESSION_PORT_BASE = 4000
TMUX_SESSION_PREFIX = "termweb-"
DEFAULT_SETTINGS_PATH = Path.home() / ".termweb-runtime" / "settings.json"
SETTINGS_NTFY_URL_KEY = "ntfy_url"
DEFAULT_NTFY_URL = ""
NTFY_TITLE = "Termweb"
NTFY_TAGS = "termweb"
NTFY_CACHE = "no"
NTFY_CONTENT_TYPE = "text/plain; charset=utf-8"
NTFY_DONE_EVENT = "done"
NTFY_TIMEOUT_SECONDS = 2.0
NTFY_DONE_MESSAGE = 'Session "{label}" is done.'
TMUX_BIN = (
    shutil.which("tmux")
    or shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin")
    or "tmux"
)
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "static"


def list_directories(prefix: str) -> Dict[str, list]:
    """Return directories matching a path prefix for autocomplete."""
    if not prefix:
        return {"paths": []}
    parent = Path(prefix)
    if parent.is_dir():
        # List children of this directory
        try:
            entries = sorted(
                str(entry) for entry in parent.iterdir()
                if entry.is_dir() and not entry.name.startswith(".")
            )
        except PermissionError:
            entries = []
    else:
        # Prefix is partial — list siblings matching the prefix
        parent_dir = parent.parent
        partial = parent.name
        try:
            entries = sorted(
                str(entry) for entry in parent_dir.iterdir()
                if entry.is_dir() and entry.name.startswith(partial)
                and not entry.name.startswith(".")
            )
        except (PermissionError, FileNotFoundError):
            entries = []
    return {"paths": entries[:50]}


def normalize_ntfy_url(value: object) -> str:
    """Return a validated ntfy publish URL, or an empty string when disabled."""
    if not isinstance(value, str):
        raise ValueError("ntfy URL must be a string")
    ntfy_url = value.strip()
    if not ntfy_url:
        return DEFAULT_NTFY_URL
    if "://" not in ntfy_url:
        ntfy_url = "https://" + ntfy_url
    parsed = urlparse(ntfy_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path in {"", "/"}:
        raise ValueError("ntfy URL must include an http(s) host and topic")
    return ntfy_url.rstrip("/")



class TerminalSession:
    """A single tmux-backed shell session using pipe-pane for output and send-keys for input."""

    _label_counter = 0
    _label_counter_lock = threading.Lock()

    @classmethod
    def _next_label(cls) -> str:
        with cls._label_counter_lock:
            cls._label_counter += 1
            return f"Session {cls._label_counter}"

    def __init__(self, shell: str, cwd: str, cols: int, rows: int,
                 session_id: Optional[str] = None, label: Optional[str] = None,
                 port: Optional[int] = None, repo_path: Optional[str] = None,
                 worktree_path: Optional[str] = None,
                 server_url: Optional[str] = None,
                 phone_notifications_enabled: bool = False):
        self.shell = shell
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.session_id = session_id or uuid.uuid4().hex
        self.label = label or self._next_label()
        self.port = port
        self.repo_path = repo_path
        self.worktree_path = worktree_path
        self.server_url = server_url
        self.phone_notifications_enabled = phone_notifications_enabled
        self.created_at = time.time()
        self.status = "idle"
        self._buffer = ""
        self._closed = False
        self._lock = threading.Lock()
        self._output_ready = threading.Condition(self._lock)
        self._input_pending = ""
        self._input_flusher = None
        self._pending_esc = ""
        self._oob_subscribers: list = []
        self._oob_lock = threading.Lock()
        self._tmux_name = TMUX_SESSION_PREFIX + self.session_id
        self._create_tmux_session(shell, cwd, cols, rows)
        self._start_output_pipe()

    @classmethod
    def recover(cls, session_id: str, shell: str, cwd: str) -> "TerminalSession":
        """Reattach to an existing tmux session without creating a new one."""
        obj = cls.__new__(cls)
        obj.shell = shell
        obj.cwd = cwd
        obj.session_id = session_id
        obj.label = cls._next_label()
        obj.port = None
        obj.repo_path = None
        obj.worktree_path = None
        obj.server_url = None
        obj.phone_notifications_enabled = False
        obj.created_at = time.time()
        obj.status = "idle"
        obj._closed = False
        obj._lock = threading.Lock()
        obj._output_ready = threading.Condition(obj._lock)
        obj._input_pending = ""
        obj._input_flusher = None
        obj._pending_esc = ""
        obj._oob_subscribers = []
        obj._oob_lock = threading.Lock()
        obj._tmux_name = TMUX_SESSION_PREFIX + session_id
        info = subprocess.run(
            [TMUX_BIN, "display-message", "-t", obj._tmux_name, "-p",
             "#{window_width} #{window_height}"],
            capture_output=True, text=True,
        )
        if info.returncode == 0:
            parts = info.stdout.strip().split()
            obj.cols = int(parts[0])
            obj.rows = int(parts[1])
        else:
            obj.cols = DEFAULT_COLS
            obj.rows = DEFAULT_ROWS
        # Seed buffer with current pane content so clients see something immediately
        capture = subprocess.run(
            [TMUX_BIN, "capture-pane", "-t", obj._tmux_name, "-p", "-e"],
            capture_output=True, text=True,
        )
        if capture.returncode == 0 and capture.stdout:
            # Convert \n to \r\n for xterm.js and strip trailing blank lines
            lines = capture.stdout.rstrip("\n").split("\n")
            obj._buffer = "\r\n".join(lines) + "\r\n"
        else:
            obj._buffer = ""
        obj._start_output_pipe()
        return obj

    def _create_tmux_session(self, shell: str, cwd: str, cols: int, rows: int) -> None:
        """Create a detached tmux session."""
        env = os.environ.copy()
        env["PATH"] = (
            "/opt/homebrew/bin:/opt/homebrew/sbin:"
            "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        )
        env.setdefault("HOME", str(Path.home()))
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("LANG", "en_US.UTF-8")
        env.setdefault("LC_ALL", "en_US.UTF-8")
        session_env = []
        env["TERMWEB_SESSION_ID"] = self.session_id
        session_env.append(f"TERMWEB_SESSION_ID={self.session_id}")
        if self.server_url:
            env["TERMWEB_URL"] = self.server_url
            session_env.append(f"TERMWEB_URL={self.server_url}")
        if self.port is not None:
            env["TERMWEB_PORT"] = str(self.port)
            session_env.append(f"TERMWEB_PORT={self.port}")
        cmd = [TMUX_BIN, "new-session", "-d",
               "-s", self._tmux_name,
               "-x", str(cols), "-y", str(rows)]
        for kv in session_env:
            cmd.extend(["-e", kv])
        cmd.append(shell)
        subprocess.run(cmd, cwd=cwd, env=env, check=True)

    def _start_output_pipe(self) -> None:
        """Set up a named pipe to stream pane output."""
        self._fifo_path = f"/tmp/termweb-{os.getpid()}-{self.session_id}.pipe"
        try:
            os.mkfifo(self._fifo_path)
        except FileExistsError:
            os.unlink(self._fifo_path)
            os.mkfifo(self._fifo_path)
        self._fifo_fd = os.open(self._fifo_path, os.O_RDONLY | os.O_NONBLOCK)
        subprocess.run(
            [TMUX_BIN, "pipe-pane", "-O", "-t", self._tmux_name,
             f"cat > {self._fifo_path}"],
            check=True,
        )
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

    def tail_cursor(self, n_lines: int) -> int:
        """Return a buffer cursor that begins at the last n_lines of output."""
        with self._lock:
            buf = self._buffer
        if not buf or n_lines <= 0:
            return len(buf)
        idx = len(buf)
        if buf[idx - 1] == "\n":
            idx -= 1
        for _ in range(n_lines):
            nl = buf.rfind("\n", 0, idx)
            if nl == -1:
                return 0
            idx = nl
        return idx + 1

    def full_buffer(self) -> str:
        """Return the full accumulated output buffer."""
        with self._lock:
            return self._buffer

    def write(self, data: str) -> None:
        if not data:
            return
        with self._lock:
            if self._closed:
                raise RuntimeError("Session is closed")
            self._input_pending += data
            if self._input_flusher is None:
                self._input_flusher = threading.Timer(
                    INPUT_BATCH_DELAY, self._flush_input)
                self._input_flusher.start()

    def _flush_input(self) -> None:
        """Send accumulated keystrokes to tmux in one call."""
        with self._lock:
            data = self._input_pending
            self._input_pending = ""
            self._input_flusher = None
        if not data:
            return
        hex_args = " ".join(f"{b:02x}" for b in data.encode("utf-8"))
        subprocess.run(
            [TMUX_BIN, "send-keys", "-H", "-t", self._tmux_name] + hex_args.split(),
            capture_output=True,
        )

    def resize(self, cols: int, rows: int) -> Dict[str, int]:
        cols = max(int(cols), 20)
        rows = max(int(rows), 8)
        with self._lock:
            if self._closed:
                raise RuntimeError("Session is closed")
            self.cols = cols
            self.rows = rows
        subprocess.run(
            [TMUX_BIN, "resize-window", "-t", self._tmux_name,
             "-x", str(cols), "-y", str(rows)],
            capture_output=True,
        )
        return {"cols": cols, "rows": rows}

    def detach(self) -> None:
        """Stop output pipe but leave the tmux session running."""
        with self._lock:
            if self._input_flusher is not None:
                self._input_flusher.cancel()
                self._input_flusher = None
        self._flush_input()
        with self._output_ready:
            if self._closed:
                return
            self._closed = True
            self._output_ready.notify_all()
        subprocess.run(
            [TMUX_BIN, "pipe-pane", "-t", self._tmux_name],
            capture_output=True,
        )
        try:
            os.close(self._fifo_fd)
        except OSError:
            pass
        try:
            os.unlink(self._fifo_path)
        except OSError:
            pass

    def close(self) -> None:
        """Detach and kill the tmux session."""
        self.detach()
        subprocess.run(
            [TMUX_BIN, "kill-session", "-t", self._tmux_name],
            capture_output=True,
        )

    def _current_pane_path(self) -> str:
        """Query tmux for the current working directory of the pane."""
        result = subprocess.run(
            [TMUX_BIN, "display-message", "-t", self._tmux_name, "-p",
             "#{pane_current_path}"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return self.cwd

    def info(self) -> Dict[str, object]:
        with self._lock:
            closed = self._closed
        return {
            "session_id": self.session_id,
            "label": self.label,
            "cols": self.cols,
            "rows": self.rows,
            "cwd": self._current_pane_path() if not closed else self.cwd,
            "shell": self.shell,
            "port": self.port,
            "repo_path": self.repo_path,
            "worktree_path": self.worktree_path,
            "created_at": self.created_at,
            "status": self.status,
            "phone_notifications_enabled": self.phone_notifications_enabled,
            "closed": closed,
        }

    def subscribe_oob(self, callback) -> None:
        with self._oob_lock:
            self._oob_subscribers.append(callback)

    def unsubscribe_oob(self, callback) -> None:
        with self._oob_lock:
            try:
                self._oob_subscribers.remove(callback)
            except ValueError:
                pass

    def _broadcast_oob(self, data: str) -> None:
        with self._oob_lock:
            subs = list(self._oob_subscribers)
        for cb in subs:
            try:
                cb(data)
            except Exception:
                pass

    _ITERM2_START = "\x1b]1337;"

    def _split_iterm2(self, raw: str) -> "Tuple[str, list]":
        """Split a chunk into (normal_text, [oob_sequences]).

        Carries partial escape sequences across chunks via self._pending_esc.
        """
        data = self._pending_esc + raw
        self._pending_esc = ""
        start = self._ITERM2_START
        text_parts = []
        oob_parts = []
        i = 0
        n = len(data)
        while i < n:
            j = data.find(start, i)
            if j == -1:
                safe_end = n
                tail_begin = max(i, n - len(start) + 1)
                for k in range(tail_begin, n):
                    if start.startswith(data[k:]):
                        safe_end = k
                        self._pending_esc = data[k:]
                        break
                text_parts.append(data[i:safe_end])
                return "".join(text_parts), oob_parts
            text_parts.append(data[i:j])
            k = j + len(start)
            bel = data.find("\x07", k)
            st = data.find("\x1b\\", k)
            candidates = [x for x in (bel, st) if x != -1]
            if not candidates:
                self._pending_esc = data[j:]
                return "".join(text_parts), oob_parts
            term_pos = min(candidates)
            term_len = 1 if term_pos == bel else 2
            oob_parts.append(data[j:term_pos + term_len])
            i = term_pos + term_len
        return "".join(text_parts), oob_parts

    def _read_output(self) -> None:
        """Read pane output from the named pipe."""
        try:
            while not self._closed:
                try:
                    ready, _, _ = select.select([self._fifo_fd], [], [], 0.1)
                except (OSError, ValueError):
                    break
                if not ready:
                    continue
                try:
                    chunk = os.read(self._fifo_fd, DEFAULT_READ_SIZE)
                except OSError:
                    break
                if not chunk:
                    time.sleep(0.05)
                    continue
                raw = chunk.decode("utf-8", errors="replace")
                text, oob_seqs = self._split_iterm2(raw)
                if text:
                    with self._output_ready:
                        self._buffer += text
                        self._output_ready.notify_all()
                for seq in oob_seqs:
                    self._broadcast_oob(seq)
        finally:
            with self._output_ready:
                self._closed = True
                self._output_ready.notify_all()


WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_OP_TEXT = 0x1
WS_OP_BINARY = 0x2
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


WS_BIN_META = 0x00  # typed binary frame: JSON cursor metadata
WS_BIN_OOB = 0x01   # typed binary frame: out-of-band terminal data (write, don't count)


def ws_relay(sock, reader, session: "TerminalSession", cursor_hint: Optional[int] = None) -> None:
    """Relay data between a WebSocket and a PTY session until either side closes."""
    with session._lock:
        buffer_len = len(session._buffer)
    tail_start = session.tail_cursor(DEFAULT_TAIL_LINES)
    if cursor_hint is not None and 0 <= cursor_hint <= buffer_len:
        cursor = max(cursor_hint, tail_start)
    else:
        cursor = tail_start

    send_lock = threading.Lock()
    dead = [False]

    def send(op: int, payload: bytes) -> bool:
        if dead[0]:
            return False
        with send_lock:
            try:
                ws_send_frame(sock, op, payload)
                return True
            except OSError:
                dead[0] = True
                return False

    if not send(WS_OP_BINARY, bytes([WS_BIN_META]) + json.dumps({"cursor": cursor}).encode("utf-8")):
        return

    def on_oob(seq: str) -> None:
        send(WS_OP_BINARY, bytes([WS_BIN_OOB]) + seq.encode("utf-8"))

    session.subscribe_oob(on_oob)

    def send_output():
        nonlocal cursor
        while not session._closed and not dead[0]:
            result = session.read(cursor=cursor, timeout=0.5)
            cursor = result["cursor"]
            if result["data"]:
                if not send(WS_OP_TEXT, result["data"].encode("utf-8")):
                    return
            if result["closed"]:
                send(WS_OP_CLOSE, b"")
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
                send(WS_OP_PONG, payload)
            elif opcode == WS_OP_CLOSE:
                send(WS_OP_CLOSE, b"")
                break
    except OSError:
        pass
    finally:
        dead[0] = True
        session.unsubscribe_oob(on_oob)
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
        parsed_path = urlparse(path)
        parts = parsed_path.path.strip("/").split("/")
        if len(parts) < 4 or parts[0] != "api" or parts[1] != "sessions" or parts[3] != "ws":
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        session_id = parts[2]
        query = parse_qs(parsed_path.query)
        cursor_hint: Optional[int] = None
        if "cursor" in query:
            try:
                cursor_hint = int(query["cursor"][0])
            except (ValueError, IndexError):
                cursor_hint = None
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
        ws_relay(conn, conn, session, cursor_hint=cursor_hint)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


class WebTerminalServer:
    """Threaded HTTP server that exposes PTY-backed terminal sessions."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        shell: Optional[str] = None,
        cwd: Optional[str] = None,
        static_dir: Optional[Path] = None,
        settings_path: Optional[Path] = None,
    ):
        self.host = host
        self.port = port
        self.shell = shell or os.environ.get("SHELL") or "/bin/sh"
        self.cwd = cwd or str(Path.home())
        self.static_dir = Path(static_dir) if static_dir else DEFAULT_STATIC_DIR
        self.settings_path = Path(settings_path) if settings_path else DEFAULT_SETTINGS_PATH
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._running = False
        self._sessions: Dict[str, TerminalSession] = {}
        self._sessions_lock = threading.Lock()
        self._sse_clients: list = []
        self._sse_lock = threading.Lock()
        self._settings_lock = threading.Lock()
        self._settings = {SETTINGS_NTFY_URL_KEY: DEFAULT_NTFY_URL}
        self._load_settings()

    def is_running(self) -> bool:
        return self._running

    def _load_settings(self) -> None:
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            ntfy_url = normalize_ntfy_url(payload.get(SETTINGS_NTFY_URL_KEY, DEFAULT_NTFY_URL))
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return
        with self._settings_lock:
            self._settings[SETTINGS_NTFY_URL_KEY] = ntfy_url

    def _save_settings(self) -> None:
        with self._settings_lock:
            payload = dict(self._settings)
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def settings_info(self) -> Dict[str, object]:
        with self._settings_lock:
            return dict(self._settings)

    def update_settings(self, ntfy_url: object) -> Dict[str, object]:
        normalized_url = normalize_ntfy_url(ntfy_url)
        with self._settings_lock:
            self._settings[SETTINGS_NTFY_URL_KEY] = normalized_url
        self._save_settings()
        return self.settings_info()

    def _cleanup_orphan_pipes(self) -> None:
        """Remove /tmp/termweb-*.pipe files whose owning server process is gone."""
        try:
            entries = os.listdir("/tmp")
        except OSError:
            return
        for name in entries:
            if not (name.startswith("termweb-") and name.endswith(".pipe")):
                continue
            middle = name[len("termweb-"):-len(".pipe")]
            pid_str, _, _ = middle.partition("-")
            path = os.path.join("/tmp", name)
            try:
                pid = int(pid_str)
            except ValueError:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            except PermissionError:
                continue

    def _recover_sessions(self) -> None:
        """Discover existing tmux sessions and reattach to them."""
        result = subprocess.run(
            [TMUX_BIN, "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return
        prefix = TMUX_SESSION_PREFIX
        for name in result.stdout.strip().splitlines():
            if not name.startswith(prefix):
                continue
            session_id = name[len(prefix):]
            if session_id in self._sessions:
                continue
            try:
                session = TerminalSession.recover(
                    session_id=session_id,
                    shell=self.shell,
                    cwd=self.cwd,
                )
                self._sessions[session_id] = session
            except Exception:
                pass

    def serve_forever(self) -> None:
        class TerminalHTTPServer(ThreadingHTTPServer):
            daemon_threads = True

        self._cleanup_orphan_pipes()
        self._recover_sessions()
        self._httpd = TerminalHTTPServer((self.host, self.port), TerminalRequestHandler)
        self._httpd.service = self
        self._running = True
        try:
            self._httpd.serve_forever()
        finally:
            self._running = False
            self.detach_all_sessions()
            self._httpd.server_close()

    def shutdown(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()

    def _next_port(self) -> int:
        """Find the next available port starting from SESSION_PORT_BASE."""
        with self._sessions_lock:
            used = {s.port for s in self._sessions.values() if s.port is not None}
        port = SESSION_PORT_BASE
        while port in used:
            port += 1
        return port

    def create_session(self, label: Optional[str] = None,
                       port: Optional[int] = None,
                       repo_path: Optional[str] = None,
                       branch: Optional[str] = None) -> Dict[str, object]:
        if port is None:
            port = self._next_port()
        cwd = self.cwd
        worktree_path = None
        if repo_path and branch:
            worktree_path = os.path.join(
                repo_path, ".git", "termweb-worktrees", branch,
            )
            subprocess.run(
                ["git", "-C", repo_path, "worktree", "add", worktree_path, "-b", branch],
                capture_output=True, text=True,
            )
            if not os.path.isdir(worktree_path):
                # Branch already exists — check it out instead of creating
                subprocess.run(
                    ["git", "-C", repo_path, "worktree", "add", worktree_path, branch],
                    check=True, capture_output=True, text=True,
                )
            cwd = worktree_path
        session = TerminalSession(
            shell=self.shell,
            cwd=cwd,
            cols=DEFAULT_COLS,
            rows=DEFAULT_ROWS,
            label=label,
            port=port,
            repo_path=repo_path,
            worktree_path=worktree_path,
            server_url=f"http://{self.host}:{self.port}",
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

    def rename_session(self, session_id: str, label: str) -> Dict[str, object]:
        """Rename a session's label."""
        if not label:
            raise ValueError("Label must not be empty")
        session = self._get_session(session_id)
        with session._lock:
            session.label = label
        return session.info()

    def set_phone_notifications(self, session_id: str, enabled: object) -> Dict[str, object]:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        session = self._get_session(session_id)
        with session._lock:
            session.phone_notifications_enabled = enabled
        return session.info()

    def resize_session(self, session_id: str, cols: int, rows: int) -> Dict[str, int]:
        return self._get_session(session_id).resize(cols=cols, rows=rows)

    def close_session(self, session_id: str) -> Dict[str, object]:
        with self._sessions_lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            raise KeyError(session_id)
        repo_path = session.repo_path
        worktree_path = session.worktree_path
        session.close()
        if repo_path and worktree_path and os.path.isdir(worktree_path):
            subprocess.run(
                ["git", "-C", repo_path, "worktree", "remove", "--force", worktree_path],
                capture_output=True, text=True,
            )
        return {"ok": True}

    def detach_all_sessions(self) -> None:
        """Detach all PTY attachments without killing tmux sessions."""
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.detach()

    def close_all_sessions(self) -> None:
        """Kill all tmux sessions owned by this server."""
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    VALID_NOTIFY_EVENTS = {"processing", "done", "idle"}

    def notify_session(self, session_id: str, event: str) -> Dict[str, object]:
        """Update a session's status and broadcast to SSE clients."""
        if event not in self.VALID_NOTIFY_EVENTS:
            raise ValueError(f"Invalid event: {event}")
        session = self._get_session(session_id)
        with session._lock:
            session.status = event
            phone_notifications_enabled = session.phone_notifications_enabled
            label = session.label
        self._broadcast_sse({"session_id": session_id, "event": event})
        if event == NTFY_DONE_EVENT and phone_notifications_enabled:
            self._publish_ntfy_done(label)
        return {"ok": True}

    def _publish_ntfy_done(self, label: str) -> None:
        with self._settings_lock:
            ntfy_url = self._settings.get(SETTINGS_NTFY_URL_KEY, DEFAULT_NTFY_URL)
        if not ntfy_url:
            return
        message = NTFY_DONE_MESSAGE.format(label=label)
        request = Request(
            ntfy_url,
            data=message.encode("utf-8"),
            headers={
                "Title": NTFY_TITLE,
                "Tags": NTFY_TAGS,
                "Cache": NTFY_CACHE,
                "Content-Type": NTFY_CONTENT_TYPE,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=NTFY_TIMEOUT_SECONDS) as response:
                response.read()
        except OSError:
            return

    def _broadcast_sse(self, data: dict) -> None:
        """Send an event to all connected SSE clients."""
        message = f"data: {json.dumps(data)}\n\n"
        with self._sse_lock:
            dead = []
            for queue in self._sse_clients:
                try:
                    queue.put_nowait(message)
                except Exception:
                    dead.append(queue)
            for queue in dead:
                self._sse_clients.remove(queue)

    def register_sse_client(self):
        """Register a new SSE client and return its event queue."""
        q = queue.Queue()
        with self._sse_lock:
            self._sse_clients.append(q)
        return q

    def unregister_sse_client(self, q) -> None:
        """Remove an SSE client queue."""
        with self._sse_lock:
            try:
                self._sse_clients.remove(q)
            except ValueError:
                pass

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
            self._send_static_html("terminal.html")
            return
        if parsed.path == "/dashboard":
            self._send_static_html("dashboard.html")
            return
        if parsed.path == "/api/sessions":
            self._send_json(self.service.list_sessions())
            return
        if parsed.path == "/api/settings":
            self._send_json(self.service.settings_info())
            return
        if parsed.path == "/api/paths":
            query = parse_qs(parsed.query)
            prefix = query.get("prefix", [""])[0]
            self._send_json(list_directories(prefix))
            return
        if parsed.path == "/api/events":
            self._serve_sse()
            return
        if parsed.path == "/api/sounds/glass":
            self._serve_sound_aiff_as_wav("/System/Library/Sounds/Glass.aiff")
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
        if parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/history"):
            session_id = parsed.path.split("/")[3]
            try:
                session = self.service._get_session(session_id)
            except KeyError:
                self._send_error(HTTPStatus.NOT_FOUND, "Session not found")
                return
            body = session.full_buffer().encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "Route not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/sessions":
            body = self._read_json()
            payload = self.service.create_session(
                label=body.get("label"),
                port=body.get("port"),
                repo_path=body.get("repo_path"),
                branch=body.get("branch"),
            )
            self._send_json(payload, status=HTTPStatus.CREATED)
            return
        if parsed.path == "/api/settings":
            payload = self._read_json()
            try:
                response = self.service.update_settings(
                    payload.get(SETTINGS_NTFY_URL_KEY, DEFAULT_NTFY_URL)
                )
            except ValueError as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send_json(response)
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
        if parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/rename"):
            session_id = parsed.path.split("/")[3]
            payload = self._read_json()
            label = payload.get("label", "")
            try:
                response = self.service.rename_session(session_id, label)
            except KeyError:
                self._send_error(HTTPStatus.NOT_FOUND, "Session not found")
                return
            except ValueError as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send_json(response)
            return
        if parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/notifications"):
            session_id = parsed.path.split("/")[3]
            payload = self._read_json()
            try:
                response = self.service.set_phone_notifications(session_id, payload.get("enabled"))
            except KeyError:
                self._send_error(HTTPStatus.NOT_FOUND, "Session not found")
                return
            except ValueError as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send_json(response)
            return
        if parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/notify"):
            session_id = parsed.path.split("/")[3]
            payload = self._read_json()
            event = payload.get("event", "")
            try:
                response = self.service.notify_session(session_id, event)
            except KeyError:
                self._send_error(HTTPStatus.NOT_FOUND, "Session not found")
                return
            except ValueError as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
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

    def _send_static_html(self, name: str) -> None:
        path = self.service.static_dir / name
        try:
            body = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._send_error(HTTPStatus.NOT_FOUND, f"Static file not found: {name}")
            return
        self._send_html(body)

    def _send_json(self, payload: Dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self) -> None:
        """Stream Server-Sent Events to the client until disconnect."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = self.service.register_sse_client()
        try:
            while True:
                try:
                    message = q.get(timeout=30.0)
                    self.wfile.write(message.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # Send keepalive comment
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.service.unregister_sse_client(q)

    _wav_cache: Dict[str, bytes] = {}

    def _serve_sound_aiff_as_wav(self, path: str) -> None:
        """Convert an AIFF file to WAV and serve it. Caches the result."""
        if path in self._wav_cache:
            data = self._wav_cache[path]
        else:
            import tempfile
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            try:
                subprocess.run(
                    ["afconvert", path, tmp.name, "-d", "LEI16", "-f", "WAVE"],
                    check=True, capture_output=True,
                )
                with open(tmp.name, "rb") as fh:
                    data = fh.read()
            except (FileNotFoundError, subprocess.CalledProcessError):
                self._send_error(HTTPStatus.NOT_FOUND, "Sound file not found")
                return
            finally:
                os.unlink(tmp.name)
            self._wav_cache[path] = data
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the browser terminal server."""
    parser = argparse.ArgumentParser(description="Run a browser terminal server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--shell", default=None)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--static-dir", default=None,
                        help="Directory containing terminal.html and dashboard.html")
    return parser


def main() -> None:
    """Run the web terminal server until interrupted."""
    args = build_argument_parser().parse_args()
    server = WebTerminalServer(
        host=args.host,
        port=args.port,
        shell=args.shell,
        cwd=args.cwd,
        static_dir=args.static_dir,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
