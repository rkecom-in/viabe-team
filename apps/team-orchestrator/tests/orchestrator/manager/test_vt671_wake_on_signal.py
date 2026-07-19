"""VT-671 — wake-on-signal for the owner-wait loops (latency-tail fix).

Pins the three pieces:
  1. park stamps the LIVE workflow id into stall_metadata (redrive-suffixed ids can't be derived);
  2. resolution wakes the stamped workflow via DBOS.send (ANY decision; content-free hint);
  3. every failure is fail-soft — the poll ladder remains the fallback authority.

The recv-loop behavior itself (recv returns early → condition re-check) is exercised end-to-end by
test_sr_loop_e2e in the pre-push DB job; these are the seam units.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("psycopg")

from orchestrator.agent import approval_resume as ar  # noqa: E402


def _patch_bound(monkeypatch: pytest.MonkeyPatch, bound: dict[str, Any] | None) -> None:
    import orchestrator.manager.task_store as ts

    monkeypatch.setattr(ts, "find_task_for_resolved_approval", lambda t, a, conn=None: bound)


def test_wake_sends_to_stamped_workflow_id(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str, str | None]] = []

    import dbos as dbos_mod

    monkeypatch.setattr(
        dbos_mod.DBOS, "send",
        classmethod(lambda cls, dest, msg, topic=None, **kw: sent.append((dest, msg, topic))),
    )
    _patch_bound(monkeypatch, {
        "id": str(uuid4()), "status": "waiting_owner", "approval_type": "campaign_send",
        "stall_metadata": {"awaiting_approval_run_id": "x", "wait_workflow_id": "manager_task:t:1-redrive-2"},
    })
    ar._wake_waiting_workflow(object(), uuid4(), uuid4())
    assert sent == [("manager_task:t:1-redrive-2", "resolved", "owner_signal")]


def test_wake_noop_without_stamp_or_task(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []
    import dbos as dbos_mod

    monkeypatch.setattr(
        dbos_mod.DBOS, "send", classmethod(lambda cls, *a, **k: sent.append(a))
    )
    # No bound task → no send.
    _patch_bound(monkeypatch, None)
    ar._wake_waiting_workflow(object(), uuid4(), uuid4())
    # Bound but pre-VT-671 park (no stamp) → no send (poll ladder covers).
    _patch_bound(monkeypatch, {
        "id": str(uuid4()), "status": "waiting_owner", "approval_type": "campaign_send",
        "stall_metadata": {"awaiting_approval_run_id": "x"},
    })
    ar._wake_waiting_workflow(object(), uuid4(), uuid4())
    assert sent == []


def test_wake_fails_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    import dbos as dbos_mod

    def _boom(cls, *a, **k):
        raise RuntimeError("dbos down")

    monkeypatch.setattr(dbos_mod.DBOS, "send", classmethod(_boom))
    _patch_bound(monkeypatch, {
        "id": str(uuid4()), "status": "waiting_owner", "approval_type": "campaign_send",
        "stall_metadata": {"wait_workflow_id": "wf-1"},
    })
    ar._wake_waiting_workflow(object(), uuid4(), uuid4())  # must not raise


def test_park_stamps_wait_workflow_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """park_awaiting_approval merges wait_workflow_id into the stall_metadata stamp."""
    import importlib

    from orchestrator.manager import task_store as ts

    captured: dict[str, Any] = {}

    class _Cur:
        rowcount = 1

    class _Conn:
        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params
            return _Cur()

    from contextlib import contextmanager

    @contextmanager
    def _fake(tenant_id):
        yield _Conn()

    tc_mod = importlib.import_module("orchestrator.db.tenant_connection")
    monkeypatch.setattr(tc_mod, "tenant_connection", _fake)
    # task_store binds tenant_connection at import — patch its module-level ref too.
    monkeypatch.setattr(ts, "tenant_connection", _fake, raising=False)

    assert ts.park_awaiting_approval(
        uuid4(), uuid4(), run_id=uuid4(), wait_workflow_id="manager_task:t:9-redrive-1"
    )
    meta = captured["params"][0]
    # Jsonb wrapper: inspect the wrapped object.
    obj = getattr(meta, "obj", meta)
    assert obj["wait_workflow_id"] == "manager_task:t:9-redrive-1"
    assert "awaiting_approval_run_id" in obj
