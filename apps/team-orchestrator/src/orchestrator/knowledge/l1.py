"""L1 Knowledge Graph — typed retrieval over hand-built relational + pgvector
substrate (VT-7.1 / CL-324).

Two surfaces:

- ``search_entities`` — multi-signal retrieval over ``l1_entities`` combining
  pgvector cosine similarity, relational filters (entity_type, attributes
  containment), and Postgres full-text search over JSONB attribute text.
- ``traverse_relationships`` — recursive-CTE traversal over ``l1_relationships``
  from a start entity, with a depth cap (default 3, hard ceiling
  ``MAX_TRAVERSAL_DEPTH``) and cycle prevention via path-array membership.

Pillar 3 — defence in depth: every query goes through ``tenant_connection``
(RLS + GUC scoped) AND carries an explicit ``WHERE tenant_id = %s`` clause.
RLS is the enforcer; the explicit predicate is the belt-and-braces guard
documented across this codebase (CL-71 / CL-190 / CL-71).

Embedding format — pgvector's text literal ``[v1,v2,...]`` plus an SQL
``::vector`` cast. Avoids per-connection ``register_vector`` calls and the
state-leak surface that would create against the shared pool.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

from orchestrator.db import tenant_connection

MAX_TRAVERSAL_DEPTH = 5
_DEFAULT_TRAVERSAL_DEPTH = 3
_SEARCH_LIMIT_CAP = 100


@dataclass(frozen=True, slots=True)
class L1Entity:
    """A row from ``l1_entities``. ``embedding`` is None when not loaded
    (the retrieval queries omit it from the projection by default to keep
    payloads small; the eager-load helper is for population pipelines that
    re-embed)."""

    id: UUID
    tenant_id: UUID
    entity_type: str
    attributes: dict[str, Any]
    valid_from: datetime | None
    valid_to: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class L1Relationship:
    """A row from ``l1_relationships``. ``from_entity`` / ``to_entity`` are
    raw UUIDs — callers join against ``l1_entities`` for the typed entity."""

    id: UUID
    tenant_id: UUID
    from_entity: UUID
    to_entity: UUID
    relationship_type: str
    attributes: dict[str, Any]
    valid_from: datetime | None
    valid_to: datetime | None


@dataclass(frozen=True, slots=True)
class L1Path:
    """One traversal result — the ordered chain of entities + relationship
    types from the start entity to a reachable entity at ``depth`` hops.

    ``entities`` has length ``depth + 1`` (includes the start entity at
    index 0); ``relationship_types`` has length ``depth`` (one per hop).
    """

    entities: list[UUID]
    relationship_types: list[str]
    depth: int


def _vec_literal(vec: list[float]) -> str:
    """Format a Python float list as pgvector's text literal.

    pgvector parses ``'[1.0,2.0,3.0]'::vector`` natively. We use this over
    ``pgvector.psycopg.register_vector`` to keep the shared connection pool
    free of per-connection adapter state.
    """
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def search_entities(
    tenant_id: UUID,
    *,
    query_embedding: list[float] | None = None,
    entity_type: str | None = None,
    attributes_filter: dict[str, Any] | None = None,
    text_query: str | None = None,
    limit: int = 10,
) -> list[L1Entity]:
    """Multi-signal entity retrieval. Returns up to ``limit`` rows
    (hard-capped at ``_SEARCH_LIMIT_CAP``), tenant-scoped.

    Signals (AND-combined):

    - ``query_embedding``: pgvector cosine ranking via the ``<=>`` operator
      (matches the ``vector_cosine_ops`` HNSW index). When provided, results
      are ordered by ascending cosine distance.
    - ``entity_type``: exact match on the ``entity_type`` column.
    - ``attributes_filter``: ``attributes @> %s::jsonb`` containment filter.
      Use this for L3-style locality filters (``{"locality": "..."}``).
    - ``text_query``: full-text search over ``attributes::text`` using
      ``plainto_tsquery('english', ...)``.

    Without ``query_embedding`` results fall back to ``ORDER BY created_at
    DESC`` so the call shape stays consistent (most-recent-first listing).
    """
    where_parts: list[sql.Composable] = [sql.SQL("tenant_id = %(tenant_id)s")]
    params: dict[str, Any] = {"tenant_id": tenant_id}

    if entity_type is not None:
        where_parts.append(sql.SQL("entity_type = %(entity_type)s"))
        params["entity_type"] = entity_type
    if attributes_filter is not None:
        where_parts.append(sql.SQL("attributes @> %(attrs)s::jsonb"))
        params["attrs"] = Jsonb(attributes_filter)
    if text_query is not None:
        where_parts.append(
            sql.SQL(
                "to_tsvector('english', attributes::text) "
                "@@ plainto_tsquery('english', %(text_q)s)"
            )
        )
        params["text_q"] = text_query

    order_clause: sql.Composable
    if query_embedding is not None:
        order_clause = sql.SQL("embedding <=> %(embedding)s::vector")
        params["embedding"] = _vec_literal(query_embedding)
    else:
        order_clause = sql.SQL("created_at DESC")

    params["lim"] = min(max(int(limit), 0), _SEARCH_LIMIT_CAP)

    stmt = sql.SQL(
        "SELECT id, tenant_id, entity_type, attributes, "
        "valid_from, valid_to, created_at "
        "FROM l1_entities "
        "WHERE {where} "
        "ORDER BY {order} "
        "LIMIT %(lim)s"
    ).format(
        where=sql.SQL(" AND ").join(where_parts),
        order=order_clause,
    )

    with tenant_connection(tenant_id) as conn:
        raw = conn.execute(stmt, params).fetchall()
    rows = cast("list[dict[str, Any]]", raw)
    return [_row_to_entity(row) for row in rows]


def traverse_relationships(
    tenant_id: UUID,
    *,
    start_entity: UUID,
    max_depth: int = _DEFAULT_TRAVERSAL_DEPTH,
    relationship_type: str | None = None,
) -> list[L1Path]:
    """Recursive-CTE traversal from ``start_entity`` over ``l1_relationships``.

    Depth is capped (default 3, ceiling ``MAX_TRAVERSAL_DEPTH``). Cycles are
    prevented via ``NOT (r.to_entity = ANY(path_entities))`` — a node already
    on the current path is not re-visited.

    When ``relationship_type`` is supplied, only edges of that type are
    followed. Returns one ``L1Path`` per reachable distinct path (a node
    reachable via multiple paths appears multiple times).
    """
    if max_depth < 1:
        raise ValueError("max_depth must be >= 1")
    if max_depth > MAX_TRAVERSAL_DEPTH:
        raise ValueError(
            f"max_depth {max_depth} exceeds ceiling {MAX_TRAVERSAL_DEPTH}"
        )

    stmt = sql.SQL(
        """
        WITH RECURSIVE traversal AS (
            SELECT
                ARRAY[%(start)s]::uuid[] AS path_entities,
                ARRAY[]::text[] AS path_relationships,
                %(start)s::uuid AS current_entity,
                0 AS depth
            UNION ALL
            SELECT
                t.path_entities || r.to_entity,
                t.path_relationships || r.relationship_type,
                r.to_entity,
                t.depth + 1
            FROM traversal t
            JOIN l1_relationships r
                ON r.from_entity = t.current_entity
               AND r.tenant_id = %(tenant_id)s
            WHERE t.depth < %(max_depth)s
              AND (%(rel_type)s::text IS NULL
                   OR r.relationship_type = %(rel_type)s)
              AND NOT (r.to_entity = ANY(t.path_entities))
        )
        SELECT path_entities, path_relationships, depth
        FROM traversal
        WHERE depth > 0
        ORDER BY depth ASC, current_entity ASC
        """
    )
    params: dict[str, Any] = {
        "start": start_entity,
        "tenant_id": tenant_id,
        "max_depth": max_depth,
        "rel_type": relationship_type,
    }

    with tenant_connection(tenant_id) as conn:
        raw = conn.execute(stmt, params).fetchall()
    rows = cast("list[dict[str, Any]]", raw)
    return [
        L1Path(
            entities=[_as_uuid(e) for e in row["path_entities"]],
            relationship_types=list(row["path_relationships"]),
            depth=int(row["depth"]),
        )
        for row in rows
    ]


def _row_to_entity(row: dict[str, Any]) -> L1Entity:
    return L1Entity(
        id=_as_uuid(row["id"]),
        tenant_id=_as_uuid(row["tenant_id"]),
        entity_type=row["entity_type"],
        attributes=_as_dict(row["attributes"]),
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        created_at=row["created_at"],
    )


def _as_uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _as_dict(value: Any) -> dict[str, Any]:
    """psycopg returns JSONB as already-parsed dict; tolerate string fallback."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, dict):
            return loaded
    return {}


