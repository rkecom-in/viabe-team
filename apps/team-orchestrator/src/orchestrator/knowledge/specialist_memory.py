"""VT-711 tenant-scoped specialist customisation and card-assignment write paths.

This module is deliberately not registered on a live route. It provides the governed persistence
seam for a future VTR/Manager caller: tenant RLS is mandatory, writes carry who/when/why provenance,
and database triggers append the lifecycle event atomically. Retrieval remains advisory and cannot
authorize customer sends, money movement, consent changes, or any other external effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID

from orchestrator.knowledge_contracts import (
    KnowledgeAssignmentScope,
    specialist_assignment_scope,
    validate_assignment_scope,
)
from orchestrator.privacy.pii_redactor import redact

MemoryAuthor = Literal["vtr", "manager"]
MemoryStatus = Literal["active", "disabled", "superseded"]


@dataclass(frozen=True)
class SpecialistMemoryWriteResult:
    memory_card_id: UUID
    version: int
    assignment_scope: str
    idempotent_replay: bool


@dataclass(frozen=True)
class CardAssignmentWriteResult:
    assignment_id: UUID
    scope: str
    enabled: bool
    idempotent_replay: bool


def _required(value: str, field: str, *, max_length: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must be non-empty")
    if len(normalized) > max_length:
        raise ValueError(f"{field} exceeds {max_length} characters")
    return normalized


def _redacted(value: str, field: str, *, max_length: int) -> str:
    normalized = _required(value, field, max_length=max_length)
    scrubbed = redact(normalized)
    return _required(
        scrubbed if isinstance(scrubbed, str) else str(scrubbed),
        field,
        max_length=max_length,
    )


def _validate_author(author: str) -> MemoryAuthor:
    if author not in {"vtr", "manager"}:
        raise ValueError("authored_by must be 'vtr' or 'manager'")
    return cast(MemoryAuthor, author)


def _column(row: Any, index: int, name: str) -> Any:
    """Read psycopg tuple rows and dict_row rows without coupling this seam to pool config."""

    return row[name] if isinstance(row, dict) else row[index]


def write_specialist_customization(
    tenant_id: UUID | str,
    *,
    agent: str,
    task_scope: str,
    memory_key: str,
    customization: str,
    authored_by: MemoryAuthor,
    author_id: str,
    reason: str,
    idempotency_key: str,
    status: MemoryStatus = "active",
    conn: Any = None,
) -> SpecialistMemoryWriteResult:
    """Append one immutable specialist-memory version under the exact agent+task scope.

    The database creates ``specialist_memory_events`` in the same statement transaction. Replaying
    an idempotency key returns the original version and never creates a second event.
    """

    agent_scope = specialist_assignment_scope(agent)
    task_scope = _required(task_scope, "task_scope", max_length=300)
    memory_key = _required(memory_key, "memory_key", max_length=300)
    customization = _redacted(customization, "customization", max_length=4_000)
    author = _validate_author(authored_by)
    author_id = _required(author_id, "author_id", max_length=200)
    reason = _redacted(reason, "reason", max_length=2_000)
    idempotency_key = _required(idempotency_key, "idempotency_key", max_length=300)
    if status not in {"active", "disabled", "superseded"}:
        raise ValueError("invalid specialist-memory status")

    if conn is None:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as tenant_conn:
            return _write_specialist_customization(
                tenant_conn,
                tenant_id=str(tenant_id),
                agent=agent,
                task_scope=task_scope,
                memory_key=memory_key,
                customization=customization,
                authored_by=author,
                author_id=author_id,
                reason=reason,
                idempotency_key=idempotency_key,
                status=status,
                assignment_scope=agent_scope,
            )
    return _write_specialist_customization(
        conn,
        tenant_id=str(tenant_id),
        agent=agent,
        task_scope=task_scope,
        memory_key=memory_key,
        customization=customization,
        authored_by=author,
        author_id=author_id,
        reason=reason,
        idempotency_key=idempotency_key,
        status=status,
        assignment_scope=agent_scope,
    )


def _write_specialist_customization(
    conn: Any,
    *,
    tenant_id: str,
    agent: str,
    task_scope: str,
    memory_key: str,
    customization: str,
    authored_by: MemoryAuthor,
    author_id: str,
    reason: str,
    idempotency_key: str,
    status: MemoryStatus,
    assignment_scope: str,
) -> SpecialistMemoryWriteResult:
    with conn.transaction():
        existing = conn.execute(
            "SELECT id, version, assignment_scope FROM specialist_memory_cards "
            "WHERE tenant_id = %s AND idempotency_key = %s",
            (tenant_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            return SpecialistMemoryWriteResult(
                memory_card_id=UUID(str(_column(existing, 0, "id"))),
                version=int(_column(existing, 1, "version")),
                assignment_scope=str(_column(existing, 2, "assignment_scope")),
                idempotent_replay=True,
            )

        lock_key = f"specialist-memory:{tenant_id}:{agent}:{task_scope}:{memory_key}"
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
        previous = conn.execute(
            "SELECT id, version FROM specialist_memory_cards "
            "WHERE tenant_id = %s AND agent = %s AND task_scope = %s AND memory_key = %s "
            "ORDER BY version DESC LIMIT 1",
            (tenant_id, agent, task_scope, memory_key),
        ).fetchone()
        previous_id = _column(previous, 0, "id") if previous is not None else None
        version = int(_column(previous, 1, "version")) + 1 if previous is not None else 1
        row = conn.execute(
            "INSERT INTO specialist_memory_cards "
            "(tenant_id, agent, task_scope, memory_key, customization, authored_by, author_id, "
            " reason, status, version, supersedes_memory_card_id, idempotency_key) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id, version, assignment_scope",
            (
                tenant_id,
                agent,
                task_scope,
                memory_key,
                customization,
                authored_by,
                author_id,
                reason,
                status,
                version,
                previous_id,
                idempotency_key,
            ),
        ).fetchone()
    return SpecialistMemoryWriteResult(
        memory_card_id=UUID(str(_column(row, 0, "id"))),
        version=int(_column(row, 1, "version")),
        assignment_scope=str(_column(row, 2, "assignment_scope")),
        idempotent_replay=False,
    )


def set_card_assignment(
    tenant_id: UUID | str,
    *,
    card_id: UUID | str,
    scope: str,
    enabled: bool,
    actor: MemoryAuthor,
    actor_id: str,
    reason: str,
    idempotency_key: str,
    conn: Any = None,
) -> CardAssignmentWriteResult:
    """Create/change one tenant override; its event is emitted atomically by migration 186."""

    scope = validate_assignment_scope(scope)
    if scope == KnowledgeAssignmentScope.DISABLED.value:
        enabled = False
    actor = _validate_author(actor)
    actor_id = _required(actor_id, "actor_id", max_length=200)
    reason = _redacted(reason, "reason", max_length=2_000)
    idempotency_key = _required(idempotency_key, "idempotency_key", max_length=300)

    if conn is None:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as tenant_conn:
            return _set_card_assignment(
                tenant_conn,
                tenant_id=str(tenant_id),
                card_id=str(card_id),
                scope=scope,
                enabled=enabled,
                actor=actor,
                actor_id=actor_id,
                reason=reason,
                idempotency_key=idempotency_key,
            )
    return _set_card_assignment(
        conn,
        tenant_id=str(tenant_id),
        card_id=str(card_id),
        scope=scope,
        enabled=enabled,
        actor=actor,
        actor_id=actor_id,
        reason=reason,
        idempotency_key=idempotency_key,
    )


def _set_card_assignment(
    conn: Any,
    *,
    tenant_id: str,
    card_id: str,
    scope: str,
    enabled: bool,
    actor: MemoryAuthor,
    actor_id: str,
    reason: str,
    idempotency_key: str,
) -> CardAssignmentWriteResult:
    row = conn.execute(
        "INSERT INTO knowledge_card_assignments "
        "(tenant_id, card_id, scope, enabled, reason, actor, actor_id, change_idempotency_key) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (tenant_id, card_id) DO UPDATE SET "
        "scope = EXCLUDED.scope, enabled = EXCLUDED.enabled, reason = EXCLUDED.reason, "
        "actor = EXCLUDED.actor, actor_id = EXCLUDED.actor_id, "
        "change_idempotency_key = EXCLUDED.change_idempotency_key "
        "WHERE knowledge_card_assignments.change_idempotency_key "
        "      IS DISTINCT FROM EXCLUDED.change_idempotency_key "
        "RETURNING id, scope, enabled",
        (tenant_id, card_id, scope, enabled, reason, actor, actor_id, idempotency_key),
    ).fetchone()
    replay = row is None
    if row is None:
        row = conn.execute(
            "SELECT id, scope, enabled FROM knowledge_card_assignments "
            "WHERE tenant_id = %s AND card_id = %s AND change_idempotency_key = %s",
            (tenant_id, card_id, idempotency_key),
        ).fetchone()
        if row is None:
            raise RuntimeError("card-assignment upsert produced no attributable row")
    return CardAssignmentWriteResult(
        assignment_id=UUID(str(_column(row, 0, "id"))),
        scope=str(_column(row, 1, "scope")),
        enabled=bool(_column(row, 2, "enabled")),
        idempotent_replay=replay,
    )


def read_specialist_customizations(
    tenant_id: UUID | str,
    *,
    agent: str,
    task_scope: str,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Read only the latest ACTIVE customisation for this exact specialist and task scope."""

    specialist_assignment_scope(agent)
    task_scope = _required(task_scope, "task_scope", max_length=300)
    sql = (
        "SELECT memory_key, customization, version, authored_by, reason, assignment_scope "
        "FROM (SELECT DISTINCT ON (memory_key) memory_key, customization, version, authored_by, "
        "             reason, assignment_scope, status "
        "      FROM specialist_memory_cards "
        "      WHERE tenant_id = app_current_tenant() AND agent = %s AND task_scope = %s "
        "      ORDER BY memory_key, version DESC) latest "
        "WHERE status = 'active' ORDER BY memory_key"
    )
    if conn is None:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as tenant_conn:
            rows = tenant_conn.execute(sql, (agent, task_scope)).fetchall()
    else:
        rows = conn.execute(sql, (agent, task_scope)).fetchall()
    keys = ("memory_key", "customization", "version", "authored_by", "reason", "assignment_scope")
    return [dict(row) if isinstance(row, dict) else dict(zip(keys, row, strict=True)) for row in rows]


__all__ = [
    "CardAssignmentWriteResult",
    "SpecialistMemoryWriteResult",
    "read_specialist_customizations",
    "set_card_assignment",
    "write_specialist_customization",
]
