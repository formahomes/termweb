# ABOUTME: Selenium-based debug script to diagnose WebSocket disconnects in Firefox.
# ABOUTME: Starts the server, connects via Firefox, and reports WebSocket state + console logs.

import json
import socket
import subprocess
import sys
import textwrap
import threading
import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def find_free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def start_server(port):
    """Start the terminal server in a subprocess."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "web_terminal.server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc


def drain_output(proc, lines):
    """Read server stdout/stderr into a list."""
    for line in proc.stdout:
        lines.append(line.rstrip())
        print(f"  [SERVER] {line.rstrip()}", flush=True)


def wait_for_server(host, port, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main():
    port = find_free_port()
    host = "127.0.0.1"
    url = f"http://{host}:{port}"

    print(f"Starting server on {url}")
    proc = start_server(port)
    server_lines = []
    drain_thread = threading.Thread(target=drain_output, args=(proc, server_lines), daemon=True)
    drain_thread.start()

    if not wait_for_server(host, port):
        print("ERROR: Server didn't start")
        proc.kill()
        return 1

    print("Server is up. Launching Firefox via Selenium...")

    opts = Options()
    # Enable browser console logging
    opts.set_preference("devtools.console.stdout.content", True)
    opts.add_argument("--headless")

    driver = webdriver.Firefox(options=opts)
    try:
        driver.get(url)
        print(f"Page loaded: {driver.title}")

        # Inject JS to intercept WebSocket lifecycle with detailed logging
        driver.execute_script(textwrap.dedent("""
            window.__wsDebug = [];
            const origWS = window.WebSocket;
            window.WebSocket = function(url, protocols) {
                const ws = new origWS(url, protocols);
                const log = (msg) => {
                    const entry = Date.now() + ': ' + msg;
                    window.__wsDebug.push(entry);
                    console.log('[WS DEBUG] ' + entry);
                };
                log('new WebSocket(' + url + ')');
                ws.addEventListener('open', () => log('EVENT open, readyState=' + ws.readyState));
                ws.addEventListener('message', (e) => log('EVENT message, len=' + (e.data ? e.data.length : 0)));
                ws.addEventListener('close', (e) => log('EVENT close, code=' + e.code + ' reason=' + JSON.stringify(e.reason) + ' wasClean=' + e.wasClean));
                ws.addEventListener('error', (e) => log('EVENT error'));
                return ws;
            };
            window.WebSocket.OPEN = origWS.OPEN;
            window.WebSocket.CLOSED = origWS.CLOSED;
            window.WebSocket.CONNECTING = origWS.CONNECTING;
            window.WebSocket.CLOSING = origWS.CLOSING;
            window.WebSocket.prototype = origWS.prototype;
        """))

        # The page auto-creates a session on load (or we need to click New).
        # Wait for the WebSocket to connect by polling the page's ws variable.
        print("Waiting for session + WebSocket connection...")
        time.sleep(2)

        # If no session yet, try clicking "New Session"
        has_session = driver.execute_script("return typeof sessionId !== 'undefined' && sessionId !== null;")
        if not has_session:
            print("No auto-session, clicking New...")
            buttons = driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                if "new" in btn.text.lower() or "create" in btn.text.lower():
                    btn.click()
                    break

        # Wait for WebSocket events to accumulate
        print("Waiting 5 seconds for WebSocket events...")
        time.sleep(5)

        # Collect debug info
        ws_debug = driver.execute_script("return window.__wsDebug || [];")
        ws_state = driver.execute_script("""
            if (typeof ws !== 'undefined' && ws !== null) {
                return {readyState: ws.readyState, url: ws.url, protocol: ws.protocol};
            }
            return null;
        """)
        status_text = driver.execute_script("""
            var el = document.getElementById('status');
            return el ? el.textContent : 'not found';
        """)

        print("\n=== RESULTS ===")
        print(f"Status bar text: {status_text}")
        print(f"WebSocket object state: {json.dumps(ws_state, indent=2)}")
        print(f"WebSocket debug log ({len(ws_debug)} entries):")
        for entry in ws_debug:
            print(f"  {entry}")

        # Grab browser console logs if available
        try:
            logs = driver.get_log("browser")
            if logs:
                print(f"\nBrowser console ({len(logs)} entries):")
                for log in logs:
                    print(f"  [{log['level']}] {log['message']}")
        except Exception as e:
            print(f"\n(Could not get browser logs: {e})")

        # Print server output
        print(f"\nServer output ({len(server_lines)} lines):")
        for line in server_lines:
            print(f"  {line}")

    finally:
        driver.quit()
        proc.kill()
        proc.wait()

    return 0


if __name__ == "__main__":
    sys.exit(main())
