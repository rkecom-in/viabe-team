"""VT-57/58/59 + VT-275 — raw-import write path (the two-surface model).

The RAW import surface that complements the clean attributed ledger (VT-273
customer_ledger_entries). Cowork APPROVED + plan-reviewed 2026-06-01.

``record_imported_transactions`` is the single write seam the import methods
(UPI / KOT-POS / cash-book) route through:
  - EVERY row → an ``imported_transactions`` row, idempotent on
    UNIQUE(tenant_id, source, provider_ref) (re-import = no-op).
  - ATTRIBUTED rows (customer_id resolved by the method) that are a positive
    transaction (direction='credit') ALSO write a clean
    ``customer_ledger_entries`` row — reusing ``ledger.record_ledger_entries``
    (single-sourced; no duplicated ledger SQL, no parallel idempotency).
  - UNATTRIBUTED rows (customer_id None — the common POS/UPI case) live ONLY in
    imported_transactions until ``match_transactions`` (VT-275) attributes them.

N1 — REFUNDS (Cowork plan note): direction='debit' (refund/return) is RETAINED in
  imported_transactions (real attribution signal) but NOT promoted to the ledger:
  customer_ledger_entries.entry_type is (sale|payment) with NO refund/debit type
  yet (a ledger-schema follow-up). So a debit parks raw, awaiting that.
N2 — NO DOUBLE-COUNT (Cowork plan note): the ledger write fires ONLY for rows
  NEWLY inserted this call (raw rowcount>0); a re-import skips the raw row AND the
  ledger write. The ledger's own entry_key idempotency (061) is the second net.

Pillars: P3 tenant_id from invocation context → tenant_connection (RLS, CL-82/88).
P8 source validated against the single-source VT-54 ACQUIRED_VIA enum; ledger
write delegated to the single-source ledger module. CL-417 per-field columns.
CL-422 dev = synthetic only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.integrations.dedup_merge import ACQUIRED_VIA, AcquiredViaError
from orchestrator.integrations.ledger import LedgerEntryIn, record_ledger_entries

logger = logging.getLogger(__name__)

Direction = Literal["credit", "debit"]
EntryType = Literal["sale", "payment"]


class ImportedTxnIn(BaseModel):
    """One raw imported transaction row.

    ``customer_id`` None = unattributed (awaits VT-275). ``direction`` is the raw
    surface's axis (credit = money in / debit = refund). ``entry_type`` is the
    ledger axis used ONLY when an attributed credit is promoted (defaults to the
    'sale' convention the image methods already use). ``confidence`` gates that
    ledger write (P4: <0.7 deferred, not committed).
    """

    model_config = ConfigDict(frozen=True)

    provider_ref: str = Field(..., min_length=1)
    amount_paise: int = Field(..., ge=0)
    txn_date: date
    direction: Direction
    customer_id: UUID | None = None
    entry_type: EntryType = "sale"
    confidence: float = Field(0.95, ge=0.0, le=1.0)
    notes: str | None = None


@dataclass(frozen=True)
class RecordImportResult:
    written: int                 # raw rows newly inserted
    skipped_duplicate: int       # raw rows that were a re-import (no-op)
    attributed_ledger_written: int  # clean ledger rows also written (attributed credits)
    deferred_low_confidence: int    # attributed credits whose ledger write deferred (<0.7)


def record_imported_transactions(
    tenant_id: UUID | str,
    rows: list[ImportedTxnIn],
    *,
    acquired_via: str,
) -> RecordImportResult:
    """Persist raw imports (idempotent) + promote attributed credits to the ledger.

    ``acquired_via`` is the import source; stored as ``imported_transactions.source``
    and validated against the VT-54 enum (raises ``AcquiredViaError`` otherwise).
    tenant_id from invocation context (P3). Returns counts only (no PII, CL-390).
    """
    if acquired_via not in ACQUIRED_VIA:
        raise AcquiredViaError(
            f"unknown acquired_via {acquired_via!r} — not in the VT-6 enum"
        )

    from orchestrator.db.tenant_connection import tenant_connection

    written = skipped = 0
    # Rows NEWLY inserted this call that are attributed positive transactions —
    # the only ones promoted to the ledger (N2: re-imports never re-promote).
    to_ledger: list[ImportedTxnIn] = []
    with tenant_connection(tenant_id) as conn:
        for row in rows:
            # Attributed AT IMPORT (customer_id set via a strong VPA/phone signal,
            # and promoted to the ledger below) = 'confirmed'; else 'unattributed'
            # (the VT-275 bridge may later set 'tentative'). N3.
            status = "confirmed" if row.customer_id is not None else "unattributed"
            cur = conn.execute(
                """
                INSERT INTO imported_transactions
                    (tenant_id, customer_id, source, provider_ref, amount_paise,
                     txn_date, direction, notes, attribution_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, source, provider_ref) DO NOTHING
                """,
                (
                    str(tenant_id),
                    str(row.customer_id) if row.customer_id is not None else None,
                    acquired_via, row.provider_ref, row.amount_paise,
                    row.txn_date, row.direction, row.notes, status,
                ),
            )
            if cur.rowcount and cur.rowcount > 0:
                written += 1
                if row.customer_id is not None and row.direction == "credit":
                    to_ledger.append(row)  # N1: debits NOT promoted (no ledger type)
            else:
                skipped += 1  # ON CONFLICT → idempotent no-op (re-import)

    # Promote attributed credits to the clean ledger via the single-source writer.
    ledger_written = deferred = 0
    for row in to_ledger:
        assert row.customer_id is not None  # guarded above (mypy narrowing)
        res = record_ledger_entries(
            tenant_id, row.customer_id,
            [LedgerEntryIn(
                amount_paise=row.amount_paise, entry_type=row.entry_type,
                entry_date=row.txn_date, confidence=row.confidence, notes=row.notes,
            )],
            acquired_via=acquired_via,
        )
        ledger_written += res.written
        deferred += res.deferred_low_confidence

    logger.info(
        "record_imported_transactions: tenant=%s source=%s rows=%d written=%d "
        "dup=%d ledger=%d deferred=%d",
        tenant_id, acquired_via, len(rows), written, skipped, ledger_written, deferred,
    )
    return RecordImportResult(
        written=written, skipped_duplicate=skipped,
        attributed_ledger_written=ledger_written, deferred_low_confidence=deferred,
    )


__all__ = [
    "Direction",
    "EntryType",
    "ImportedTxnIn",
    "RecordImportResult",
    "record_imported_transactions",
]
