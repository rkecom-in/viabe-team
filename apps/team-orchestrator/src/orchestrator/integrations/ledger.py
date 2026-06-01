"""VT-273 / VT-6.3 — customer ledger write path (transaction history).

The ingestion methods (VT-55..63), after resolving a customer via
dedup_and_merge, persist that customer's transactions here. Idempotent:
re-photographing the same ledger does NOT duplicate rows.

Pillars / decisions
    P3 — tenant_id from invocation context, threaded to tenant_connection (RLS,
      CL-82/88); never taken from the extracted row.
    P4 — an ask-level entry (<0.7) is NOT written; it is returned as
      deferred_low_confidence for the caller to route to the VT-53 clarification
      flow — never committed with a guess.
    P8 — confidence thresholds single-sourced from field_mapping._route; the
      acquired_via enum single-sourced from dedup_merge.ACQUIRED_VIA. No parallel
      logic. CL-417: per-field columns, no JSONB envelope.

Idempotency (+ its known limitation): entry_key = sha256(tenant:customer:date:
amount:type); INSERT ON CONFLICT (tenant_id, entry_key) DO NOTHING. Two genuinely
separate identical entries collapse to one (under-count) — accepted Phase-1
default (VT-274 tracks future disambiguation); re-ingest double-count is the worse
failure this prevents.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.integrations.dedup_merge import ACQUIRED_VIA, AcquiredViaError
from orchestrator.integrations.field_mapping import _route

logger = logging.getLogger(__name__)

EntryType = Literal["sale", "payment"]


class LedgerEntryIn(BaseModel):
    """One extracted transaction destined for the ledger."""

    model_config = ConfigDict(frozen=True)

    amount_paise: int = Field(..., ge=0)
    entry_type: EntryType
    entry_date: date
    confidence: float = Field(..., ge=0.0, le=1.0)
    notes: str | None = None


@dataclass(frozen=True)
class RecordResult:
    written: int
    skipped_duplicate: int
    deferred_low_confidence: int


def _entry_key(
    tenant_id: UUID | str, customer_id: UUID | str, entry: LedgerEntryIn
) -> str:
    """Deterministic idempotency key (see module header for the known limitation)."""
    raw = f"{tenant_id}:{customer_id}:{entry.entry_date.isoformat()}:{entry.amount_paise}:{entry.entry_type}"
    return hashlib.sha256(raw.encode()).hexdigest()


def record_ledger_entries(
    tenant_id: UUID | str,
    customer_id: UUID | str,
    entries: list[LedgerEntryIn],
    *,
    acquired_via: str,
) -> RecordResult:
    """Persist ``entries`` for ``customer_id``. Idempotent + confidence-gated.

    Raises ``AcquiredViaError`` if acquired_via is not a VT-54 method. tenant_id
    from invocation context (P3). An ask-level entry (<0.7) is deferred, not
    written (P4 → caller routes to VT-53).
    """
    if acquired_via not in ACQUIRED_VIA:
        raise AcquiredViaError(
            f"unknown acquired_via {acquired_via!r} — not in the VT-6 enum"
        )

    from psycopg.types.json import Jsonb  # noqa: F401 — ensure psycopg present

    from orchestrator.db.tenant_connection import tenant_connection

    written = skipped = deferred = 0
    with tenant_connection(tenant_id) as conn:
        for entry in entries:
            if _route(entry.confidence) == "ask_owner":
                deferred += 1
                continue
            cur = conn.execute(
                """
                INSERT INTO customer_ledger_entries
                    (tenant_id, customer_id, amount_paise, entry_type, entry_date,
                     notes, acquired_via, source_confidence, entry_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, entry_key) DO NOTHING
                """,
                (
                    str(tenant_id), str(customer_id), entry.amount_paise,
                    entry.entry_type, entry.entry_date, entry.notes, acquired_via,
                    entry.confidence, _entry_key(tenant_id, customer_id, entry),
                ),
            )
            if cur.rowcount and cur.rowcount > 0:
                written += 1
            else:
                skipped += 1  # ON CONFLICT → idempotent no-op (re-ingest)
    logger.info(
        "record_ledger_entries: tenant=%s customer=%s written=%d dup=%d deferred=%d",
        tenant_id, customer_id, written, skipped, deferred,
    )
    return RecordResult(written=written, skipped_duplicate=skipped,
                        deferred_low_confidence=deferred)


def resolve_customer_by_phone_token(
    tenant_id: UUID | str, phone_token: str
) -> UUID | None:
    """Resolve a customer_id from a phone_token (the VT-258 read seam).

    No ``customers.phone_token`` column exists (the stale assumption); the link is
    ``phone_token_resolutions.customer_id``, which dedup_and_merge populates on
    insert. Reads via the DB-owner pool + an EXPLICIT ``tenant_id`` filter — the
    established phone_token_resolutions access pattern (that table grants to the
    owner role, not app_role; ``tenant_connection``/app_role lacks SELECT on it).
    Tenant-scoped by the WHERE clause (P3): a foreign tenant matches no row → None.
    """
    from orchestrator.graph import get_pool

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('app.current_tenant', %s, false)", (str(tenant_id),)
        )
        cur.execute(
            "SELECT customer_id FROM phone_token_resolutions "
            "WHERE phone_token = %s AND tenant_id = %s",
            (phone_token, str(tenant_id)),
        )
        row = cur.fetchone()
    if row is None:
        return None
    cid = row["customer_id"] if isinstance(row, dict) else row[0]
    return UUID(str(cid)) if cid is not None else None


__all__ = [
    "EntryType",
    "LedgerEntryIn",
    "RecordResult",
    "record_ledger_entries",
    "resolve_customer_by_phone_token",
]
