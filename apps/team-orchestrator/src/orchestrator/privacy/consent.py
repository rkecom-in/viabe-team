"""VT-8.5 — customer consent-capture machinery (privacy half of the QR opt-in).

``record_of_consent`` (migration 067) is the proof-of-consent surface: a
customer who scans a business's QR and accepts the terms gets one row per
(tenant, phone_token). The clean-ledger half — writing the customer + the
first message — is VT-60, which fail-CLOSED gates on ``has_consent`` before
any customer write.

Privacy
-------
- Phone is tokenised at the boundary via ``hash_phone`` (CL-390): the raw E.164
  number is NEVER persisted in this table. Resolve-back, if ever needed, goes
  through the ``phone_token_resolutions`` encrypted seam — not this surface.
- RLS via ``app_current_tenant()`` (CL-82/88); every read/write enters through
  ``tenant_connection`` (``SET ROLE app_role`` + tenant GUC).
- ``consent_text_version`` stores the VERSION STRING only; the copy + locale
  text live single-sourced in ``.viabe/consent-text.md`` (Cowork drafts, Fazal
  legal-validates — RKeCom Services OPC Pvt Ltd).

Re-consent (Fix 1, Cowork VT-85 review 2026-06-02)
--------------------------------------------------
A previously opted-out customer who re-consents must have ``opted_out_at``
RESET to NULL on the same (tenant, phone_token) row — otherwise they stay
permanently blocked. ``record_consent``'s UPSERT does this in ``DO UPDATE``.

opt-out writer (Fix 3, Cowork VT-85 review 2026-06-02)
------------------------------------------------------
``opt_out`` is THE writer for ``opted_out_at``. The existing
``direct_handlers/opt_out_handler`` is OWNER/tenant-level (``tenants.opt_out``),
NOT per-customer, so no per-customer opt-out writer existed before this. The
inbound customer-facing STOP trigger that calls ``opt_out`` is exposed today
via the ``/consent/opt-out`` endpoint; a WhatsApp-inbound customer-STOP path is
rostered as a follow-up (customer-facing channel = VT-251 campaigns, post-launch).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from orchestrator.db import tenant_connection
from orchestrator.utils.phone_token import hash_phone

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    """Outcome of a consent write. ``active`` mirrors ``opted_out_at IS NULL``."""

    tenant_id: UUID
    phone_token: str
    consent_text_version: str
    consent_method: str
    active: bool


def _col(row: Any, key: str, idx: int) -> Any:
    """Read a column from a psycopg row that may be a dict or a tuple."""
    return row[key] if isinstance(row, dict) else row[idx]


def record_consent(
    tenant_id: UUID | str,
    phone_e164: str,
    *,
    consent_text_version: str,
    consent_method: str = "qr_optin",
    source: str | None = None,
    locale: str | None = None,
) -> ConsentRecord:
    """Record (or re-affirm) a customer's consent.

    Idempotent on (tenant, phone_token): a second call UPSERTs the same row,
    refreshing the version + ``consented_at`` and — critically (Fix 1) —
    clearing ``opted_out_at`` so a re-consenting customer is un-blocked.

    The raw ``phone_e164`` is tokenised here and NEVER persisted (CL-390).
    """
    phone_token = hash_phone(phone_e164)
    tid = str(tenant_id)
    with tenant_connection(tenant_id) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO record_of_consent
                (tenant_id, phone_token, consent_text_version,
                 consent_method, source, locale)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, phone_token) DO UPDATE SET
                consent_text_version = EXCLUDED.consent_text_version,
                consent_method       = EXCLUDED.consent_method,
                source               = EXCLUDED.source,
                locale               = EXCLUDED.locale,
                consented_at         = now(),
                opted_out_at         = NULL
            RETURNING consent_text_version, consent_method,
                      (opted_out_at IS NULL) AS active
            """,
            (tid, phone_token, consent_text_version, consent_method, source, locale),
        )
        row = cur.fetchone()
    assert row is not None
    # CL-390: log the token, never the raw number.
    logger.info(
        "record_consent tenant=%s token=%s version=%s method=%s",
        tid, phone_token, consent_text_version, consent_method,
    )
    return ConsentRecord(
        tenant_id=UUID(tid),
        phone_token=phone_token,
        consent_text_version=str(_col(row, "consent_text_version", 0)),
        consent_method=str(_col(row, "consent_method", 1)),
        active=bool(_col(row, "active", 2)),
    )


def has_consent(tenant_id: UUID | str, phone_token: str) -> bool:
    """True iff an active (non-opted-out) consent row exists for this token.

    Fail-CLOSED: no row, or an opted-out row, returns False. This is the gate
    VT-60 calls before any customer write/message on the QR path.
    """
    with tenant_connection(tenant_id) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM record_of_consent "
            "WHERE tenant_id = %s AND phone_token = %s AND opted_out_at IS NULL",
            (str(tenant_id), phone_token),
        )
        return cur.fetchone() is not None


def has_consent_for_phone(tenant_id: UUID | str, phone_e164: str) -> bool:
    """``has_consent`` keyed by raw phone (tokenised here, never persisted)."""
    return has_consent(tenant_id, hash_phone(phone_e164))


def opt_out(tenant_id: UUID | str, phone_token: str) -> bool:
    """Withdraw consent: stamp ``opted_out_at`` on an active row.

    Returns True iff a row was updated (False if no active consent existed).
    THE writer for ``opted_out_at`` (Fix 3). Re-consent via ``record_consent``
    clears the flag again.
    """
    with tenant_connection(tenant_id) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE record_of_consent SET opted_out_at = now() "
            "WHERE tenant_id = %s AND phone_token = %s AND opted_out_at IS NULL",
            (str(tenant_id), phone_token),
        )
        return cur.rowcount > 0


def opt_out_for_phone(tenant_id: UUID | str, phone_e164: str) -> bool:
    """``opt_out`` keyed by raw phone (tokenised here, never persisted)."""
    return opt_out(tenant_id, hash_phone(phone_e164))


def purge_consent(tenant_id: UUID | str, phone_token: str) -> int:
    """Per-customer DSR erasure: hard-delete the consent row(s) for a token.

    Returns the number of rows deleted. Tenant-WIDE DSR additionally sweeps
    ``record_of_consent`` via ``dsr_purge._PURGE_ORDER``; this is the targeted
    single-customer erasure path.
    """
    with tenant_connection(tenant_id) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM record_of_consent WHERE tenant_id = %s AND phone_token = %s",
            (str(tenant_id), phone_token),
        )
        return cur.rowcount


__all__ = [
    "ConsentRecord",
    "record_consent",
    "has_consent",
    "has_consent_for_phone",
    "opt_out",
    "opt_out_for_phone",
    "purge_consent",
]