# --- VT-195: Context Composer read path ------------------------------------
#
# L1's per-tenant IDENTITY lives as ONE entity per tenant: entity_type =
# BUSINESS_PROFILE_ENTITY_TYPE, durable attributes in `attributes`.
# `assemble_context_bundle` reads it (RLS-scoped via search_entities ->
# tenant_connection) and renders a compact, token-bounded system block that
# Phase 2 pre-injects as a SEPARATE system block AFTER the VT-194 cached prefix
# (D2). Returns None when the tenant has no business_profile entity (nothing to
# inject) -> Phase 2 injects nothing rather than an empty header.

BUSINESS_PROFILE_ENTITY_TYPE = "business_profile"
# VT-197: the agent's learned calibration — a SEPARATE, agent-owned entity, NEVER
# the owner-curated business_profile. Rendered in its own labeled section so the
# model never confuses agent inference with owner-stated policy.
AGENT_REFLECTION_ENTITY_TYPE = "agent_reflection"

# Token-safety bound for the rendered block (chars; ~4 chars/token heuristic) —
# a stray huge owner note must not blow the context window.
_L1_BLOCK_MAX_CHARS = 4000

# Rendered fields (in order) from the business_profile entity's attributes.
_L1_BLOCK_FIELDS: tuple[tuple[str, str], ...] = (
    ("business_archetype", "Business archetype"),
    ("owner_persona", "Owner persona"),
    ("working_hours", "Working hours"),
    ("integration_map", "Integrations"),
    ("escalation_thresholds", "Escalation thresholds"),
    ("communication_prefs", "Communication preferences"),
    ("owner_curated_context", "Owner notes"),
)

