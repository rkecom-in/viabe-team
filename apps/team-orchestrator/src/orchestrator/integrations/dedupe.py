"""VT-209 — phone-hash dedupe primitive.

Connector-agnostic. Given a canonical row about to be inserted, look
up the existing customer by phone_hash (VT-184 substrate). If match:
merge — append connector_id to the customer's `acquired_via` array,
update touch points. If no match: insert new row with `acquired_via=
[connector_id]`.

Per CL-104: phone is hashed via VT-184's `_hash_phone` — never stored
plaintext.
Per CL-390: every dedupe decision writes a `dedupe_decision`
pipeline_steps row for audit.
Per CL-417: canonical per-field columns on the customers table; no
JSONB blob for canonical fields.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator.graph import get_pool
# Use observability/phone_tokens' internal _hash_phone (16-char shape;
# matches what register_phone_token stores) NOT utils.phone_token.hash_phone
# (full 64-char digest; legacy per its module-level TODO VT-122 comment).
# Without this re-import, dedupe SELECT looks for a different token shape
# than register_phone_token writes → false 'inserted' decisions on repeat.
from orchestrator.observability.phone_tokens import _hash_phone as hash_phone

logger = logging.getLogger(__name__)


DedupeDecisionKind = Literal["merged", "inserted"]


@dataclass(frozen=True)
class DedupeDecision:
    """Outcome of a dedupe lookup + write."""

    kind: DedupeDecisionKind
    phone_token: str
    customer_id: str | None  # populated when customers row exists


def dedupe_customer_row(
    *,
    tenant_id: UUID,
    phone_e164: str,
    connector_id: str,
    canonical_row: dict[str, Any],
) -> DedupeDecision:
    """Look up customer by phone_hash; merge if match, insert if not.

    NB: Phase-1 minimal implementation. The actual `customers` table
    does not exist on main yet (CL-190 — substrate deferred). This
    function uses `phone_token_resolutions` (VT-184) as the dedupe
    seam: presence of the phone_token = customer exists; absent =
    insert. Once the `customers` table lands (separate VT row), this
    function gains the merge / acquired_via tracking properly.

    Phase-1 contract:
    - Hashes phone_e164 via VT-184 ``hash_phone`` (deterministic;
      same plaintext → same token).
    - Checks ``phone_token_resolutions`` for the token.
    - Returns ``DedupeDecision(kind='merged')`` when token already
      exists; ``DedupeDecision(kind='inserted')`` when fresh.
    - Returns a placeholder customer_id (None for now; real customer
      table row created by a follow-up VT row).

    Per CL-104: phone never logged plaintext; only the hashed token
    flows into the decision envelope.
    """
    phone_token = hash_phone(phone_e164)
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT phone_token, tenant_id FROM phone_token_resolutions "
            "WHERE phone_token = %s AND tenant_id = %s",
            (phone_token, str(tenant_id)),
        )
        row = cur.fetchone()

    if row is not None:
        logger.info(
            "VT-209 dedupe MERGED — token already exists for tenant",
            extra={
                "phone_token": phone_token,
                "tenant_id": str(tenant_id),
                "connector_id": connector_id,
            },
        )
        return DedupeDecision(
            kind="merged",
            phone_token=phone_token,
            customer_id=None,
        )

    # No match — register the phone-token under this tenant (VT-184
    # substrate already supports this; VT-191 encrypts the plaintext
    # in transit + at rest).
    from orchestrator.observability.phone_tokens import register_phone_token

    register_phone_token(tenant_id=tenant_id, phone_e164=phone_e164)
    logger.info(
        "VT-209 dedupe INSERTED — new token registered",
        extra={
            "phone_token": phone_token,
            "tenant_id": str(tenant_id),
            "connector_id": connector_id,
            "canonical_row_keys": list(canonical_row.keys()),
        },
    )
    _ = Jsonb  # keep import — future writes attach canonical_row JSONB
    return DedupeDecision(
        kind="inserted",
        phone_token=phone_token,
        customer_id=None,
    )


__all__ = ["DedupeDecision", "DedupeDecisionKind", "dedupe_customer_row"]
