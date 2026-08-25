# Termweb session slowdown investigation

## 2026-08-04 — Root cause: unbounded, quadratically-appended session buffer

Steve reported a session (labelled "organzier speed") getting slower and slower
over time until codex could not even scroll.

### Root cause

`TerminalSession._buffer` in `python/src/web_terminal/server.py` is a plain `str`
that accumulates every byte tmux `pipe-pane` ever emits, and is never trimmed.
`_read_output` grows it with `self._buffer += text` on an *attribute*, which
cannot use CPython's in-place unicode concat optimisation, so every 4 KB chunk
costs a full O(n) copy of the whole buffer. Total cost is quadratic in session
output.

Two things make this fatal rather than theoretical:

1. `pipe-pane` captures raw repaint traffic, not logical scrollback. Measured a
   repainting TUI (`top -s 1`) in a 160x51 tmux pane: **2,718,225 bytes in 20 s
   = 136 KB/s = 466 MB/hour**. Codex's TUI is in the same class. So the buffer
   reaches hundreds of MB within tens of minutes of real use.
2. The append happens while holding `_output_ready`, which wraps the *same*
   `_lock` that `write()` / `_flush_input()` need for keystrokes. So the copy
   blocks input as well as output — that is why scrolling stops responding, not
   just why output lags.

### Measurements (this machine, 4 KB reads)

| fed    | ~TUI time | unbounded (current) | capped 4 MB | capped 4 MB + 64 KB reads |
|--------|-----------|---------------------|-------------|---------------------------|
| 16 MB  | 2.1 min   | 2.62 s              | 0.98 s      | 0.08 s                    |
| 32 MB  | 4.1 min   | 13.39 s             | 3.88 s      | 0.34 s                    |
| 64 MB  | 8.2 min   | 69.70 s             | 6.63 s      | 0.40 s                    |

Unbounded is clearly quadratic (2.6 -> 13 -> 70). Extrapolated, ~30 min of TUI
output costs more CPU than wall-clock, i.e. the reader thread can no longer
drain the pipe. Capping is linear; capping plus larger reads is ~free.

Corroborating evidence: the long-lived server process (pid 2886, 4 days uptime)
had accumulated **854 minutes of CPU** while sitting at only 21 MB RSS and 6
threads — the CPU was burned by reader threads of earlier, since-closed sessions
with huge buffers.

### Also noted (secondary, not the cause)

- Memory amplification: a Python `str` holding box-drawing chars is 2 bytes/char,
  and any non-BMP char (emoji, which codex emits) promotes the whole string to
  4 bytes/char. A 200 MB UTF-8 stream can become ~700 MB of RSS.
- `/api/sessions/<id>/history` returns `full_buffer()` with no limit — the
  "History" button would ship hundreds of MB to the browser.
- `ws_relay` sets no socket timeout, so a half-open TCP connection (phone leaves
  wifi) leaks a relay thread and an oob subscriber. Not observed in the current
  process (only 6 threads), but it is a real leak.
- `DEFAULT_READ_SIZE = 4096` is small; `os.read` on the non-blocking fifo returns
  whatever is available, so raising it only batches more under load and never
  adds latency.

### Outcome

Fixed on `fix/bound-session-buffer`. Marginal cost of ~1 minute of TUI output
(2000 x 4 KB appends), measured against the real `TerminalSession` at various
session ages:

| buffer at start | pre-fix | with cap |
|-----------------|---------|----------|
| 4 MB            | 1.21 s  | 0.57 s   |
| 16 MB           | 3.50 s  | 0.54 s   |
| 64 MB           | 16.27 s | 0.79 s   |
| 256 MB          | 74.66 s | 0.83 s   |

At 256 MB the pre-fix reader needs 75 s of CPU to absorb 60 s of output, so it
can never catch up — and it holds `_lock` while doing it, which is why keystrokes
and scrolling died rather than just output lagging. With the cap the cost is flat
regardless of session age.

Verified live through real tmux (`verify_live.py`): 5.9 MB streamed, 4.9 MB
retained, 1.0 MB dropped — trimming works on the actual pipe-pane path.

Not done, deliberately: raising `DEFAULT_READ_SIZE` from 4096. Benchmarks showed
64 KB reads cut the capped cost another ~17x, but at ~1% of a core the capped
cost is already irrelevant. YAGNI unless something else shows up.

### Fix shape

Bound the retained buffer and track a `_dropped` char count so the absolute
cursors that clients hold (`?cursor=`, `lastCursor` in the JS) stay monotonic.
Trim with hysteresis (cap + slack) so trimming is amortised. Everything that
reports a cursor (`read`, `tail_cursor`, `ws_relay`) must return
`_dropped + len(_buffer)` rather than `len(_buffer)`.

## 2026-08-25 — Follow-up: bounded strings still copy the retention window

Steve reported browser keystrokes becoming severely delayed after three or four
Claude/Codex sessions matured. Six live sessions retained roughly 4–5 MB each.
Although the cap prevented unbounded growth, every small repaint still executed
`self._buffer += text` against the entire retained string.

A five-second sample of the live server showed `PyUnicode_Concat` and its
`memmove` as the dominant work on two output-reader threads. The sessions API
rose from a healthy 16.6 ms mean to 191.6 ms during the slowdown even though the
active sessions produced only about 9.5 KiB/s combined. Reader threads held the
GIL while copying their 4–5 MB strings and held the per-session lock that browser
input also needs.

Firefox memory pressure amplified the delay: its GPU helper had a 9.9 GB
physical footprint, another content process had a 3.0 GB footprint, compressed
memory was about 10 GB, and 5.77 of 6 GB swap was in use. The OS data does not
identify which tab owns every graphics allocation, so this remains a separate
contributing condition rather than an attributed Termweb defect.

### Outcome

`RetainedOutput` now stores output in bounded pieces. Small writes coalesce only
up to 16 KB, so continuous append copies are capped at 16 KB rather than the
4–5 MB retention window. Reads at the live cursor walk backward through only the
requested suffix. Trimming and all public cursor values retain their existing
character-based semantics.

Regression measurements:

- Appending one character to a full 4 MB string allocated about 8.4 MB before
  the fix; the regression test now stays below 2 MB.
- 100,000 eight-character appends retained 5.7 MB of Python-object overhead in
  the initial deque implementation. Bounded coalescing reduced traced memory to
  about 0.8 MB and completed in 0.17 seconds.
- The complete isolated suite passes: 44 tests in 34.69 seconds.

The integration tests now remove inherited `TMUX` and use a short private tmux
socket root. This prevents test teardown from discovering and detaching live
Termweb sessions when pytest runs from inside one. The running service was not
restarted while preparing this fix.
