"""VT-57 / VT-6 Method 3 — UPI transaction-history export (PhonePe / GPay / Paytm).

Owner uploads a UPI app's transaction export (CSV; PDF is a deferred follow-up).
Rows → the two-surface write seam (record_imported_transactions, VT-276):
  - CREDIT (customer paid the owner) → kept. Attributed (VPA resolves to a
    customer) → imported_transactions + a clean ledger 'sale' (money received for
    goods/services; VT-417 PR-3 — was 'payment', which the Sales-Recovery detector
    ignores); unattributed → imported_transactions only, for VT-275 to attribute
    later.
  - DEBIT (owner sent money) → kept ONLY when the counterparty resolves to a KNOWN
    customer (a refund — Cowork D2/N1: retain customer refunds, raw, not promoted);
    an unknown counterparty is an owner→vendor payment → DROPPED (not customer
    activity).

Identity (fork #1, VT-46 UPI-scoped ruling): UPI counterparties are VPAs, not
phones. Resolution: (a) upi_vpa_resolutions exact prior link; (b) a `<phone>@upi`
VPA → extract phone → dedup_and_merge → record the VPA link for next time;
(c) else unattributed. The VPA→customer table reads via tenant_connection
(app_role + RLS, Cowork D1 — NOT the phone_token_resolutions owner-pool hack).

Idempotency: provider_ref = the provider's transaction_ref (UTR / Txn ID) →
imported_transactions UNIQUE(tenant, source, provider_ref); re-uploading the same
export is a no-op. source = "upi_phonepe" / "upi_gpay" / "upi_paytm".

Scope: CSV-first (stdlib csv, no pandas). PDF (pdfplumber) + vision fallback,
the non-tenant-VPA filter (VT-277 — needs tenant primary_vpa substrate), and the
owner-summary template send (owner-surface) are follow-ups. PII never logged
(CL-390); counts only. CL-422 dev = synthetic only.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from orchestrator.integrations.dedup_merge import dedup_and_merge
from orchestrator.integrations.imported_transactions import (
    ImportedTxnIn,
    RecordImportResult,
    record_imported_transactions,
)
from orchestrator.integrations.methods._image_adapter import IngestionSummary
from orchestrator.integrations.methods.contacts import _normalize_phone

logger = logging.getLogger(__name__)

UpiSource = Literal["phonepe", "gpay", "paytm"]
FileFormat = Literal["csv"]
_CONF = 0.95

# Per-provider column aliases (lowercased, spaces→underscores). Adding a provider
# is a new map entry, not a parser rewrite (Pillar 8). VPA/name fall back to a
# free-text description column when no dedicated column exists.
_COMMON = {
    "date": {"date", "transaction_date", "txn_date", "activity_date", "datetime"},
    "amount": {"amount", "amount_(inr)", "amount_inr", "transaction_amount"},
    "type": {"type", "transaction_type", "txn_type", "cr/dr", "dr/cr", "debit/credit"},
    "ref": {"transaction_id", "txn_id", "utr", "utr_no.", "utr_no",
            "transaction_ref", "reference_no", "order_id", "upi_ref_no"},
    "vpa": {"vpa", "upi_id", "payer_vpa", "upi_handle", "from_vpa", "counterparty_vpa"},
    "name": {"name", "payer_name", "from_name", "counterparty", "counterparty_name"},
    "desc": {"transaction_details", "details", "description", "narration",
             "activity", "remarks", "note", "notes"},
}
_COLUMN_MAPS: dict[str, dict[str, set[str]]] = {
    "phonepe": _COMMON,
    "gpay": _COMMON,
    "paytm": _COMMON,
}

_VPA_RE = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z][A-Za-z0-9.]+")
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y",
                 "%d %b %Y", "%b %d, %Y", "%d-%b-%Y", "%Y/%m/%d")


@dataclass(frozen=True)
class UPITransactionRow:
    """Canonical parsed UPI row (provider-agnostic)."""

    txn_date: date
    amount_paise: int
    direction: Literal["credit", "debit"]
    transaction_ref: str
    payer_vpa: str | None
    payer_name: str | None


def _norm_keys(row: dict[str, str]) -> dict[str, str]:
    return {str(k).lower().strip().replace(" ", "_"): (v or "") for k, v in row.items()}


def _pick(low: dict[str, str], aliases: set[str]) -> str | None:
    for k in aliases:
        if low.get(k) not in (None, ""):
            return low[k]
    return None


def _parse_amount_paise(raw: str | None) -> int | None:
    """Paise-precise rupee parse ('1,500.50' → 150050). None if no number."""
    if not raw:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d{1,2})?", raw)
    if not m:
        return None
    return round(float(m.group().replace(",", "")) * 100)


def _parse_date(raw: str | None, now: datetime) -> date | None:
    if not raw:
        return None
    s = raw.strip()[:24]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s.split()[0] if fmt == "%Y-%m-%d" else s, fmt).date()
        except ValueError:
            continue
    # ISO with time / trailing noise.
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _direction(low: dict[str, str], amount_raw: str | None) -> Literal["credit", "debit"] | None:
    """credit = money received (customer→owner); debit = money sent (owner→…)."""
    t = (_pick(low, _COLUMN_MAPS["phonepe"]["type"]) or "").lower()
    if any(w in t for w in ("credit", "cr", "received", "receive", "in")):
        return "credit"
    if any(w in t for w in ("debit", "dr", "paid", "sent", "out")):
        return "debit"
    # Fall back to amount sign (some exports encode direction as -amount).
    if amount_raw and amount_raw.strip().startswith("-"):
        return "debit"
    if amount_raw and re.search(r"\d", amount_raw):
        return "credit"  # positive amount, no type → treat as received
    return None


def _vpa_and_name(low: dict[str, str]) -> tuple[str | None, str | None]:
    vpa = _pick(low, _COLUMN_MAPS["phonepe"]["vpa"])
    name = _pick(low, _COLUMN_MAPS["phonepe"]["name"])
    if vpa is None:
        desc = _pick(low, _COLUMN_MAPS["phonepe"]["desc"])
        if desc:
            m = _VPA_RE.search(desc)
            vpa = m.group() if m else None
    return (vpa.strip() if vpa else None), (name.strip() if name else None)


def _phone_from_vpa(vpa: str | None) -> str | None:
    """A `<phone>@upi` handle → the phone digits (else None)."""
    if not vpa:
        return None
    local = vpa.split("@", 1)[0]
    digits = re.sub(r"\D", "", local)
    return digits if 10 <= len(digits) <= 12 else None


def _parse_upi_csv(text: str, source: UpiSource, now: datetime) -> list[UPITransactionRow]:
    reader = csv.DictReader(io.StringIO(text))
    cmap = _COLUMN_MAPS[source]
    rows: list[UPITransactionRow] = []
    for raw in reader:
        if not isinstance(raw, dict):
            continue
        low = _norm_keys(raw)
        amount_raw = _pick(low, cmap["amount"])
        paise = _parse_amount_paise(amount_raw)
        txn_date = _parse_date(_pick(low, cmap["date"]), now)
        direction = _direction(low, amount_raw)
        ref = _pick(low, cmap["ref"])
        if paise is None or txn_date is None or direction is None or not ref:
            continue  # structurally incomplete row — skip (P4: no invention)
        vpa, name = _vpa_and_name(low)
        rows.append(UPITransactionRow(
            txn_date=txn_date, amount_paise=paise, direction=direction,
            transaction_ref=ref.strip(), payer_vpa=vpa, payer_name=name,
        ))
    return rows


# --- VPA → customer resolution (tenant_connection, app_role + RLS; Cowork D1) ---

def resolve_vpa(tenant_id: UUID | str, vpa: str) -> UUID | None:
    """Exact prior VPA→customer link for this tenant (RLS-scoped). None if unseen."""
    from orchestrator.db.tenant_connection import tenant_connection

    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT customer_id FROM upi_vpa_resolutions WHERE vpa = %s", (vpa,)
        ).fetchone()
    if row is None:
        return None
    cid = row["customer_id"] if isinstance(row, dict) else row[0]
    return UUID(str(cid)) if cid is not None else None


def record_vpa_resolution(tenant_id: UUID | str, vpa: str, customer_id: UUID | str) -> None:
    """Persist a VPA→customer link (idempotent on (tenant, vpa))."""
    from orchestrator.db.tenant_connection import tenant_connection

    with tenant_connection(tenant_id) as conn:
        conn.execute(
            """
            INSERT INTO upi_vpa_resolutions (tenant_id, vpa, customer_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (tenant_id, vpa) DO NOTHING
            """,
            (str(tenant_id), vpa, str(customer_id)),
        )


def _resolve_customer(
    tenant_id: UUID | str, source: UpiSource, row: UPITransactionRow
) -> UUID | None:
    """Resolve a UPI counterparty to a customer (exact link → phone@upi dedup → None)."""
    if not row.payer_vpa:
        return None
    cid = resolve_vpa(tenant_id, row.payer_vpa)
    if cid is not None:
        return cid
    phone = _phone_from_vpa(row.payer_vpa)
    if phone:
        e164, _conf = _normalize_phone(phone)
        if e164 is not None:
            merge = dedup_and_merge(
                tenant_id, acquired_via=f"upi_{source}",
                phone_e164=e164, display_name=row.payer_name,
            )
            if merge.customer_id is not None:
                record_vpa_resolution(tenant_id, row.payer_vpa, merge.customer_id)
                return merge.customer_id
    return None


def ingest_upi_export(
    tenant_id: UUID | str,
    file_bytes: bytes,
    source: UpiSource,
    file_format: FileFormat = "csv",
    *,
    run_id: str | None = None,
    now: datetime | None = None,
) -> IngestionSummary:
    """Parse a UPI export → resolve counterparties → two-surface write. Counts only.

    CREDIT kept (attributed→ledger 'sale' + raw; else raw). DEBIT kept only when
    the counterparty is a KNOWN customer (refund, raw); unknown debit (owner→vendor)
    dropped. tenant_id from invocation context (P3). run_id for telemetry parity.
    """
    if file_format != "csv":
        raise ValueError(f"unsupported file_format {file_format!r} — CSV-first (PDF deferred)")
    now = now or datetime.now(UTC)
    parsed = _parse_upi_csv(file_bytes.decode("utf-8", errors="replace"), source, now)

    to_write: list[ImportedTxnIn] = []
    dropped = 0
    for row in parsed:
        cid = _resolve_customer(tenant_id, source, row)
        if row.direction == "debit" and cid is None:
            dropped += 1  # owner→vendor outgoing payment, not customer activity (D2)
            continue
        to_write.append(ImportedTxnIn(
            provider_ref=row.transaction_ref, amount_paise=row.amount_paise,
            txn_date=row.txn_date, direction=row.direction, customer_id=cid,
            # VT-417 PR-3: a UPI credit (customer→owner) IS a sale (money received
            # for goods/services). The Sales-Recovery detector counts ONLY
            # entry_type='sale' (db/wrappers _LAPSED_CANDIDATES_SQL), so 'payment'
            # made every UPI sale invisible to win-back targeting. entry_type only
            # reaches the ledger for credits (record_imported_transactions promotes
            # direction=='credit' only; debits/refunds park raw), so this is correct
            # and never mislabels a refund.
            entry_type="sale", confidence=_CONF,
        ))

    result: RecordImportResult = record_imported_transactions(
        tenant_id, to_write, acquired_via=f"upi_{source}",
    )
    committed = result.attributed_ledger_written          # attributed credits → ledger
    parked = result.written - result.attributed_ledger_written  # raw-only rows
    logger.info(
        "ingest_upi_export: tenant=%s source=%s parsed=%d committed=%d parked=%d "
        "dup=%d dropped=%d",
        tenant_id, source, len(parsed), committed, parked,
        result.skipped_duplicate, dropped,
    )
    return IngestionSummary(
        entries_extracted=len(parsed), committed=committed,
        pending_clarification=0, dropped=dropped, parked=parked,
    )


__all__ = [
    "FileFormat",
    "UPITransactionRow",
    "UpiSource",
    "ingest_upi_export",
    "record_vpa_resolution",
    "resolve_vpa",
]
