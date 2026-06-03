"""VT-53 / VT-6.2 — shared clarifying-question flow (BACKEND).

When VT-52 vision extraction returns a field below the ask threshold (<0.7), the
ingestion path bundles the ambiguous fields (max 3) into ONE clarification and
parks it here. The owner-facing WhatsApp send is VT-9.4 (OUT of scope); this
module owns the substrate + the deterministic reply parsing.

Pillars
    P4 — timeout => DROP the upload (status 'expired'); the original extraction is
      NEVER committed with a guessed value. ``sweep_expired`` only marks rows
      expired; it does not commit anything.
    P7 — the owner's reply is the source of truth; ``record_reply`` writes exactly
      what the owner gave, parsed but not invented.
    P8 — ONE shared flow; ingestion methods do not roll their own.
    P3 — tenant_id is derived from invocation context (never caller-spoofed) and
      threaded to ``tenant_connection`` for RLS (CL-82/88).

Bundling: 1..3 questions per clarification. 4+ => ``TooManyQuestionsError`` and the
caller drops the upload (extraction quality too low to repair via Q&A).

Reply parsing is DETERMINISTIC (Pillar-1-friendly; no LLM for numerics):
Devanagari digits, ASCII digits, ₹/comma stripping, and a small English
number-word grammar (units/teens/tens + hundred/thousand/lakh/crore).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# 7-day owner-response window. Type-2 governance to change (VT-53 rule):
# a shorter window drops more uploads, a longer one holds owner attention.
_CLARIFICATION_TIMEOUT_DAYS = 7

# Max bundled questions per clarification (VT-6). 4+ => drop the upload.
_MAX_QUESTIONS = 3

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

_WORD_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_WORD_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_WORD_SCALES = {"hundred": 100, "thousand": 1000, "lakh": 100000, "lac": 100000,
                "crore": 10000000}
_WORD_SKIP = {"and", "rupees", "rupee", "rs", "only", "inr"}


class TooManyQuestionsError(Exception):
    """Raised when >3 fields are ambiguous — the caller drops the upload."""


class ClarificationQuestion(BaseModel):
    """One ambiguous field + the owner-facing prompt for it."""

    model_config = ConfigDict(frozen=True)

    field: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Deterministic reply parsing (pure — no DB, no LLM)
# ---------------------------------------------------------------------------

def _words_to_int(text: str) -> int | None:
    """Parse an English number phrase ('fifteen hundred') to an int.

    Returns None if any token is not a recognised number word (so a non-numeric
    reply doesn't silently parse to a wrong value — P4).
    """
    tokens = re.findall(r"[a-z]+", text.lower())
    tokens = [t for t in tokens if t not in _WORD_SKIP]
    if not tokens:
        return None
    total = 0
    current = 0
    saw_number = False
    for t in tokens:
        if t in _WORD_UNITS:
            current += _WORD_UNITS[t]
            saw_number = True
        elif t in _WORD_TENS:
            current += _WORD_TENS[t]
            saw_number = True
        elif t in _WORD_SCALES:
            scale = _WORD_SCALES[t]
            saw_number = True
            if scale >= 1000:
                current = (current or 1) * scale
                total += current
                current = 0
            else:  # hundred
                current = (current or 1) * scale
        else:
            return None  # unknown word — not a parseable number
    if not saw_number:
        return None
    value = total + current
    return value if value > 0 else None


def parse_numeric(raw: str) -> int | None:
    """Parse an owner reply to an integer (rupees / count). None if unparseable.

    Handles ASCII digits, Devanagari digits (०-९), ₹/comma/space noise, and
    English number words. NEVER guesses — an unparseable reply returns None so
    the caller can re-ask rather than commit garbage (P4).
    """
    if raw is None:
        return None
    s = raw.translate(_DEVANAGARI_DIGITS)
    cleaned = re.sub(r"[₹,\s]", "", s)
    if re.fullmatch(r"\d+", cleaned):
        return int(cleaned)
    words = _words_to_int(s)
    if words is not None:
        return words
    # Last resort: a leading digit run inside other text ("1500/-").
    m = re.search(r"\d+", cleaned)
    return int(m.group()) if m else None


def parse_amount_to_paise(raw: str) -> int | None:
    """Parse an owner reply for a rupee amount into PAISE (₹1500 -> 150000)."""
    rupees = parse_numeric(raw)
    return None if rupees is None else rupees * 100


# ---------------------------------------------------------------------------
# Persistence (RLS-scoped via tenant_connection)
# ---------------------------------------------------------------------------

def open_clarification(
    tenant_id: UUID | str,
    subject_ref: str,
    questions: list[ClarificationQuestion] | list[dict[str, str]],
    *,
    now: datetime | None = None,
) -> UUID:
    """Persist a bundled clarification (1..3 questions). Returns its id.

    Raises ``TooManyQuestionsError`` if >3 (caller drops the upload) and
    ``ValueError`` if 0. tenant_id is derived from invocation context (P3).
    """
    # Bundling validation runs BEFORE any DB import so it is unit-testable
    # without psycopg (and so a drop-the-upload case never touches the pool).
    qs = [
        q.model_dump() if isinstance(q, ClarificationQuestion) else dict(q)
        for q in questions
    ]
    if len(qs) == 0:
        raise ValueError("open_clarification: no questions")
    if len(qs) > _MAX_QUESTIONS:
        raise TooManyQuestionsError(
            f"{len(qs)} ambiguous fields > max {_MAX_QUESTIONS} — drop the upload"
        )

    from psycopg.types.json import Jsonb

    from orchestrator.db.tenant_connection import tenant_connection

    now = now or datetime.now(UTC)
    expires = now + timedelta(days=_CLARIFICATION_TIMEOUT_DAYS)
    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            """
            INSERT INTO pending_clarifications
                (tenant_id, subject_ref, questions, status, created_at, expires_at)
            VALUES (%s, %s, %s, 'pending', %s, %s)
            RETURNING id
            """,
            (str(tenant_id), subject_ref, Jsonb(qs), now, expires),
        ).fetchone()
    cid = row["id"] if isinstance(row, dict) else row[0]
    logger.info(
        "open_clarification: tenant=%s subject=%s questions=%d id=%s",
        tenant_id, subject_ref, len(qs), cid,
    )
    return cid


def record_reply(
    tenant_id: UUID | str,
    clarification_id: UUID | str,
    resolution: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Resolve a pending clarification with the owner's parsed answers.

    Returns True iff a *pending* row for this tenant was updated (RLS-scoped).
    Idempotent: a second call (already answered/expired) returns False.
    """
    from psycopg.types.json import Jsonb

    from orchestrator.db.tenant_connection import tenant_connection

    now = now or datetime.now(UTC)
    # VT-309: resolve + emit the L2 episodic clarification_resolved ATOMICALLY
    # (the autocommit site the plan flagged; now wrapped per Cowork ruling
    # 20260603T191000Z). Emit only when a pending row was actually resolved.
    with tenant_connection(tenant_id) as conn, conn.transaction():
        cur = conn.execute(
            """
            UPDATE pending_clarifications
               SET resolution = %s, status = 'answered', resolved_at = %s
             WHERE id = %s AND status = 'pending'
            """,
            (Jsonb(resolution), now, str(clarification_id)),
        )
        updated = cur.rowcount
        if updated > 0:
            from orchestrator.knowledge.l2_types import L2EventType
            from orchestrator.knowledge.l2_writer import (
                deterministic_event_id,
                record_episodic_event,
            )

            record_episodic_event(
                tenant_id,
                L2EventType.CLARIFICATION_RESOLVED,
                payload={
                    "clarification_id": str(clarification_id),
                    "decision": "answered",
                },
                referenced_entity_type="clarification",
                referenced_entity_id=clarification_id,
                event_id=deterministic_event_id(
                    tenant_id, L2EventType.CLARIFICATION_RESOLVED, clarification_id
                ),
                conn=conn,
            )
    logger.info(
        "record_reply: tenant=%s id=%s updated=%d", tenant_id, clarification_id, updated
    )
    return updated > 0


def sweep_expired(tenant_id: UUID | str, *, now: datetime | None = None) -> int:
    """Mark this tenant's overdue pending clarifications 'expired'. Returns count.

    P4: this ONLY marks expiry — the original extraction is dropped by the
    caller; nothing is committed with a guessed value.
    """
    from orchestrator.db.tenant_connection import tenant_connection

    now = now or datetime.now(UTC)
    with tenant_connection(tenant_id) as conn:
        cur = conn.execute(
            """
            UPDATE pending_clarifications
               SET status = 'expired'
             WHERE status = 'pending' AND expires_at <= %s
            """,
            (now,),
        )
        n = cur.rowcount
    logger.info("sweep_expired: tenant=%s expired=%d", tenant_id, n)
    return n


__all__ = [
    "ClarificationQuestion",
    "TooManyQuestionsError",
    "open_clarification",
    "parse_amount_to_paise",
    "parse_numeric",
    "record_reply",
    "sweep_expired",
]
