"""VT-679 D2/D3/D4 — behavioral tests for ``orchestrator.business_plan.daily_initiative``.

Pure-logic / monkeypatched-collaborator tests only (no DB): the selection rule (order,
skip-busy-this-month, month-scoping), the back-pressure statuses set (deliberately excludes
'blocked'), and the dispatch ordering contract (surface BEFORE start_manager_task_workflow;
report_item_status AFTER; the 'queued'-not-admitted edge case never surfaces/starts).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

from orchestrator.business_plan import daily_initiative as di  # noqa: E402
from orchestrator.business_plan.store import RoadmapItem  # noqa: E402

_TENANT = UUID("00000000-0000-4000-8000-0000000000d1")


def _item(
    *,
    item_id: str = "item-1",
    seq: int = 1,
    objective: str = "Reply to Zomato reviews",
    why: str = "Rated 4.2 on Zomato [F1]",
    owning_agent: str = "reputation",
) -> RoadmapItem:
    return RoadmapItem(
        item_id=item_id,
        seq=seq,
        month=1,
        objective=objective,
        why=why,
        owning_agent=owning_agent,
        status="accepted",
    )


# ---------------------------------------------------------------------------
# _idempotency_key
# ---------------------------------------------------------------------------


def test_idempotency_key_format() -> None:
    assert di._idempotency_key("abc-123", "202608") == "plan-item:abc-123:202608"


# ---------------------------------------------------------------------------
# D4 — back-pressure statuses set (deliberately narrower than task_store.TASK_ACTIVE)
# ---------------------------------------------------------------------------


def test_busy_statuses_excludes_blocked_queued_shadow() -> None:
    """The ratified design brief deliberately excludes 'blocked' (and 'queued'/'shadow', which
    were never active-task statuses to begin with) — a blocked/escalated task needs an operator,
    not a permanently-starved proactive queue."""
    assert set(di._BUSY_STATUSES) == {
        "running", "waiting_owner", "verifying", "clarifying", "planned",
    }
    assert "blocked" not in di._BUSY_STATUSES


# ---------------------------------------------------------------------------
# select_next_item — merge across agents, plan-order (seq), skip-busy-this-month
# ---------------------------------------------------------------------------


def test_select_next_item_merges_across_agents_and_sorts_by_seq(monkeypatch) -> None:
    """Items from DIFFERENT owning agents must re-merge into overall plan order (seq), not
    per-agent order."""
    by_agent = {
        "reputation": [_item(item_id="rep-3", seq=3, owning_agent="reputation")],
        "sales_recovery": [_item(item_id="sr-1", seq=1, owning_agent="sales_recovery")],
        "retention": [_item(item_id="ret-2", seq=2, owning_agent="retention")],
    }

    def _fake_items_for_agent(tenant_id, owning_agent, *, statuses=("accepted",)):
        return by_agent.get(owning_agent, [])

    monkeypatch.setattr(di, "items_for_agent", _fake_items_for_agent)

    from orchestrator.manager import task_store

    monkeypatch.setattr(task_store, "find_task_id", lambda tenant_id, key: None)

    item = di.select_next_item(_TENANT, "202608")
    assert item is not None
    assert item.item_id == "sr-1", "seq=1 (sales_recovery) must win regardless of agent order"


def test_select_next_item_skips_items_with_existing_task_this_month(monkeypatch) -> None:
    items = [_item(item_id="a", seq=1), _item(item_id="b", seq=2)]
    monkeypatch.setattr(
        di, "items_for_agent",
        lambda tenant_id, agent, *, statuses=("accepted",): items if agent == "reputation" else [],
    )

    from orchestrator.manager import task_store

    existing_key = di._idempotency_key("a", "202608")

    def _fake_find(tenant_id, key):
        return uuid4() if key == existing_key else None

    monkeypatch.setattr(task_store, "find_task_id", _fake_find)

    item = di.select_next_item(_TENANT, "202608")
    assert item is not None and item.item_id == "b", "item 'a' already has a task this month"


def test_select_next_item_month_scoping(monkeypatch) -> None:
    """An item with an existing task for a DIFFERENT month is selectable again — the idempotency
    key is month-scoped, not item-scoped."""
    items = [_item(item_id="a", seq=1)]
    monkeypatch.setattr(
        di, "items_for_agent",
        lambda tenant_id, agent, *, statuses=("accepted",): items if agent == "reputation" else [],
    )

    from orchestrator.manager import task_store

    july_key = di._idempotency_key("a", "202607")

    def _fake_find(tenant_id, key):
        return uuid4() if key == july_key else None

    monkeypatch.setattr(task_store, "find_task_id", _fake_find)

    assert di.select_next_item(_TENANT, "202607") is None, "already dispatched in July"
    assert di.select_next_item(_TENANT, "202608") is not None, "August is a fresh month"


def test_select_next_item_returns_none_when_no_candidates(monkeypatch) -> None:
    monkeypatch.setattr(di, "items_for_agent", lambda tenant_id, agent, *, statuses=("accepted",): [])
    assert di.select_next_item(_TENANT, "202608") is None


# ---------------------------------------------------------------------------
# dispatch_daily_initiative — back-pressure skip, no-item skip, and the ordering contract
# ---------------------------------------------------------------------------


def test_dispatch_skips_when_tenant_busy(monkeypatch) -> None:
    monkeypatch.setattr(di, "_tenant_is_busy", lambda tenant_id: True)
    called = {"n": 0}
    monkeypatch.setattr(di, "select_next_item", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    from datetime import datetime, timezone

    result = di.dispatch_daily_initiative(_TENANT, now=datetime(2026, 8, 2, tzinfo=timezone.utc))
    assert result is None
    assert called["n"] == 0, "back-pressure must short-circuit BEFORE selection runs"


def test_dispatch_returns_none_when_no_item(monkeypatch) -> None:
    monkeypatch.setattr(di, "_tenant_is_busy", lambda tenant_id: False)
    monkeypatch.setattr(di, "select_next_item", lambda tenant_id, year_month: None)

    from datetime import datetime, timezone

    result = di.dispatch_daily_initiative(_TENANT, now=datetime(2026, 8, 2, tzinfo=timezone.utc))
    assert result is None


def test_dispatch_happy_path_order_and_citation_strip(monkeypatch) -> None:
    """The FULL D2+D3 ordering contract: create_plan -> (task admitted 'planned') -> surface
    message BEFORE start_manager_task_workflow -> report_item_status AFTER. Citation markers
    ([F#]) must never reach the ManagerPlan text."""
    item = _item(
        item_id="item-42", seq=1, owning_agent="reputation",
        objective="Reply to Zomato reviews [F2][F5]",
        why="Rated 4.2 on Zomato [F2]",
    )
    monkeypatch.setattr(di, "_tenant_is_busy", lambda tenant_id: False)
    monkeypatch.setattr(di, "select_next_item", lambda tenant_id, year_month: item)

    events: list[str] = []
    captured: dict[str, Any] = {}
    task_id = uuid4()

    from orchestrator.manager import plan_store, task_store
    from orchestrator.manager import workflow as manager_workflow

    def _fake_create_plan(tenant_id, plan, *, source_message_sid, **kw):
        captured["plan"] = plan
        captured["source_message_sid"] = source_message_sid
        events.append("create_plan")
        return task_id

    def _fake_get_task(tenant_id, tid):
        return {"status": "planned"}

    def _fake_start(tenant_id, tid):
        events.append("start_manager_task_workflow")

    def _fake_surface(tenant_id, itm):
        events.append("surface")

    def _fake_report_status(tenant_id, item_id, new_status, *, agent):
        events.append("report_item_status")
        captured["report_status_args"] = (item_id, new_status, agent)

    monkeypatch.setattr(plan_store, "create_plan", _fake_create_plan)
    monkeypatch.setattr(task_store, "get_task", _fake_get_task)
    monkeypatch.setattr(manager_workflow, "start_manager_task_workflow", _fake_start)
    monkeypatch.setattr(di, "_surface_initiative", _fake_surface)
    monkeypatch.setattr(di, "report_item_status", _fake_report_status)

    from datetime import datetime, timezone

    result = di.dispatch_daily_initiative(
        _TENANT, now=datetime(2026, 8, 2, tzinfo=timezone.utc)
    )

    assert result == {
        "task_id": str(task_id), "item_id": "item-42",
        "owning_agent": "reputation", "status": "planned",
    }
    assert events == ["create_plan", "surface", "start_manager_task_workflow", "report_item_status"], (
        "surface MUST fire before start_manager_task_workflow (D3: surface before effect)"
    )
    assert captured["source_message_sid"] == "plan-item:item-42:202608"
    plan = captured["plan"]
    assert "[F2]" not in plan.objective and "[F5]" not in plan.objective
    assert "[F2]" not in plan.steps[0].situation
    assert captured["report_status_args"] == ("item-42", "in_progress", "reputation")


def test_dispatch_not_admitted_planned_skips_surface_and_start(monkeypatch) -> None:
    """create_plan admitted 'queued' (an active/blocked task already occupies the slot) —
    dispatch must NOT surface or start the workflow, and must NOT flip the item status."""
    item = _item(item_id="item-9", seq=1)
    monkeypatch.setattr(di, "_tenant_is_busy", lambda tenant_id: False)
    monkeypatch.setattr(di, "select_next_item", lambda tenant_id, year_month: item)

    task_id = uuid4()
    events: list[str] = []

    from orchestrator.manager import plan_store, task_store
    from orchestrator.manager import workflow as manager_workflow

    monkeypatch.setattr(
        plan_store, "create_plan", lambda tenant_id, plan, *, source_message_sid, **kw: task_id
    )
    monkeypatch.setattr(task_store, "get_task", lambda tenant_id, tid: {"status": "queued"})
    monkeypatch.setattr(
        manager_workflow, "start_manager_task_workflow",
        lambda *a, **k: events.append("start_manager_task_workflow"),
    )
    monkeypatch.setattr(di, "_surface_initiative", lambda *a, **k: events.append("surface"))
    monkeypatch.setattr(
        di, "report_item_status", lambda *a, **k: events.append("report_item_status")
    )

    from datetime import datetime, timezone

    result = di.dispatch_daily_initiative(_TENANT, now=datetime(2026, 8, 2, tzinfo=timezone.utc))

    assert events == [], "queued (not planned) must skip surface/start/report entirely"
    assert result == {
        "task_id": str(task_id), "item_id": "item-9", "owning_agent": "reputation",
        "status": "queued",
    }
