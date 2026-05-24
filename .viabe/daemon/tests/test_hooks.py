"""Hook callback tests — Pillar-7 enforcement, task-log appending, transcript archive.

Hooks are async callbacks. Tests drive them via asyncio.run so we don't need a
pytest-asyncio marker.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

DAEMON_DIR = Path(__file__).resolve().parent.parent
if str(DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(DAEMON_DIR))

import hooks  # noqa: E402


def _pre_tool_input(command: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": "test"},
        "session_id": "sess_test",
        "cwd": "/tmp",
    }


def _post_tool_input(tool: str, args: dict) -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": tool,
        "tool_input": args,
        "tool_response": {"stdout": "ok", "stderr": "", "exit_code": 0},
        "session_id": "sess_test",
        "cwd": "/tmp",
    }


def _run(coro):
    return asyncio.run(coro)


def test_pillar7_blocks_unauthorized_merge() -> None:
    hooks._active_signal_context = None  # no active task signal at all
    result = _run(
        hooks.pre_tool_use_block_merges(
            _pre_tool_input("gh pr merge 53 --squash --delete-branch"),
            tool_use_id="t1",
            context=None,
        )
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Pillar 7" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_pillar7_allows_authorized_merge() -> None:
    hooks._active_signal_context = {
        "type": "task",
        "authorized_by": "fazal",
        "task": "VT-OIV",
    }
    try:
        result = _run(
            hooks.pre_tool_use_block_merges(
                _pre_tool_input("gh pr merge 53 --squash --delete-branch"),
                tool_use_id="t2",
                context=None,
            )
        )
        assert result == {}
    finally:
        hooks._active_signal_context = None


def test_pillar7_blocks_task_signal_without_fazal_authorization() -> None:
    hooks._active_signal_context = {"type": "task", "authorized_by": "someone-else"}
    try:
        result = _run(
            hooks.pre_tool_use_block_merges(
                _pre_tool_input("gh pr merge 53"),
                tool_use_id="t3",
                context=None,
            )
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    finally:
        hooks._active_signal_context = None


def test_pillar7_ignores_non_merge_bash() -> None:
    hooks._active_signal_context = None
    result = _run(
        hooks.pre_tool_use_block_merges(
            _pre_tool_input("ls -la"), tool_use_id="t4", context=None
        )
    )
    assert result == {}


def test_pillar7_blocks_extra_whitespace_variant() -> None:
    hooks._active_signal_context = None
    result = _run(
        hooks.pre_tool_use_block_merges(
            _pre_tool_input("gh   pr   merge  53"), tool_use_id="t5", context=None
        )
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_post_tool_use_log_appends_to_active_task_log(tmp_path) -> None:
    queue_dir = tmp_path / "VT-X"
    queue_dir.mkdir()
    (queue_dir / "task_log.md").write_text("[existing]\n")

    hooks._active_task_log_path = queue_dir / "task_log.md"
    try:
        _run(
            hooks.post_tool_use_log(
                _post_tool_input("Read", {"file_path": "/tmp/foo"}),
                tool_use_id="t6",
                context=None,
            )
        )
    finally:
        hooks._active_task_log_path = None

    contents = (queue_dir / "task_log.md").read_text()
    assert "[existing]" in contents
    assert "Read" in contents
    assert "/tmp/foo" in contents or "tool: Read" in contents


def test_post_tool_use_log_silent_when_no_active_task(tmp_path) -> None:
    hooks._active_task_log_path = None
    result = _run(
        hooks.post_tool_use_log(
            _post_tool_input("Bash", {"command": "ls"}),
            tool_use_id="t7",
            context=None,
        )
    )
    assert result == {}


def test_pre_compact_archive_copies_existing_jsonl(tmp_path) -> None:
    src = tmp_path / "sess.jsonl"
    src.write_text('{"role": "user", "content": "hi"}\n')
    dest_dir = tmp_path / "transcripts"

    hooks._active_session_jsonl = src
    hooks._transcripts_dir = dest_dir
    try:
        result = _run(
            hooks.pre_compact_archive(
                {"hook_event_name": "PreCompact", "trigger": "auto", "session_id": "sess123"},
                tool_use_id=None,
                context=None,
            )
        )
    finally:
        hooks._active_session_jsonl = None
        hooks._transcripts_dir = None

    assert result == {}
    assert dest_dir.exists()
    archived = list(dest_dir.glob("sess123-*.jsonl"))
    assert len(archived) == 1
    assert archived[0].read_text() == '{"role": "user", "content": "hi"}\n'


def test_pre_compact_archive_skips_when_jsonl_missing(tmp_path, capsys) -> None:
    hooks._active_session_jsonl = tmp_path / "nonexistent.jsonl"
    hooks._transcripts_dir = tmp_path / "transcripts"
    try:
        result = _run(
            hooks.pre_compact_archive(
                {"hook_event_name": "PreCompact", "trigger": "manual", "session_id": "sess123"},
                tool_use_id=None,
                context=None,
            )
        )
    finally:
        hooks._active_session_jsonl = None
        hooks._transcripts_dir = None

    assert result == {}
    assert not (tmp_path / "transcripts").exists() or not list((tmp_path / "transcripts").glob("*.jsonl"))


def test_stop_log_status_writes_to_daemon_log(tmp_path) -> None:
    daemon_log = tmp_path / "agent-loop.log"
    status_file = tmp_path / "status"
    status_file.write_text("in-pr\n")

    hooks._daemon_log_path = daemon_log
    hooks._active_task_id = "VT-Y"
    hooks._active_status_path = status_file
    try:
        result = _run(
            hooks.stop_log_status(
                {"hook_event_name": "Stop", "session_id": "sess_test"},
                tool_use_id=None,
                context=None,
            )
        )
    finally:
        hooks._daemon_log_path = None
        hooks._active_task_id = None
        hooks._active_status_path = None

    assert result == {}
    content = daemon_log.read_text()
    assert "VT-Y" in content
    assert "in-pr" in content
