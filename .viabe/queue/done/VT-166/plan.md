---
task: VT-AGENTSDK-LOOP
author: claudecode
ts: 2026-05-24T22:02:24+05:30
estimated_tokens: 320000
estimated_minutes: 170
---

## Approach

Build a Python 3.10+ daemon at `.viabe/daemon/agent-loop.py` that replaces the foreground bash watcher. The daemon embeds `claude_agent_sdk.query()` so a single Claude Code conversation persists across every signal it processes — `session_id` is captured from each `ResultMessage` and round-tripped via `resume=` on the next call, persisted to `.viabe/daemon/session.state` so a launchd restart picks up exactly where the prior run left off. The scheduler is a 30-second polling loop with deterministic policy (`pick_next_action`): if any task is in `planning`/`implementing`, only same-task signals are processable; otherwise it FIFO-drains the inbox, then dispatches the oldest `queued` task. Pillar 7 is enforced in two places — a `PreToolUse` hook that inspects every `Bash` call for `gh pr merge` patterns and returns `permissionDecision: "deny"` unless the active signal is a `type: task` brief carrying `authorized_by: fazal` (read from a per-call context object passed via the prompt), and a defense-in-depth check that refuses to even synthesize a merge prompt unless the originating signal frontmatter validates. Merge-detection (`gh pr view <N> --json mergedAt`) runs each iteration for every `in-pr` task and auto-flips `in-pr → merged → done`, scans dependent tasks, and unblocks them — closing the manual "tell Cowork the PR is merged" loop. Tests are state-machine + smoke at the daemon edges (`pick_next_action`, `process_signal` with a mocked `query()`); the live test the brief calls out is Fazal-driven post-install. The launchd LaunchAgent is plain `KeepAlive=true` + `RunAtLoad=true` + an explicit venv-python in `ProgramArguments`; `ANTHROPIC_API_KEY` is intentionally absent from the plist per CL-385.

## File changes

- **NEW `.viabe/daemon/agent-loop.py`** — the daemon (~500-700 lines incl. docstrings).
  - Top-level constants (REPO, INBOX, QUEUE, etc. exactly as the brief sketch shows).
  - `load_session_id()` / `save_session_id(sid)` — atomic tmp+rename writes to `.viabe/daemon/session.state`.
  - `read_queue_state() -> dict[str, str]` — globs `.viabe/queue/*/status` (excluding `done/`), strips whitespace.
  - `parse_signal_frontmatter(path) -> dict` — YAML front-matter parse for `type`, `task`, `authorized_by`, `command`, etc.
  - `pick_next_action(state, signals) -> Optional[Action]` — pure function. Returns `ProcessSignal(path)`, `StartTask(task_id)`, or `None`. Encodes the policy verbatim from the brief.
  - `process_signal(path, session_id, options) -> tuple[str, ResultMessage]` — calls `query()` with the signal as prompt, awaits messages, captures new `session_id` from `ResultMessage`, returns both. Moves signal to `.running/processed/` only after success.
  - `dispatch_queued_task(task_id, session_id, options)` — writes a synthesized `brief-ready` signal to INBOX naming the queued task, then immediately calls `process_signal`. This routes through the same code path so hooks fire.
  - `record_cost(task_id, signal_type, result_msg)` — append-only line to `.viabe/daemon/cost.log` with the format the brief specifies.
  - `detect_pr_merges(state) -> list[tuple[task_id, sha]]` — for each `in-pr` task, read `.viabe/queue/<task>/pr.md` for `pr_url:`, parse PR number, run `gh pr view <N> --json mergedAt --jq .mergedAt`. Returns merged tasks.
  - `apply_merge_cleanup(task_id, sha)` — flip status `in-pr → merged → done`, mv dir to `done/`, scan all other `<task>/brief.md` files for `depends_on:` matching this task, flip those `blocked → queued`. Mirrors what I just did manually for VT-OIV.
  - `should_stop()` — `STOP_FILE.exists()`.
  - `main_loop()` — orchestrator: load session_id, ClaudeAgentOptions with hooks, poll loop with merge-detection first then signals then queued tasks then sleep 30s. SIGTERM/SIGINT handlers for graceful shutdown.

