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
