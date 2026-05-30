"""VT-170 — cohort integrity primitive.

`resolve_cohort_recipients` links a campaign's cohort (the
`customer_ids` from campaigns.plan_json.target_cohort, mig 018) to real
`customers` rows via the normalized `campaign_recipients` table.

The same-tenant composite FKs on `campaign_recipients` make cross-tenant
linkage physically impossible at the DB layer — but this function ALSO
pre-validates against `customers` so it can SURFACE rejected ids
(non-existent / cross-tenant) instead of letting the FK raise opaquely.
It NEVER silently drops an unresolved id (Fazal requirement) — every
input id lands in either `resolved` or `rejected`.

Standalone primitive (approach (b), Cowork review 2026-05-30): VT-170
ships this callable; the collapse-path call-site + the reject-vs-proceed
product ruling are VT-241. Until VT-241 wires it, campaign_recipients
stays empty — VT-43's cohort_size lift (VT-240) must wait for VT-241,
not point at an empty COUNT.

Pillar 1: deterministic, no LLM. RLS via set_config('app.current_tenant', ...).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class CohortResolution(BaseModel):
    """Outcome of linking a cohort. Every input id is in exactly one list."""

    model_config = ConfigDict(frozen=True)

    campaign_id: str
    resolved: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)


class CohortRejectedError(Exception):
    """VT-241 — raised when a cohort has unresolvable/cross-tenant ids and
    the caller wants FAIL-CLOSED (reject the campaign, roll back). Carries
    the structured outcome so the caller can surface it (owner message +
    audit log)."""

    def __init__(self, resolution: CohortResolution) -> None:
        self.resolution = resolution
        super().__init__(
            f"cohort rejected: {len(resolution.rejected)} invalid id(s) "
            f"for campaign {resolution.campaign_id}"
        )


def _resolve_core(
    cur: Any, tenant_id: str, campaign_id: str, unique_ids: list[str]
) -> CohortResolution:
    """Core resolution against an already-tenant-scoped cursor. Caller
    guarantees `app.current_tenant` is set (pool path sets it; the
    cur-injected collapse path inherits it from tenant_connection)."""
    cur.execute(
        """
        SELECT id::text
        FROM customers
        WHERE tenant_id = %s AND id = ANY(%s::uuid[])
        """,
        (tenant_id, unique_ids),
    )
    real = {(r["id"] if isinstance(r, dict) else r[0]) for r in cur.fetchall()}
    resolved = [cid for cid in unique_ids if cid in real]
    rejected = [cid for cid in unique_ids if cid not in real]
    for cid in resolved:
        cur.execute(
            """
            INSERT INTO campaign_recipients
                (campaign_id, customer_id, tenant_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (campaign_id, customer_id) DO NOTHING
            """,
            (campaign_id, cid, tenant_id),
        )
    logger.info(
        "resolve_cohort_recipients: tenant=%s campaign=%s resolved=%d rejected=%d",
        tenant_id, campaign_id, len(resolved), len(rejected),
    )
    return CohortResolution(
        campaign_id=campaign_id, resolved=resolved, rejected=rejected
    )


def resolve_cohort_recipients(
    *,
    tenant_id: str,
    campaign_id: str,
    customer_ids: list[str],
    pool: Any | None = None,
    cur: Any | None = None,
) -> CohortResolution:
    """Link `customer_ids` to `campaign_id` in campaign_recipients.

    Validates each id exists in `customers` same-tenant; inserts the
    resolved set; returns {resolved, rejected}. Idempotent per
    (campaign_id, customer_id) via ON CONFLICT DO NOTHING. Cross-tenant /
    unknown ids are surfaced in `rejected`, never linked.

    VT-241: pass `cur` (an open cursor inside the collapse path's
    `tenant_connection` transaction) to resolve in the SAME transaction —
    so a fail-closed rollback removes both the campaign and any linked
    recipients atomically. Otherwise pass `pool` (standalone path).
    """
    unique_ids = sorted({str(c) for c in customer_ids})
    if not unique_ids:
        return CohortResolution(campaign_id=campaign_id, resolved=[], rejected=[])

    if cur is not None:
        # Same-transaction path: GUC already set by tenant_connection.
        return _resolve_core(cur, tenant_id, campaign_id, unique_ids)

    if pool is None:
        raise ValueError("resolve_cohort_recipients: pass either pool or cur")
    with pool.connection() as conn, conn.cursor() as own_cur:
        own_cur.execute(
            "SELECT set_config('app.current_tenant', %s, false)", (tenant_id,)
        )
        return _resolve_core(own_cur, tenant_id, campaign_id, unique_ids)


__all__ = [
    "CohortResolution",
    "CohortRejectedError",
    "resolve_cohort_recipients",
]
