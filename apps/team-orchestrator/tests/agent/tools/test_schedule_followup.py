"""VT-48 — schedule_followup tests.

Write primitive only (insert / idempotency / validation / cancel_if
persistence / cross-tenant RLS). Scheduler fire/poll is VT-3.5, out of
scope. CI stdlib-only smoke skips via importorskip("langchain").
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langchain")


def _future(minutes: int = 0, days: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes, days=days)


def _pool(*, insert_returns: Any, existing_row: Any = None,
           raise_exc: Exception | None = None) -> tuple[Any, list[str]]:
    """Stub: set_config, INSERT ... RETURNING (fetchone), optional
    re-SELECT existing (fetchone)."""
    issued: list[str] = []
    cur = MagicMock()
    fetchone_q = [insert_returns, existing_row]

    def _execute(sql: str, params: tuple | None = None) -> None:
        issued.append(sql)
        if raise_exc is not None and "INSERT INTO scheduled_followups" in sql:
            raise raise_exc

    cur.execute.side_effect = _execute
    cur.fetchone.side_effect = lambda: fetchone_q.pop(0) if fetchone_q else None
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, issued


def _input(**over: Any):
    from orchestrator.agent.tools.schedule_followup import ScheduleFollowupInput

    base = dict(
        tenant_id="t1",
        follow_up_type="campaign_followup",
        fire_at=_future(days=3),
        follow_up_key="cfk-c1",
        payload={"campaign_id": "c1"},
    )
    base.update(over)
    return ScheduleFollowupInput(**base)  # type: ignore[arg-type]


def test_happy_path_scheduled() -> None:
    from orchestrator.agent.tools.schedule_followup import schedule_followup

    pool, issued = _pool(insert_returns={"id": "sched_1"})
    out = schedule_followup(_input(), pool=pool)
    assert out.status == "scheduled"
    assert out.scheduled_id == "sched_1"
    assert "set_config('app.current_tenant'" in issued[0]


def test_idempotent_duplicate_key() -> None:
    from orchestrator.agent.tools.schedule_followup import schedule_followup

    existing_fire = _future(days=3)
    pool, _ = _pool(
        insert_returns=None,  # ON CONFLICT DO NOTHING → no row
        existing_row={"id": "sched_existing", "fire_at": existing_fire},
    )
    out = schedule_followup(_input(), pool=pool)
    assert out.status == "duplicate_key"
    assert out.scheduled_id == "sched_existing"
    assert out.existing_fire_at == existing_fire


def test_fire_at_too_soon() -> None:
    from orchestrator.agent.tools.schedule_followup import schedule_followup

    pool, _ = _pool(insert_returns={"id": "x"})
    out = schedule_followup(_input(fire_at=_future(minutes=5)), pool=pool)
    assert out.status == "error"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "invalid_fire_at"


def test_fire_at_too_far() -> None:
    from orchestrator.agent.tools.schedule_followup import schedule_followup

    pool, _ = _pool(insert_returns={"id": "x"})
    out = schedule_followup(_input(fire_at=_future(days=100)), pool=pool)
    assert out.status == "error"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "invalid_fire_at"


def test_payload_too_large() -> None:
    from orchestrator.agent.tools.schedule_followup import schedule_followup

    big = {"blob": "x" * 5000}
    pool, _ = _pool(insert_returns={"id": "x"})
    out = schedule_followup(_input(payload=big), pool=pool)
    assert out.status == "error"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "payload_too_large"


def test_cancel_if_valid_grammar() -> None:
    from orchestrator.agent.tools.schedule_followup import schedule_followup

    pool, issued = _pool(insert_returns={"id": "x"})
    out = schedule_followup(
        _input(cancel_if=["campaign_status_in:[approved,sent]",
                          "phase_in:[cancelled]"]),
        pool=pool,
    )
    assert out.status == "scheduled"


def test_cancel_if_invalid_grammar() -> None:
    from orchestrator.agent.tools.schedule_followup import schedule_followup

    pool, _ = _pool(insert_returns={"id": "x"})
    out = schedule_followup(_input(cancel_if=["garbage_condition:x"]), pool=pool)
    assert out.status == "error"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "invalid_cancel_condition"


def test_sets_tenant_guc_before_insert() -> None:
    from orchestrator.agent.tools.schedule_followup import schedule_followup

    pool, issued = _pool(insert_returns={"id": "x"})
    schedule_followup(_input(tenant_id="tenant_xyz"), pool=pool)
    # GUC must be set before the INSERT (RLS path).
    set_idx = next(i for i, s in enumerate(issued)
                   if "set_config('app.current_tenant'" in s)
    ins_idx = next(i for i, s in enumerate(issued)
                   if "INSERT INTO scheduled_followups" in s)
    assert set_idx < ins_idx


def test_db_error_returns_envelope_never_raises() -> None:
    from orchestrator.agent.tools.schedule_followup import schedule_followup

    exc = type("UndefinedTable", (Exception,), {})("relation does not exist")
    pool, _ = _pool(insert_returns=None, raise_exc=exc)
    out = schedule_followup(_input(), pool=pool)
    assert out.status == "error"
    assert out.error_envelope is not None
    assert out.error_envelope.code == "db_error"
