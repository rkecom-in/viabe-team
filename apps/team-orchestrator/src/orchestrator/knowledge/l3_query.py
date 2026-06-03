"""VT-69 — L3 retrieval + 180-day quarantine.

``lookup_pattern`` returns the global L3 prior for a cohort, EXCEPT for tenants
younger than 180 days: they neither contribute (VT-68 construction excludes them)
nor read (here). Quarantine is structural (Pillar 7) — there is NO override
parameter and no admin bypass (Type-3 commitment; do not add one).

l3_patterns is cross-tenant global (no RLS, no tenant_id), so reads go through the
service-role pool. The quarantine check reads the CALLING tenant's signed_up_at
only. Telemetry (l3_query_attempted / l3_quarantine_skip / l3_no_match) is written
to pipeline_log with the coarse cohort_key — never PII.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from orchestrator.graph import get_pool
from orchestrator.knowledge.l3_types import L3Pattern

logger = logging.getLogger(__name__)

QUARANTINE_DAYS = 180  # Type-3 (Pillar 7) — no override path exists.

_COLS = (
    "id, pattern_type, cohort_key, n_tenants, n_campaigns, metrics, "
    "confidence_band, constructed_at, expires_at"
)


def _telemetry(event_type: str, tenant_id: UUID, run_id: UUID, payload: dict[str, Any]) -> None:
    """Append a tenant-scoped L3 telemetry row to pipeline_log. Coarse cohort_key
    only — never PII. Best-effort; never raises."""
    try:
        with get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO pipeline_log (run_id, tenant_id, event_type, severity, component, payload) "
                "VALUES (%s, %s, %s, 'info', 'l3_query', %s)",
                (str(run_id), str(tenant_id), event_type, Jsonb(payload)),
            )
    except Exception:  # noqa: BLE001 — telemetry must never fail a context build
        logger.exception("VT-69 L3 telemetry failed (%s)", event_type)


def _quarantine_age_days(tenant_id: UUID, now: datetime) -> int | None:
    """Days since the tenant signed up; None if no signed_up_at on record
    (treated as quarantined — a tenant with no signup date can't be cleared)."""
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT signed_up_at FROM tenants WHERE id = %s", (str(tenant_id),)
        ).fetchone()
    if row is None:
        return None
    signed = dict(row)["signed_up_at"]
    if signed is None:
        return None
    return (now - signed).days


def lookup_pattern(
    tenant_id: UUID | str,
    pattern_type: str,
    cohort_key: str,
    *,
    run_id: UUID | None = None,
    now: datetime | None = None,
) -> L3Pattern | None:
    """Return the L3 prior for (pattern_type, cohort_key), or None.

    None when EITHER the tenant is within the 180-day quarantine (logs
    l3_quarantine_skip) OR no pattern exists for the cohort (logs l3_no_match —
    typically the cohort didn't meet k≥10 at construction). The caller renders a
    structured "no L3 prior" marker on None (NEVER a fabricated default).
    """
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    rid = run_id or uuid4()
    now = now or datetime.now(UTC)

    age = _quarantine_age_days(tid, now)
    if age is None or age < QUARANTINE_DAYS:  # inclusive boundary: exactly 180d is eligible
        _telemetry("l3_quarantine_skip", tid, rid, {
            "pattern_type": pattern_type, "cohort_key": cohort_key,
            "days_remaining": (QUARANTINE_DAYS - age) if age is not None else QUARANTINE_DAYS,
        })
        return None

    with get_pool().connection() as conn:
        row = conn.execute(
            f"SELECT {_COLS} FROM l3_patterns "  # noqa: S608 — _COLS static literal
            "WHERE pattern_type = %s AND cohort_key = %s",
            (pattern_type, cohort_key),
        ).fetchone()

    if row is None:
        _telemetry("l3_no_match", tid, rid, {
            "pattern_type": pattern_type, "cohort_key": cohort_key,
        })
        return None

    _telemetry("l3_query_attempted", tid, rid, {
        "pattern_type": pattern_type, "cohort_key": cohort_key, "result": "hit",
    })
    d = cast("dict[str, Any]", dict(row))
    return L3Pattern(
        id=d["id"], pattern_type=d["pattern_type"], cohort_key=d["cohort_key"],
        n_tenants=d["n_tenants"], n_campaigns=d["n_campaigns"], metrics=d["metrics"] or {},
        confidence_band=d["confidence_band"], constructed_at=d["constructed_at"],
        expires_at=d["expires_at"],
    )


__all__ = ["QUARANTINE_DAYS", "lookup_pattern"]
