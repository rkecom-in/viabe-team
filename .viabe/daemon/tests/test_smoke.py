"""End-to-end smoke test: drop a signal in tmp INBOX, run one daemon iteration
with a mocked claude_agent_sdk.query, assert the expected side effects on disk.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

DAEMON_DIR = Path(__file__).resolve().parent.parent
if str(DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(DAEMON_DIR))

import core  # noqa: E402
from tests.conftest import make_fake_result  # noqa: E402


def _write_signal(inbox: Path, name: str, task: str, sig_type: str, body: str = "test") -> Path:
    path = inbox / name
    path.write_text(
        f"---\nfrom: cowork\nto: claudecode\ntask: {task}\ntype: {sig_type}\nts: 2026-05-24T22:00:00+05:30\n---\n\n{body}\n"
    )
    return path


def _write_brief(queue: Path, task: str, status: str = "queued", body: str | None = None) -> Path:
    task_dir = queue / task
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "status").write_text(f"{status}\n")
    body = body or (
        f"---\ntask: {task}\nauthor: cowork\nts: 2026-05-24T20:00:00+05:30\n"
        f"budget_tokens: 100000\nbudget_minutes: 60\n---\n\nbrief body\n"
    )
    (task_dir / "brief.md").write_text(body)
    return task_dir


def test_process_signal_brief_ready_writes_state(daemon_paths, fake_query) -> None:
    _write_brief(daemon_paths.queue, "VT-X", status="queued")
    sig = _write_signal(daemon_paths.inbox, "20260524T220000Z-brief-VT-X.md", "VT-X", "brief-ready")

    new_session = asyncio.run(
        core.process_signal(
            sig,
            session_id=None,
            options=None,
            paths=daemon_paths,
            query_fn=fake_query,
        )
    )

    assert new_session == "sess_smoke"
    assert not sig.exists(), "signal should have moved out of INBOX"
    archived = list((daemon_paths.processed).glob("*-brief-VT-X.md"))
    assert len(archived) == 1, f"expected processed copy, got {archived}"
    assert daemon_paths.session_state.read_text().strip() == "sess_smoke"
    cost_content = daemon_paths.cost_log.read_text()
    assert "VT-X" in cost_content
    assert "brief-ready" in cost_content
    assert "tokens=80" in cost_content  # 50 in + 30 out


def test_process_signal_notify_short_circuits_no_query(daemon_paths) -> None:
    """Condition #2: notify never reaches query()."""
    _write_brief(daemon_paths.queue, "VT-Y", status="in-pr")
    sig = _write_signal(daemon_paths.inbox, "20260524T220100Z-notify-VT-Y.md", "VT-Y", "notify", body="[COWORK NOTIFY] test echo")

    calls = []

    def _spy_query(*args, **kwargs):
        calls.append(kwargs)

        async def _empty():
            if False:
                yield None

        return _empty()

    new_session = asyncio.run(
        core.process_signal(
            sig,
            session_id="sess_prior",
            options=None,
            paths=daemon_paths,
            query_fn=_spy_query,
        )
    )

    assert calls == [], "notify must not invoke query()"
    assert new_session == "sess_prior", "session_id unchanged on short-circuit"
    assert not sig.exists()
    assert daemon_paths.daemon_log.read_text().strip().endswith("[COWORK NOTIFY] test echo")
    cost_lines = daemon_paths.cost_log.read_text().strip().splitlines()
    assert any("cost=$0.0" in line and "notify" in line for line in cost_lines)


def test_process_signal_query_exception_triggers_backoff_then_blocked(daemon_paths, monkeypatch) -> None:
    """Condition #1: persistent query() failure ⇒ type:blocked + leave signal in INBOX."""
    _write_brief(daemon_paths.queue, "VT-Z", status="implementing")
    sig = _write_signal(daemon_paths.inbox, "20260524T220200Z-review-VT-Z.md", "VT-Z", "review")

    call_count = {"n": 0}

    def _raising(*args, **kwargs):
        call_count["n"] += 1

        async def _gen():
            raise RuntimeError("api 5xx boom")
            yield None  # pragma: no cover

        return _gen()

    monkeypatch.setattr(core, "_BACKOFF_SLEEPS", [0, 0])

    result = asyncio.run(
        core.process_signal(
            sig,
            session_id="sess_prior",
            options=None,
            paths=daemon_paths,
            query_fn=_raising,
        )
    )

    assert call_count["n"] == 3, "expected initial + 2 retries"
    assert sig.exists(), "failed signal must stay in INBOX for re-trigger after fix"
    assert result == "sess_prior"
    blocked_files = list(daemon_paths.outbox.glob("*-blocked-VT-Z.md"))
    assert len(blocked_files) == 1
    blocked_text = blocked_files[0].read_text()
    assert "RuntimeError" in blocked_text
    assert "api 5xx boom" in blocked_text
    cost_text = daemon_paths.cost_log.read_text()
    assert "error=RuntimeError" in cost_text