- **NEW `.viabe/daemon/hooks.py`** — hook callbacks isolated for unit testing.
  - `pre_tool_use_block_merges(input_data, tool_use_id, context)` — fired on `Bash` matcher; parses `tool_input.command`; if matches `r"\bgh\s+pr\s+merge\b"` AND the active signal isn't a `type: task` with `authorized_by: fazal`, returns `permissionDecision: "deny"` with reason `"Cowork policy: PR merge is Fazal-only (Pillar 7). Use a type: task signal with authorized_by: fazal."`. The "active signal" is communicated via a module-level `_active_signal_context` set by `process_signal` before each `query()` call — simple sync pattern, daemon is single-threaded.
  - `post_tool_use_log(input_data, tool_use_id, context)` — appends `<ts> <tool_name> <brief-input-summary>` to `.viabe/queue/<active-task>/task_log.md`. The active-task is set from the signal's `task:` frontmatter at dispatch time.
  - `pre_compact_archive(input_data, tool_use_id, context)` — copies the session JSONL from `~/.claude/projects/<encoded-cwd>/<session>.jsonl` to `.viabe/daemon/transcripts/<session>-<ts>.jsonl` before compaction.
  - `stop_log_status(input_data, tool_use_id, context)` — reads `.viabe/queue/<active-task>/status` after the agent run; writes a chat-equivalent summary line to `.viabe/daemon/agent-loop.log`.

- **NEW `.viabe/daemon/install-launchd.sh`** — exact script the brief specifies. Creates venv at `.viabe/daemon/.venv`, installs `claude-agent-sdk` into it, copies plist to `~/Library/LaunchAgents/`, `launchctl load`. Print install verification + uninstall steps.

- **NEW `.viabe/daemon/com.viabe.team.agent-loop.plist`** — LaunchAgent plist with `RunAtLoad=true`, `KeepAlive=true`, `WorkingDirectory=<repo>`, `ProgramArguments=[<repo>/.viabe/daemon/.venv/bin/python, <repo>/.viabe/daemon/agent-loop.py]`, `StandardOutPath=<repo>/.viabe/daemon/launchd.log`, `StandardErrorPath=<repo>/.viabe/daemon/launchd.err`, `EnvironmentVariables.PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:<user-home>/.local/bin` (so `git`, `gh`, `claude` are findable). `ANTHROPIC_API_KEY` deliberately NOT set per CL-385.

- **NEW `.viabe/daemon/tests/__init__.py`** — empty.
- **NEW `.viabe/daemon/tests/test_pick_next_action.py`** — pytest. The 7 state-machine cases the brief enumerates. Pure-function tests, no I/O.
- **NEW `.viabe/daemon/tests/test_hooks.py`** — pytest. Pillar-7 hook accepts a Bash `gh pr merge` call IFF `_active_signal_context` carries `type: task` + `authorized_by: fazal`; denies otherwise. Also: `post_tool_use_log` writes to a tmp path correctly; `pre_compact_archive` copies the jsonl.
- **NEW `.viabe/daemon/tests/test_smoke.py`** — pytest with `monkeypatch.setattr("claude_agent_sdk.query", fake_query)`. Drops a `brief-ready` signal in a temp INBOX. Runs one `process_signal` iteration. Asserts: signal → processed/, session_id → SESSION_STATE, cost line → cost.log.
- **NEW `.viabe/daemon/tests/conftest.py`** — pytest fixtures: `tmp_repo` recreates the dir structure under `tmp_path`; `fake_query` async-generator returning canned messages.

- **MODIFY `.viabe/daemon/README.md`** — append a "**SUPERSEDED**" banner at the top pointing to the new daemon; keep the existing bash-watcher docs as Phase-1 fallback per pass-criterion #7. Add a new "## Python daemon (`agent-loop.py`)" section: install steps, log locations, kill-switch, debug tips.

- **NEW `.viabe/daemon/.gitignore`** — ignore `.venv/`, `launchd.log`, `launchd.err`, `agent-loop.log`, `cost.log`, `session.state`, `STOP`, `transcripts/`. The `.viabe/daemon/watch.log` is already covered by `.running/`-ish patterns? Verify — if not, add it too.

- **NO CHANGES** to `watch-claude-inbox.sh` itself (pass-criterion #7: keep as Phase-1 fallback).

## Test plan

Behavioral tests — no `inspect.getsource`, no transform copies. The unit `test_pick_next_action.py` drives the policy function with constructed dict inputs and asserts return-type identity/equality. `test_hooks.py` exercises each hook with realistic input shapes (the `input_data` dict mirroring what SDK passes per the docs). `test_smoke.py` is the integration check: monkeypatches `claude_agent_sdk.query` with a fake async generator that yields `[SystemMessage, AssistantMessage, ResultMessage]`, drops a real signal file into a tmp INBOX, runs one iteration of the daemon's main loop *manually* via a `run_one_iteration()` extracted helper (so the test doesn't have to manage SIGTERM), then asserts the on-disk side effects.

