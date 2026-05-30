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

Pillar 1: deterministic, no LLM. RLS via SET LOCAL app.current_tenant.
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


def resolve_cohort_recipients(
    *,
    tenant_id: str,
    campaign_id: str,
    customer_ids: list[str],
    pool: Any,
) -> CohortResolution:
    """Link `customer_ids` to `campaign_id` in campaign_recipients.

    Validates each id exists in `customers` same-tenant; inserts the
    resolved set; returns {resolved, rejected}. Idempotent per
    (campaign_id, customer_id) via ON CONFLICT DO NOTHING. Cross-tenant /
    unknown ids are surfaced in `rejected`, never linked.
    """
    # Dedupe input, preserve a deterministic order for reproducible output.
    unique_ids = sorted(set(customer_ids))
    if not unique_ids:
        return CohortResolution(campaign_id=campaign_id, resolved=[], rejected=[])

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.current_tenant = %s", (tenant_id,))

            # Which ids are real, same-tenant customers? (RLS already
            # scopes to tenant; the explicit tenant_id filter is belt +
            # braces.)
            cur.execute(
                """
                SELECT id::text
                FROM customers
                WHERE tenant_id = %s AND id = ANY(%s::uuid[])
                """,
                (tenant_id, unique_ids),
            )
            real = {
                (r["id"] if isinstance(r, dict) else r[0])
                for r in cur.fetchall()
            }

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


__all__ = ["CohortResolution", "resolve_cohort_recipients"]