def test_process_signal_task_merge_idempotent_move_before_query(daemon_paths) -> None:
    """Condition idempotency: type:task moves to processed/ BEFORE query() so a
    crash mid-call doesn't re-fire the merge on next iteration."""
    sig = _write_signal(
        daemon_paths.inbox,
        "20260524T220300Z-task-merge.md",
        "VT-OIV",
        "task",
        body="authorized_by frontmatter test body",
    )

    seen_paths = {"sig_existed": None}

    def _checking_query(*, prompt, options=None, **kwargs):
        # By the time query() runs, the signal should already be in processed/
        seen_paths["sig_existed"] = sig.exists()

        async def _gen():
            yield make_fake_result(session_id="sess_after_task")

        return _gen()

    asyncio.run(
        core.process_signal(
            sig,
            session_id=None,
            options=None,
            paths=daemon_paths,
            query_fn=_checking_query,
        )
    )

    assert seen_paths["sig_existed"] is False, "type:task must be archived before query()"


def test_budget_check_blocks_dispatch_at_80pct(daemon_paths, fake_query) -> None:
    """Condition #3: per-task token budget check refuses dispatch ≥80%."""
    _write_brief(
        daemon_paths.queue,
        "VT-W",
        status="queued",
        body=(
            "---\ntask: VT-W\nauthor: cowork\nts: 2026-05-24T20:00:00+05:30\n"
            "budget_tokens: 1000\nbudget_minutes: 60\n---\n\nbrief body\n"
        ),
    )
    # Pre-populate cost.log with 800 tokens consumed (80% of 1000).
    daemon_paths.cost_log.parent.mkdir(parents=True, exist_ok=True)
    daemon_paths.cost_log.write_text(
        "2026-05-24T21:00:00+05:30 VT-W brief-ready cost=$0.50 turns=5 tokens=800 session=sess_x error=None\n"
    )
    sig = _write_signal(daemon_paths.inbox, "20260524T220400Z-review-VT-W.md", "VT-W", "review")

    calls = []

    def _spy_query(*args, **kwargs):
        calls.append(kwargs)

        async def _e():
            if False:
                yield None

        return _e()

    asyncio.run(
        core.process_signal(
            sig,
            session_id=None,
            options=None,
            paths=daemon_paths,
            query_fn=_spy_query,
        )
    )

    assert calls == [], "dispatch should not call query() over budget"
    blocked = list(daemon_paths.outbox.glob("*-blocked-VT-W.md"))
    assert len(blocked) == 1
    assert "budget" in blocked[0].read_text().lower()
    assert sig.exists(), "blocked signal stays in INBOX until budget reset"


def test_load_save_session_id_roundtrip(tmp_path) -> None:
    state_file = tmp_path / "session.state"
    assert core.load_session_id(state_file) is None
    core.save_session_id(state_file, "sess_roundtrip")
    assert core.load_session_id(state_file) == "sess_roundtrip"


def test_apply_merge_cleanup_moves_dir_and_unblocks_dependents(daemon_paths) -> None:
    _write_brief(daemon_paths.queue, "VT-PARENT", status="in-pr")
    (daemon_paths.queue / "VT-PARENT/pr.md").write_text("pr_url: https://github.com/o/r/pull/99\n")
    _write_brief(
        daemon_paths.queue,
        "VT-CHILD",
        status="blocked",
        body=(
            "---\ntask: VT-CHILD\nauthor: cowork\nts: 2026-05-24T20:00:00+05:30\n"
            "depends_on: VT-PARENT (waiting for the parent to land)\n"
            "budget_tokens: 1000\nbudget_minutes: 60\n---\n\nchild brief\n"
        ),
    )

    core.apply_merge_cleanup("VT-PARENT", merge_sha="abc123", paths=daemon_paths)

    assert not (daemon_paths.queue / "VT-PARENT").exists()
    assert (daemon_paths.queue / "done/VT-PARENT/status").read_text().strip() == "done"
    assert (daemon_paths.queue / "VT-CHILD/status").read_text().strip() == "queued"


def test_pick_next_action_via_real_filesystem(daemon_paths) -> None:
    _write_brief(daemon_paths.queue, "VT-FS-A", status="queued")
    _write_brief(daemon_paths.queue, "VT-FS-B", status="blocked")
    sig = _write_signal(daemon_paths.inbox, "20260524T220500Z-brief-FS.md", "VT-FS-A", "brief-ready")

    state = core.read_queue_state(daemon_paths.queue)
    inbox = core.scan_inbox(daemon_paths.inbox)
    action = core.pick_next_action(state, inbox)

    assert isinstance(action, core.ProcessSignal)
    assert action.path == sig