Live-test scaffolding (pass-criterion #4) is documented in the README, not automated — Fazal runs it manually after `bash install-launchd.sh`. Test artifact: a fake brief at `.viabe/queue/VT-AGENTSDK-TEST/` with status=`queued`, expect plan within 60s. The brief calls this an acceptance check; daemon code must support it but I don't run it from this PR (no manual driver available in CI).

Pillar-7 hook test (pass-criterion #10): `test_hooks.py::test_pillar7_blocks_unauthorized_merge` constructs a `PreToolUse` input with `tool_name=Bash`, `tool_input.command="gh pr merge 53 --squash"`, no active type:task context, asserts return == `{"hookSpecificOutput": {..., "permissionDecision": "deny", ...}}`. Counterpart `test_pillar7_allows_authorized_merge` sets `_active_signal_context` to a dict with `type=task` + `authorized_by=fazal`, asserts return == `{}` (allow).

Session-continuity test (pass-criterion #9): `test_session_persistence` writes a session_id to SESSION_STATE, calls `load_session_id()`, asserts round-trip. Then mocks `query()` to inspect that `resume=<loaded-id>` is in the call kwargs on the next dispatch.

Local validation before PR: `pytest .viabe/daemon/tests/ -v` (target: all pass), `ruff check .viabe/daemon/` (clean), `python -m mypy .viabe/daemon/agent-loop.py` (only if mypy already in project — check first; if not, skip and document).

## Risks

1. **`feedback_snapshot_sequencing.md` cross-check (Step-0 #4)** is in Cowork's memory, not the repo — I cannot verify it from this side. If Cowork's memory describes a different protocol than what's in `.viabe/protocol.md`, the daemon's implementation tracks the repo's version. Surfacing here so Cowork can flag any divergence at plan-review.

2. **`PreToolUse` hook can only match by tool name** (per the SDK docs: "matchers only filter by tool name, not by file paths or other arguments"). The Pillar-7 enforcement therefore matches on `Bash` broadly and inspects `tool_input.command` inside the callback. Means every Bash call pays the hook-callback cost — fine, ~µs of regex.

3. **`SessionStart` / `SessionEnd` hooks are TypeScript-only** (Python SDK docs confirm). The daemon does its session-id capture from `ResultMessage.session_id` in `process_signal` instead. The brief's "hook" requirement for session lifecycle becomes daemon-code rather than SDK-hook — functionally equivalent.

4. **Race on `status` file writes between daemon and Cowork**. Both sides must use tmp+rename. The brief calls this out (line 73-74) and so does my plan — I'll implement helper `atomic_write_status(task_id, status)` and document the same expectation for Cowork in the README.

5. **`gh pr merge` detection regex**. Pillar-7 hook uses `r"\bgh\s+pr\s+merge\b"`. Easy to bypass with `gh  pr  merge` (extra spaces) — `\s+` covers that. With aliasing (`alias mrg='gh pr merge'`) — covered by additional matchers if Fazal/Cowork ever introduces them; not a realistic adversarial threat in our single-user setup, but I'll add a comment noting the threat model.

6. **Process restart mid-`query()`**. If the daemon is killed mid-LLM call, the signal is still in INBOX (we only move to processed/ after success). Next start re-processes the same signal. Idempotency depends on the signal's content — `brief-ready` is idempotent (planning is a fresh write); `task` may not be (a half-completed `gh pr merge` could re-fire). Mitigation: `process_signal` moves the signal to `processed/` BEFORE calling `query()` when the signal is a `type: task` for merge (the action is one-shot bash). For `brief-ready` and `review`, move-after-success is fine. I'll document this asymmetry in the daemon code + README.

7. **Budget enforcement.** `max_budget_usd` is a per-`query()` call cap. The brief's task-level cap (400K tokens / 180 min) is daemon-level bookkeeping, not SDK-enforced. I'll track per-task spend in `cost.log` and have the daemon refuse to dispatch a new signal to a task that's already at 80% of its budget — the daemon signals `type: blocked` to Cowork and skips. This means the daemon needs to parse `budget_tokens` / `budget_minutes` from the active brief frontmatter.

8. **Pip install reliability in the install script**. `pip install claude-agent-sdk` could fail on Fazal's machine (network, sdk version drift, Python version mismatch). The install script captures stderr and exits with a clear message; the README documents the manual `python -m pip install claude-agent-sdk==0.2.87` fallback at the version I tested against.
