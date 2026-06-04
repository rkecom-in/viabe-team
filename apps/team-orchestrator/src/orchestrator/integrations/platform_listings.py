"""VT-325 — platform_listings per-listing writer.

The SOURCE side: persist one listing row + emit `platform_listing_updated` to the
VT-65 outbox in the SAME transaction (atomic), then drain post-commit so the
existing `_h_platform_listing_updated` KG consumer projects it (PLATFORM_LISTING
node + HAS_LISTING edge). Distinct from VT-6 `business_profile` (the per-tenant
AGGREGATE) — this is per (tenant, platform, external_listing_id).

CL-390: callers pass ONLY structured, non-PII attributes (name/category/cuisines/
hours/items) — never raw review text. The outbox payload is minimal
{listing_id, platform, rating}; VT-308 reads the row via the wrapper for the rest.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


def write_platform_listing(
    tenant_id: UUID | str,
    platform: str,
    external_listing_id: str,
    *,
    rating: float | None = None,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Upsert a listing + emit its KG event atomically, then drain (best-effort).

    Returns the persisted row. The wrapper owns the tenant_connection (SET ROLE
    app_role + GUC); the upsert + emit commit together (or roll back together).
    """
    from orchestrator.db import tenant_connection
    from orchestrator.db.wrappers import PlatformListingsWrapper
    from orchestrator.knowledge.kg_emit import drain_kg_events, emit_kg_event
    from orchestrator.knowledge.kg_vocab import KgEventType

    with tenant_connection(tenant_id) as conn:
        with conn.transaction():
            row = PlatformListingsWrapper().upsert(
                tenant_id,
                platform,
                external_listing_id,
                rating=rating,
                attributes=attributes,
                conn=conn,
            )
            # CL-390: minimal, PII-free payload (the row carries the structured rest).
            emit_kg_event(
                conn,
                KgEventType.PLATFORM_LISTING_UPDATED,
                tenant_id,
                {
                    "listing_id": str(row["id"]),
                    "platform": platform,
                    "rating": float(rating) if rating is not None else None,
                },
            )
    drain_kg_events(tenant_id)
    logger.info(
        "platform_listing written tenant=%s platform=%s ext=%s rating=%s",
        tenant_id, platform, external_listing_id, rating,
    )
    return row


__all__ = ["write_platform_listing"]
