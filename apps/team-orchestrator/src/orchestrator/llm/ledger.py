"""Migration-173 per-call cost ledger: ``record_llm_call``.

The WRITE side of the cost audit (Fazal: "each LLM call must be recorded"). One
call records ONE ``llm_call_events`` row — tenant, agent, call_site, provider,
model, service_tier, tokens in/out, and the COMPUTED ``cost_usd`` — and also bumps
the VT-619 ``tenant_agent_usage`` monthly rollup the caps read.

Two write paths, mirroring the isolation contract in ``orchestrator.db``:
  * tenant call (``tenant_id`` is not None) → ``tenant_connection`` (RLS + GUC); the
    row's tenant_id equals ``app_current_tenant()`` so the FORCE-RLS policy admits
    it. The VT-619 rollup runs on the SAME connection (one txn).
  * platform call (``tenant_id`` is None — blind judges / plan validators run
    tenantless) → the privileged pool connection directly (BYPASSRLS service-role
    path, the same one ``db/__init__.py`` reserves for cross-tenant / NULL-tenant
    writes). app_role could never write a NULL-tenant row (the RLS policy is
    ``tenant_id = app_current_tenant()``), so audit completeness requires the
    service role here. No per-tenant rollup for a tenantless call.

FAIL-SOFT end to end (CL-122): metering must NEVER break a turn. The whole body is
swallowed to a logged warning. Synchronous + cheap — one INSERT (+ one UPSERT for a
tenant call); no LLM, no retries.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from orchestrator.llm.pricing import compute_cost_usd

logger = logging.getLogger(__name__)

_INSERT_SQL = (
    "INSERT INTO llm_call_events "
    "  (tenant_id, agent, call_site, provider, model, service_tier, "
    "   tokens_in, tokens_out, cost_usd, request_id) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


def record_llm_call(
    *,
    tenant_id: Any,
    agent: str,
    call_site: str,
    provider: str,
    model: str,
    service_tier: str = "standard",
    tokens_in: int,
    tokens_out: int,
    cached_tokens_in: int = 0,
    request_id: str | None = None,
) -> None:
    """Record one LLM call to ``llm_call_events`` + bump the VT-619 rollup. Fail-soft.

    ``tokens_in`` is the full-price (uncached) input count; ``cached_tokens_in`` the
    cache-read count (priced at ``cached_in_multiplier``). ``cost_usd`` reflects the
    split, while the persisted ``tokens_in`` is the TOTAL input (uncached + cached)
    for audit — the ledger has no separate cached-token column (migration 173 folds
    cache economics into ``cost_usd``). Cache-unaware callers pass ``cached_tokens_in=0``
    → total == uncached, full-price cost (unchanged behavior).
    """
    try:
        cost = compute_cost_usd(model, service_tier, tokens_in, tokens_out, cached_tokens_in)
        total_in = int(tokens_in or 0) + int(cached_tokens_in or 0)
        params = _event_params(
            tenant_id=tenant_id,
            agent=agent,
            call_site=call_site,
            provider=provider,
            model=model,
            service_tier=service_tier,
            tokens_in=total_in,
            tokens_out=tokens_out,
            cost=cost,
            request_id=request_id,
        )
        if tenant_id is not None:
            _insert_tenant(
                tenant_id, params, agent=agent, tokens_in=total_in, tokens_out=tokens_out
            )
        else:
            _insert_platform(params)
    except Exception:  # noqa: BLE001 — CL-122: metering never breaks a turn
        logger.warning("173 record_llm_call swallowed (best-effort)", exc_info=True)


def _event_params(
    *,
    tenant_id: Any,
    agent: str,
    call_site: str,
    provider: str,
    model: str,
    service_tier: str,
    tokens_in: int,
    tokens_out: int,
    cost: Decimal,
    request_id: str | None,
) -> tuple[Any, ...]:
    return (
        str(tenant_id) if tenant_id is not None else None,
        agent,
        call_site,
        provider,
        model,
        service_tier or "standard",
        int(tokens_in or 0),
        int(tokens_out or 0),
        cost,
        request_id,
    )


def _insert_tenant(
    tenant_id: Any, params: tuple[Any, ...], *, agent: str, tokens_in: int, tokens_out: int
) -> None:
    """Insert the event under the tenant's RLS scope + bump the rollup in one txn.

    The VT-619 ``tenant_agent_usage`` rollup reuses ``meter_llm_call`` (single
    source of truth for the monthly counters + soft-notify) on the SAME
    RLS-scoped connection. Imports are lazy so this module stays dep-less at load.
    """
    from orchestrator.agent.usage_meter import meter_llm_call
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as conn:
        conn.execute(_INSERT_SQL, params)
        meter_llm_call(
            tenant_id=tenant_id,
            agent=agent,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            conn=conn,
        )


def _insert_platform(params: tuple[Any, ...]) -> None:
    """Insert a NULL-tenant (platform) event via the privileged pool (BYPASSRLS).

    No tenant GUC is set — the pool's privileged role writes the tenantless audit
    row the RLS policy would otherwise forbid. There is no per-tenant rollup for a
    tenantless call.
    """
    from orchestrator.graph import get_pool

    with get_pool().connection() as conn:
        conn.execute(_INSERT_SQL, params)


__all__ = ["record_llm_call"]
