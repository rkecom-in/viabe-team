"""VT-417 — connector-agnostic customer ingestion (the inbound-lineage writer).

Replaces the Phase-1 ``dedupe.dedupe_customer_row`` stub on every NETWORK-inbound
path (Shopify webhook, Sheet/Drive push, scheduler pull). The stub wrote ONLY a
``phone_token_resolutions`` row and returned ``customer_id=None`` — no
``customers`` row, no ``customer_ledger_entries``, no consent. So a real Shopify
order resolved a phone token and discarded everything else, and the Sales-Recovery
detector (which counts ``customers`` + ``customer_ledger_entries WHERE
entry_type='sale'`` AND-gated on an active ``record_of_consent``) could never
produce a lapsed candidate from an inbound connector.

This module makes the inbound lineage write the SAME substrate the image lineage
already writes (``methods/_image_adapter.ingest_entries``). It is a SIBLING of
``ingest_entries``, not a caller: connectors emit already-parsed structured rows
(no OCR confidence, no clarification Q&A), so forcing them through
``ExtractionResult`` would lie about provenance. The shared single-source mapping
is the THREE writers — ``dedup_and_merge`` / ``record_ledger_entries`` /
``record_consent`` — which both front-ends funnel through. Cross-checks with
``ingest_entries``: both HARD-CODE ``entry_type='sale'`` for ledger writes and both
derive ``tenant_id`` from the invocation context (P3), NEVER from the row.

PII / DPDP (CL-390 / 425 / 426): ``CanonicalRow`` holds ONLY fields the writers'
schemas persist (name / phone / email + sale magnitude/date). Address and order
line-items are dropped at the connector's mapper, never reaching this module.
Logging is counts-only — NEVER phone / email / name / amount-as-rupees (the
``IngestSummary`` mirrors ``_image_adapter.IngestionSummary``'s "NO PII" contract).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.integrations.dedup_merge import dedup_and_merge
from orchestrator.integrations.ledger import LedgerEntryIn, record_ledger_entries

logger = logging.getLogger(__name__)


class SaleLine(BaseModel):
    """One sale a connector attributes to a customer → one ``sale`` ledger entry.

    ``amount_paise`` is an INR-minor-unit magnitude (the ledger column,
    ``061:25``). ``confidence`` defaults to 1.0 because structured connector data
    is certain — unlike vision OCR, there is no per-field confidence to gate on.
    """

    model_config = ConfigDict(frozen=True)

    amount_paise: int = Field(..., ge=0)
    entry_date: date
    confidence: float = Field(1.0, ge=0.0, le=1.0)


class ConsentSignal(BaseModel):
    """A consent affirmation a connector carries inline (NOT used for Shopify —
    option A, §2.4: Shopify ingestion writes NO consent; the detector's consent
    AND-gate keeps a Shopify-ingested customer out of lapsed candidates until they
    opt in via the existing WhatsApp/QR path). Present for connectors that DO
    carry a first-party, our-channel consent (none today; kept for the shared
    schema so the writer seam is complete)."""

    model_config = ConfigDict(frozen=True)

    consent_text_version: str
    consent_method: str = "qr_optin"


class CanonicalRow(BaseModel):
    """The SINGLE schema every connector maps into — the writers can persist
    nothing more than this (§3). Address / line-items / order-id are dropped at
    each connector's mapper and never appear here.

    Identity needs at least one of phone / email / name to anchor a customer; a
    row with none of the three is dropped (no anchor). ``sales`` may be empty
    (identity-only rows, e.g. a bare contact) → no ledger write.
    """

    model_config = ConfigDict(frozen=True)

    phone_e164: str | None = None
    email: str | None = None
    display_name: str | None = None
    sales: tuple[SaleLine, ...] = ()
    consent: ConsentSignal | None = None

    def has_anchor(self) -> bool:
        return bool(self.phone_e164 or self.email or self.display_name)


@dataclass(frozen=True)
class IngestSummary:
    """Counts only — NO PII (CL-390). Mirrors ``_image_adapter.IngestionSummary``.

    ``committed`` — rows that resolved/created a customer (``new_customers + matched_customers``).
    ``new_customers`` / ``matched_customers`` (VT-608 fix round MINOR 1) — the ``committed``
    breakdown by ``dedup_and_merge``'s own ``MergeResult.kind``: ``inserted`` (a genuinely NEW
    customer) vs ``merged`` (the row matched an EXISTING customer). Callers reporting "found N
    customers" to an owner should use ``new_customers``, not ``committed`` — a RE-RUN over
    already-ingested rows (a recurring pull, or a retried commit) reports every row as
    ``committed`` again even though most already existed, silently OVER-COUNTING "found N
    customers" on each re-run.
    ``ambiguous`` — rows whose identity matched >1 existing customer (parked in
        ``pending_dedup_resolution`` by ``dedup_and_merge``; NO ledger written).
    ``dropped``   — rows with no identity anchor.
    ``sales_written`` / ``sales_skipped_duplicate`` — ledger outcomes (idempotent
        re-delivery collapses on ``entry_key`` → counted as skipped).
    """

    rows: int
    committed: int
    ambiguous: int
    dropped: int
    sales_written: int
    sales_skipped_duplicate: int
    new_customers: int = 0
    matched_customers: int = 0


def ingest_customer_rows(
    tenant_id: UUID | str,
    rows: list[CanonicalRow],
    *,
    acquired_via: str,
    now: datetime | None = None,
) -> IngestSummary:
    """Land already-parsed connector rows into the real customer substrate.

    Per row (mirrors ``_image_adapter.ingest_entries`` 192-229):
      1. Identity gate — no phone AND no email AND no name → drop.
      2. ``dedup_and_merge`` (identity). ``ambiguous`` (``customer_id is None``)
         → count + NO ledger (no resolved customer to attach to).
      3. For each ``SaleLine`` on a resolved customer → ONE ``sale``
         ``record_ledger_entries`` (idempotent on ``(tenant, entry_key)`` — a
         re-delivered webhook does NOT double-count). ``entry_type`` is HARD-CODED
         ``'sale'``.
      4. ``record_consent`` ONLY if ``row.consent`` present — NEVER defaulted
         (option A: Shopify passes ``consent=None``, so nothing is written and the
         detector's consent AND-gate stays closed until the customer opts in).

    ``tenant_id`` is the invocation context (P3) — threaded to the writers' RLS
    ``tenant_connection``; never taken from a row. ``acquired_via`` MUST be in the
    VT-6 enum (``dedup_merge.ACQUIRED_VIA``) or the writers RAISE ``AcquiredViaError``.
    Returns counts only (no PII).
    """
    now = now or datetime.now(UTC)
    committed = ambiguous = dropped = 0
    sales_written = sales_skipped = 0
    new_customers = matched_customers = 0

    for row in rows:
        if not row.has_anchor():
            dropped += 1
            continue

        merge = dedup_and_merge(
            tenant_id,
            acquired_via=acquired_via,
            phone_e164=row.phone_e164,
            email=row.email,
            display_name=row.display_name,
        )
        if merge.customer_id is None:
            # ambiguous → parked in pending_dedup_resolution by dedup_and_merge;
            # no resolved customer, so no ledger / consent write.
            ambiguous += 1
            continue
        committed += 1
        if merge.kind == "inserted":
            new_customers += 1
        else:  # "merged" — matched an existing customer
            matched_customers += 1

        if row.sales:
            entries = [
                LedgerEntryIn(
                    amount_paise=s.amount_paise,
                    entry_type="sale",   # a connector sale is a sale — full stop
                    entry_date=s.entry_date,
                    confidence=s.confidence,
                )
                for s in row.sales
            ]
            result = record_ledger_entries(
                tenant_id, merge.customer_id, entries, acquired_via=acquired_via
            )
            sales_written += result.written
            sales_skipped += result.skipped_duplicate

        if row.consent is not None:
            # Lazy import — consent is an optional, connector-specific affordance
            # (NOT used by Shopify; option A). Keeps the common path import-light.
            from orchestrator.privacy.consent import record_consent

            if row.phone_e164:
                record_consent(
                    tenant_id,
                    row.phone_e164,
                    consent_text_version=row.consent.consent_text_version,
                    consent_method=row.consent.consent_method,
                    source=acquired_via,
                )

    summary = IngestSummary(
        rows=len(rows),
        committed=committed,
        ambiguous=ambiguous,
        dropped=dropped,
        sales_written=sales_written,
        sales_skipped_duplicate=sales_skipped,
        new_customers=new_customers,
        matched_customers=matched_customers,
    )
    logger.info(
        "ingest_customer_rows: tenant=%s acquired_via=%s rows=%d committed=%d "
        "ambiguous=%d dropped=%d sales_written=%d sales_dup=%d",
        tenant_id, acquired_via, summary.rows, committed, ambiguous, dropped,
        sales_written, sales_skipped,
    )
    return summary


# ---------- VT-417 PR-2: Sheet/CSV row → CanonicalRow mapping ----------
# The shared mapper for the SHEET lineage (api/sheet_push, api/integration_push
# google_sheet pushes, and the Drive/scheduler sheet pulls). A sheet row is an
# arbitrary {column -> cell} dict — owners label columns freely. We map ONLY the
# fields the writers' schemas persist (§3): phone / email / name + an optional
# amount+date sale. Address / GST / notes columns are dropped here (never read
# into the CanonicalRow). A contacts-only sheet (no amount column) lands
# identity-only (empty ``sales``) — that is by design, not a bug.

# Case/space-insensitive column aliases. First non-empty match wins.
_PHONE_KEYS = ("phone", "mobile", "phone_number", "phoneno", "contact", "whatsapp")
_EMAIL_KEYS = ("email", "e-mail", "email_address", "mail")
_NAME_KEYS = ("name", "customer_name", "customer", "full_name", "fullname", "display_name")
_AMOUNT_KEYS = ("amount", "order_amount", "total", "total_amount", "sale_amount", "value", "price")
_DATE_KEYS = ("date", "order_date", "sale_date", "txn_date", "transaction_date", "created_at")


def _normalize_e164(raw: Any) -> str | None:
    """Best-effort E.164 for an Indian-first sheet; ``None`` if un-normalizable.

    Mirrors the Shopify connector's normalizer (an IN store-owner's sheet carries
    bare 10-digit or +91 numbers). We never invent a country code for ambiguous
    bare digits — email / name still anchor the customer.

    VT-487 (SAFETY): a Google-Sheet / Excel cell that LOOKS numeric is read as a
    FLOAT/INT by openpyxl / gspread / pandas; ``str()``-ing it yields scientific
    notation ("9.98886e+11") or a trailing ".0". The old digit-strip
    (``re.sub(r"\\D","")``) silently GLUED the mantissa + exponent digits into a
    plausible-but-WRONG number — the corruption that sent six Twilio 21211
    "invalid To" failures. We now REJECT (return None) any value carrying an
    'e'/'E' (scientific notation) or a '.' (decimal) — a phone has neither — so a
    numerically-corrupted cell never normalizes into a bad number; email/name
    still anchor the customer.
    """
    if raw is None:
        return None
    s = str(raw).strip()  # coerce-to-str; a float/int never survives as a number
    if not s:
        return None
    # VT-487: scientific-notation / decimal artifact = corrupted numeric phone → reject, don't glue.
    if "e" in s or "E" in s or "." in s:
        return None
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if has_plus and digits.startswith("91") and len(digits) == 12:
        return "+" + digits
    if has_plus:
        return "+" + digits  # already international — trust it
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "+91" + digits[1:]
    return None  # ambiguous → don't guess


def _amount_to_paise(raw: Any) -> int | None:
    """A sheet amount cell (e.g. "₹499", "499.00", "1,250") → paise (INR minor).

    Strips currency symbols / thousands separators. Returns ``None`` on a missing
    / unparseable / negative value (the sale is then skipped, never written as 0).
    The sheet lineage is INR-only (an owner's sheet is in their local currency);
    no FX, mirroring the Shopify INR guard.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Keep digits, decimal point, leading minus; drop ₹/Rs/commas/spaces.
    cleaned = re.sub(r"[^\d.\-]", "", s)
    if not cleaned or cleaned in ("-", ".", "-."):
        return None
    try:
        paise = int((Decimal(cleaned) * 100).to_integral_value())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return paise if paise >= 0 else None


def _sheet_date(raw: Any) -> date | None:
    """A sheet date cell → ``date``. Accepts ISO 8601 / ISO date / dd/mm/yyyy."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # ISO 8601 datetime or date.
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        pass
    try:
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        pass
    # dd/mm/yyyy or dd-mm-yyyy (the common Indian sheet shape).
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)
    if m:
        d, mo, y = (int(g) for g in m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


def _norm_key(s: str) -> str:
    """Case/space/underscore/hyphen-insensitive column-name key, shared by the alias table AND
    (VT-608 fix round) the owner-confirmed-mapping resolver below."""
    return re.sub(r"[\s_\-]", "", str(s).strip().lower())


def _first_by_alias(row: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    """Return the first non-empty cell whose column name (case/space/underscore-
    insensitive) matches one of ``aliases``. None if no match."""
    norm = {_norm_key(k): v for k, v in row.items()}
    for alias in aliases:
        val = norm.get(_norm_key(alias))
        if val is not None and str(val).strip() != "":
            return val
    return None


def _first_by_mapping(row: dict[str, Any], mapping: dict[str, str], canonical_field: str) -> Any:
    """VT-608 fix round CRITICAL 2 — return the first non-empty cell whose SOURCE column the
    owner-confirmed ``mapping`` ({source_column: canonical_field}) maps to ``canonical_field``.
    Column-name matching is the SAME case/space/underscore-insensitive comparison
    ``_first_by_alias`` uses (defensive — the header read at commit time should match the header
    read at sample time exactly, but this tolerates incidental whitespace/casing drift)."""
    norm_row = {_norm_key(k): v for k, v in row.items()}
    for source_col, target in mapping.items():
        if target != canonical_field:
            continue
        val = norm_row.get(_norm_key(source_col))
        if val is not None and str(val).strip() != "":
            return val
    return None


def _first_by_mapping_or_alias(
    row: dict[str, Any], mapping: dict[str, str], canonical_field: str, alias_keys: tuple[str, ...]
) -> Any:
    """VT-611 pre-work #3(b) — the partial-mapping sharp edge: a confirmed mapping that covers
    SOME canonical fields but not others must not silently drop identity coverage for the ones
    it doesn't mention. ``_first_by_mapping`` alone had this gap — a mapping confirming ONLY
    ``phone`` meant ``email``/``customer_name`` got NO alias-table lookup at all, even when the
    row carries an obviously-aliased column (e.g. literally named "email") the alias table would
    have caught with no mapping present. The owner-confirmed mapping is authoritative for what it
    covers (tried first); anything it doesn't cover — or maps to an empty cell — falls back to the
    SAME alias table the no-mapping path always used, so a confirmed mapping can only ADD coverage
    over the alias-only baseline, never remove it."""
    val = _first_by_mapping(row, mapping, canonical_field)
    if val is not None:
        return val
    return _first_by_alias(row, alias_keys)


def sheet_row_to_canonical(
    row: dict[str, Any], *, mapping: dict[str, str] | None = None
) -> CanonicalRow | None:
    """Map an arbitrary owner-sheet row → ``CanonicalRow`` (or None if no anchor).

    Identity = phone(E.164) / email / name. Sale = amount + date columns when BOTH
    are present & parseable → ONE ``SaleLine`` (confidence 1.0 — an owner-typed
    sheet cell is structured, not OCR). A contacts-only sheet (no amount column, or
    an unparseable amount) lands identity-only (empty ``sales``). Consent is NEVER
    written from a sheet (a column header is not lawful WhatsApp consent — option-A
    analog; consent arrives via the WhatsApp/QR ``record_consent`` path).

    PII boundary (§3): only phone / email / name + the one sale magnitude/date are
    read; every other column (address, GST, notes, line items) is dropped here.

    ``mapping`` (VT-608 fix round, CRITICAL 2): an owner-CONFIRMED ``{source_column:
    canonical_field}`` map (``propose_mapping``/``confirm_mapping``'s own output) — when given, it
    DRIVES the transform instead of the alias table below, closing the defect where a confirmed
    mapping that overrides an alias (e.g. a column named "mob" the owner confirmed means "phone")
    had zero effect on ingestion. Canonical fields with no ``CanonicalRow`` slot
    (last_seen/address/tags) are accepted in the mapping but simply have nowhere to land — dropped,
    same as an unmapped column always was. Omitted (every pre-VT-608 caller, and every connector
    with no reasoner — Shopify) keeps the EXACT alias-table behavior, byte-for-byte.

    VT-611 pre-work #3(b) — a PARTIAL mapping (confirms some canonical fields, silent on others)
    ADDS coverage over the alias-only baseline, never subtracts from it: each field tries the
    mapping first, then falls back to the SAME alias table the no-mapping path uses
    (``_first_by_mapping_or_alias``). A confirmed mapping can only make identity detection better,
    never worse, than running with no mapping at all.
    """
    if mapping:
        phone_e164 = _normalize_e164(_first_by_mapping_or_alias(row, mapping, "phone", _PHONE_KEYS))
        email_raw = _first_by_mapping_or_alias(row, mapping, "email", _EMAIL_KEYS)
        email = (
            str(email_raw).strip().lower()
            if email_raw is not None and str(email_raw).strip()
            else None
        )
        name_raw = _first_by_mapping_or_alias(row, mapping, "customer_name", _NAME_KEYS)
        display_name = (
            str(name_raw).strip() if name_raw is not None and str(name_raw).strip() else None
        )
        if not (phone_e164 or email or display_name):
            return None
        sales = ()  # type: tuple[SaleLine, ...]
        paise = _amount_to_paise(_first_by_mapping_or_alias(row, mapping, "order_amount", _AMOUNT_KEYS))
        entry_date = _sheet_date(_first_by_mapping_or_alias(row, mapping, "order_date", _DATE_KEYS))
        if paise is not None and entry_date is not None:
            sales = (SaleLine(amount_paise=paise, entry_date=entry_date, confidence=1.0),)
        return CanonicalRow(
            phone_e164=phone_e164, email=email, display_name=display_name,
            sales=sales, consent=None,
        )

    phone_e164 = _normalize_e164(_first_by_alias(row, _PHONE_KEYS))
    email_raw = _first_by_alias(row, _EMAIL_KEYS)
    email = (
        str(email_raw).strip().lower()
        if email_raw is not None and str(email_raw).strip()
        else None
    )
    name_raw = _first_by_alias(row, _NAME_KEYS)
    display_name = (
        str(name_raw).strip() if name_raw is not None and str(name_raw).strip() else None
    )

    if not (phone_e164 or email or display_name):
        return None

    sales2: tuple[SaleLine, ...] = ()
    paise2 = _amount_to_paise(_first_by_alias(row, _AMOUNT_KEYS))
    entry_date2 = _sheet_date(_first_by_alias(row, _DATE_KEYS))
    if paise2 is not None and entry_date2 is not None:
        sales2 = (SaleLine(amount_paise=paise2, entry_date=entry_date2, confidence=1.0),)

    return CanonicalRow(
        phone_e164=phone_e164,
        email=email,
        display_name=display_name,
        sales=sales2,
        consent=None,  # sheets carry no lawful WhatsApp consent (option-A analog)
    )


__all__ = [
    "CanonicalRow",
    "ConsentSignal",
    "IngestSummary",
    "SaleLine",
    "ingest_customer_rows",
    "sheet_row_to_canonical",
]
