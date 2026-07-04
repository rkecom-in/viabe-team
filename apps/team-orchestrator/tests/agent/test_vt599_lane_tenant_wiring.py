"""VT-599 — the shared ``resolve_lane_tenant`` wiring across the accounting / finance / tech /
cost_opt lanes (marketing_lane gets its own full-coverage suite:
``tests/orchestrator/agent/test_marketing_lane_tenant_scope.py`` — it was the live-failure
module). ``sales_lane`` is asserted to hold NO ``tenant_id``-taking tool at all (it reasons over a
manager-supplied ledger slice, never a tenant-scoped DB read of its own) — there is nothing to
wire there, and this file pins that fact rather than silently skipping the lane.

Two things are proven:

  1. EVERY ``tenant_id``-taking tool across the four lanes (18 tools total) returns the
     structured, non-raising ``lane_tenant_error`` shape when there is no run context AND the
     model-supplied value is not a UUID — never a raise (parametrized across all 18; this is the
     one behavior common to every tool regardless of what it does downstream, since resolution
     fails BEFORE any DB/rail call).
  2. ONE representative tool per lane proves the full override wiring end-to-end: a run context
     present + a model-supplied business name (the live-defect shape) still executes the tool
     against the CONTEXT tenant, with a mismatch warning logged — reusing each lane's own existing
     monkeypatch fixtures so no live DB/pool is touched.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("langchain")

from orchestrator.observability.decorators import observability_context  # noqa: E402

_LOGGER_NAME = "orchestrator.agent.lane_tenant"

# (module path, tool attr name, kwargs sans tenant_id) for every tenant_id-taking tool across the
# four lanes NOT covered by the dedicated marketing_lane suite. 4 + 4 + 5 + 5 = 18 tools.
_LANE_TOOLS: list[tuple[str, str, dict[str, Any]]] = [
    # accounting_lane (4)
    ("orchestrator.agent.accounting_lane", "accounting_categorize_books", {}),
    ("orchestrator.agent.accounting_lane", "accounting_prepare_tax_summary", {}),
    ("orchestrator.agent.accounting_lane", "accounting_organize_invoices_expenses", {}),
    ("orchestrator.agent.accounting_lane", "accounting_reconcile_transactions", {"lookback_days": 90}),
    # finance_lane (4)
    ("orchestrator.agent.finance_lane", "analyze_cash_flow", {}),
    ("orchestrator.agent.finance_lane", "analyze_receivables", {}),
    ("orchestrator.agent.finance_lane", "pricing_margin_input", {}),
    (
        "orchestrator.agent.finance_lane",
        "propose_payment_reminder",
        {"customer_id": str(uuid4()), "reason": "x", "reminder_text": "x"},
    ),
    # tech_lane (5)
    ("orchestrator.agent.tech_lane", "read_integration_health", {}),
    ("orchestrator.agent.tech_lane", "read_listing_health", {}),
    ("orchestrator.agent.tech_lane", "read_tech_context", {}),
    (
        "orchestrator.agent.tech_lane",
        "propose_config_change",
        {"target": "shopify", "change_summary": "x"},
    ),
    ("orchestrator.agent.tech_lane", "check_config_change_intent", {"target": "shopify"}),
    # cost_opt_lane (5)
    ("orchestrator.agent.cost_opt_lane", "analyze_tenant_spend", {"window_days": 30}),
    ("orchestrator.agent.cost_opt_lane", "analyze_unit_economics", {"window_days": 30}),
    ("orchestrator.agent.cost_opt_lane", "identify_spend_anomaly", {}),
    ("orchestrator.agent.cost_opt_lane", "analyze_marketing_roi", {"window_days": 30}),
    ("orchestrator.agent.cost_opt_lane", "read_cost_context", {}),
]


def _tool_id(entry: tuple[str, str, dict[str, Any]]) -> str:
    module_path, tool_name, _ = entry
    return f"{module_path.rsplit('.', 1)[-1]}.{tool_name}"


@pytest.mark.parametrize("entry", _LANE_TOOLS, ids=_tool_id)
def test_no_context_garbage_tenant_id_returns_tool_error_never_raises(
    entry: tuple[str, str, dict[str, Any]],
) -> None:
    """No run context + a non-UUID model value (a business name) -> the structured tool_error,
    for EVERY tenant_id-taking tool across the four lanes. Never a raise (the VT-599 live defect
    shape: these lane sub-graphs hold no VT-484 tool-error middleware of their own)."""
    import importlib

    module_path, tool_name, kwargs = entry
    mod = importlib.import_module(module_path)
    tool = getattr(mod, tool_name)

    out = tool.func(tenant_id="Sundaram Stores", **kwargs)  # type: ignore[attr-defined]
    assert out == {"status": "error", "error": f"{tool_name}: no resolvable tenant context"}


# --- one representative tool per lane: full context-override wiring, no live DB ------------------


def test_accounting_categorize_books_business_name_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import orchestrator.agent.accounting_lane as lane
    from orchestrator.agent.accounting_lane import accounting_categorize_books

    seen: dict[str, Any] = {}

    def _fake_summary(tid: Any) -> dict[str, Any]:
        seen["tid"] = tid
        return {}

    monkeypatch.setattr(lane, "_read_ledger_summary", _fake_summary)

    run_id, tenant_id = uuid4(), uuid4()
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        with observability_context(run_id=run_id, tenant_id=tenant_id):
            out = accounting_categorize_books.func("Sundaram Stores")  # type: ignore[attr-defined]

    assert seen["tid"] == tenant_id  # the CONTEXT tenant reached the ledger read, not the name
    assert "PREPARED" in out["note"]
    mismatches = [r for r in caplog.records if "accounting_categorize_books" in r.getMessage()]
    assert len(mismatches) == 1


def test_analyze_cash_flow_business_name_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from orchestrator.agent import finance_lane

    seen: dict[str, Any] = {}

    class _FakeConn:
        def execute(self, _sql: Any, params: Any = None) -> "_FakeConn":
            seen["tid"] = params[0] if params else None
            return self

        def fetchone(self) -> dict[str, Any]:
            # Covers both the ledger read (inflow/collected/sale_count/payment_count) and the
            # nested imported_transactions read (credit/debit) with one shared shape.
            return {"inflow": 0, "collected": 0, "sale_count": 0, "payment_count": 0, "credit": 0, "debit": 0}

        def __enter__(self) -> "_FakeConn":
            return self

        def __exit__(self, *a: Any) -> bool:
            return False

    monkeypatch.setattr(finance_lane, "tenant_connection", lambda _tid: _FakeConn())

    run_id, tenant_id = uuid4(), uuid4()
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        with observability_context(run_id=run_id, tenant_id=tenant_id):
            finance_lane.analyze_cash_flow.func("Sundaram Stores")  # type: ignore[attr-defined]

    assert seen["tid"] == str(tenant_id)
    mismatches = [r for r in caplog.records if "analyze_cash_flow" in r.getMessage()]
    assert len(mismatches) == 1


def test_read_integration_health_business_name_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import sys

    from orchestrator.agent import tech_lane

    seen: dict[str, Any] = {}

    class _FakeConn:
        def execute(self, _sql: Any, params: Any) -> "_FakeConn":
            seen["tid"] = params[0]
            return self

        def fetchall(self) -> list[Any]:
            return []

        def __enter__(self) -> "_FakeConn":
            return self

        def __exit__(self, *a: Any) -> bool:
            return False

    import orchestrator.db.tenant_connection  # noqa: F401 — ensure the submodule is loaded

    tc_mod = sys.modules["orchestrator.db.tenant_connection"]
    monkeypatch.setattr(tc_mod, "tenant_connection", lambda _tid, **kw: _FakeConn())

    run_id, tenant_id = uuid4(), uuid4()
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        with observability_context(run_id=run_id, tenant_id=tenant_id):
            out = tech_lane.read_integration_health.func("Sundaram Stores")  # type: ignore[attr-defined]

    assert out["count"] == 0
    assert seen["tid"] == str(tenant_id)
    mismatches = [r for r in caplog.records if "read_integration_health" in r.getMessage()]
    assert len(mismatches) == 1


def test_analyze_tenant_spend_business_name_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from types import SimpleNamespace

    import orchestrator.observability.cost_dashboard as cd
    from orchestrator.agent.cost_opt_lane import analyze_tenant_spend

    seen: dict[str, Any] = {}

    def _fake_get_tenant_cost(tid: Any, since: Any, until: Any) -> Any:
        seen["tid"] = tid
        return SimpleNamespace(total_paise=0, by_category={}, event_count=0)

    monkeypatch.setattr(cd, "get_tenant_cost", _fake_get_tenant_cost)

    run_id, tenant_id = uuid4(), uuid4()
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        with observability_context(run_id=run_id, tenant_id=tenant_id):
            analyze_tenant_spend.func("Sundaram Stores", 30)  # type: ignore[attr-defined]

    assert seen["tid"] == tenant_id
    mismatches = [r for r in caplog.records if "analyze_tenant_spend" in r.getMessage()]
    assert len(mismatches) == 1


# --- sales_lane: no tenant_id-taking tool exists — pinned, not silently skipped -------------------


def test_sales_lane_has_no_tenant_id_taking_tool() -> None:
    """``sales_lane`` reasons over a manager-supplied ledger slice; it holds no tool that takes a
    ``tenant_id`` param at all, so there is nothing for VT-599 to wire here. Pinned explicitly so a
    FUTURE tenant_id-taking tool added to this lane trips this test into review (it must then use
    ``resolve_lane_tenant`` like every other lane)."""
    import inspect

    from orchestrator.agent.sales_lane import SALES_LANE_TOOLS

    for t in SALES_LANE_TOOLS:
        params = inspect.signature(t.func).parameters
        assert "tenant_id" not in params, f"{t.name} now takes tenant_id — wire resolve_lane_tenant"
