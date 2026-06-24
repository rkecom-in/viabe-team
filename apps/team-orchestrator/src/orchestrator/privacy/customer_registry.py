"""VT-170 — customer-name registry service for the PII redactor.

Backs the VT-104 redactor `name_registry` callable (shipped inert as
None). `get_customer_names_for_tenant` returns the tenant's customer
display names; `make_name_registry` wraps that as the
`Callable[[str], bool]` the redactor expects (exact-match, case-folded).

In-process per-tenant cache with explicit invalidation on customer
UPSERT. NOT an LRU eviction cache — Phase-1 single-process, bounded by
tenant count; invalidate(tenant_id) is called by the customer write path.

NO PII leak (CL-390): names are held in-process only for redaction
matching; never logged. RLS via set_config('app.current_tenant', ...) on
the read.
"""

from __future__ import annotations

import logging
from typing import Callable

from orchestrator.db.wrappers import CustomersWrapper
from orchestrator.privacy.redaction_health import (
    RegistryOutcome,
    record_registry_outcome,
)

logger = logging.getLogger(__name__)

# tenant_id -> frozenset of case-folded display names.
_CACHE: dict[str, frozenset[str]] = {}


def _record_outcome(tenant_id: str, outcome: RegistryOutcome) -> None:
    """Bump the §A health counter; never let health-counting break the build."""
    try:
        record_registry_outcome(str(tenant_id), outcome)
    except Exception:  # noqa: BLE001 — health telemetry is best-effort
        logger.debug("redaction_health outcome record failed", exc_info=True)


def invalidate(tenant_id: str) -> None:
    """Drop the cached name set for a tenant. Call on customer UPSERT."""
    _CACHE.pop(tenant_id, None)


def invalidate_all() -> None:
    """Drop the entire cache (tests / process-wide reset)."""
    _CACHE.clear()


def get_customer_names_for_tenant(
    tenant_id: str,
    *,
    use_cache: bool = True,
) -> frozenset[str]:
    """Return the tenant's customer display names, case-folded.

    Cached per tenant; `invalidate(tenant_id)` clears it. Absent
    customers table (forward-compat) → empty set, never raises.
    """
    if use_cache and tenant_id in _CACHE:
        return _CACHE[tenant_id]

    # VT-306: read through the typed tenant wrapper (RLS + GUC + result validation
    # intrinsic) — it owns its tenant_connection. (VT-324: vestigial ``pool`` param
    # dropped through make_name_registry + the redactor seam.)
    names: set[str] = set()
    try:
        names = set(CustomersWrapper().list_display_names(tenant_id))
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ != "UndefinedTable":
            # VT-386 Part A: a real outage (not the forward-compat absent-table
            # case) — count it as build_error before re-raising so the caller's
            # fail-soft path (and the §B alert) sees the outage.
            _record_outcome(tenant_id, RegistryOutcome.BUILD_ERROR)
            raise
        # Forward-compat: customers table absent → empty set, no leak yet.
        _record_outcome(tenant_id, RegistryOutcome.UNDEFINED_TABLE)
        logger.info(
            "customer_registry: customers table absent (tenant=%s); empty",
            tenant_id,
        )
    else:
        _record_outcome(tenant_id, RegistryOutcome.OK)

    frozen = frozenset(names)
    if use_cache:
        _CACHE[tenant_id] = frozen
    logger.info(
        "customer_registry: tenant=%s names=%d", tenant_id, len(frozen)
    )
    return frozen


def make_name_registry(
    tenant_id: str,
) -> Callable[[str], bool]:
    """Build the redactor `name_registry` callable for a tenant.

    Returns a predicate: True iff `text` exact-matches (case-folded) a
    known customer display name. Pass the result as `name_registry=` to
    `redact_for_log` / `redact_for_otel_span`. None-safe by construction —
    callers without tenant context simply don't build one. (VT-324: vestigial
    ``pool`` param dropped — the wrapper owns its tenant_connection.)
    """
    names = get_customer_names_for_tenant(tenant_id)

    def _predicate(text: str) -> bool:
        return text.casefold() in names

    return _predicate


__all__ = [
    "get_customer_names_for_tenant",
    "make_name_registry",
    "invalidate",
    "invalidate_all",
]
