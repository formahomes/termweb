# Termweb

Browser terminal service for remote shell access from a phone or browser.

## Requirements

- `python3`
- `tmux`
- `launchctl`
- macOS user session with access to `~/Library/LaunchAgents`

## Test

```bash
python3 -m pytest -q python/tests/test_web_terminal_server.py
python3 -m pytest -q python/tests/test_project_files.py
```

The touch scrolling browser test uses real xterm instances and an isolated tmux
session. It checks both terminal pages, scroll direction, mouse reporting, cursor
keys, ordinary scrollback, and gestures that should not send input. To include it:

```bash
npm install --prefix /tmp/termweb-browser-tests playwright@1.61.1
/tmp/termweb-browser-tests/node_modules/.bin/playwright install chromium webkit
NODE_PATH=/tmp/termweb-browser-tests/node_modules TERMWEB_BROWSER_TESTS=1 python3 -m pytest -q python/tests
NODE_PATH=/tmp/termweb-browser-tests/node_modules TERMWEB_BROWSER_TESTS=1 TERMWEB_BROWSER=webkit python3 -m pytest -q python/tests/test_web_terminal_server.py -k terminal_touch_scrolling
```

## Start

```bash
scripts/start_termweb.sh
```

The start script publishes the service under `com.termweb.web-terminal` on port `8765`.

## Access

- `http://127.0.0.1:8765/`
- `http://<lan-ip>:8765/`
- `http://<tailscale-ip>:8765/`

## Notes

- The launchd runtime copy is stored under `~/.termweb-runtime/`
- Restarting the script replaces any existing `com.termweb.web-terminal` job
