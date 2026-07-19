"""VT-558 (B6) — VTR takeover: an operator SEIZES a tenant's automation.

Takeover is deliberately built by COMPOSING the two enforcement primitives that already exist +
are already honored by the coordinator sweep — not a new gate:

  * pause the tenant's ``agent_dispatch`` workflow_kind (workflow_controls, mig-131) → the
    coordinator's per-tenant sweep already skips a paused workflow_kind, so no new dispatch runs.
  * freeze EVERY registered agent's autonomy (``autonomy.vtr_autonomy_override('freeze')``, which
    ATOMICALLY cancels in-flight batches, incl awaiting-approval) → no autonomous send can fire, and
    live work is halted.

While taken over, the manual VTR surfaces (plan-edit, VT-556 directive, confirm-field, ownership)
stay authoritative — the human drives. ``release_takeover`` reverses both legs. Both run on the
caller's conn so takeover is atomic with the ops_audit row the endpoint writes in the same txn.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from orchestrator.agents.autonomy import vtr_autonomy_override
from orchestrator.business_plan.store import OWNING_AGENTS

logger = logging.getLogger(__name__)

_TAKEOVER_WORKFLOW_KIND = "agent_dispatch"


def _registered_agents() -> list[str]:
    """The specialist agents a takeover freezes — the coordinator registry set (OWNING_AGENTS minus
    the sentinel). Sorted for a deterministic freeze order."""
    return sorted(OWNING_AGENTS - {"unassigned"})


def take_over_tenant(
    tenant_id: UUID | str, *, operator_id: str, reason: str, conn: Any
) -> dict[str, Any]:
    """Seize the tenant: pause agent_dispatch + freeze every registered agent (cancelling in-flight
    work). Idempotent — a re-takeover is a no-op on the pause (ON CONFLICT) and re-freezes cleanly.
    Returns {paused, frozen_agents}. ``conn`` MUST be a Connection (the autonomy-freeze tm_audit
    opens its own cursor) — runs in the caller's txn, atomic with the endpoint's ops_audit row."""
    conn.execute(
        "INSERT INTO workflow_controls (tenant_id, workflow_kind, set_by, reason) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (tenant_id, workflow_kind) WHERE released_at IS NULL DO NOTHING",
        (str(tenant_id), _TAKEOVER_WORKFLOW_KIND, operator_id, reason),
    )
    frozen: list[str] = []
    for agent in _registered_agents():
        vtr_autonomy_override(
            tenant_id, agent, "freeze", reason=reason, vtr_id=operator_id, conn=conn
        )
        frozen.append(agent)
    logger.info(
        "VT-558 takeover tenant=%s operator=%s frozen=%d", tenant_id, operator_id, len(frozen)
    )
    return {"paused": True, "frozen_agents": frozen}


def release_takeover(
    tenant_id: UUID | str, *, operator_id: str, reason: str, conn: Any
) -> dict[str, Any]:
    """Reverse a takeover: release the agent_dispatch hold + unfreeze every registered agent (work
    re-enters via the next sweep). Idempotent. Returns {released, unfrozen_agents}."""
    conn.execute(
        "UPDATE workflow_controls SET released_at = now() "
        "WHERE tenant_id = %s AND workflow_kind = %s AND released_at IS NULL",
        (str(tenant_id), _TAKEOVER_WORKFLOW_KIND),
    )
    unfrozen: list[str] = []
    for agent in _registered_agents():
        vtr_autonomy_override(
            tenant_id, agent, "unfreeze", reason=reason, vtr_id=operator_id, conn=conn
        )
        unfrozen.append(agent)
    logger.info(
        "VT-558 release-takeover tenant=%s operator=%s unfrozen=%d",
        tenant_id, operator_id, len(unfrozen),
    )
    return {"released": True, "unfrozen_agents": unfrozen}


__all__ = ["take_over_tenant", "release_takeover"]
