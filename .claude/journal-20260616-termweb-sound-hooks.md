# 2026-06-16 — termweb sound silent: REAL root cause was SSE had no primer

## The actual root cause (supersedes the CLAUDE_CONFIG_DIR theory below)
The dashboard's `EventSource("/api/events")` never connected in Firefox —
console showed "Firefox can't establish a connection to the server at
.../api/events". No SSE => `done` events never reach the browser => no
sound. The CLAUDE_CONFIG_DIR/hook findings below were real but secondary;
even claude0 (with hooks) was silent because the browser side was dead.

Why: `_serve_sse` sent the response headers, then blocked on
`q.get(timeout=30)` sending NO body bytes until a real event or the 30s
keepalive. Firefox's EventSource will not establish a stream that is silent
after the headers — it reports a connection failure. curl never cared
(it doesn't time out), which is why every server-side test passed.

## How it was proven (controlled experiment, ports 8799-8804)
Stood up minimal SSE servers, identical except one variable each, tested in
Steve's actual Firefox:
- 8799 HTTP/1.0, ticks every 2s        -> GREEN (works)
- 8800 HTTP/1.1, ticks every 2s        -> GREEN  (=> HTTP version is NOT it)
- 8801 HTTP/1.0 + termweb's exact setup() MSG_PEEK intercept, ticks -> GREEN
                                          (=> the WS-intercept hack is NOT it)
- 8802 clean page -> EventSource at prod 8765/api/events -> RED (prod refuses FF)
- 8803 HTTP/1.0, SILENT after headers  -> RED  (reproduces the bug!)
- 8804 HTTP/1.0, one ": connected\n\n" then silent -> GREEN (fix proven)
Also ruled out earlier: IPv6 (`localhost`->::1 refused since server binds
0.0.0.0 IPv4-only; but 127.0.0.1 failed too, so not the cause).

## Fix
`python/src/web_terminal/server.py` `_serve_sse`: write `b": connected\n\n"`
+ flush immediately after `end_headers()`, before registering the client /
blocking on the queue. TDD test added: `test_sse_sends_initial_primer`
(reads /api/events, expects an immediate `:`-comment ending in `\n\n`;
times out red without the fix). Full suite green except one PRE-EXISTING
stale failure unrelated to this work:
`test_dashboard_audio_only_plays_for_active_done_session` — asserts the old
active-only audio gate that commit 4a4db7f ("Play done sound regardless of
active session") deliberately removed. Needs updating separately.

## Deploy
Running server is the deployed copy `~/.termweb-runtime/web_terminal_server.py`
(static/ served live from repo, but server.py needs redeploy). Redeploy +
restart via `scripts/start_termweb.sh` (copies server.py, kills port-8765
holder, `launchctl submit`). tmux sessions survive the restart; browser
reconnects. NOTE: `scripts/start_termweb.sh` had an uncommitted local change
(python3 discovery via `command -v` + guard) before this work started.

## Outcome (verified working)
After deploy, Steve hard-reloaded the dashboard at `127.0.0.1:8765` and
clicked once (audio unlock). Firefox then held TWO persistent connections to
8765 (WebSocket + SSE — the SSE finally established), and a fired `done`
rang Glass. End-to-end chain confirmed: hook -> notify -> SSE -> browser ->
sound. Committed (server.py primer + `test_sse_sends_initial_primer` + this
journal) on a branch off main.

Key debugging lesson: a stale browser tab keeps its FAILED EventSource — the
server-side fix only takes after a fresh page load. And every server-side
test (curl, integration) passed throughout because curl tolerates a silent
stream; only a real browser exposed the bug. When a browser symptom can't be
reproduced server-side, reproduce in the actual browser with a controlled
matrix (which is what ports 8799-8804 were for).

## Follow-ups still open
- IPv6: server binds `0.0.0.0` (IPv4 only); `localhost` -> `::1` is refused,
  so `localhost:8765` is fragile. Proper fix: bind dual-stack (`::` with
  IPV6_V6ONLY=0) so `localhost` works without forcing `127.0.0.1`.
- Stale test `test_dashboard_audio_only_plays_for_active_done_session`
  (pre-existing; commit 4a4db7f changed behavior, test not updated).
- Restart did NOT preserve session labels ("dashboard" came back as
  "Session 1"). Also `dxf`/`concrete` sessions were absent after restart —
  unconfirmed whether Steve closed them or recovery dropped them. Worth
  verifying recovery isn't losing sessions.
- `scripts/start_termweb.sh` has an unrelated uncommitted local change
  (python3 discovery); left out of this commit.

---

# 2026-06-16 — termweb sound silent on mac.lan: wrong CLAUDE_CONFIG_DIR

## Symptom
No Glass sound on mac.lan's termweb when Claude (or codex) finishes a task.
mac-mini-1 worked.

## Root cause (Claude)
mac.lan runs Claude via the `claude1` shell alias
(`~/.zshrc`: `alias claude1="CLAUDE_CONFIG_DIR=/Users/skim/.claude1 claude ..."`).
That points Claude at `~/.claude1/settings.json`, which had **no hooks**.
All the termweb notify hooks live in `~/.claude/settings.json` (the `claude0`
alias). So the `Stop` hook never fired -> no `done` event posted -> no SSE
broadcast -> no sound. The settings *content* on both machines was fine; the
difference was which config dir the running Claude used.

## How it was proven (not guessed)
- Server only sets session status to `idle` at creation/recovery
  (`server.py:166,197`) and only changes it via the notify endpoint
  (`:1007`). There is **no auto-revert timer**.
- My own termweb session (id `976342…`, label "dashboard") sat at `idle`
  the entire turn despite a prompt submit -> no hook ever fired.
- Running the exact hook command manually flipped `idle`->`processing`
  instantly -> the command is correct; Claude just never invoked it.
- `echo $CLAUDE_CONFIG_DIR` = `/Users/skim/.claude1`; that file had no
  `hooks` key.

## Pipeline is healthy
Subscribed to `/api/events`, POSTed `done`, captured the broadcast
`data: {"session_id":"…","event":"done"}`. Server -> SSE -> `/api/sounds/glass`
all work. NOTE: the SSE listener must be started and POSTed-to within the
**same** Bash tool call — a background `curl &` from one call is dead by the
next call's shell.

## Fix applied
Copied the full `hooks` block from `~/.claude/settings.json` into
`~/.claude1/settings.json` (merged; existing keys preserved). Validated JSON.

## Still required for sound even with hooks firing
- A **dashboard** tab must be open — `terminal.html` has zero sound code;
  sound lives only in `dashboard.html` (SSE + `new Audio("/api/sounds/glass")`).
- Audio must be **unlocked by a click** first (browser autoplay policy,
  `dashboard.html:1031`).

## Open / not yet verified
- Whether the running Claude session hot-reloads `settings.json` or needs a
  restart for the new hooks to take effect.
- Codex: its hooks are correctly in `~/.codex/config.toml` (codex ignores
  CLAUDE_CONFIG_DIR). mac.lan also has a leftover `~/.codex/hooks.json`
  (Superset only) that mac-mini-1 lacks — possible shadowing of config.toml
  hooks, unconfirmed. Did not run a codex session to test.
