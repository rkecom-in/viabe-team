"""VT-58 / VT-6 Method 4 — KOT/POS export ingestion.

Two flavors, both over the shared adapter (Pillar 8):
  (a) digital export (CSV/JSON) from a POS → records path via ingest_entries;
  (b) printed-receipt photo (image_kot) → vision via ingest_image.
acquired_via='kot_pos'.

UNATTRIBUTED rows: POS often does NOT capture the customer (no phone/name). Such
rows are now PARKED in imported_transactions (migration 062, VT-276) via
ingest_entries(park_unattributed=True) for later match_transactions attribution
(VT-275) — they are NO LONGER dropped (VT-58 completion, post-imported_transactions
2026-06-01). ATTRIBUTED rows (phone/name present) → dedup_and_merge +
record_ledger_entries (the clean ledger). Both flavors (CSV/JSON + receipt photo)
opt into parking.

Idempotency: the parked rows key on imported_transactions UNIQUE(tenant, source,
provider_ref) — bill_number (mapped to the 'provider_ref' field) when the export
has one, else a deterministic content+ordinal fallback. Attributed rows use the
ledger's content entry_key. Vendor parsers (PetPooja/Posist/Slick): the generic
CSV/JSON header-sniff covers the common shape; vendor-specific stubs are a
follow-up. PII never logged.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Any, Literal
from uuid import UUID

from orchestrator.integrations.methods._image_adapter import (
    IngestionSummary,
    ingest_entries,
    ingest_image,
)
from orchestrator.integrations.methods.contacts import _normalize_phone
from orchestrator.integrations.vision_extraction import (
    ExtractedField,
    ExtractionResult,
)

logger = logging.getLogger(__name__)

FileFormat = Literal["csv", "json", "image_kot", "auto"]
_CONF = 0.95

_AMOUNT_KEYS = {"amount", "total", "total_amount", "total_amount_paise", "bill_amount", "grand_total"}
_DATE_KEYS = {"date", "transaction_date", "bill_date", "order_date", "datetime"}
_PHONE_KEYS = {"phone", "mobile", "customer_phone", "phone_number", "contact"}
_NAME_KEYS = {"customer", "customer_name", "name"}
_BILL_KEYS = {"bill_number", "bill_no", "billno", "order_id", "order_no",
              "invoice", "invoice_no", "invoice_number", "receipt_no", "txn_id",
              "transaction_id", "ref", "reference"}


def _row_to_entry(row: dict[str, Any]) -> ExtractionResult | None:
    """Map a POS row (dict) to an ExtractionResult; None if no amount (useless)."""
    # Normalize header keys: lowercase + spaces→underscores so "Bill Date" and
    # "bill_date" both match the *_KEYS sets.
    low = {str(k).lower().strip().replace(" ", "_"): v for k, v in row.items()}
    amount = next((low[k] for k in _AMOUNT_KEYS if low.get(k) not in (None, "")), None)
    if amount is None:
        return None
    fields = [ExtractedField(name="amount", value=str(amount), confidence=_CONF)]
    date = next((low[k] for k in _DATE_KEYS if low.get(k) not in (None, "")), None)
    if date is not None:
        fields.append(ExtractedField(name="entry_date", value=str(date), confidence=_CONF))
    phone = next((low[k] for k in _PHONE_KEYS if low.get(k) not in (None, "")), None)
    if phone is not None:
        # Normalize to E.164 (shared with contacts) so the SAME customer dedups
        # across methods; foreign/odd numbers get a low confidence → clarification.
        e164, pconf = _normalize_phone(str(phone))
        if e164 is not None:
            fields.append(ExtractedField(name="phone", value=e164, confidence=pconf))
    name = next((low[k] for k in _NAME_KEYS if low.get(k) not in (None, "")), None)
    if name is not None:
        fields.append(ExtractedField(name="customer_name", value=str(name), confidence=_CONF))
    # bill/order id → provider_ref: the idempotency key for an unattributed park.
    bill = next((low[k] for k in _BILL_KEYS if low.get(k) not in (None, "")), None)
    if bill is not None:
        fields.append(ExtractedField(name="provider_ref", value=str(bill), confidence=_CONF))
    return ExtractionResult(fields=tuple(fields), acquired_via="kot_pos", model="parse")


def _parse_records(text: str, fmt: str) -> list[ExtractionResult]:
    rows: list[dict[str, Any]]
    if fmt == "json":
        data = json.loads(text)
        rows = data if isinstance(data, list) else data.get("transactions") or data.get("orders") or []
    else:  # csv
        rows = list(csv.DictReader(io.StringIO(text)))
    return [e for e in (_row_to_entry(r) for r in rows if isinstance(r, dict)) if e is not None]


def ingest_kot_pos(
    tenant_id: UUID | str,
    file_bytes: bytes,
    file_format: FileFormat = "auto",
    *,
    media_type: str = "image/jpeg",
) -> IngestionSummary:
    """Ingest a KOT/POS export (CSV/JSON) or a receipt photo. tenant from context (P3)."""
    fmt: str = file_format
    if fmt == "auto":
        if media_type.startswith("image/"):
            fmt = "image_kot"
        else:
            head = file_bytes[:64].lstrip()
            fmt = "json" if head[:1] in (b"{", b"[") else "csv"

    if fmt == "image_kot":
        # A receipt photo: ingest_image (vision → entries → attributed commit;
        # unattributed receipts parked in imported_transactions for VT-275).
        return ingest_image(
            tenant_id, file_bytes, acquired_via="kot_pos",
            media_type=media_type, park_unattributed=True,
        )

    entries = _parse_records(file_bytes.decode("utf-8", errors="replace"), fmt)
    logger.info("ingest_kot_pos: tenant=%s format=%s rows=%d", tenant_id, fmt, len(entries))
    # POS rows without a customer → park for later attribution (VT-275), not drop.
    return ingest_entries(
        tenant_id, entries, acquired_via="kot_pos", park_unattributed=True,
    )


__all__ = ["FileFormat", "ingest_kot_pos"]
