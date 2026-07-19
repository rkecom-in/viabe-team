"""mig 173 — per-tenant AND platform-wide LLM $-budget gate.

The runtime enforcement read for the Fazal directive "cap model usage on tenant AND overall;
only VTR admin can set/control limits" (schema: ``migrations/173_llm_cost_audit_and_caps.sql``).
:func:`check_llm_budget` is called BEFORE an LLM call fires and answers one question — is this
tenant (or the platform) inside budget?

    ``ok``   — under both the tenant cap and the platform cap (or no caps set).
    ``soft`` — crossed a ``soft_pct`` threshold on some cap (warn, keep serving).
    ``hard`` — at/over some cap (the caller degrades to the deterministic nets; the money gates
               are NEVER bent — that is the caller's contract, not this gate's).

Two independent legs, each fail-OPEN on its own read error (availability over enforcement,
mirroring VT-619 ``usage_meter.budget_status``) — but a SUCCESSFUL read that shows over-cap is
authoritative, so one leg erroring never MASKS the other leg's real breach:

  * TENANT leg — ``tenant_llm_limits`` (RLS SELECT) + this month's ``llm_call_events`` aggregate
    for the tenant, read UNDER ``tenant_connection`` (app_role, ``tenant_id = app_current_tenant()``
    — the runtime enforces but can never self-edit its caps; that is the migration's whole point).
  * GLOBAL leg — ``global_llm_limits`` singleton + the PLATFORM-WIDE ``llm_call_events`` cost sums
    (day + month, ALL tenants incl. the NULL-tenant platform rows), read via the service-role pool
    (``get_pool()``, BYPASSRLS). The cross-tenant sum is impossible under app_role's per-tenant RLS,
    so the global leg mirrors the ops-console service path (the CL-431 service-role read idiom).

Import hygiene (VT-619 convention): every orchestrator import is LAZY (inside a function) so this
module imports with no langgraph/dbos dependency — the dep-less smoke passes and enforcement wiring
elsewhere pays the import cost only when a gate actually fires.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

logger = logging.getLogger(__name__)

BudgetState = Literal["ok", "soft", "hard"]

# Small TTL cache so the gate adds no meaningful latency on the hot pre-call path: at most one
# tenant+global read per tenant per ~60s per process. A cost cap taking effect within a minute is
# acceptable; the VTR write endpoints proactively invalidate the entry so a just-set cap is instant.
_CACHE_TTL_S = 60.0
_CACHE: dict[str, tuple[float, BudgetState]] = {}  # tenant_key -> (expiry_monotonic, state)

_RANK = {"ok": 0, "soft": 1, "hard": 2}


def _worst(*states: str) -> BudgetState:
    """The most severe of several leg/dimension verdicts (ok < soft < hard)."""
    return max(states, key=lambda s: _RANK.get(s, 0))  # type: ignore[return-value]


def severity(usage: float, cap: float | None, soft_pct: float) -> BudgetState:
    """One cap dimension's verdict.

    ``cap is None`` → no cap (the schema's NULL sentinel) → ``ok``. A stored ``0`` cap is a
    deliberate freeze (no budget) → ``hard`` at any usage. Otherwise: at/over the cap → ``hard``;
    at/over ``soft_pct``% of the cap → ``soft``; else ``ok``.
    """
    if cap is None:
        return "ok"
    cap_f = float(cap)
    if cap_f <= 0:
        return "hard"
    if usage >= cap_f:
        return "hard"
    if usage >= cap_f * (soft_pct / 100.0):
        return "soft"
    return "ok"


def check_llm_budget(tenant_id: Any, agent: str | None) -> BudgetState:
    """Budget verdict for a would-be LLM call served by ``agent`` on behalf of ``tenant_id``.

    Returns ``'ok' | 'soft' | 'hard'`` = the worst of the tenant leg and the platform leg. FAILS
    OPEN (``'ok'``) on any read error within a leg; a successful over-cap read is authoritative.
    On a soft/hard verdict (computed on a cache MISS) emits a once-per-period notification.
    """
    key = str(tenant_id)
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]

    state = _worst(_tenant_leg(tenant_id), _global_leg())
    _CACHE[key] = (now + _CACHE_TTL_S, state)
    if state != "ok":
        _maybe_notify(tenant_id, agent, state)
    return state


def _tenant_leg(tenant_id: Any) -> BudgetState:
    """Per-tenant cost/token verdict from ``tenant_llm_limits`` + this month's own ``llm_call_events``
    aggregate, under RLS (``tenant_connection``). No limits row or ``enabled=false`` → ``ok``."""
    try:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            lim = conn.execute(
                "SELECT max_cost_usd_month, max_tokens_in_month, max_tokens_out_month, "
                "       soft_pct, enabled "
                "FROM tenant_llm_limits WHERE tenant_id = %s",
                (str(tenant_id),),
            ).fetchone()
            if not lim or not lim["enabled"]:
                return "ok"
            agg = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0)   AS cost, "
                "       COALESCE(SUM(tokens_in), 0)  AS tin, "
                "       COALESCE(SUM(tokens_out), 0) AS tout "
                "FROM llm_call_events "
                "WHERE tenant_id = %s AND occurred_at >= date_trunc('month', now())",
                (str(tenant_id),),
            ).fetchone()
        soft_pct = float(lim["soft_pct"] or 80)
        return _worst(
            severity(float(agg["cost"]), lim["max_cost_usd_month"], soft_pct),
            severity(float(agg["tin"]), lim["max_tokens_in_month"], soft_pct),
            severity(float(agg["tout"]), lim["max_tokens_out_month"], soft_pct),
        )
    except Exception:  # noqa: BLE001 — fail OPEN (never block on a metering read blip)
        logger.warning("mig173 tenant budget leg read failed; failing open", exc_info=True)
        return "ok"


def _global_leg() -> BudgetState:
    """Platform-wide cost verdict from ``global_llm_limits`` + the cross-tenant ``llm_call_events``
    cost sums (day + month, ALL tenants incl. NULL-tenant), via the service-role pool (BYPASSRLS —
    a per-tenant app_role connection cannot see the platform total). Singleton missing or
    ``enabled=false`` → ``ok``."""
    try:
        from orchestrator.graph import get_pool

        with get_pool().connection() as conn:
            lim = conn.execute(
                "SELECT max_cost_usd_day, max_cost_usd_month, soft_pct, enabled "
                "FROM global_llm_limits WHERE id = true"
            ).fetchone()
            if not lim or not lim["enabled"]:
                return "ok"
            agg = conn.execute(
                "SELECT COALESCE(SUM(cost_usd) FILTER "
                "         (WHERE occurred_at >= date_trunc('day', now())), 0) AS day_cost, "
                "       COALESCE(SUM(cost_usd), 0)                            AS month_cost "
                "FROM llm_call_events "
                "WHERE occurred_at >= date_trunc('month', now())"
            ).fetchone()
        soft_pct = float(lim["soft_pct"] or 80)
        return _worst(
            severity(float(agg["day_cost"]), lim["max_cost_usd_day"], soft_pct),
            severity(float(agg["month_cost"]), lim["max_cost_usd_month"], soft_pct),
        )
    except Exception:  # noqa: BLE001 — fail OPEN
        logger.warning("mig173 global budget leg read failed; failing open", exc_info=True)
        return "ok"


def _maybe_notify(tenant_id: Any, agent: str | None, state: BudgetState) -> None:
    """Emit ONE ``llm_budget_soft`` / ``llm_budget_hard`` tm_audit breadcrumb per tenant per
    calendar month per kind (the tenant cap is monthly → month is the period).

    Dedup is a ``tm_audit_log`` query, NOT the ``tenant_agent_usage.*_notified_at`` stamps:
    those stamps are owned by VT-619's TOKEN/api-call cap notifications, and reusing them would
    cross-contaminate two independent limit systems (a VT-619 token-cap stamp would silently
    suppress this $-cost notification, and vice versa). The tm_audit dedup is per-tenant (matches
    "once per period per tenant") and collision-free.

    The dedup SELECT runs on the service pool because ``tm_audit_log`` grants app_role INSERT only
    (no app_role SELECT policy). Best-effort throughout — a notify blip never breaks the gate; the
    SELECT→emit gap can rarely double-emit under concurrency, acceptable for an audit breadcrumb.
    """
    try:
        from orchestrator.graph import get_pool

        event_kind = f"llm_budget_{state}"
        with get_pool().connection() as conn:
            existing = conn.execute(
                "SELECT 1 FROM tm_audit_log "
                "WHERE tenant_id = %s AND event_kind = %s "
                "  AND created_at >= date_trunc('month', now()) LIMIT 1",
                (str(tenant_id), event_kind),
            ).fetchone()
        if existing is not None:
            return

        from orchestrator.observability.tm_audit import emit_tm_audit

        emit_tm_audit(
            event_layer="does",
            event_kind=event_kind,
            actor="platform",
            tenant_id=str(tenant_id),
            summary=f"LLM budget {state} threshold reached",
            decision={"state": state, "agent": str(agent) if agent else None},
            severity="critical" if state == "hard" else "warning",
            status="blocked" if state == "hard" else "ok",
        )
    except Exception:  # noqa: BLE001 — notification is best-effort; never break the gate
        logger.warning("mig173 llm budget notify swallowed (best-effort)", exc_info=True)


def reset_budget_cache(tenant_id: Any = None) -> None:
    """Drop the TTL cache for one tenant (or all) so a freshly-set cap takes effect immediately.
    Called by the VTR write endpoints after a limits change; also the test seam."""
    if tenant_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(str(tenant_id), None)


__all__ = ["BudgetState", "check_llm_budget", "reset_budget_cache", "severity"]
