---
task: VT-AGENTSDK-LOOP
vt_row: (no Notion row — pure infrastructure task created today by Cowork)
author: cowork
ts: 2026-05-24T18:30:00+05:30
budget_tokens: 400000
budget_minutes: 180
priority: Critical
sprint: Hardening
area: Infrastructure
assignee: claudecode
parent: VT-EngineeringReference (356387c2-cc5a-81c8-8f55-db365c759a92)
depends_on: VT-OIV (merge PR #53 first)
---

# Brief — build the agent-loop daemon (VT-AGENTSDK-LOOP)

## Why this task

The current automation has two manual triggers per task (Fazal kicks off Claude Code; Fazal merges the PR). The Cowork-side poller closes the Claude-Code-→-Cowork direction. A bash watcher closed the Cowork-→-Claude-Code direction but spawns a new `claude --print` process per signal — no session continuity, no native budget enforcement, fragile.

The Agent SDK ([code.claude.com/docs/en/agent-sdk/agent-loop](https://code.claude.com/docs/en/agent-sdk/agent-loop)) gives us the right primitive: a Python program embedding the same agent loop that powers Claude Code, with programmatic `session_id` resume, `permission_mode="bypassPermissions"`, `max_budget_usd` per call, and hooks (`PreToolUse`, `Stop`, etc.) for protocol enforcement. We're replacing the bash watcher with a Python daemon that maintains ONE conversation across all signals — Fazal's "one session that listens" vision, properly implemented.

## Goal

Produce a production-quality Python daemon `.viabe/daemon/agent-loop.py` that:

1. Runs continuously (foreground for testing, launchd for survival).
2. Polls `.running/to-claudecode/` (signals from Cowork) and `.viabe/queue/*/status` (queued tasks) every 30 seconds.
3. Enforces the parallelism + priority policy (Section "Policy" below) strictly.
4. Calls `query()` from `claude_agent_sdk` with `resume=session_id` so the SAME Claude Code conversation persists across all signals — session context preserved.
5. Uses `permission_mode="bypassPermissions"`, `setting_sources=["project"]`, and a per-call `max_budget_usd` cap.
6. Updates `.viabe/queue/<task>/status` and `task_log.md` via hooks as work progresses.
7. Logs cost per task to `.viabe/daemon/cost.log`.
8. Honors a kill-switch: `touch .viabe/daemon/STOP` exits cleanly after the current task completes.
9. Survives daemon restart: `session_id` persisted to `.viabe/daemon/session.state`; the daemon resumes the same conversation on restart.
10. Installs as a launchd LaunchAgent for survival across reboots / closed terminals.

## Step-0 ground-truth check (do before planning)

1. `git fetch && git log --oneline -5` — confirm HEAD (expected: VT-OIV merged at top, then `de8c0c1`).
2. Confirm `pip install claude-agent-sdk` works in a fresh venv. If the package name has changed, surface to Cowork as `type: question`.
3. Read the Agent SDK docs you'll use:
   - [Agent loop](https://code.claude.com/docs/en/agent-sdk/agent-loop)
   - [Python SDK reference](https://code.claude.com/docs/en/agent-sdk/python) — `ClaudeAgentOptions`, `query`, `ClaudeSDKClient`, `ResultMessage`
   - [Hooks](https://code.claude.com/docs/en/agent-sdk/hooks) — `PreToolUse`, `PostToolUse`, `Stop`
   - [Sessions](https://code.claude.com/docs/en/agent-sdk/sessions) — resume / fork patterns
4. Confirm `.viabe/protocol.md`, `.viabe/automation-plan.md`, and `feedback_snapshot_sequencing.md` (in Cowork memory — link not in repo) all describe the same protocol you'll implement.

If anything contradicts the brief, write a `type: blocked` signal and stop.

## Policy — strict parallelism + priority (encode in the daemon)

A task is "actively occupying Claude Code" when its `status` is `planning` or `implementing`.

**Daemon scheduling logic each polling iteration:**

```
1. Discover all .viabe/queue/<task>/status files (excluding done/).
2. Build state set: { task -> status }.
3. If ANY task has status in {planning, implementing}:
   - Daemon is "busy" — process ONLY signals in .running/to-claudecode/ that target the active task.
   - Other signals stay in the inbox.
   - No new task started.
4. If NO task is in planning/implementing:
   - Process oldest signal in inbox first (FIFO by filename timestamp).
   - Then check for queued tasks; if any, pick OLDEST queued (by created field in brief frontmatter) and dispatch.
5. Skip tasks with status in {blocked, deferred}.
6. Tasks in {review, in-pr, merged, done} are Claude-Code-idle states — they don't block new work.
```

**Concretely:** if VT-OIV is `in-pr` (waiting for Fazal merge) and VT-MIG-SPRINT is `queued`, the daemon will pick up VT-MIG-SPRINT. If a `review` signal arrives for VT-A while VT-B is mid-`implementing`, the review for VT-A waits in the inbox until VT-B finishes implementing (reaches `in-pr` state).

**Race-safety:** the daemon writes to `status` atomically (write to tmp file + rename). External writers (Cowork) MUST do the same. The daemon re-reads `status` immediately before dispatching to confirm no race.

## Detailed scope

### File: `.viabe/daemon/agent-loop.py`

Python 3.11+. Async via `asyncio`. Top-level structure:

```python
import asyncio
from pathlib import Path
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, AssistantMessage
import json, os, time, sys, signal

REPO = Path("/Users/fazalkhan/development/viabe-team")
INBOX = REPO / ".running/to-claudecode"
QUEUE = REPO / ".viabe/queue"
PROCESSED = REPO / ".running/processed"
COST_LOG = REPO / ".viabe/daemon/cost.log"
SESSION_STATE = REPO / ".viabe/daemon/session.state"
STOP_FILE = REPO / ".viabe/daemon/STOP"
DAEMON_LOG = REPO / ".viabe/daemon/agent-loop.log"
POLL_SECONDS = 30
PER_CALL_BUDGET_USD = 5.0   # per-signal cap; brief frontmatter overrides if larger
```

Functions to implement:

- `load_session_id() -> Optional[str]` — read from SESSION_STATE; return None if absent.
- `save_session_id(sid: str)` — atomic write to SESSION_STATE.
- `read_queue_state() -> Dict[task_id, status]` — scan QUEUE/*/status.
- `pick_next_action() -> Optional[Action]` — implements the policy above; returns either `ProcessSignal(path)`, `StartTask(task_id)`, or None.
- `process_signal(path, session_id)` — calls `query()` with the signal as prompt; returns new session_id.
- `dispatch_queued_task(task_id, session_id)` — synthesizes a `brief-ready` signal in INBOX and processes it.
- `record_cost(task_id, result_msg)` — append a line to COST_LOG.
- `should_stop() -> bool` — check STOP_FILE.
- `main_loop()` — orchestrator.

### Hooks (defined in agent-loop.py, passed via `ClaudeAgentOptions.hooks`)

- **`PreToolUse`** — block any tool call that would auto-merge a PR (`gh pr merge`, `Bash` with `gh pr merge` in the command). Pillar 7 enforcement. Return `block` decision with reason "Cowork policy: PR merge is Fazal-only."
- **`PostToolUse`** — append the tool name + brief summary to `.viabe/queue/<active-task>/task_log.md`. Per the protocol's append-only task-log rule.
- **`Stop`** — when an agent run finishes, read the new status from `.viabe/queue/<task>/status` and write a chat-side update to `.viabe/daemon/agent-loop.log` (the daemon's chat-equivalent surface — Cowork's 3-min poller doesn't read this, but Fazal can `tail -f` it).
- **`PreCompact`** — archive the full transcript to `.viabe/daemon/transcripts/<session_id>-<timestamp>.jsonl` before context compaction so the audit trail survives.

### File: `.viabe/daemon/install-launchd.sh`

A one-shot script Fazal runs ONCE:

```bash
#!/bin/bash
set -e
REPO="/Users/fazalkhan/development/viabe-team"
PLIST_NAME="com.viabe.team.agent-loop"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

# 1. install Python deps (in a venv to avoid system pollution)
cd "$REPO/.viabe/daemon"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install claude-agent-sdk

# 2. copy plist
cp com.viabe.team.agent-loop.plist "$PLIST_PATH"

# 3. load
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo "Installed. Verify: launchctl list | grep agent-loop"
echo "To stop temporarily: touch $REPO/.viabe/daemon/STOP"
echo "To uninstall: launchctl unload $PLIST_PATH && rm $PLIST_PATH"
```

### File: `.viabe/daemon/com.viabe.team.agent-loop.plist`

Standard launchd LaunchAgent with `RunAtLoad=true`, `KeepAlive=true`, `WorkingDirectory=$REPO`, `ProgramArguments` pointing at the venv's python + `agent-loop.py`, `StandardOutPath` + `StandardErrorPath` to `.viabe/daemon/launchd.log` + `.err`, and an `EnvironmentVariables` block with the right PATH for `claude` / `python` / `git` / `gh` discoverability. Env var `ANTHROPIC_API_KEY` must NOT be set in the plist (per CL-385: that flag is a SHIP-GATE controlled separately).

### File: `.viabe/daemon/STOP` (not committed; written by Fazal to halt)

If exists, daemon exits cleanly after current task completes. Honors graceful shutdown via `SIGTERM` from launchctl.

### File: `.viabe/daemon/session.state`

Single line: the current session_id. Atomic-write (tmp + rename).

### File: `.viabe/daemon/cost.log`

Append-only. One line per signal processed:
```
2026-05-24T18:30:00+05:30 VT-OIV plan-ready cost=$0.4521 turns=8 tokens=82345 session=sess_abc...
```

### File: `.viabe/daemon/README.md`

Document the daemon: install, configure, stop, debug, log locations. Replaces the bash-watcher README which becomes obsolete.

## Test plan

### 1. Unit tests in `.viabe/daemon/tests/test_pick_next_action.py`

State-machine tests for `pick_next_action`:
- All tasks `done` → returns None.
- One task `queued`, none `planning`/`implementing`, no signals → returns `StartTask(queued_task)`.
- One task `implementing`, one signal for that task in inbox → returns `ProcessSignal(signal_for_implementing_task)`.
- One task `implementing`, one signal for a DIFFERENT task in inbox → returns None (signal waits).
- Mixed: one task `in-pr`, one `queued`, one signal for the `in-pr` task → returns `ProcessSignal(that_signal)` (Claude Code idle on `in-pr`, so it can answer signals there).
- One task `blocked`, one task `queued` → returns `StartTask(queued)` (blocked is skipped).
- One task `deferred`, one task `queued` → returns `StartTask(queued)` (deferred is skipped).

### 2. Integration smoke test in `.viabe/daemon/tests/test_smoke.py`

A test that:
1. Drops a sample `brief-ready` signal into a temp INBOX.
2. Runs `pick_next_action()` + dispatches a single iteration of `process_signal` with a mocked `claude_agent_sdk.query`.
3. Asserts signal moved to PROCESSED, session_id saved to SESSION_STATE, cost logged.

### 3. Live test (Fazal runs manually after install)

After `bash install-launchd.sh`:
1. `launchctl list | grep agent-loop` confirms the daemon is loaded.
2. `touch .viabe/queue/VT-AGENTSDK-TEST/brief.md` + a minimal brief.
3. `echo queued > .viabe/queue/VT-AGENTSDK-TEST/status`
4. Within 60 seconds, daemon picks up, writes a plan, status flips to `review`. Confirms end-to-end.

## Pass criteria

1. `pip install claude-agent-sdk` succeeds in a fresh venv on Fazal's machine.
2. `python .viabe/daemon/agent-loop.py` runs without errors in foreground mode.
3. Unit tests pass: `pytest .viabe/daemon/tests/ -v`.
4. Live test (item 3 above) succeeds end-to-end.
5. `touch .viabe/daemon/STOP` causes clean exit within 60 seconds (or after current task).
6. `launchctl unload` cleanly stops the daemon.
7. The bash watcher (`watch-claude-inbox.sh`) is NOT deleted — keep as Phase-1 fallback. Add a banner to `.viabe/daemon/README.md` saying it's superseded.
8. Cost-per-task observable in `.viabe/daemon/cost.log` after the live test.
9. Session continuity demonstrable: kill daemon, restart, verify next `query()` call uses the same `session_id` from SESSION_STATE.
10. Pillar 7 enforced: hook test demonstrates blocked `gh pr merge` attempt.

## Out of scope

- Changing the Cowork-side poller (`viabe-team-queue-poller`) — it stays as is, every 3 min, 8-22 IST.
- Migrating Notion → repo (that's VT-MIG-SPRINT, deferred until this is merged).
- Building a custom MCP server that lets Claude Code write back to Cowork without polling (Phase 3 idea).
- Auto-merging PRs (NEVER in scope — Pillar 7).
- Replacing `claude` interactive CLI for manual debugging — Fazal can still open `claude` interactively whenever desired.
- Wrapping the daemon in Docker / systemd / non-macOS tooling.

## Reference materials

- Agent SDK docs: [agent loop](https://code.claude.com/docs/en/agent-sdk/agent-loop), [Python ref](https://code.claude.com/docs/en/agent-sdk/python), [hooks](https://code.claude.com/docs/en/agent-sdk/hooks), [sessions](https://code.claude.com/docs/en/agent-sdk/sessions), [permissions](https://code.claude.com/docs/en/agent-sdk/permissions), [subagents](https://code.claude.com/docs/en/agent-sdk/subagents).
- Project protocol: `.viabe/protocol.md`
- Architecture spec: `.viabe/automation-plan.md`
- Existing watcher to replace: `.viabe/daemon/watch-claude-inbox.sh`
- Daemon README to update: `.viabe/daemon/README.md`

## Hard rules

- Daemon NEVER auto-merges PRs (Pillar 7). Enforced via `PreToolUse` hook.
- Daemon NEVER processes more than one task in `planning`/`implementing` at a time.
- Daemon RESPECTS `STOP` file as a clean-exit signal.
- Daemon RESUMES the same `session_id` across restarts (session continuity is the whole point).
- Daemon NEVER assumes an activated venv. All Python invocations use the explicit venv-python path (e.g., `cd apps/team-orchestrator && ./.venv/bin/python -m pytest ...`). Same for the agent-loop daemon itself — its launchd plist invokes `.viabe/daemon/.venv/bin/python` explicitly.
- Daemon AUTO-DETECTS PR merges. For any task with status `in-pr`, the daemon polls `gh pr view <N> --json mergedAt --jq .mergedAt` every loop iteration. When the value transitions from null → ISO timestamp, the daemon: (a) flips that task's status to `merged`, then to `done` after post-merge cleanup, (b) scans all other tasks for `depends_on:` references to the just-merged task and flips matching `blocked` statuses to `queued`. The PR number is read from `.viabe/queue/<task>/pr.md` (the `pr_url:` line). This removes the manual "tell Cowork the PR is merged" step.
- For shell pipelines that capture pytest output via `tee`, use `${PIPESTATUS[0]}` to get pytest's exit code (NOT `$?` which gives tee's exit code, always 0).
- For `pre-merge-check` signals, the daemon verifies all `env_required` vars are set BEFORE running the command. If any missing, signal `blocked` with the missing var name. Never silently proceed.
- PR title: `feat(infra): agent-loop daemon — Python Agent SDK orchestrator (VT-AGENTSDK-LOOP)`
- Branch: `feat/vt-agentsdk-loop-daemon`

## Estimated effort

- Step-0 + planning: 30 min
- `agent-loop.py` implementation: 60-90 min
- Hooks + tests: 30-45 min
- Launchd plist + install script: 20 min
- README update + bash-watcher banner: 10 min
- Live test + iteration: 15-30 min
- PR opening + CI: 10 min
- **Total: ~3 hours.** Budget cap set to 180 min in frontmatter.

If you approach the 180 min cap with the live test not yet green, signal `type: blocked` and stop. Cowork will scope down or split.

---

**When done:** signal `.running/to-cowork/<ts>-pr-ready-VT-AGENTSDK-LOOP.md`. Cowork verifies; Fazal merges. Fazal then runs `bash .viabe/daemon/install-launchd.sh` once. Daemon takes over from that point — no further manual triggers needed for routine work.
