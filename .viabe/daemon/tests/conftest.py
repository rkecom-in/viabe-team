"""Shared fixtures for daemon tests."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

DAEMON_DIR = Path(__file__).resolve().parent.parent
if str(DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(DAEMON_DIR))


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Build a minimal daemon-aware repo layout under tmp_path."""
    (tmp_path / ".running/to-claudecode").mkdir(parents=True)
    (tmp_path / ".running/to-cowork").mkdir(parents=True)
    (tmp_path / ".running/processed").mkdir(parents=True)
    (tmp_path / ".viabe/queue").mkdir(parents=True)
    (tmp_path / ".viabe/queue/done").mkdir(parents=True)
    (tmp_path / ".viabe/daemon/transcripts").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def daemon_paths(tmp_repo: Path):
    """A DaemonPaths constructed under tmp_repo. Imported lazily so core can
    fail import without poisoning the rest of the test session."""
    from core import DaemonPaths

    return DaemonPaths(
        repo=tmp_repo,
        inbox=tmp_repo / ".running/to-claudecode",
        outbox=tmp_repo / ".running/to-cowork",
        processed=tmp_repo / ".running/processed",
        queue=tmp_repo / ".viabe/queue",
        cost_log=tmp_repo / ".viabe/daemon/cost.log",
        session_state=tmp_repo / ".viabe/daemon/session.state",
        daemon_log=tmp_repo / ".viabe/daemon/agent-loop.log",
        transcripts=tmp_repo / ".viabe/daemon/transcripts",
        stop_file=tmp_repo / ".viabe/daemon/STOP",
        telegram_env=tmp_repo / ".viabe/secrets/telegram.env",
        notifications_log=tmp_repo / ".viabe/notifications/log",
    )


def make_fake_result(
    *,
    session_id: str = "sess_smoke",
    cost_usd: float = 0.001,
    input_tokens: int = 50,
    output_tokens: int = 30,
    subtype: str = "success",
    result_text: str = "done",
) -> SimpleNamespace:
    """Construct a duck-typed ResultMessage substitute."""
    return SimpleNamespace(
        subtype=subtype,
        session_id=session_id,
        total_cost_usd=cost_usd,
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        num_turns=1,
        result=result_text,
        is_error=(subtype != "success"),
        duration_ms=100,
        duration_api_ms=80,
    )


def make_fake_assistant(text: str = "done") -> SimpleNamespace:
    """Duck-typed AssistantMessage substitute."""
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        model="claude-test",
    )


def fake_query_factory(result_msg=None):
    """Return a callable that, when called, returns an async iterator yielding
    one assistant message + one result message. Mirrors claude_agent_sdk.query
    signature loosely (keyword-only `prompt` + `options`)."""
    if result_msg is None:
        result_msg = make_fake_result()

    async def _gen(*, prompt, options=None, **_kwargs):
        yield make_fake_assistant("ack")
        yield result_msg

    def _factory(*args, **kwargs):
        return _gen(*args, **kwargs)

    return _factory


@pytest.fixture
def fake_query():
    """A drop-in for claude_agent_sdk.query that yields a deterministic transcript."""
    return fake_query_factory()
