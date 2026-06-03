"""VT-65 — generic L1 KG write primitives (idempotent, tenant-scoped).

The existing knowledge/l1.py writers are single-entity specializations
(business_profile / agent_reflection). The population pipeline needs generic
entity + edge writes keyed by a stable natural key (the source row id) for
idempotent (re-backfill-safe) upserts.

Tenant-scoping (Cowork 20260603T171500Z, guardrail 3): every write runs through
``tenant_connection`` (layer-1 RLS) AND validates the returned row via
``assert_tenant_scoped`` (the VT-72 layer-2 primitive) — a cross-tenant write
raises ``TenantIsolationError`` + emits ``tenant_isolation_breach`` (→ VT-79
Detector-1). Full L1 wrappers fold into VT-306.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator._tenant_guard import assert_tenant_scoped
from orchestrator.db import tenant_connection


def _uuid(tenant_id: UUID | str) -> UUID:
    return tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))


def upsert_entity(
    tenant_id: UUID | str,
    entity_type: str,
    external_key: str,
    attributes: dict[str, Any],
) -> UUID:
    """Idempotent MERGE of a KG entity keyed by (tenant, entity_type, external_key).

    Re-running with the same key MERGEs attributes (no clobber, no dup row).
    Returns the entity id.
    """
    tid = _uuid(tenant_id)
    with tenant_connection(tid) as conn:
        row = conn.execute(
            """
            INSERT INTO l1_entities (tenant_id, entity_type, external_key, attributes)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tenant_id, entity_type, external_key)
                WHERE external_key IS NOT NULL
            DO UPDATE SET attributes = l1_entities.attributes || EXCLUDED.attributes
            RETURNING id, tenant_id
            """,
            (str(tid), entity_type, external_key, Jsonb(attributes)),
        ).fetchone()
    d = dict(row)
    assert_tenant_scoped([d], tid)
    return d["id"] if isinstance(d["id"], UUID) else UUID(str(d["id"]))


def add_relationship(
    tenant_id: UUID | str,
    from_entity: UUID | str,
    to_entity: UUID | str,
    relationship_type: str,
) -> None:
    """Idempotent directed edge (no-op on re-insert)."""
    tid = _uuid(tenant_id)
    with tenant_connection(tid) as conn:
        row = conn.execute(
            """
            INSERT INTO l1_relationships
              (tenant_id, from_entity, to_entity, relationship_type)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tenant_id, from_entity, to_entity, relationship_type)
            DO NOTHING
            RETURNING tenant_id
            """,
            (str(tid), str(from_entity), str(to_entity), relationship_type),
        ).fetchone()
    # RETURNING is NULL on a no-op (already-present edge) — only validate a real write.
    if row is not None:
        assert_tenant_scoped([dict(row)], tid)


__all__ = ["add_relationship", "upsert_entity"]
