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
from orchestrator.integrations.ledger import LedgerEntryIn, record_ledger_entries
from orchestrator.integrations.vision_extraction import (
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


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except (ValueError, TypeError):
        return None


def ingest_image(
    tenant_id: UUID | str,
    image_bytes: bytes,
    *,
    acquired_via: str,
    media_type: str = "image/jpeg",
    now: datetime | None = None,
    extract_fn: Callable[..., list[ExtractionResult]] = extract_entries_from_image,
    **extract_kwargs,
) -> IngestionSummary:
    """Ingest a multi-entry image (paper ledger photo, etc.). Returns counts only.

    ``extract_fn`` is injectable for tests (defaults to the real vision primitive);
    extra kwargs (client / consent_check / model) pass through to it.
    """
    now = now or datetime.now(UTC)
    results = extract_fn(
        image_bytes, tenant_id=tenant_id, target_fields=TARGET_FIELDS,
        acquired_via=acquired_via, media_type=media_type, **extract_kwargs,
    )

    committed = pending = dropped = 0
    for result in results:
        by_name = {f.name: f for f in result.fields}
        present = [f for f in result.fields if f.value is not None]
        # Route on the LOWEST-confidence present field (VT-55: any field <0.7 → ask).
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
                open_clarification(tenant_id, f"image-ingest:{uuid4()}", questions)
                pending += 1
            except TooManyQuestionsError:
                dropped += 1  # too low-quality to repair via Q&A — drop (P4)
            continue

        # Commit path. Identity → dedup_and_merge; transaction → ledger.
        name = by_name.get("customer_name")
        phone = by_name.get("phone")
        if not (phone and phone.value) and not (name and name.value):
            dropped += 1  # nothing to anchor a customer on
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

    summary = IngestionSummary(
        entries_extracted=len(results), committed=committed,
        pending_clarification=pending, dropped=dropped,
    )
    logger.info(
        "ingest_image: tenant=%s acquired_via=%s extracted=%d committed=%d pending=%d dropped=%d",
        tenant_id, acquired_via, summary.entries_extracted, committed, pending, dropped,
    )
    return summary


__all__ = ["IngestionSummary", "TARGET_FIELDS", "ingest_image"]
