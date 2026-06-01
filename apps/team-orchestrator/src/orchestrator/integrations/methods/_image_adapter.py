"""VT-55 — shared image-ingestion adapter (the pattern VT-55/58/59 reuse).

THIN orchestration over the 3 primitives — adds NO extraction/threshold/dedup
logic of its own (Pillar 8):

  extract_entries_from_image (consent-gated, fail-closed)
    → per ENTRY: route by min field confidence (field_mapping._route)
        · any field <0.7  → bundle the low-conf fields → clarifying_flow
                            (≤3 questions, else drop the entry)               [P4]
        · else            → dedup_and_merge (identity: name/phone)
                            + record_ledger_entries (transactions)            [F2]

tenant_id from invocation context (P3). acquired_via from the VT-54 enum. CL-422:
synthetic only. Confidence thresholds single-sourced from _route.

Field contract (shared across image ledger methods): customer_name, phone
(identity → dedup_and_merge); amount, entry_date (transaction → ledger, parsed
with the VT-53 deterministic parser). Spend/notes beyond this map cleanly when a
method needs them; the adapter ignores fields it isn't told to commit.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from orchestrator.integrations.clarifying_flow import (
    ClarificationQuestion,
    TooManyQuestionsError,
    open_clarification,
    parse_amount_to_paise,
)
from orchestrator.integrations.dedup_merge import dedup_and_merge
from orchestrator.integrations.field_mapping import _route
from orchestrator.integrations.imported_transactions import (
    ImportedTxnIn,
    record_imported_transactions,
)
from orchestrator.integrations.ledger import LedgerEntryIn, record_ledger_entries
from orchestrator.integrations.vision_extraction import (
    ExtractedField,
    ExtractionResult,
    extract_entries_from_image,
)

logger = logging.getLogger(__name__)

# Identity + transaction field names the image methods extract.
IDENTITY_FIELDS = ("customer_name", "phone")
_TXN_AMOUNT = "amount"
_TXN_DATE = "entry_date"
TARGET_FIELDS = [*IDENTITY_FIELDS, _TXN_AMOUNT, _TXN_DATE]


@dataclass(frozen=True)
class IngestionSummary:
    """Counts only — NO PII (CL-390)."""

    entries_extracted: int
    committed: int
    pending_clarification: int
    dropped: int
    # Unattributed rows PARKED in imported_transactions for later VT-275
    # attribution (only when the caller opts in via park_unattributed — POS/UPI).
    parked: int = 0


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except (ValueError, TypeError):
        return None


def _fallback_provider_ref(
    acquired_via: str, amount_paise: int, entry_date: date, idx: int
) -> str:
    """Deterministic provider_ref when the source row carries no stable id.

    A re-import of the SAME file → same row order → same ref → idempotent. Two
    genuinely-identical rows in one file collapse (accepted, same posture as the
    ledger entry_key limitation). A source WITH a real id (bill_number / UPI ref)
    maps it to the 'provider_ref' field and never reaches this fallback.
    """
    h = hashlib.sha256(
        f"{amount_paise}:{entry_date.isoformat()}:{idx}".encode()
    ).hexdigest()[:16]
    return f"{acquired_via}:{h}"


def _to_imported_txn(
    by_name: dict[str, ExtractedField], acquired_via: str, idx: int, now: datetime
) -> ImportedTxnIn | None:
    """Build an unattributed ImportedTxnIn from a no-anchor entry (None if no amount)."""
    amount_f = by_name.get(_TXN_AMOUNT)
    if not (amount_f and amount_f.value):
        return None
    paise = parse_amount_to_paise(amount_f.value)
    if paise is None:
        return None
    date_f = by_name.get(_TXN_DATE)
    entry_date = _parse_date(date_f.value if date_f else None) or now.date()
    ref_f = by_name.get("provider_ref")
    provider_ref = (
        ref_f.value if (ref_f and ref_f.value)
        else _fallback_provider_ref(acquired_via, paise, entry_date, idx)
    )
    return ImportedTxnIn(
        provider_ref=provider_ref, amount_paise=paise, txn_date=entry_date,
        direction="credit", confidence=amount_f.confidence,
    )


def ingest_image(
    tenant_id: UUID | str,
    image_bytes: bytes,
    *,
    acquired_via: str,
    media_type: str = "image/jpeg",
    now: datetime | None = None,
    park_unattributed: bool = False,
    extract_fn: Callable[..., list[ExtractionResult]] = extract_entries_from_image,
    **extract_kwargs: object,
) -> IngestionSummary:
    """Ingest a multi-entry image (paper ledger photo, etc.). Returns counts only.

    ``extract_fn`` is injectable for tests (defaults to the real vision primitive);
    extra kwargs (client / consent_check / model) pass through to it.
    ``park_unattributed`` forwards to ingest_entries (POS/UPI: no-anchor rows →
    imported_transactions rather than dropped).
    """
    now = now or datetime.now(UTC)
    results = extract_fn(
        image_bytes, tenant_id=tenant_id, target_fields=TARGET_FIELDS,
        acquired_via=acquired_via, media_type=media_type, **extract_kwargs,
    )
    return ingest_entries(
        tenant_id, results, acquired_via=acquired_via, now=now,
        park_unattributed=park_unattributed,
    )


def ingest_entries(
    tenant_id: UUID | str,
    entries: list[ExtractionResult],
    *,
    acquired_via: str,
    now: datetime | None = None,
    park_unattributed: bool = False,
) -> IngestionSummary:
    """Route + commit PRE-EXTRACTED entries — the shared post-extraction step for
    BOTH image (vision) and records (vCard/CSV/structured) methods. Per entry: any
    field <0.7 → clarifying_flow (≤3 else drop, P4); else dedup_and_merge identity
    + record_ledger_entries transactions (only when an amount field is present —
    identity-only methods like contacts pass none). Returns counts only (no PII).

    ``park_unattributed`` (POS/UPI methods, VT-58/57): a no-anchor row (no phone +
    no name) with an amount is PARKED in imported_transactions for later VT-275
    attribution instead of being dropped. Identity-only methods (contacts,
    owner_typed) leave it False → no-anchor rows drop as before.
    """
    now = now or datetime.now(UTC)
    committed = pending = dropped = parked = 0
    parked_rows: list[ImportedTxnIn] = []
    for idx, result in enumerate(entries):
        by_name = {f.name: f for f in result.fields}
        present = [f for f in result.fields if f.value is not None]
        # Route on the LOWEST-confidence present field (any field <0.7 → ask).
        low = [f for f in present if _route(f.confidence) == "ask_owner"]

        if low:
            questions = [
                ClarificationQuestion(
                    field=f.name,
                    prompt=f"Please confirm the {f.name.replace('_', ' ')} for this entry.",
                )
                for f in low
            ]
            try:
                open_clarification(tenant_id, f"ingest:{uuid4()}", questions)
                pending += 1
            except TooManyQuestionsError:
                dropped += 1  # too low-quality to repair via Q&A — drop (P4)
            continue

        # Commit path. Identity → dedup_and_merge; transaction → ledger.
        name = by_name.get("customer_name")
        phone = by_name.get("phone")
        if not (phone and phone.value) and not (name and name.value):
            # No customer anchor. POS/UPI (park_unattributed): retain as a raw
            # imported_transaction for VT-275 attribution. Else drop (identity-only
            # methods have nothing to park).
            if park_unattributed:
                parked_row = _to_imported_txn(by_name, acquired_via, idx, now)
                if parked_row is not None:
                    parked_rows.append(parked_row)
                    parked += 1
                    continue
            dropped += 1  # nothing to anchor a customer on (and not parking)
            continue
        merge = dedup_and_merge(
            tenant_id,
            acquired_via=acquired_via,
            phone_e164=phone.value if phone else None,
            display_name=name.value if name else None,
            field_confidences={f.name: f.confidence for f in present},
        )
        amount_f = by_name.get(_TXN_AMOUNT)
        if merge.customer_id is not None and amount_f and amount_f.value:
            paise = parse_amount_to_paise(amount_f.value)
            if paise is not None:
                entry_date = _parse_date(
                    by_name[_TXN_DATE].value if _TXN_DATE in by_name else None
                ) or now.date()
                record_ledger_entries(
                    tenant_id, merge.customer_id,
                    [LedgerEntryIn(
                        amount_paise=paise, entry_type="sale",
                        entry_date=entry_date, confidence=amount_f.confidence,
                    )],
                    acquired_via=acquired_via,
                )
        committed += 1

    # Flush parked unattributed rows to imported_transactions (idempotent).
    if parked_rows:
        record_imported_transactions(tenant_id, parked_rows, acquired_via=acquired_via)

    summary = IngestionSummary(
        entries_extracted=len(entries), committed=committed,
        pending_clarification=pending, dropped=dropped, parked=parked,
    )
    logger.info(
        "ingest_entries: tenant=%s acquired_via=%s entries=%d committed=%d "
        "pending=%d dropped=%d parked=%d",
        tenant_id, acquired_via, summary.entries_extracted, committed, pending,
        dropped, parked,
    )
    return summary


__all__ = ["IngestionSummary", "TARGET_FIELDS", "ingest_entries", "ingest_image"]
