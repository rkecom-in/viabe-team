"""VT-449 / VT-411 — MCA company-master persistence + tier-2 ownership flag.

The store half of the Sandbox MCA bundle (integrations/methods/mca.py parses; this module
persists). Two public entry points:

- ``store_company_master_data(tenant_id, cmd)`` — UPSERT the parsed ``CompanyMasterData`` into
  ``tenant_mca_data`` (one row per tenant). The PII (``registered_address`` + ``directors[]``)
  is Fernet-ENCRYPTED at the boundary (CL-390/425/426): the plaintext NEVER lands in a column,
  a log, or an LLM prompt; only ciphertext is stored. Company financials/status/class/category/
  roc/incorporation/cin are NON-PII registry facts, stored plain. BEST-EFFORT: a failure logs
  (counts-only — never names/address/cin values) and RETURNS; it never raises into onboarding.
- ``set_owner_channel_verified(tenant_id)`` — flip the VT-411 tier-2 ownership flag on the
  tenants row (``owner_channel_verified = true`` + stamp ``owner_channel_verified_at``).

Both writers go through ``tenant_connection(tenant_id)`` so ``FORCE ROW LEVEL SECURITY`` on
``tenant_mca_data`` (mig 142) is genuinely enforced (CL-71/CL-122). The tenants column is ``id``
(the table has no ``tenant_id`` column — see every other tenants writer, e.g. verification.py).

``encrypt_value`` is imported INSIDE ``store_company_master_data`` so this module stays
import-light for the dep-less smoke job (the helper pulls ``cryptography.fernet``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID

from orchestrator.db import tenant_connection

if TYPE_CHECKING:
    from orchestrator.integrations.methods.mca import CompanyMasterData

logger = logging.getLogger(__name__)


def store_company_master_data(tenant_id: UUID | str, cmd: CompanyMasterData) -> None:
    """UPSERT the parsed MCA company-master into ``tenant_mca_data`` (one row per tenant).

    The PII fields are Fernet-encrypted before they touch a column. Best-effort: on ANY failure
    (crypto key missing, DB error, …) this logs a counts-only line and returns — it NEVER raises
    into the onboarding flow. NEVER logs director names / address / cin values.
    """
    # Deferred import — pulls cryptography.fernet; keep the module import-light (dep-less smoke).
    from orchestrator.observability.encrypt_value import encrypt_value

    tid = str(tenant_id)
    try:
        directors = list(cmd.directors or ())
        registered_address_encrypted = (
            encrypt_value(cmd.registered_address) if cmd.registered_address else None
        )
        directors_encrypted = encrypt_value(json.dumps(directors)) if directors else None

        with tenant_connection(tid) as conn:
            conn.execute(
                """
                INSERT INTO tenant_mca_data (
                    tenant_id, cin, company_name, status, active_compliance,
                    class_of_company, company_category, roc_code, date_of_incorporation,
                    paid_up_capital, authorised_capital,
                    registered_address_encrypted, directors_encrypted
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    cin = EXCLUDED.cin,
                    company_name = EXCLUDED.company_name,
                    status = EXCLUDED.status,
                    active_compliance = EXCLUDED.active_compliance,
                    class_of_company = EXCLUDED.class_of_company,
                    company_category = EXCLUDED.company_category,
                    roc_code = EXCLUDED.roc_code,
                    date_of_incorporation = EXCLUDED.date_of_incorporation,
                    paid_up_capital = EXCLUDED.paid_up_capital,
                    authorised_capital = EXCLUDED.authorised_capital,
                    registered_address_encrypted = EXCLUDED.registered_address_encrypted,
                    directors_encrypted = EXCLUDED.directors_encrypted
                """,
                (
                    tid,
                    cmd.cin,
                    cmd.company_name,
                    cmd.status,
                    cmd.active_compliance,
                    cmd.class_of_company,
                    cmd.company_category,
                    cmd.roc_code,
                    cmd.date_of_incorporation,
                    cmd.paid_up_capital,
                    cmd.authorised_capital,
                    registered_address_encrypted,
                    directors_encrypted,
                ),
            )
        # Counts-only logging — NEVER the PII values (names/address/cin).
        logger.info(
            "mca_store: stored company-master tenant_id=%s directors=%d address=%s",
            tid,
            len(directors),
            "set" if registered_address_encrypted else "none",
        )
    except Exception:
        # Best-effort: persistence failure must not break onboarding. No PII in the log.
        logger.exception("mca_store: store_company_master_data failed (best-effort) tenant_id=%s", tid)
        return


def set_owner_channel_verified(tenant_id: UUID | str) -> None:
    """Flip the VT-411 tier-2 ownership flag on the tenants row.

    ``owner_channel_verified = true`` + stamp ``owner_channel_verified_at = now()``. Scoped via
    ``tenant_connection`` (RLS); the tenants PK column is ``id``.
    """
    tid = str(tenant_id)
    with tenant_connection(tid) as conn:
        conn.execute(
            "UPDATE tenants SET owner_channel_verified = true, "
            "owner_channel_verified_at = %s WHERE id = %s",
            (datetime.now(timezone.utc), tid),
        )


__all__ = ["store_company_master_data", "set_owner_channel_verified"]
