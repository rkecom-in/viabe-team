"""VT-56 / VT-6 Method 2 — phone contacts import (vCard / CSV).

Records path (no vision): parse a contacts export → ExtractionResults
(customer_name + phone) → the shared ingest_entries. Identity-only — contacts
carry no transactions, so no ledger rows (a customer from contacts accrues
transactions later as UPI/cash flows in). acquired_via='contacts'.

Confidence: structured data → 0.95. A non-Indian phone (or unparseable) gets a
low confidence so ingest_entries routes it to the clarifying flow ("import this
foreign number?") rather than committing silently (VT-56 rule).

Discarded at parse (never stored): email, address, photo — not customer-ledger
relevant. PII never logged (CL-390); counts only.

Scope notes: the owner bulk-import confirmation (request_owner_approval, VT-5.9)
is the owner-surface/webhook layer — out of scope here, same posture as VT-55.
vCard is parsed with a minimal FN/TEL line reader + stdlib csv; swapping to
vobject/phonenumbers is a hardening follow-up if real exports need it.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from typing import Literal
from uuid import UUID

from orchestrator.integrations.methods._image_adapter import (
    IngestionSummary,
    ingest_entries,
)
from orchestrator.integrations.vision_extraction import (
    ExtractedField,
    ExtractionResult,
)

logger = logging.getLogger(__name__)

FileFormat = Literal["vcard", "csv", "auto"]
_HIGH_CONF = 0.95
_FOREIGN_CONF = 0.6  # < ask threshold → routes to clarifying flow (VT-56)

_NAME_COLS = {"name", "full name", "fullname", "customer", "customer name", "display name"}
_FIRST_COLS = {"first name", "firstname", "given name"}
_LAST_COLS = {"last name", "lastname", "surname", "family name"}


class ContactsParseError(Exception):
    """Raised when a CSV's columns can't be mapped — caller asks the owner to fix the export."""


def _normalize_phone(raw: str) -> tuple[str | None, float]:
    """Return (E.164-ish, confidence). Indian → 0.95; foreign/odd → 0.6; junk → None."""
    if not raw:
        return None, 0.0
    s = raw.strip()
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None, 0.0
    if has_plus and digits.startswith("91") and len(digits) == 12:
        return "+" + digits, _HIGH_CONF
    if has_plus:
        return "+" + digits, _FOREIGN_CONF  # non-Indian → low conf → ask owner
    if len(digits) == 10:
        return "+91" + digits, _HIGH_CONF
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits, _HIGH_CONF
    if len(digits) == 11 and digits.startswith("0"):
        return "+91" + digits[1:], _HIGH_CONF
    return "+" + digits, _FOREIGN_CONF  # odd length → low conf → ask


def _parse_vcard(text: str) -> list[dict[str, str | None]]:
    contacts: list[dict[str, str | None]] = []
    cur: dict[str, str | None] = {}
    for line in text.splitlines():
        u = line.strip()
        up = u.upper()
        if up.startswith("BEGIN:VCARD"):
            cur = {}
        elif up.startswith("END:VCARD"):
            if cur.get("phone"):
                contacts.append(cur)
            cur = {}
        elif up.startswith("FN") and ":" in u:
            cur["name"] = u.split(":", 1)[1].strip() or None
        elif up.startswith("TEL") and ":" in u and not cur.get("phone"):
            cur["phone"] = u.split(":", 1)[1].strip()
    return contacts


def _parse_csv(text: str) -> list[dict[str, str | None]]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ContactsParseError("CSV has no header row")
    cols = {c.lower().strip(): c for c in reader.fieldnames if c}
    name_col = next((cols[k] for k in cols if k in _NAME_COLS), None)
    first_col = next((cols[k] for k in cols if k in _FIRST_COLS), None)
    last_col = next((cols[k] for k in cols if k in _LAST_COLS), None)
    phone_col = next(
        (cols[k] for k in cols if "phone" in k or k in ("mobile", "tel", "contact", "number")),
        None,
    )
    if phone_col is None:
        raise ContactsParseError(
            "CSV columns unclear — need a phone column (e.g. 'Phone Number'); "
            "please adjust the export."
        )
    out: list[dict[str, str | None]] = []
    for row in reader:
        phone = (row.get(phone_col) or "").strip()
        if not phone:
            continue
        if name_col:
            name = (row.get(name_col) or "").strip() or None
        else:
            name = " ".join(
                p for p in [(row.get(first_col) or "").strip() if first_col else "",
                            (row.get(last_col) or "").strip() if last_col else ""] if p
            ).strip() or None
        out.append({"name": name, "phone": phone})
    return out


def ingest_contacts(
    tenant_id: UUID | str,
    file_bytes: bytes,
    file_format: FileFormat = "auto",
    *,
    run_id: str | None = None,
) -> IngestionSummary:
    """Parse a vCard/CSV contacts export → dedup + commit identity rows.

    tenant_id from invocation context (P3). Raises ContactsParseError on an
    unmappable CSV. run_id accepted for telemetry parity (unused here).
    """
    text = file_bytes.decode("utf-8", errors="replace")
    fmt = file_format
    if fmt == "auto":
        fmt = "vcard" if "BEGIN:VCARD" in text.upper() else "csv"
    raw = _parse_vcard(text) if fmt == "vcard" else _parse_csv(text)

    entries: list[ExtractionResult] = []
    for c in raw:
        e164, conf = _normalize_phone(str(c.get("phone") or ""))
        if e164 is None:
            continue  # no usable phone → skip (a contact without phone is useless)
        fields = [ExtractedField(name="phone", value=e164, confidence=conf)]
        if c.get("name"):
            fields.append(
                ExtractedField(name="customer_name", value=str(c["name"]), confidence=_HIGH_CONF)
            )
        entries.append(
            ExtractionResult(fields=tuple(fields), acquired_via="contacts", model="parse")
        )
    logger.info(
        "ingest_contacts: tenant=%s format=%s parsed=%d usable=%d",
        tenant_id, fmt, len(raw), len(entries),
    )
    return ingest_entries(tenant_id, entries, acquired_via="contacts")


__all__ = ["ContactsParseError", "FileFormat", "ingest_contacts"]
