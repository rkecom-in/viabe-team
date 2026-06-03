"""VT-54 / VT-6.3 — dedup + merge across ingestion methods.

The same customer photographed in a paper book, exported from UPI, and scraped
from contacts must collapse to ONE customers row. This primitive is the merge
seam every ingestion method (VT-55..63) funnels through.

Extends the VT-209 phone-hash seam (``dedupe.py`` + ``phone_token_resolutions`` +
``_hash_phone``); adds the real customers-table merge now that the table exists
(mig 045) + the acquired_via provenance column (mig 060).

Dedup identity (Cowork D4): the phone is the identity. customers enforces it via
UNIQUE(tenant_id, phone_e164); the privacy-preserving ``phone_token`` (sha256[:16]
+ salt) is registered for the redaction/audit layer, not used as the join key
(there is no phone_token column on customers). Matching phone_e164 ≡ matching the
deterministic token. tenant_id is derived from invocation context (P3) and
threaded to ``tenant_connection`` for RLS — never taken from the incoming row.

Merge semantics (approved plan):
  * acquired_via: APPEND the new tag, de-duplicated (acquired_via_history).
  * canonical fields: ADDITIVE NON-OVERWRITE — fill a NULL column from an
    eligible incoming value; never clobber an existing non-NULL.
  * confidence gates eligibility via the SINGLE-SOURCE ``field_mapping._route``:
    an ask-level field (<0.7) is NOT committed here — it routes to the VT-53
    clarification flow. (CL-417: customers holds canonical columns, no per-field
    confidence storage, so confidence governs eligibility-to-commit, not a stored
    value.)
  * ambiguous (incoming matches >1 existing customer): P4 — do NOT auto-merge;
    park in pending_dedup_resolution (mig 059) for owner resolution (VT-53).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from orchestrator.integrations.field_mapping import _route
from orchestrator.utils.phone_token import hash_phone

logger = logging.getLogger(__name__)

# Single-source ingestion-method enum (VT-6). Validated app-side; a CI gate test
# asserts an invalid tag is REJECTED. Adding a method = a Python change here
# (Pillar 8), not a migration.
ACQUIRED_VIA: frozenset[str] = frozenset(
    {
        "paper_book", "contacts", "upi_phonepe", "upi_gpay", "upi_paytm",
        "kot_pos", "cash_book", "qr_opt_in", "apify_zomato", "apify_swiggy",
        "apify_magicpin", "apify_gbp", "owner_typed",
    }
)

MergeKind = Literal["inserted", "merged", "ambiguous"]


class AcquiredViaError(Exception):
    """Raised when acquired_via is not in the VT-6 enum."""


@dataclass(frozen=True)
class MergeResult:
    kind: MergeKind
    customer_id: UUID | None       # the resolved/created customer (None if ambiguous)
    acquired_via: tuple[str, ...]  # the row's tags after merge (empty if ambiguous)
    pending_dedup_id: UUID | None  # set when kind == 'ambiguous'


def _eligible(field: str, confidences: dict[str, float] | None) -> bool:
    """A field may commit iff its confidence is not ask-level (<0.7).

    No confidence given => treat as eligible (e.g. owner_typed). Uses the
    single-source threshold via field_mapping._route (criterion 7).
    """
    if confidences is None or field not in confidences:
        return True
    return _route(confidences[field]) != "ask_owner"


def dedup_and_merge(
    tenant_id: UUID | str,
    *,
    acquired_via: str,
    phone_e164: str | None = None,
    email: str | None = None,
    display_name: str | None = None,
    field_confidences: dict[str, float] | None = None,
    now: datetime | None = None,
) -> MergeResult:
    """Resolve an incoming customer against existing rows; merge or insert.

    Raises ``AcquiredViaError`` if acquired_via is not a VT-6 method.
    tenant_id derived from invocation context (P3). Needs at least one of
    phone_e164 / email to match on; with neither it always inserts.
    """
    if acquired_via not in ACQUIRED_VIA:
        raise AcquiredViaError(
            f"unknown acquired_via {acquired_via!r} — not in the VT-6 enum"
        )

    from psycopg.types.json import Jsonb

    from orchestrator.db.tenant_connection import tenant_connection
    from orchestrator.knowledge.kg_emit import drain_kg_events, emit_kg_event
    from orchestrator.knowledge.kg_vocab import KgEventType

    now = now or datetime.now(UTC)
    fc = field_confidences

    with tenant_connection(tenant_id) as conn:
        # --- find candidate matches (RLS-scoped to this tenant) ---
        candidates: dict[str, dict[str, Any]] = {}
        if phone_e164:
            for r in conn.execute(
                "SELECT id, display_name, phone_e164, email, acquired_via "
                "FROM customers WHERE phone_e164 = %s",
                (phone_e164,),
            ).fetchall():
                candidates[str(r["id"])] = r
        if email:
            for r in conn.execute(
                "SELECT id, display_name, phone_e164, email, acquired_via "
                "FROM customers WHERE email = %s",
                (email,),
            ).fetchall():
                candidates[str(r["id"])] = r

        # --- >1 distinct customer matched: AMBIGUOUS (P4, no auto-merge) ---
        if len(candidates) > 1:
            incoming = {
                "phone_e164": phone_e164, "email": email,
                "display_name": display_name, "acquired_via": acquired_via,
            }
            row = conn.execute(
                "INSERT INTO pending_dedup_resolution "
                "(tenant_id, candidate_customer_ids, incoming, reason) "
                "VALUES (%s, %s, %s, 'multiple_candidate_match') RETURNING id",
                (str(tenant_id), [UUID(c) for c in candidates],
                 Jsonb(incoming)),
            ).fetchone()
            pid = row["id"] if isinstance(row, dict) else row[0]
            logger.info(
                "dedup_and_merge AMBIGUOUS tenant=%s candidates=%d -> pending=%s",
                tenant_id, len(candidates), pid,
            )
            return MergeResult("ambiguous", None, (), pid)

        # --- exactly 1 match: MERGE (additive, non-overwrite) ---
        if len(candidates) == 1:
            existing = next(iter(candidates.values()))
            cid = existing["id"]
            cur_acq = list(existing["acquired_via"] or [])
            new_acq = sorted(set(cur_acq) | {acquired_via})
            new_name = existing["display_name"] or (
                display_name if _eligible("display_name", fc) else None
            )
            new_email = existing["email"] or (
                email if _eligible("email", fc) else None
            )
            new_phone = existing["phone_e164"] or (
                phone_e164 if _eligible("phone_e164", fc) else None
            )
            # VT-65 PR-2: UPDATE + customer_updated emit atomic in one txn.
            with conn.transaction():
                conn.execute(
                    "UPDATE customers SET display_name = %s, email = %s, "
                    "phone_e164 = %s, acquired_via = %s, updated_at = %s WHERE id = %s",
                    (new_name, new_email, new_phone, new_acq, now, str(cid)),
                )
                # VT-315 / CL-390: emit the canonical phone HASH, never the raw
                # phone — the kg_events outbox payload persists durably (rows are
                # not deleted on drain, only drained_at stamped).
                emit_kg_event(conn, KgEventType.CUSTOMER_UPDATED, tenant_id, {
                    "customer_id": str(cid),
                    "phone_hash": hash_phone(new_phone) if new_phone else None,
                })
            logger.info(
                "dedup_and_merge MERGED tenant=%s customer=%s acquired_via=%s",
                tenant_id, cid, new_acq,
            )
            drain_kg_events(tenant_id)
            return MergeResult("merged", cid, tuple(new_acq), None)

        # --- no match: INSERT (only eligible canonical fields) ---
        ins_name = display_name if _eligible("display_name", fc) else None
        ins_email = email if _eligible("email", fc) else None
        # VT-65 PR-2: INSERT + customer_created emit atomic in one txn.
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO customers "
                "(tenant_id, display_name, phone_e164, email, acquired_via, source) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (str(tenant_id), ins_name, phone_e164, ins_email,
                 [acquired_via], acquired_via),
            ).fetchone()
            cid = row["id"] if isinstance(row, dict) else row[0]
            # VT-315 / CL-390: hash before emit (see CUSTOMER_UPDATED above).
            emit_kg_event(conn, KgEventType.CUSTOMER_CREATED, tenant_id, {
                "customer_id": str(cid),
                "phone_hash": hash_phone(phone_e164) if phone_e164 else None,
            })

    # Register the privacy-preserving token outside the RLS block (its own
    # connection), linking it to the new customer for the redaction layer.
    if phone_e164:
        from orchestrator.observability.phone_tokens import register_phone_token

        register_phone_token(
            tenant_id=tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id)),
            phone_e164=phone_e164,
            customer_id=cid if isinstance(cid, UUID) else UUID(str(cid)),
        )
    logger.info(
        "dedup_and_merge INSERTED tenant=%s customer=%s acquired_via=%s",
        tenant_id, cid, acquired_via,
    )
    drain_kg_events(tenant_id)
    return MergeResult("inserted", cid, (acquired_via,), None)


__all__ = [
    "ACQUIRED_VIA",
    "AcquiredViaError",
    "MergeResult",
    "dedup_and_merge",
]
