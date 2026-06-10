"""VT-366 Gap-2a — the Auto-Discovery DRAFT business profile + the owner-confirm gate.

The Auto-Discovery Engine writes a DRAFT (public-source guesses, per-field provenance) here. NOTHING
in the draft is asserted to the canonical ``business_profile`` (l1_entities) or the KG until the owner
CONFIRMS it during onboarding — public data hallucinates / goes stale. ``confirm_draft`` is the single
promotion gate: only owner-confirmed fields become fact. This is the load-bearing accuracy + privacy
boundary (CL-390). The draft table is tenant-scoped + RLS'd (migration 122) AND swept by dsr_purge.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator.db import tenant_connection


def write_draft(
    tenant_id: UUID | str,
    fields: dict[str, Any],
    *,
    source: str,
    now: datetime | None = None,
) -> None:
    """MERGE drafted ``fields`` into the tenant's single business_profile_draft, stamping per-field
    provenance ``{field: {source, fetched_at}}``. Idempotent (one row per tenant); re-discovery /
    a second source merges in without clobbering other sources' fields. Writes NOTHING to the
    canonical profile or the KG — this is a DRAFT only."""
    if not fields:
        return
    now = now or datetime.now(UTC)
    stamp = now.isoformat()
    prov = {k: {"source": source, "fetched_at": stamp} for k in fields}
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            """
            INSERT INTO business_profile_draft (tenant_id, attributes, provenance)
            VALUES (%s, %s, %s)
            ON CONFLICT (tenant_id) DO UPDATE
              SET attributes = business_profile_draft.attributes || EXCLUDED.attributes,
                  provenance = business_profile_draft.provenance || EXCLUDED.provenance,
                  updated_at = now()
            """,
            (str(tenant_id), Jsonb(fields), Jsonb(prov)),
        )


def get_draft(tenant_id: UUID | str) -> dict[str, Any]:
    """The tenant's draft as ``{attributes, provenance}`` for the onboarding confirm UI; ``{}`` if
    no draft exists yet (engine hasn't run / found nothing)."""
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT attributes, provenance FROM business_profile_draft WHERE tenant_id = %s",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return {}
    attrs = row["attributes"] if isinstance(row, dict) else row[0]
    prov = row["provenance"] if isinstance(row, dict) else row[1]
    return {"attributes": dict(attrs or {}), "provenance": dict(prov or {})}


def confirm_draft(
    tenant_id: UUID | str,
    confirmed_fields: dict[str, Any],
    *,
    emit_kg: bool = True,
) -> None:
    """THE promotion gate. Promote ONLY the owner-confirmed (possibly owner-EDITED) fields → the
    canonical ``business_profile`` (the fact store the agent reads), and emit a best-effort KG fact.
    Unconfirmed / un-edited draft fields are NEVER asserted. ``confirmed_fields`` is the owner's
    final dict (a subset of the draft, with any corrections applied)."""
    if not confirmed_fields:
        return
    from orchestrator.knowledge.l1 import upsert_business_profile

    upsert_business_profile(tenant_id, confirmed_fields)

    if not emit_kg:
        return
    # Best-effort KG fact (service-role outbox + drain), mirroring the signup emit pattern. A KG
    # failure must never undo a confirmed promotion (the L1 write above is the authoritative fact).
    try:
        from orchestrator.graph import get_pool
        from orchestrator.knowledge.kg_emit import drain_kg_events, emit_kg_event

        with get_pool().connection() as conn:
            emit_kg_event(
                conn,
                "business_profile_confirmed",
                tenant_id,
                {"tenant_id": str(tenant_id), "confirmed_fields": sorted(confirmed_fields.keys())},
            )
        drain_kg_events()
    except Exception:  # noqa: BLE001 — KG is downstream of the authoritative L1 promotion
        import logging

        logging.getLogger(__name__).exception(
            "confirm_draft: KG emit failed tenant=%s (profile promotion already committed)",
            tenant_id,
        )