# Rendered fields (in order) from the agent_reflection entity's attributes (VT-197).
_REFLECTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("summary", "Summary"),
    ("verdict", "Day-39 verdict"),
    ("arrr_paise", "Attributed recovery (paise)"),
    ("cumulative_fees_paise", "Cumulative fees (paise)"),
    ("decided_at", "Decided at"),
)


def _render_l1_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _render_l1_section(
    title: str, attrs: dict[str, Any], fields: tuple[tuple[str, str], ...]
) -> list[str]:
    """Render one labeled section; [] when no field is populated."""
    lines: list[str] = []
    for key, label in fields:
        val = attrs.get(key)
        if val in (None, "", {}, []):
            continue
        lines.append(f"- {label}: {_render_l1_value(val)}")
    return [f"## {title}", *lines] if lines else []


def assemble_context_bundle(tenant_id: UUID | str) -> str | None:
    """Render the tenant's L1 context as a system block, in two labeled sections.

    Reads BOTH the owner-curated ``business_profile`` and the agent-owned
    ``agent_reflection`` entities (RLS-scoped via search_entities) and renders
    them as DISTINCT sections — "Owner-stated (business profile)" vs
    "Agent-learned (Day-39)" — so the model never treats an agent reflection as
    owner policy (VT-197 scope guard). Returns None when neither carries content.
    Phase 2 pre-injects the result as a separate system block after the VT-194
    cached prefix.

    CL-390: owner_curated_context is owner-authored business context, not customer
    PII. Never logs attribute values — only tenant_id + block length.
    """
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    profile = search_entities(tid, entity_type=BUSINESS_PROFILE_ENTITY_TYPE, limit=1)
    reflection = search_entities(tid, entity_type=AGENT_REFLECTION_ENTITY_TYPE, limit=1)

    sections: list[str] = []
    if profile:
        sections += _render_l1_section(
            "Owner-stated (business profile)", profile[0].attributes or {}, _L1_BLOCK_FIELDS
        )
    if reflection:
        sections += _render_l1_section(
            "Agent-learned (Day-39)", reflection[0].attributes or {}, _REFLECTION_FIELDS
        )
    if not sections:
        return None
    block = "# Tenant context (L1)\n" + "\n".join(sections)
    if len(block) > _L1_BLOCK_MAX_CHARS:
        block = block[:_L1_BLOCK_MAX_CHARS] + "\n- [truncated]"
    return block


