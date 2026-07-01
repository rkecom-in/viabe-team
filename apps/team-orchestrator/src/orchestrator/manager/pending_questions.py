"""VT-527 (B4) — the manager's generic owner-clarification mechanism.

Ask an arbitrary clarifying/confirming question, correlate the owner's reply back (redelivery-safe
via ``last_message_sid``, terminal-safe — the first answer wins), and expire stale questions past
their TTL. This is what B3's CLARIFY decision reaches for; it is deliberately NOT the
onboarding journey (which is singular-per-tenant and reset-on-restart).

All free text is PII-redacted at write. All access is tenant-scoped via ``tenant_connection``
(RLS-enforced). The expiry sweep runs service-role + best-effort (the pending_approvals /
orphan_reaper shape).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from orchestrator.db import tenant_connection
from orchestrator.privacy.pii_redactor import redact

logger = logging.getLogger(__name__)

QUESTION_KINDS = frozenset({"clarification", "confirmation", "business_fact"})


def _uuid(row: Any) -> UUID:
    val = row["id"] if isinstance(row, dict) else row[0]
    return val if isinstance(val, UUID) else UUID(str(val))


def ask(
    tenant_id: UUID | str,
    question_text: str,
    *,
    task_id: UUID | str | None = None,
    run_id: UUID | str | None = None,
    question_kind: str = "clarification",
    expires_at: Any = None,
) -> UUID:
    """Open a pending question (or return the existing OPEN one for the same task — a task holds
    at most one open clarification). ``question_text`` is redacted before insert."""
    if question_kind not in QUESTION_KINDS:
        raise ValueError(f"unknown question_kind {question_kind!r}")
    with tenant_connection(tenant_id) as conn, conn.transaction():
        conn.execute("SELECT id FROM tenants WHERE id = %s FOR UPDATE", (str(tenant_id),)).fetchone()
        if task_id is not None:
            existing = conn.execute(
                "SELECT id FROM pending_questions "
                "WHERE tenant_id = %s AND task_id = %s AND status = 'open'",
                (str(tenant_id), str(task_id)),
            ).fetchone()
            if existing is not None:
                return _uuid(existing)
        row = conn.execute(
            "INSERT INTO pending_questions "
            "(tenant_id, task_id, run_id, question_kind, question_text, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                str(tenant_id),
                str(task_id) if task_id is not None else None,
                str(run_id) if run_id is not None else None,
                question_kind,
                _redact_text(question_text),
                expires_at,
            ),
        ).fetchone()
    return _uuid(row)


def correlate_reply(
    tenant_id: UUID | str,
    answer_text: str,
    message_sid: str | None,
    *,
    question_id: UUID | str | None = None,
    task_id: UUID | str | None = None,
) -> UUID | None:
    """Record the owner's reply against an OPEN question → ``answered``.

    Target selection: explicit ``question_id``, else the open question for ``task_id``, else the
    most-recent open question for the tenant. Redelivery-safe: a reply whose ``message_sid`` already
    landed on that question is a no-op (returns the question id). Terminal-safe: only an ``open``
    row flips (first answer wins). Returns the answered question id, or None if nothing matched.
    """
    with tenant_connection(tenant_id) as conn, conn.transaction():
        # Redelivery guard (journey pattern): this exact reply (message_sid) already landed on a
        # question → idempotent no-op success, regardless of that question's current status. Catches
        # a redelivered Twilio webhook whether or not the first delivery already answered.
        if message_sid:
            dup = conn.execute(
                "SELECT id FROM pending_questions WHERE tenant_id = %s AND last_message_sid = %s "
                "LIMIT 1",
                (str(tenant_id), message_sid),
            ).fetchone()
            if dup is not None:
                return _uuid(dup)
        qid = _select_open_question(conn, tenant_id, question_id=question_id, task_id=task_id)
        if qid is None:
            return None  # nothing open to answer (terminal-safe — first answer already won)
        conn.execute(
            "UPDATE pending_questions SET status = 'answered', answer_text = %s, "
            "last_message_sid = %s, answered_at = now(), updated_at = now() "
            "WHERE tenant_id = %s AND id = %s AND status = 'open'",
            (_redact_text(answer_text), message_sid, str(tenant_id), str(qid)),
        )
    return qid


def _select_open_question(
    conn: Any,
    tenant_id: UUID | str,
    *,
    question_id: UUID | str | None,
    task_id: UUID | str | None,
) -> UUID | None:
    if question_id is not None:
        sql = ("SELECT id FROM pending_questions "
               "WHERE tenant_id = %s AND id = %s AND status = 'open'")
        params: tuple[Any, ...] = (str(tenant_id), str(question_id))
    elif task_id is not None:
        sql = ("SELECT id FROM pending_questions "
               "WHERE tenant_id = %s AND task_id = %s AND status = 'open' "
               "ORDER BY asked_at DESC LIMIT 1")
        params = (str(tenant_id), str(task_id))
    else:
        sql = ("SELECT id FROM pending_questions "
               "WHERE tenant_id = %s AND status = 'open' ORDER BY asked_at DESC LIMIT 1")
        params = (str(tenant_id),)
    row = conn.execute(sql, params).fetchone()
    return _uuid(row) if row is not None else None


def get_open(tenant_id: UUID | str, *, task_id: UUID | str | None = None) -> list[dict[str, Any]]:
    with tenant_connection(tenant_id) as conn:
        if task_id is not None:
            rows = conn.execute(
                "SELECT id, task_id, question_kind, question_text, status, asked_at, expires_at "
                "FROM pending_questions WHERE tenant_id = %s AND task_id = %s AND status = 'open' "
                "ORDER BY asked_at",
                (str(tenant_id), str(task_id)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, task_id, question_kind, question_text, status, asked_at, expires_at "
                "FROM pending_questions WHERE tenant_id = %s AND status = 'open' ORDER BY asked_at",
                (str(tenant_id),),
            ).fetchall()
    return [dict(r) for r in rows]


def expire_stale(*, pool: Any = None) -> int:
    """Sweep OPEN questions past ``expires_at`` → ``expired`` (cross-tenant, service-role,
    best-effort, never raises — the pending_approvals / orphan_reaper shape)."""
    try:
        active = pool
        if active is None:
            from orchestrator.graph import get_pool

            active = get_pool()
        with active.connection() as conn:
            rows = conn.execute(
                "UPDATE pending_questions SET status = 'expired', updated_at = now() "
                "WHERE status = 'open' AND expires_at IS NOT NULL AND expires_at <= now() "
                "RETURNING id",
            ).fetchall()
        n = len(rows)
        if n:
            logger.info("VT-527 pending_questions: expired %d stale open question(s)", n)
        return n
    except Exception:  # noqa: BLE001 — best-effort sweep; must never raise into a scheduler tick
        logger.warning("VT-527 pending_questions expiry sweep failed (best-effort)", exc_info=True)
        return 0


def _redact_text(text: str) -> str:
    out = redact(text)
    return out if isinstance(out, str) else str(out)


__all__ = ["QUESTION_KINDS", "ask", "correlate_reply", "get_open", "expire_stale"]
