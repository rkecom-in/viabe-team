"""VT-368 — versioned business_plan persistence (the spine's storage contract).

Append-only: every change (generation, a Gap-6 single-item edit, an agent status advance) mints a NEW
(tenant_id, version) row; content is immutable post-insert (only delivery metadata updates in place).
Latest = ``ORDER BY version DESC LIMIT 1``. The version mint locks the PARENT tenants row — NOT the
aggregate (Postgres rejects ``FOR UPDATE`` on an aggregate, and it would lock zero rows for v1; the
canonical in-repo pattern is pipeline_observability's run-row lock). The lock wraps ONLY mint+insert
(milliseconds) — never the LLM generation or delivery (the hot tenants row must not be held for
seconds; Cowork refinement #1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator.db import tenant_connection

# The closed Gap-5 dispatch enum (mirrors the specialist-agent keys; new specialists land here +
# models.yaml together). Enforced by schema.validate_plan (a CHECK can't reach into a JSONB array).
OWNING_AGENTS = frozenset(
    {"sales_recovery", "reputation", "acquisition", "retention", "menu_pricing", "unassigned"}
)
ITEM_STATUSES = frozenset({"proposed", "accepted", "in_progress", "done", "dropped"})


@dataclass(frozen=True)
class BusinessPlan:
    tenant_id: UUID
    version: int
    summary: dict[str, Any]
    roadmap: list[dict[str, Any]]
    fact_bundle: dict[str, Any]
    generated_by: str
    model_id: str | None
    delivered_parts: int
    created_at: Any = None


@dataclass(frozen=True)
class RoadmapItem:
    item_id: str
    seq: int
    month: int
    objective: str
    why: str
    cited_facts: list[str] = field(default_factory=list)
    owning_agent: str = "unassigned"
    owner_action_needed: bool = False
    owner_action: str | None = None
    owner_action_hi: str | None = None
    status: str = "proposed"
    provenance: dict[str, Any] = field(default_factory=dict)


def write_new_version(
    tenant_id: UUID | str,
    *,
    summary: dict[str, Any],
    roadmap: list[dict[str, Any]],
    fact_bundle: dict[str, Any],
    generated_by: str,
    model_id: str | None = None,
) -> int:
    """Mint the next version + insert, atomically. The parent-row lock serializes concurrent minters
    (two journey-complete replays / an edit racing a regenerate) INCLUDING the v1 case. Held for the
    mint+insert only — call this AFTER generation/validation, never around them."""
    with tenant_connection(tenant_id) as conn, conn.transaction():
        conn.execute(
            "SELECT id FROM tenants WHERE id = %s FOR UPDATE", (str(tenant_id),)
        ).fetchone()
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM business_plan WHERE tenant_id = %s",
            (str(tenant_id),),
        ).fetchone()
        nxt = int(row["v"] if isinstance(row, dict) else row[0])
        conn.execute(
            "INSERT INTO business_plan "
            "(tenant_id, version, summary_json, roadmap_json, fact_bundle_json, generated_by, model_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (str(tenant_id), nxt, Jsonb(summary), Jsonb(roadmap), Jsonb(fact_bundle), generated_by, model_id),
        )
    return nxt


def get_active_plan(tenant_id: UUID | str) -> BusinessPlan | None:
    """The latest plan version (the ONE read Gap-5 calls); None if no plan yet. RLS-scoped."""
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT version, summary_json, roadmap_json, fact_bundle_json, generated_by, model_id, "
            "delivered_parts, created_at FROM business_plan WHERE tenant_id = %s "
            "ORDER BY version DESC LIMIT 1",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    g = row if isinstance(row, dict) else dict(
        zip(("version", "summary_json", "roadmap_json", "fact_bundle_json", "generated_by",
             "model_id", "delivered_parts", "created_at"), row, strict=False)
    )
    return BusinessPlan(
        tenant_id=tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id)),
        version=g["version"], summary=dict(g["summary_json"] or {}),
        roadmap=list(g["roadmap_json"] or []), fact_bundle=dict(g["fact_bundle_json"] or {}),
        generated_by=g["generated_by"], model_id=g["model_id"],
        delivered_parts=int(g["delivered_parts"] or 0), created_at=g["created_at"],
    )


def plan_exists(tenant_id: UUID | str) -> bool:
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT 1 FROM business_plan WHERE tenant_id = %s LIMIT 1", (str(tenant_id),)
        ).fetchone()
    return row is not None


def plan_history(tenant_id: UUID | str) -> list[dict[str, Any]]:
    """The full version trail (the table IS the audit log) — metadata only, oldest first."""
    with tenant_connection(tenant_id) as conn:
        rows = conn.execute(
            "SELECT version, generated_by, model_id, created_at FROM business_plan "
            "WHERE tenant_id = %s ORDER BY version",
            (str(tenant_id),),
        ).fetchall()
    out = []
    for r in rows:
        g = r if isinstance(r, dict) else dict(zip(("version", "generated_by", "model_id", "created_at"), r, strict=False))
        out.append(dict(g))
    return out


def mark_part_delivered(tenant_id: UUID | str, version: int, part_index: int, *, final: bool) -> None:
    """Set the part bit (idempotent replay resume — Cowork-refined delivery contract); stamp
    delivered_at on the final part. The ONLY in-place update on the table (delivery metadata)."""
    bit = 1 << part_index
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "UPDATE business_plan SET delivered_parts = delivered_parts | %s, "
            "delivered_at = CASE WHEN %s THEN now() ELSE delivered_at END "
            "WHERE tenant_id = %s AND version = %s",
            (bit, final, str(tenant_id), version),
        )
