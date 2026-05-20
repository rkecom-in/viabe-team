"""LangGraph node wrapper for the sales_recovery specialist (VT-32).

Imports and calls ``run_sales_recovery_agent``; translates the returned
``AgentResult`` into a LangGraph state update.

This module is EXPORTED but NOT yet wired into ``supervisor.py`` (the
dispatch graph). PR 2/3 of VT-3.4 left the supervisor routing through
the stub specialist; switching the dispatch call site from the stub to
this real node is a later subtask. For now, this module exists so VT-35
can plan against a real entry point.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from orchestrator._tenant_guard import TenantIsolationError
from orchestrator.agent.sales_recovery import (
    SalesRecoveryContext,
    run_sales_recovery_agent,
)


def sales_recovery_node(state: Mapping[str, Any]) -> dict[str, Any]:
    """Run the specialist; return a state update carrying the AgentResult.

    Fail-loud (Pillar 3 / CL-202): ``tenant_id`` and ``run_id`` MUST be
    present in ``state``. A missing value means an upstream producer
    skipped the tenant boundary — raise rather than silently inventing
    one. ``trigger_reason``-style fallbacks are NOT appropriate here:
    the agent loop spends API budget; an unscoped run cannot be
    attributed cleanly.

    The returned dict places the result under ``agent_result`` as a
    plain mapping (LangGraph state values must be reducer-friendly;
    dataclasses survive but mappings are more portable across graph
    schemas).
    """
    tenant_id = state.get("tenant_id")
    if tenant_id is None:
        raise TenantIsolationError(
            "sales_recovery_node: tenant_id missing from state"
        )
    run_id = state.get("run_id")
    if run_id is None:
        raise TenantIsolationError(
            "sales_recovery_node: run_id missing from state"
        )

    context = SalesRecoveryContext(
        tenant_id=str(tenant_id), run_id=str(run_id)
    )
    result = run_sales_recovery_agent(context)
    return {"agent_result": asdict(result)}


__all__ = ["sales_recovery_node"]
