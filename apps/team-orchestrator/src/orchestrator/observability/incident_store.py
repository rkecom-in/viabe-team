"""VT-552 (B1 part-2b) — the durable incident store + owner→VTR escalation ladder.

``create_incident`` is idempotent per (run, kind). ``escalate_incident`` walks the ladder
(0 detected → 1 owner-contacted → 2 vtr-escalated) under a CAS guard; at the VTR tier it creates an
``escalations`` row (mig 073 — the VTR queue, a deny-all ops table → written via the SERVICE path,
idempotent on its uq_escalations_run) and links it back. Detail is PII-redacted at write.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator.db import tenant_connection
from orchestrator.privacy.pii_redactor import redact

logger = logging.getLogger(__name__)

INCIDENT_KINDS = frozenset(
    {"silent_terminal", "failed_run", "owner_unreachable", "other", "limit_exhausted"}
)


def _id(row: Any) -> UUID:
    raw = row["id"] if isinstance(row, dict) else row[0]
    return raw if isinstance(raw, UUID) else UUID(str(raw))


def _field(row: Any, key: str, idx: int) -> Any:
    return row[key] if isinstance(row, dict) else row[idx]


def create_incident(
    tenant_id: UUID | str,
    *,
    incident_kind: str,
    run_id: UUID | str | None = None,
    severity: str = "warning",
    detail: dict[str, Any] | None = None,
    conn: Any = None,
) -> UUID | None:
    """Create an incident, idempotent per (run, kind). Returns the incident id (new or existing)."""

    def _run(c: Any) -> UUID | None:
        row = c.execute(
            "INSERT INTO incidents (tenant_id, run_id, incident_kind, severity, detail) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (run_id, incident_kind) WHERE run_id IS NOT NULL DO NOTHING "
            "RETURNING id",
            (
                str(tenant_id),
                str(run_id) if run_id is not None else None,
                incident_kind,
                severity,
                Jsonb(redact(detail)) if detail is not None else None,
            ),
        ).fetchone()
        if row is not None:
            return _id(row)
        if run_id is not None:  # conflict — return the existing incident
            ex = c.execute(
                "SELECT id FROM incidents WHERE run_id = %s AND incident_kind = %s",
                (str(run_id), incident_kind),
            ).fetchone()
            return _id(ex) if ex is not None else None
        return None

    if conn is not None:
        return _run(conn)
    with tenant_connection(tenant_id) as c:
        return _run(c)


def escalate_incident(
    tenant_id: UUID | str,
    incident_id: UUID | str,
    *,
    to_tier: int,
    owner_contacted: bool | None = None,
    conn: Any = None,
) -> bool:
    """CAS-advance the ladder to ``to_tier`` (only if current tier < to_tier and not resolved). At
    tier ≥ 2 create the VTR escalations row. Returns True iff it advanced."""

    def _run(c: Any) -> Any:
        return c.execute(
            "UPDATE incidents SET escalation_tier = %s, version = version + 1, updated_at = now(), "
            "  status = CASE WHEN %s >= 2 THEN 'escalated' ELSE status END, "
            "  owner_contacted = COALESCE(%s, owner_contacted) "
            "WHERE id = %s AND tenant_id = %s AND escalation_tier < %s "
            "  AND status IN ('open', 'escalated') "
            "RETURNING run_id",
            (to_tier, to_tier, owner_contacted, str(incident_id), str(tenant_id), to_tier),
        ).fetchone()

    row = _run(conn) if conn is not None else _tenant_run(tenant_id, _run)
    if row is None:
        return False  # already at/above the tier, or resolved — no advance
    if to_tier >= 2:
        _create_vtr_escalation(tenant_id, incident_id, run_id=_field(row, "run_id", 0))
    return True


def resolve_incident(
    tenant_id: UUID | str, incident_id: UUID | str, *, conn: Any = None
) -> bool:
    def _run(c: Any) -> Any:
        return c.execute(
            "UPDATE incidents SET status = 'resolved', version = version + 1, updated_at = now() "
            "WHERE id = %s AND tenant_id = %s AND status <> 'resolved' RETURNING id",
            (str(incident_id), str(tenant_id)),
        ).fetchone()

    row = _run(conn) if conn is not None else _tenant_run(tenant_id, _run)
    return row is not None


def get_incident(tenant_id: UUID | str, incident_id: UUID | str, *, conn: Any = None) -> dict | None:
    def _run(c: Any) -> Any:
        return c.execute(
            "SELECT id, run_id, incident_kind, severity, status, escalation_tier, owner_contacted, "
            "       vtr_escalation_id, version "
            "FROM incidents WHERE id = %s AND tenant_id = %s",
            (str(incident_id), str(tenant_id)),
        ).fetchone()

    row = _run(conn) if conn is not None else _tenant_run(tenant_id, _run)
    if row is None:
        return None
    cols = ("id", "run_id", "incident_kind", "severity", "status", "escalation_tier",
            "owner_contacted", "vtr_escalation_id", "version")
    return {k: _field(row, k, i) for i, k in enumerate(cols)}


def _tenant_run(tenant_id: UUID | str, fn: Any) -> Any:
    with tenant_connection(tenant_id) as c:
        return fn(c)


def _create_vtr_escalation(
    tenant_id: UUID | str, incident_id: UUID | str, *, run_id: Any
) -> None:
    """Create the VTR-queue escalations row (deny-all ops table → SERVICE path) + link it back.
    Idempotent on uq_escalations_run; fail-soft (the tier advance already committed)."""
    try:
        from orchestrator.graph import get_pool

        with get_pool().connection() as c:
            row = c.execute(
                "INSERT INTO escalations (tenant_id, run_id, kind, severity, status) "
                "VALUES (%s, %s, 'silent_terminal', 'high', 'open') "
                "ON CONFLICT (run_id) WHERE run_id IS NOT NULL DO NOTHING RETURNING id",
                (str(tenant_id), str(run_id) if run_id is not None else None),
            ).fetchone()
            if row is not None:
                c.execute(
                    "UPDATE incidents SET vtr_escalation_id = %s, updated_at = now() WHERE id = %s",
                    (str(_id(row)), str(incident_id)),
                )
    except Exception:  # noqa: BLE001 — the incident tier already advanced; the queue link is best-effort
        logger.warning("VT-552 VTR escalation create failed (fail-soft)", exc_info=True)


__all__ = [
    "INCIDENT_KINDS",
    "create_incident",
    "escalate_incident",
    "resolve_incident",
    "get_incident",
]
