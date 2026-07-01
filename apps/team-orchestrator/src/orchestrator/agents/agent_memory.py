"""VT-550 (C3b) — the seedable learnable-memory MECHANISM.

Knowledge = the LLM's reasoning + a memory the agent GROWS, not a fixed note-set
(CL-2026-07-01-no-fixed-playbook). This module is the mechanism + the seed-then-learn-beyond posture:

  - ``upsert_seed`` writes a SEED entry — GLOBAL (archetype head-start, service path) or per-tenant.
  - ``upsert_learned`` writes a LEARNED entry that OVERWRITES the seed for the same key (version+1) —
    the agent grows beyond the seed.
  - ``get_active_memory`` is the retrieval interface — DEFAULT-CLOSED (returns [] until Phase-2 flips
    ``retrieval_eligible``; capture/seed-now, retrieve-later, exactly like agent_corrections).
  - ``mark_retrievable`` is the Phase-2 activation flip.

Content is PII-redacted at write. Seed CONTENT is a separate Fazal/archetype follow-up.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from orchestrator.privacy.pii_redactor import redact

logger = logging.getLogger(__name__)


def _redact_text(text: str) -> str:
    out = redact(text)
    return out if isinstance(out, str) else str(out)


def upsert_seed(
    *,
    memory_key: str,
    content: str,
    scope: str = "global",
    agent: str = "",
    archetype: str = "",
    tenant_id: UUID | str | None = None,
    conn: Any = None,
) -> None:
    """Write/overwrite a SEED memory entry. ``scope='global'`` (tenant_id None) is written via the
    SERVICE path (tenants cannot write global seeds); ``scope='tenant'`` requires a tenant conn."""
    if scope == "global":
        if tenant_id is not None:
            raise ValueError("a global seed must not carry a tenant_id")
        _upsert_global(agent=agent, archetype=archetype, memory_key=memory_key, content=content)
    else:
        if tenant_id is None:
            raise ValueError("a tenant seed requires a tenant_id")
        _upsert_tenant(
            tenant_id, source="seed", agent=agent, memory_key=memory_key, content=content, conn=conn
        )


def upsert_learned(
    tenant_id: UUID | str,
    *,
    memory_key: str,
    content: str,
    agent: str = "",
    conn: Any = None,
) -> None:
    """Write/overwrite a LEARNED entry for a tenant — OVERWRITES the seed/prior entry for the same
    (tenant, agent, key), version+1. This is the 'grow beyond the seed' write."""
    _upsert_tenant(
        tenant_id, source="learned", agent=agent, memory_key=memory_key, content=content, conn=conn
    )


def _upsert_tenant(
    tenant_id: UUID | str, *, source: str, agent: str, memory_key: str, content: str, conn: Any
) -> None:
    from orchestrator.db import tenant_connection

    sql = (
        "INSERT INTO agent_memory "
        "(tenant_id, memory_scope, source, agent, memory_key, content) "
        "VALUES (%s, 'tenant', %s, %s, %s, %s) "
        "ON CONFLICT (tenant_id, agent, memory_key) WHERE tenant_id IS NOT NULL "
        "DO UPDATE SET content = EXCLUDED.content, source = EXCLUDED.source, "
        "              version = agent_memory.version + 1, updated_at = now()"
    )
    params = (str(tenant_id), source, agent, memory_key, _redact_text(content))
    if conn is not None:
        conn.execute(sql, params)
    else:
        with tenant_connection(tenant_id) as c:
            c.execute(sql, params)


def _upsert_global(*, agent: str, archetype: str, memory_key: str, content: str) -> None:
    from orchestrator.graph import get_pool

    with get_pool().connection() as c:  # SERVICE path — global seeds are not tenant-writable
        c.execute(
            "INSERT INTO agent_memory "
            "(tenant_id, memory_scope, source, agent, archetype, memory_key, content) "
            "VALUES (NULL, 'global', 'seed', %s, %s, %s, %s) "
            "ON CONFLICT (archetype, agent, memory_key) WHERE tenant_id IS NULL "
            "DO UPDATE SET content = EXCLUDED.content, version = agent_memory.version + 1, "
            "              updated_at = now()",
            (agent, archetype, memory_key, _redact_text(content)),
        )


def get_active_memory(
    tenant_id: UUID | str, *, agent: str = "", conn: Any = None
) -> list[dict[str, Any]]:
    """Retrieval interface — RETRIEVAL-ELIGIBLE tenant + global entries for ``agent`` (learned first).
    DEFAULT-CLOSED: nothing is eligible until Phase-2 flips ``retrieval_eligible``, so this returns []
    today (seed/learn-now, retrieve-later — the agent_corrections posture)."""
    sql = (
        "SELECT memory_scope, source, agent, memory_key, content, version "
        "FROM agent_memory "
        "WHERE retrieval_eligible "
        "  AND (tenant_id = app_current_tenant() OR tenant_id IS NULL) "
        "  AND agent IN (%s, '') "
        "ORDER BY (source = 'learned') DESC, updated_at DESC"
    )
    rows = (
        conn.execute(sql, (agent,)).fetchall()
        if conn is not None
        else _read(tenant_id, sql, (agent,))
    )
    return [_row_to_dict(r) for r in rows]


_MEMORY_COLS = ("memory_scope", "source", "agent", "memory_key", "content", "version")


def _row_to_dict(r: Any) -> dict[str, Any]:
    if isinstance(r, dict):
        return {k: r[k] for k in _MEMORY_COLS}
    return dict(zip(_MEMORY_COLS, r))


def _read(tenant_id: UUID | str, sql: str, params: tuple) -> list:
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant_id) as c:
        return c.execute(sql, params).fetchall()


def mark_retrievable(
    tenant_id: UUID | str, *, memory_key: str, agent: str = "", eligible: bool = True, conn: Any = None
) -> int:
    """Phase-2 activation flip — mark a tenant memory entry retrieval-eligible. Returns rowcount."""
    from orchestrator.db import tenant_connection

    sql = (
        "UPDATE agent_memory SET retrieval_eligible = %s, updated_at = now() "
        "WHERE tenant_id = %s AND agent = %s AND memory_key = %s"
    )
    params = (eligible, str(tenant_id), agent, memory_key)
    if conn is not None:
        return conn.execute(sql, params).rowcount
    with tenant_connection(tenant_id) as c:
        return c.execute(sql, params).rowcount


__all__ = ["upsert_seed", "upsert_learned", "get_active_memory", "mark_retrievable"]
