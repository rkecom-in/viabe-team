"""VT-65 — L1 KG population consumer + per-event handlers (PR-1).

``process_kg_event`` is the single entry point: idempotency-checked
(kg_events_processed), dispatched by event_type to a handler that writes L1
entities/edges via the generic primitives. Per-event try/except → mark failed,
NEVER crash the caller (spec §5).

Privacy (NON-negotiable):
- Customer nodes store ONLY the canonical ``hash_phone`` (CL-390 — never raw
  phone/name; display_name is deliberately not stored).
- Every write is tenant-scoped (l1_write primitives → RLS + assert_tenant_scoped).
- owner_inputs lawful basis (CL-425) governs owner-supplied customer-derived data.

PR-2 wires live emitters at the 8 write sites; PR-1's backfill synthesizes these
events from canonical history so the KG fills now.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from orchestrator.db import tenant_connection
from orchestrator.knowledge.kg_vocab import EntityType, KgEventType, RelationshipType
from orchestrator.knowledge.l1_write import add_relationship, upsert_entity
from orchestrator.utils.phone_token import hash_phone

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KgEvent:
    event_id: UUID
    event_type: str
    tenant_id: UUID
    payload: dict[str, Any] = field(default_factory=dict)


def _norm(s: str) -> str:
    return " ".join(str(s).split()).lower()


def _tenant_node(tid: UUID, business_name: str | None = None) -> UUID:
    """Idempotent tenant entity (external_key = tenant id)."""
    attrs: dict[str, Any] = {}
    if business_name:
        attrs["business_name"] = business_name
    return upsert_entity(tid, EntityType.TENANT, str(tid), attrs)


# --- handlers ---------------------------------------------------------------


def _h_tenant_created(tid: UUID, p: dict[str, Any]) -> None:
    t = _tenant_node(tid, p.get("business_name"))
    if p.get("locality"):
        loc = upsert_entity(tid, EntityType.LOCALITY, _norm(p["locality"]), {"name": p["locality"]})
        add_relationship(tid, t, loc, RelationshipType.OPERATES_IN)
    if p.get("business_type"):
        bt = upsert_entity(tid, EntityType.BUSINESS_TYPE, _norm(p["business_type"]), {"label": p["business_type"]})
        add_relationship(tid, t, bt, RelationshipType.CLASSIFIED_AS)


def _h_customer_upsert(tid: UUID, p: dict[str, Any]) -> None:
    cid = str(p["customer_id"])
    attrs: dict[str, Any] = {}
    # CL-390: hash only, never raw phone/name.
    if p.get("phone_e164"):
        attrs["phone_hash"] = hash_phone(p["phone_e164"])
    cust = upsert_entity(tid, EntityType.CUSTOMER, cid, attrs)
    add_relationship(tid, _tenant_node(tid), cust, RelationshipType.OWNS)


def _h_transaction_created(tid: UUID, p: dict[str, Any]) -> None:
    txn = upsert_entity(
        tid, EntityType.TRANSACTION, str(p["transaction_id"]),
        {"amount_paise": p.get("amount_paise"), "txn_date": str(p.get("txn_date") or "")},
    )
    if p.get("customer_id"):
        cust = upsert_entity(tid, EntityType.CUSTOMER, str(p["customer_id"]), {})
        add_relationship(tid, cust, txn, RelationshipType.MADE)


def _h_campaign_created(tid: UUID, p: dict[str, Any]) -> None:
    camp = upsert_entity(
        tid, EntityType.CAMPAIGN, str(p["campaign_id"]),
        {"status": p.get("status")},
    )
    add_relationship(tid, _tenant_node(tid), camp, RelationshipType.SENT)


def _h_campaign_sent(tid: UUID, p: dict[str, Any]) -> None:
    camp = upsert_entity(tid, EntityType.CAMPAIGN, str(p["campaign_id"]), {"status": "sent"})
    for cid in p.get("customer_ids", []) or []:
        cust = upsert_entity(tid, EntityType.CUSTOMER, str(cid), {})
        add_relationship(tid, camp, cust, RelationshipType.TARGETED)


def _h_attribution_created(tid: UUID, p: dict[str, Any]) -> None:
    camp = upsert_entity(
        tid, EntityType.CAMPAIGN, str(p["campaign_id"]),
        {"arrr_paise": p.get("arrr_paise")} if p.get("arrr_paise") is not None else {},
    )
    if p.get("transaction_id"):
        txn = upsert_entity(tid, EntityType.TRANSACTION, str(p["transaction_id"]), {})
        add_relationship(tid, camp, txn, RelationshipType.ATTRIBUTED)


def _h_platform_listing_updated(tid: UUID, p: dict[str, Any]) -> None:
    listing = upsert_entity(
        tid, EntityType.PLATFORM_LISTING, str(p["listing_id"]),
        {"platform": p.get("platform"), "rating": p.get("rating")},
    )
    add_relationship(tid, _tenant_node(tid), listing, RelationshipType.HAS_LISTING)


_HANDLERS = {
    KgEventType.TENANT_CREATED: _h_tenant_created,
    KgEventType.CUSTOMER_CREATED: _h_customer_upsert,
    KgEventType.CUSTOMER_UPDATED: _h_customer_upsert,
    KgEventType.TRANSACTION_CREATED: _h_transaction_created,
    KgEventType.CAMPAIGN_CREATED: _h_campaign_created,
    KgEventType.CAMPAIGN_SENT: _h_campaign_sent,
    KgEventType.ATTRIBUTION_CREATED: _h_attribution_created,
    KgEventType.PLATFORM_LISTING_UPDATED: _h_platform_listing_updated,
}


def _already_processed(tid: UUID, event_id: UUID) -> bool:
    with tenant_connection(tid) as conn:
        row = conn.execute(
            "SELECT status FROM kg_events_processed WHERE event_id = %s AND tenant_id = %s",
            (str(event_id), str(tid)),
        ).fetchone()
    return row is not None and (row["status"] if isinstance(row, dict) else row[0]) == "processed"


def _mark(tid: UUID, event: KgEvent, status: str, error: str | None) -> None:
    with tenant_connection(tid) as conn:
        conn.execute(
            """
            INSERT INTO kg_events_processed (event_id, event_type, tenant_id, status, error)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO UPDATE
              SET status = EXCLUDED.status, error = EXCLUDED.error, processed_at = now()
            """,
            (str(event.event_id), event.event_type, str(tid), status, error),
        )


def process_kg_event(event: KgEvent) -> str:
    """Process one KG event idempotently. Returns 'processed'|'skipped'|'failed'.
    Never raises — a handler failure is recorded + the pipeline continues."""
    tid = event.tenant_id
    if _already_processed(tid, event.event_id):
        return "skipped"
    handler = _HANDLERS.get(event.event_type)
    if handler is None:
        _mark(tid, event, "failed", f"unknown event_type: {event.event_type}")
        return "failed"
    try:
        handler(tid, event.payload)
    except Exception as exc:  # noqa: BLE001 — never crash the consumer (spec §5)
        logger.exception("VT-65 kg handler failed (event=%s type=%s)", event.event_id, event.event_type)
        _mark(tid, event, "failed", repr(exc))
        return "failed"
    _mark(tid, event, "processed", None)
    return "processed"


__all__ = ["KgEvent", "process_kg_event"]