def upsert_business_profile(
    tenant_id: UUID | str, attributes: dict[str, Any]
) -> UUID:
    """Idempotent upsert of the tenant's single 'business_profile' L1 entity.

    RLS-scoped via tenant_connection (SET ROLE app_role + GUC). Re-runs are safe:
    ON CONFLICT targets the partial unique index l1_entities_one_business_profile_
    _per_tenant (migration 055, one business_profile per tenant) and replaces the
    attributes. Returns the entity id. The write path for the Cowork-curated seed
    + future onboarding (VT-267) / dashboard edits.

    CL-390: attributes are the tenant's OWN business identity (archetype, owner
    persona, operating notes), NOT customer PII.
    """
    tid = _as_uuid(tenant_id)
    with tenant_connection(tid) as conn:
        row = conn.execute(
            """
            INSERT INTO l1_entities (tenant_id, entity_type, attributes)
            VALUES (%s, 'business_profile', %s)
            ON CONFLICT (tenant_id) WHERE entity_type = 'business_profile'
            DO UPDATE SET attributes = EXCLUDED.attributes
            RETURNING id
            """,
            (str(tid), Jsonb(attributes)),
        ).fetchone()
    return _as_uuid(row["id"] if isinstance(row, dict) else row[0])


def upsert_agent_reflection(
    tenant_id: UUID | str, attributes: dict[str, Any]
) -> UUID:
    """Idempotent upsert of the tenant's single AGENT-owned 'agent_reflection'
    L1 entity (VT-197 — the Day-39 learning loop's latest calibration).

    RLS-scoped via tenant_connection. ON CONFLICT targets the partial unique
    index l1_entities_one_agent_reflection_per_tenant (migration 056, one latest
    reflection per tenant) and replaces the attributes. Returns the entity id.

    SCOPE GUARD: this writes ONLY the 'agent_reflection' entity — NEVER the
    owner-curated 'business_profile' (Fazal D3; VT-268). Keep it LLM-free at the
    callsite (the Day-39 trigger subtree is deterministic, Pillar 1).
    """
    tid = _as_uuid(tenant_id)
    with tenant_connection(tid) as conn:
        row = conn.execute(
            """
            INSERT INTO l1_entities (tenant_id, entity_type, attributes)
            VALUES (%s, 'agent_reflection', %s)
            ON CONFLICT (tenant_id) WHERE entity_type = 'agent_reflection'
            DO UPDATE SET attributes = EXCLUDED.attributes
            RETURNING id
            """,
            (str(tid), Jsonb(attributes)),
        ).fetchone()
    return _as_uuid(row["id"] if isinstance(row, dict) else row[0])
