# Cowork ↔ Claude Code daemon

Closes the **Cowork → Claude Code** direction of the queue automation. The other direction (Claude Code → Cowork) is handled by the Cowork-side scheduled task `viabe-team-queue-poller`.

> **SUPERSEDED — Phase-1 bash watcher**
>
> The bash watcher below is kept as a Phase-1 fallback only. The supported daemon is now the Python agent-loop in `agent-loop.py` — install via `bash install-launchd.sh`. See the [Python daemon](#python-daemon-agent-looppy) section. Do **not** run both at the same time.

## Files

- `agent-loop.py` — Python daemon (Phase 2). Embeds `claude_agent_sdk.query()`; maintains one persistent Claude Code session across all signals.
- `core.py` — daemon core: scheduling policy, signal dispatch, merge-detection, budget enforcement.
- `hooks.py` — SDK hook callbacks (Pillar-7 enforcement, task-log appending, transcript archive).
- `install-launchd.sh` — one-shot installer (venv + plist + `launchctl load`).
- `com.viabe.team.agent-loop.plist` — LaunchAgent definition.
- `tests/` — pytest suite for `core`, `hooks`, and a smoke test for `process_signal`.
- `watch-claude-inbox.sh` — Phase-1 bash watcher (superseded; kept as fallback).
- `watch.log` — runtime log for the bash watcher (gitignored).

## Quick start

In a terminal:

```bash
cd /Users/fazalkhan/development/viabe-team
bash .viabe/daemon/watch-claude-inbox.sh
```

Leave the terminal open. Stop with Ctrl-C.

When Cowork writes a signal to `.running/to-claudecode/`, the watcher picks it up within 30s, fires `claude --print --dangerously-skip-permissions` with a prompt that points Claude Code at the signal + the protocol. Claude Code processes per `.viabe/protocol.md`, moves the signal to `.running/processed/`, and exits. The watcher loops back to polling.

## Per-run timeout

Each Claude Code invocation is wrapped in `timeout 3600` (60 min). Requires GNU `timeout` (`gtimeout` on macOS via `brew install coreutils`). If neither is installed, the watcher logs a warning and runs without timeout — long-running Claude Code sessions could hang the watcher.

## Failure-mode handling

If Claude Code crashes / hangs / returns non-zero, the watcher logs a WARN and (defensively) moves the signal to `processed/` anyway — so the same signal doesn't fire repeatedly. The audit trail is preserved in `watch.log`.

## Validating the install

After starting the watcher:

1. From Cowork chat, ask Cowork to write a test signal:
   ```
   echo -e "---\nfrom: cowork\nto: claudecode\ntask: VT-TEST\ntype: question\nts: $(date -u +%FT%TZ)\n---\nThis is a test signal. Read /tmp/nonexistent — but if you got here, the watcher works. Move me to processed and exit." > .running/to-claudecode/$(date -u +%Y%m%dT%H%M%SZ)-test.md
   ```
2. Within 30s, the watcher should fire Claude Code, the signal moves to `.running/processed/`, and `watch.log` shows the FOUND + DONE lines.
3. If nothing happens after 60s, check `watch.log` for errors. Common causes:
   - `claude` CLI not in PATH
   - `claude --print --dangerously-skip-permissions` flags differ from your installed version (edit script)
   - File permissions on `.running/`

## Adjusting `claude` CLI flags

If your installed `claude` CLI uses different syntax for headless mode, edit the two `claude --print --dangerously-skip-permissions ...` lines in `watch-claude-inbox.sh`. Common alternatives:

- `claude --noninteractive ...`
- `claude run --auto-approve ...`
- `claude -p ...` (short flag)

Test with `claude --help` to see your version's flags.

## Future: launchd LaunchAgent (survival)

The foreground watcher works as long as the terminal is open. For survival across reboots / closed terminals, wrap in a launchd LaunchAgent (`~/Library/LaunchAgents/com.viabe.team.claude-code-trigger.plist`). Use `launchd`'s `WatchPaths` feature for event-driven instead of polling. Defer until the foreground watcher is proven over a week of real use.

## Loop closed

With this watcher running AND the `viabe-team-queue-poller` scheduled task running every 10 min, the full Cowork ↔ Claude Code loop is automated:

- Cowork writes signal → watcher fires Claude Code → Claude Code processes → writes back signal.
- Claude Code writes signal → poller (Cowork) fires every 10 min → processes → writes back signal.

The only manual human-in-the-loop steps remaining are: Fazal merge button, Fazal Type-3 decisions, Cowork (me) being open in the app for scheduled tasks to fire.

## Python daemon (`agent-loop.py`)

The Phase-2 replacement for `watch-claude-inbox.sh`. Differences:

| Property | Bash watcher | Python daemon |
|---|---|---|
| Session continuity | New `claude --print` per signal — no shared context | ONE `claude_agent_sdk.query()` session resumed via `session_id` across every signal |
| Budget enforcement | None | SDK `max_budget_usd` per call + per-task aggregation from `cost.log` |
| Pillar-7 enforcement | Trust-only (not enforced) | `PreToolUse` hook denies `gh pr merge` unless a `type:task` signal carries `authorized_by: fazal` |
| Auto-merge-detection | Not implemented (manual `type: task`) | Polls `gh pr view <N> --json mergedAt` every iteration; flips `in-pr → done` + unblocks dependents automatically |
| Survival | Terminal-bound | launchd LaunchAgent (`RunAtLoad=true`, `KeepAlive=true`) |
| Logs | `watch.log` | `agent-loop.log` (daemon-side), `launchd.log` / `.err` (process stdio), `cost.log` (per-signal accounting), `transcripts/*.jsonl` (PreCompact archive) |

### Install

```bash
cd /Users/fazalkhan/development/viabe-team/.viabe/daemon
bash install-launchd.sh
# Verify
launchctl list | grep com.viabe.team.agent-loop
tail -f agent-loop.log
```

### Stop / restart

```bash
# Graceful — exits after current iteration
touch /Users/fazalkhan/development/viabe-team/.viabe/daemon/STOP

# Resume after stop
rm   /Users/fazalkhan/development/viabe-team/.viabe/daemon/STOP
launchctl unload ~/Library/LaunchAgents/com.viabe.team.agent-loop.plist
launchctl load   ~/Library/LaunchAgents/com.viabe.team.agent-loop.plist

# Full uninstall
launchctl unload ~/Library/LaunchAgents/com.viabe.team.agent-loop.plist
rm               ~/Library/LaunchAgents/com.viabe.team.agent-loop.plist
```

### Files written by the daemon (gitignored)

- `agent-loop.log` — daemon's own log (start, iteration errors, NOTIFY echoes).
- `launchd.log` / `launchd.err` — process stdout / stderr captured by launchd.
- `cost.log` — append-only per-signal accounting (used by the per-task budget gate).
- `session.state` — current `session_id`; atomic-replaced across runs.
- `transcripts/<session>-<unix-ts>.jsonl` — PreCompact-archived transcripts.
- `STOP` — kill-switch file; daemon exits gracefully when present.
- `.viabe/notifications/log` — outcome log for macOS / Telegram dispatch on `priority: high` notify (success / failure / no-op when `telegram.env` missing).

### `priority: high` notify dispatch

When the daemon receives a `type: notify` signal with `priority: high`, it dispatches from this host (where Cowork's sandbox cannot):

1. `osascript -e 'display notification "<body>" with title "Cowork" subtitle "<task-id>"'`
2. If `.viabe/secrets/telegram.env` exists, POST to the Telegram Bot API using `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` from that file (subshell-isolated; not exported to the daemon's broader environment).

Both dispatches are best-effort: failures are logged to `.viabe/notifications/log` and never crash the daemon. `priority: normal` (or missing) skips both — terminal echo only.

### Tests

```bash
cd /Users/fazalkhan/development/viabe-team/.viabe/daemon
.venv/bin/pip install pytest
.venv/bin/pytest tests/ -v
```

`tests/test_pick_next_action.py` covers the scheduling state machine; `tests/test_hooks.py` covers Pillar-7 + log-append + transcript-archive; `tests/test_smoke.py` covers an end-to-end `process_signal` call with a mocked `claude_agent_sdk.query`.

### Debugging

- Daemon won't start: `launchctl list | grep com.viabe.team.agent-loop` — non-zero exit code means it's crash-looping; check `launchd.err` for the traceback.
- No signals being picked up: confirm the daemon is running, then `ls .running/to-claudecode/*.md` — files there should disappear within `POLL_SECONDS` (30s default).
- A signal is stuck in INBOX with a `type: blocked` reply in `.running/to-cowork/`: SDK call failed; daemon left the signal so Cowork can re-trigger after the underlying issue (auth, rate limit) is resolved.
- An `in-pr` task isn't auto-merge-detecting: confirm `.viabe/queue/<task>/pr.md` contains a `pull/<N>` URL and that `gh pr view <N>` works in your shell.
