# Terminal output

Each browser connection opens a tmux control client for its session. The client
uses `ignore-size` so attaching does not resize the pane, and `-E` so attaching
does not update the session environment.

The connection starts with output disabled. One tmux command sequence captures
the pane modes and cursor, the screen and up to 2,000 history lines, and any
unfinished escape sequence, then enables output. The browser receives a terminal
reset and the snapshot before the subsequent `%output` notifications from that
same connection. This orders the snapshot and live output without depending on
how quickly the server drains its separate output pipe.

tmux encodes control characters and backslashes in `%output` as octal escapes.
The connection decodes those bytes and uses an incremental UTF-8 decoder so a
character can span multiple notifications. The output-pipe reader also uses an
incremental decoder.

Keyboard input uses WebSocket text frames. Swipes in full-screen programs without
mouse capture use binary JSON frames containing `type: "scroll"`, signed `lines`,
one-based `column` and `row`, and a `start` flag for the first movement of a gesture.
The connection checks the pane's foreground process group at each gesture start.
Codex accepts SGR wheel reports even with mouse capture disabled, so its transcript
receives incremental wheel input. Unrecognized programs receive a single page key
per gesture. Each browser connection keeps its own gesture state.

The output pipe retains a bounded stream for the HTTP output and history routes.
Browser reconnects use a fresh snapshot instead of a cursor into that stream.
Snapshots restore text and text attributes; previously emitted inline images are
not reconstructed by pane capture. Live image sequences travel with the live
terminal output.

Closing a browser detaches its control client. Stopping the web service detaches
its clients and output pipes; the tmux sessions continue running. This uses one
tmux client process per connected browser.

Regression tests use isolated tmux servers and real terminal programs. They cover
trimmed history, queued output, output during capture, normal and alternate
screens, cursor placement, unfinished escape sequences, split Unicode characters,
literal protocol-like text, and client cleanup.
