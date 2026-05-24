"""Hook callbacks for the agent-loop daemon.

Hooks fire inside the SDK's agent loop. They run in this process — not in the
LLM's context window — so they're load-bearing for safety properties like
Pillar 7 (no autonomous PR merges).

Context discipline
------------------
Several hooks need information about the signal currently being processed
(active task, authorization status). The daemon is single-threaded by design;
a module-level set of context globals is acceptable. `process_signal` in
core.py sets these in a try/finally so a crash in query() can't leak stale
context into the next signal or a subsequent retry.

WARNING: not thread-safe. If we ever go concurrent, refactor to
contextvars.ContextVar.
"""
from __future__ import annotations

import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

_active_signal_context: Optional[dict] = None
_active_task_log_path: Optional[Path] = None
_active_session_jsonl: Optional[Path] = None
_transcripts_dir: Optional[Path] = None
_daemon_log_path: Optional[Path] = None
_active_task_id: Optional[str] = None
_active_status_path: Optional[Path] = None

_MERGE_PATTERN = re.compile(r"\bgh\s+pr\s+merge\b")


def _is_merge_command(command: str) -> bool:
    return bool(_MERGE_PATTERN.search(command or ""))


def _is_fazal_authorized_task(ctx: Optional[dict]) -> bool:
    if not isinstance(ctx, dict):
        return False
    return ctx.get("type") == "task" and ctx.get("authorized_by") == "fazal"


async def pre_tool_use_block_merges(input_data: dict, tool_use_id: Optional[str], context: Any) -> dict:
    """Pillar-7 enforcement.

    Any Bash call containing `gh pr merge` is denied unless the currently-
    processing signal is `type: task` with `authorized_by: fazal`. The
    authorization is per-signal — there is no session-wide blanket approval.
    """
    if input_data.get("tool_name") != "Bash":
        return {}

    command = (input_data.get("tool_input") or {}).get("command", "")
    if not _is_merge_command(command):
        return {}

    if _is_fazal_authorized_task(_active_signal_context):
        return {}

    return {
        "hookSpecificOutput": {
            "hookEventName": input_data.get("hook_event_name", "PreToolUse"),
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Cowork policy: PR merge is Fazal-only (Pillar 7). Use a type:task signal "
                "with authorized_by: fazal."
            ),
        },
        "systemMessage": "Pillar-7 block: autonomous PR merge denied.",
    }


async def post_tool_use_log(input_data: dict, tool_use_id: Optional[str], context: Any) -> dict:
    """Append a one-line summary to the active task's task_log.md."""
    log_path = _active_task_log_path
    if log_path is None:
        return {}

    tool_name = input_data.get("tool_name", "?")
    tool_input = input_data.get("tool_input") or {}
    summary_parts = [tool_name]
    for key in ("file_path", "path", "command", "url"):
        value = tool_input.get(key)
        if value:
            summary_parts.append(f"{key}={value!r}")
            break
    line = f"[{_iso_now()}] HOOK PostToolUse tool: {' '.join(summary_parts)}\n"
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as e:  # pragma: no cover - filesystem error path
        _daemon_warn(f"post_tool_use_log: append to {log_path} failed: {e}")
    return {}


async def pre_compact_archive(input_data: dict, tool_use_id: Optional[str], context: Any) -> dict:
    """Archive the session jsonl transcript before context compaction.

    Source path is provided by core.py via `_active_session_jsonl` (best-effort
    resolved from the SDK's storage convention). If the path doesn't exist on
    this machine, log a warning and skip — never crash the daemon.
    """
    src = _active_session_jsonl
    dest_dir = _transcripts_dir
    if src is None or dest_dir is None:
        return {}
    if not src.exists():
        _daemon_warn(f"pre_compact_archive: jsonl not found at {src}; skipping archive.")
        return {}

    dest_dir.mkdir(parents=True, exist_ok=True)
    session_id = input_data.get("session_id") or "unknown"
    dest = dest_dir / f"{session_id}-{int(time.time())}.jsonl"
    try:
        shutil.copy2(src, dest)
    except OSError as e:  # pragma: no cover - filesystem error path
        _daemon_warn(f"pre_compact_archive: copy {src} → {dest} failed: {e}")
    return {}


async def stop_log_status(input_data: dict, tool_use_id: Optional[str], context: Any) -> dict:
    """Record the active task's post-run status to the daemon log."""
    log_path = _daemon_log_path
    if log_path is None or _active_status_path is None:
        return {}
    try:
        status = _active_status_path.read_text().strip()
    except OSError:
        status = "<unreadable>"
    session_id = input_data.get("session_id", "?")
    line = f"[{_iso_now()}] STOP task={_active_task_id} status={status} session={session_id}\n"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as e:  # pragma: no cover
        print(f"stop_log_status: write to {log_path} failed: {e}", file=sys.stderr)
    return {}


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _daemon_warn(msg: str) -> None:
    if _daemon_log_path is None:
        print(msg, file=sys.stderr)
        return
    try:
        _daemon_log_path.parent.mkdir(parents=True, exist_ok=True)
        with _daemon_log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{_iso_now()}] WARN {msg}\n")
    except OSError:
        print(msg, file=sys.stderr)
