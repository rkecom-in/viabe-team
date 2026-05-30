"""VT-48 — schedule_followup standalone tool.

Enqueues a future orchestrator invocation, idempotent on a stable
(tenant_id, follow_up_key). Standalone callable (NOT wired to an Agent
yet). Pillar 1: the tool only WRITES the row; the scheduler (VT-3.5,
out of scope) polls + fires. Pillar 8: idempotency via the composite
UNIQUE — a second identical key returns `duplicate_key` + the existing
fire_at, no second row.

`cancel_if` conditions are stored as JSONB for the scheduler's
deterministic evaluator; this tool only validates parseability against
the Phase-1 grammar, it never evaluates them (no LLM, Pillar 1).

NO PII (CL-390): the tool logs tenant_id + follow_up_type + status only;
payload contents are never logged.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_MIN_LEAD = timedelta(minutes=30)
_MAX_LEAD = timedelta(days=90)
_MAX_PAYLOAD_BYTES = 4096

# Phase-1 cancel-condition grammar. Prefix before ':' must be one of these.
_CANCEL_PREFIXES = frozenset(
    {"campaign_status_in", "customer_opt_out_status", "phase_in"}
)

FollowUpType = Literal[
    "campaign_followup", "attribution_check", "reengagement_reminder", "other"
]


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class ScheduleFollowupInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., min_length=1)
    follow_up_type: FollowUpType
    fire_at: datetime
    follow_up_key: str = Field(..., min_length=1, max_length=240)
    payload: dict[str, Any] = Field(default_factory=dict)
    cancel_if: list[str] | None = None
    run_id_origin: str | None = None


class ScheduleFollowupOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["scheduled", "duplicate_key", "error"]
    scheduled_id: str | None = None
    existing_fire_at: datetime | None = None
    error_envelope: ErrorEnvelope | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate(payload: ScheduleFollowupInput) -> ErrorEnvelope | None:
    fire_at = payload.fire_at
    if fire_at.tzinfo is None:
        fire_at = fire_at.replace(tzinfo=timezone.utc)
    delta = fire_at - _now()
    if delta < _MIN_LEAD or delta > _MAX_LEAD:
        return ErrorEnvelope(
            code="invalid_fire_at",
            message="fire_at must be between 30 minutes and 90 days from now",
        )
    serialized = json.dumps(payload.payload, separators=(",", ":")).encode("utf-8")
    if len(serialized) > _MAX_PAYLOAD_BYTES:
        return ErrorEnvelope(
            code="payload_too_large",
            message=f"payload {len(serialized)}B exceeds {_MAX_PAYLOAD_BYTES}B",
        )
    if payload.cancel_if is not None:
        for cond in payload.cancel_if:
            prefix = cond.split(":", 1)[0].strip()
            if prefix not in _CANCEL_PREFIXES:
                return ErrorEnvelope(
                    code="invalid_cancel_condition",
                    message=f"unparseable cancel condition: {prefix!r}",
                )
    return None


def schedule_followup(
    payload: ScheduleFollowupInput,
    *,
    pool: Any | None = None,
) -> ScheduleFollowupOutput:
    """Insert one idempotent scheduled_followups row.

    Idempotent on (tenant_id, follow_up_key) via ON CONFLICT DO NOTHING.
    Never raises into the caller — schema-absent / DB errors surface as
    an `error` envelope. RLS via SET LOCAL app.current_tenant.
    """
    err = _validate(payload)
    if err is not None:
        return ScheduleFollowupOutput(status="error", error_envelope=err)

    if pool is None:
        from orchestrator.graph import get_pool

        pool = get_pool()

    cancel_json = (
        json.dumps(payload.cancel_if) if payload.cancel_if is not None else None
    )
    payload_json = json.dumps(payload.payload)

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SET LOCAL app.current_tenant = %s", (payload.tenant_id,)
                )
                cur.execute(
                    """
                    INSERT INTO scheduled_followups
                        (tenant_id, run_id_origin, follow_up_type,
                         follow_up_key, fire_at, payload, cancel_if)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                    ON CONFLICT (tenant_id, follow_up_key) DO NOTHING
                    RETURNING id
                    """,
                    (
                        payload.tenant_id, payload.run_id_origin,
                        payload.follow_up_type, payload.follow_up_key,
                        payload.fire_at, payload_json, cancel_json,
                    ),
                )
                row = cur.fetchone()
                if row is not None:
                    new_id = row["id"] if isinstance(row, dict) else row[0]
                    logger.info(
                        "schedule_followup: scheduled tenant=%s type=%s",
                        payload.tenant_id, payload.follow_up_type,
                    )
                    return ScheduleFollowupOutput(
                        status="scheduled", scheduled_id=str(new_id)
                    )
                # Conflict: row already exists for this key. Re-read fire_at.
                cur.execute(
                    """
                    SELECT id, fire_at FROM scheduled_followups
                    WHERE tenant_id = %s AND follow_up_key = %s
                    LIMIT 1
                    """,
                    (payload.tenant_id, payload.follow_up_key),
                )
                existing = cur.fetchone()
                if existing is None:
                    return ScheduleFollowupOutput(
                        status="error",
                        error_envelope=ErrorEnvelope(
                            code="conflict_unresolved",
                            message="ON CONFLICT fired but existing row not found",
                        ),
                    )
                ex_id = existing["id"] if isinstance(existing, dict) else existing[0]
                ex_fire = existing["fire_at"] if isinstance(existing, dict) else existing[1]
                logger.info(
                    "schedule_followup: duplicate_key tenant=%s type=%s",
                    payload.tenant_id, payload.follow_up_type,
                )
                return ScheduleFollowupOutput(
                    status="duplicate_key",
                    scheduled_id=str(ex_id),
                    existing_fire_at=ex_fire,
                )
    except Exception as exc:  # noqa: BLE001
        # Schema-absent (migration not applied) or transient DB error.
        # Never raise; honest error envelope.
        logger.info(
            "schedule_followup: db error tenant=%s (%s)",
            payload.tenant_id, type(exc).__name__,
        )
        return ScheduleFollowupOutput(
            status="error",
            error_envelope=ErrorEnvelope(
                code="db_error", message=type(exc).__name__
            ),
        )


__all__ = [
    "ErrorEnvelope",
    "ScheduleFollowupInput",
    "ScheduleFollowupOutput",
    "schedule_followup",
]
