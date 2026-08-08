"""VT-735 — the per-tenant daily Fast-call budget.

The ratified tier policy (`.viabe/model-tier-policy.md`) says Fast "carries an internal per-tenant
daily Fast-budget; exceeding it degrades to Standard (never to Flex — a decisive moment never gets
the slow tier) and flags the tenant on the VTR console, because a tenant burning Fast budget is a
tenant with a runaway loop, not a billing event."

`resolve_service_tier` already had the hook and the degrade. This is the store behind it.

Two design commitments worth stating, because both are load-bearing:

**The count is DERIVED, never accumulated.** `llm_call_events` already records
(tenant_id, service_tier, occurred_at) for every call, so the budget reads the same rows the VT-733
cost console reads. A dedicated counter would be a second source of truth that could disagree with
the console an operator opens *because* this flagged their tenant.

**It is cached, because this runs on the safety path.** Fast exists for decisive moments — approval
resolution and opt-out/STOP — where latency is a correctness property (the VT-734 duplicate-request
race). Paying a database round-trip on every such call to enforce a cost control would spend exactly
what the tier was chosen to save. The cache is short and per-tenant, so the worst case is a small
overshoot past the cap; a cap is a runaway-loop tripwire, not a billing gate, and a tripwire that is
a few calls late still trips.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

#: Default daily Fast allowance when a tenant has no explicit `max_fast_calls_day`.
#: The policy's own volume note is the basis: Fast is ~5% of a plausible token mix and approvals are
#: rare, so a tenant legitimately spends single-digit Fast calls a day. 50 is an order of magnitude
#: above normal use — high enough that it never fires on a healthy tenant, low enough that a runaway
#: loop trips it inside minutes rather than after a day of spend.
DEFAULT_MAX_FAST_CALLS_DAY = 50

#: How long a tenant's count is reused before re-reading. See the caching rationale above.
_CACHE_TTL_SECONDS = 30.0

_lock = threading.Lock()
#: tenant key -> (expires_at_monotonic, allowed, used, limit)
_cache: dict[str, tuple[float, bool, int, int]] = {}


def _env_default() -> int:
    raw = os.environ.get("TEAM_FAST_CALLS_PER_DAY", "").strip()
    if not raw:
        return DEFAULT_MAX_FAST_CALLS_DAY
    try:
        value = int(raw)
    except ValueError:
        logger.warning("TEAM_FAST_CALLS_PER_DAY=%r is not an integer; using default", raw)
        return DEFAULT_MAX_FAST_CALLS_DAY
    return value if value >= 0 else DEFAULT_MAX_FAST_CALLS_DAY


def reset_cache() -> None:
    """Drop every cached decision. For tests, and for an operator raising a cap mid-incident."""

    with _lock:
        _cache.clear()


def _read(tenant_id: UUID | str) -> tuple[int, int]:
    """(fast calls used today, limit) straight from the ledger. Raises — the caller fails open."""

    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT max_fast_calls_day FROM tenant_llm_limits WHERE tenant_id = %s",
            (str(tenant_id),),
        ).fetchone()
        # `date_trunc('day', now())` is UTC here, matching `occurred_at`'s storage. "Day" is
        # deliberately a server-side UTC day rather than a tenant-local one: the cap is an
        # anomaly tripwire, and a tripwire does not need to respect a billing calendar.
        used_row = conn.execute(
            "SELECT count(*) AS n FROM llm_call_events "
            " WHERE tenant_id = %s AND service_tier = 'fast' "
            "   AND occurred_at >= date_trunc('day', now())",
            (str(tenant_id),),
        ).fetchone()

    limit = _column(row, "max_fast_calls_day", 0)
    if limit is None:
        limit = _env_default()
    used = _column(used_row, "n", 0) or 0
    return int(used), int(limit)


def _column(row: Any, name: str, index: int) -> Any:
    """Read one column whether the connection yields dict rows or tuples."""

    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(name)
    return row[index]


def fast_budget_check(tenant_id: UUID | str | None) -> bool:
    """True when this tenant may still spend Fast today.

    Raising is a valid outcome: `resolve_service_tier._fast_allowed` catches it and fails OPEN,
    which is the deliberate choice (a cost control must never become a correctness risk on the
    approval path). This function therefore does NOT swallow database errors itself — doing so here
    would hide a broken budget behind a permanent 'yes' with no warning logged at the seam.
    """

    if tenant_id is None:
        # A platform/tenantless call has no tenant budget to spend. Fast stays allow-listed by
        # call site, which is the control that actually matters for those.
        return True

    key = str(tenant_id)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]

    used, limit = _read(tenant_id)
    allowed = used < limit
    with _lock:
        _cache[key] = (now + _CACHE_TTL_SECONDS, allowed, used, limit)

    if not allowed:
        _flag_on_vtr(tenant_id, used=used, limit=limit)
    return allowed


def _flag_on_vtr(tenant_id: UUID | str, *, used: int, limit: int) -> None:
    """Raise the VTR trigger the policy asks for. Never breaks the call it is reporting on."""

    try:
        from orchestrator.alerts.dispatch import dispatch_alert
        from orchestrator.alerts.triggers import Trigger, severity_for

        kind = "fast_budget_exhausted"
        dispatch_alert(
            Trigger(
                tenant_id=tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id)),
                trigger_kind=kind,
                severity=severity_for(kind),
                message_text=(
                    f"Fast-tier budget exhausted: {used} Fast calls today against a cap of "
                    f"{limit}. Fast now degrades to Standard for this tenant. Fast is "
                    f"allow-listed to approval resolution and opt-out/STOP, so this volume "
                    f"points at a runaway loop rather than real owner activity — check the "
                    f"tenant's recent turns before raising the cap."
                ),
                payload={"used": used, "limit": limit},
            )
        )
    except Exception:  # noqa: BLE001 — a flag failing must not break the degrade it describes
        logger.warning(
            "VT-735 fast-budget VTR flag failed (tenant=%s used=%s limit=%s)",
            tenant_id, used, limit, exc_info=True,
        )


__all__ = ["DEFAULT_MAX_FAST_CALLS_DAY", "fast_budget_check", "reset_cache"]
