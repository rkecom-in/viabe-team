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
    # VT-755 — `delivered_at IS NOT NULL` on EVERY branch. A question the owner never received cannot
    # be the thing they just answered, so binding their message to it destroys their actual intent: on
    # dev 2026-08-14 the owner's "haan theek hai, bhej do unhe" (send it to them) was recorded as the
    # answer to a question that appears nowhere in conversation_log. This is defence in depth — the
    # primary gate is `get_open`, which stops the turn routing to `answer_pending` at all — because the
    # swallow happens in TWO places and closing only one leaves the other live.
    _delivered = " AND delivered_at IS NOT NULL"
    if question_id is not None:
        sql = ("SELECT id FROM pending_questions "
               f"WHERE tenant_id = %s AND id = %s AND status = 'open'{_delivered}")
        params: tuple[Any, ...] = (str(tenant_id), str(question_id))
    elif task_id is not None:
        sql = ("SELECT id FROM pending_questions "
               f"WHERE tenant_id = %s AND task_id = %s AND status = 'open'{_delivered} "
               "ORDER BY asked_at DESC LIMIT 1")
        params = (str(tenant_id), str(task_id))
    else:
        sql = ("SELECT id FROM pending_questions "
               f"WHERE tenant_id = %s AND status = 'open'{_delivered} ORDER BY asked_at DESC LIMIT 1")
        params = (str(tenant_id),)
    row = conn.execute(sql, params).fetchone()
    return _uuid(row) if row is not None else None


def get_latest_answered(tenant_id: UUID | str, task_id: UUID | str) -> dict[str, Any] | None:
    """VT-611 pre-work #6 — the most-recently-ANSWERED question for this task, or None.

    ``get_open`` cannot serve this: ``correlate_reply`` already flipped the row's status away
    from 'open' the moment the owner answered. This is the read the resumed-dispatch caller needs
    to thread the owner's ``answer_text`` (+ the question it answered) into the re-dispatch
    context — without it, ``manager_task_workflow``'s ask_owner-resume path only had the step's
    ORIGINAL stored situation/desired_outcome to redispatch with, so the specialist never saw what
    the owner just said and re-asked the same thing. ``ORDER BY answered_at DESC LIMIT 1`` picks
    the LATEST answer if a task asked (and got answered) more than once across its lifetime."""
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT id, question_text, answer_text, answered_at FROM pending_questions "
            "WHERE tenant_id = %s AND task_id = %s AND status = 'answered' "
            "ORDER BY answered_at DESC LIMIT 1",
            (str(tenant_id), str(task_id)),
        ).fetchone()
    return dict(row) if row is not None else None


def get_open(
    tenant_id: UUID | str,
    *,
    task_id: UUID | str | None = None,
    include_undelivered: bool = False,
) -> list[dict[str, Any]]:
    """Open questions for the tenant (or one task). **Delivered ones only, by default.**

    VT-755 — the default excludes `delivered_at IS NULL`, and that is the load-bearing part. This
    function is what `triage_seam` reads to decide whether a turn routes to `answer_pending`, so an
    UNDELIVERED question here causes the owner's message to be consumed as its answer. Measured on dev
    2026-08-14: the owner said *"haan theek hai, bhej do unhe"* ("yes fine, send it to them"), it was
    bound to a question that appears nowhere in `conversation_log`, and their instruction was discarded
    as clarification. `pending_questions` has no emitter, so **every** question is currently
    undelivered — which means this default makes owner messages fall through to normal dispatch, where
    a send instruction is read as a send instruction. That is the correct behaviour while the ask
    cannot be delivered.

    ``include_undelivered=True`` is for the seam that will eventually SEND these — an emitter must be
    able to see the un-sent ones — and for operator/forensic reads. It must never be used by a caller
    deciding whether the owner has a question to answer.
    """
    delivered_clause = "" if include_undelivered else " AND delivered_at IS NOT NULL"
    cols = (
        "SELECT id, task_id, question_kind, question_text, status, asked_at, expires_at, delivered_at "
        "FROM pending_questions WHERE tenant_id = %s"
    )
    with tenant_connection(tenant_id) as conn:
        if task_id is not None:
            rows = conn.execute(
                f"{cols} AND task_id = %s AND status = 'open'{delivered_clause} ORDER BY asked_at",
                (str(tenant_id), str(task_id)),
            ).fetchall()
        else:
            rows = conn.execute(
                f"{cols} AND status = 'open'{delivered_clause} ORDER BY asked_at",
                (str(tenant_id),),
            ).fetchall()
    return [dict(r) for r in rows]


def mark_delivered(tenant_id: UUID | str, question_id: UUID | str) -> bool:
    """VT-755 — stamp a question as actually EMITTED to the owner. Returns True iff a row flipped.

    The emission path calls this AFTER the send succeeds, never before: `delivered_at` is the fact that
    makes a question answerable, so stamping it optimistically would recreate the defect it exists to
    close. Idempotent — a second call on an already-stamped row is a no-op returning False.
    """
    with tenant_connection(tenant_id) as conn, conn.transaction():
        cur = conn.execute(
            "UPDATE pending_questions SET delivered_at = now(), updated_at = now() "
            "WHERE tenant_id = %s AND id = %s AND delivered_at IS NULL",
            (str(tenant_id), str(question_id)),
        )
    return bool(getattr(cur, "rowcount", 0))


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
    """Redact PII from an owner-facing question/answer WITHOUT destroying the text.

    VT-755 — ``hash_long_body=False`` is load-bearing, not a relaxation. The default replaces any
    string over ``_LONG_BODY_THRESHOLD`` (200) chars **wholesale** with a ``<body:hash:…>`` token, a
    rule that exists to bound LOG and span size. A pending question is not a log line: **it is text
    destined for the owner.** Measured on deployed dev 2026-08-14, **3 of 4 open questions had been
    stored as `<body:hash:…>`** — unsendable at rest, so the ask could never be delivered even once it
    has a delivery path. The two questions that survived intact were simply under 200 chars.

    This is the SAME opt-out VT-632 established for owner-facing sends (``reply_to_owner.py:85``,
    ``embeddings.py:100``) and whose rationale ``_redact_str``'s own docstring already states: *"Owner
    facing sends pass False … the pattern + registry substitutions above are the PII protection there,
    not the whole-body hash."* ``pending_questions`` was an owner-facing send that never got the flag.

    **PII protection is UNCHANGED**: PAN, IFSC, GSTIN, email, Luhn-validated cards, Aadhaar, E.164 and
    Indian-10-digit phones are all still substituted, and the customer-name registry still applies.
    Only the whole-body hash is skipped. CL-390 holds — nothing raw is persisted.
    """
    out = redact(text, hash_long_body=False)
    return out if isinstance(out, str) else str(out)


__all__ = ["QUESTION_KINDS", "ask", "correlate_reply", "get_open", "get_latest_answered", "expire_stale"]
