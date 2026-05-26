#!/usr/bin/env python3
"""Viabe Team agent-loop daemon — entry point.

Runs as a launchd LaunchAgent on Fazal's machine. Maintains ONE Claude Code
conversation across all signals via `claude_agent_sdk` `session_id` resume.
See `core.py` for the scheduling policy, hook wiring, and signal dispatch.

Stop signals
------------
- `touch .viabe/daemon/STOP` → exits cleanly after the current iteration.
- SIGTERM / SIGINT → same.

The bash watcher `.viabe/daemon/watch-claude-inbox.sh` is preserved as the
Phase-1 fallback; do not run both at once.
"""
from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

DAEMON_DIR = Path(__file__).resolve().parent
if str(DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(DAEMON_DIR))

import core  # noqa: E402
import hooks  # noqa: E402


def _build_options():
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher  # type: ignore[import-not-found]

    # MAX-EFFORT config (Fazal directive, 2026-05-25 IST).
    #   - model: Opus 4.7 — most capable. SDK default is Sonnet.
    #   - max_thinking_tokens: 32K — extended thinking budget so the model can
    #     reason hard about architecture, edge cases, multi-file changes before
    #     writing.
    #   - max_budget_usd: $25 per call — 5× the conservative $5 default; gives
    #     headroom for long sessions without removing the safety ceiling.
    #     `notify` short-circuits LLM use anyway, so the cap is irrelevant for
    #     trivial signals.
    #   - max_turns left as None — bounded implicitly by the cost cap.
    return ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        setting_sources=["project"],
        model="claude-opus-4-7",
        max_thinking_tokens=32000,
        max_budget_usd=core.PER_CALL_BUDGET_USD,
        resume=None,  # set per-call via process_signal → save_session_id
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="Bash", hooks=[hooks.pre_tool_use_block_merges]),
            ],
            "PostToolUse": [HookMatcher(hooks=[hooks.post_tool_use_log])],
            "PreCompact": [HookMatcher(hooks=[hooks.pre_compact_archive])],
            "Stop": [HookMatcher(hooks=[hooks.stop_log_status])],
        },
    )


def _install_signal_handlers(stop_file: Path) -> None:
    def _flag(_signum, _frame):
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text("SIGTERM/SIGINT requested clean exit\n")

    signal.signal(signal.SIGTERM, _flag)
    signal.signal(signal.SIGINT, _flag)


def main() -> int:
    repo = DAEMON_DIR.parent.parent
    paths = core.default_paths(repo)
    paths.daemon_log.parent.mkdir(parents=True, exist_ok=True)
    _install_signal_handlers(paths.stop_file)
    try:
        asyncio.run(core.main_loop(paths, options_builder=_build_options))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
