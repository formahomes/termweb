# 2026-05-17 — mac.lan termweb notify hooks

## Symptom
On `mac.lan`, termweb sessions never changed color or played sound when a
turn finished. mac-mini-1 worked fine.

## Root cause
mac.lan's `~/.claude/settings.json` only invoked
`$SUPERSET_HOME_DIR/hooks/notify.sh` and had no hook posting to termweb's
`/api/sessions/{id}/notify` endpoint. `~/.codex/config.toml` had no hooks
at all and was missing `[features] codex_hooks = true` (without that flag
codex ignores hooks entirely). The frontend therefore never received the
`processing`/`done` events that drive the color change and sound.

The valid notify events are defined in
`python/src/web_terminal/server.py` (`VALID_NOTIFY_EVENTS = {"processing",
"done", "idle"}`); the route is `/api/sessions/<id>/notify`.

## Fix
Mirrored mac-mini-1's setup on mac.lan:

- `~/.claude/settings.json`: appended a second hook command under
  `UserPromptSubmit` (emits `processing`) and `Stop` (emits `done`)
  alongside the existing Superset hook. Both guard on
  `$TERMWEB_SESSION_ID` and `$TERMWEB_URL` so they only fire under
  termweb; Superset hook still fires only when `$SUPERSET_HOME_DIR` is
  set, so no conflict.
- `~/.codex/config.toml`: added `[features] codex_hooks = true` plus
  `[[hooks.UserPromptSubmit]]` and `[[hooks.Stop]]` posting the same
  events. Left the old `~/.codex/hooks.json` (Superset SessionStart/Stop)
  untouched.

## Notes for future me
- The SSH alias `mac-mini-1` in `~/.ssh/config` actually resolves to a
  host that reports `hostname` as `mac-mini-2.lan`. Don't be confused —
  the alias is what matters.
- Hook commands belong in `config.toml` for codex (new format), not
  `hooks.json` — mac-mini-1 has been migrated, mac.lan now matches.
- Hook curl uses `--max-time 2` and `|| true` so a slow/down termweb
  doesn't block the prompt.
