"""VT-65 — KG backfill (PR-1): synthesize population events from canonical history.

Reads the canonical tables in chronological order and feeds synthesized KgEvents
through the same ``process_kg_event`` consumer, so the L1 KG fills from existing
data NOW (before the PR-2 live emitters land). Idempotent: event_ids are
deterministic (uuid5 over tenant:type:source_id), so a re-backfill is a no-op
via the kg_events_processed ledger + the external_key upsert.

Tenant-scoped reads dogfood the VT-72 wrappers for the lint-gated hot tables
(customers / campaigns); imported_transactions / attributions (not in the VT-72
hot-5) read directly via tenant_connection. CL-422: synthetic data only on dev.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from orchestrator.db import tenant_connection
from orchestrator.db.wrappers import CampaignsWrapper, CustomersWrapper
from orchestrator.knowledge.kg_population import KgEvent, process_kg_event
from orchestrator.knowledge.kg_vocab import KgEventType

logger = logging.getLogger(__name__)


def _eid(tenant_id: UUID, event_type: str, source_id: str) -> UUID:
    """Deterministic event id → re-backfill is idempotent."""
    return uuid5(NAMESPACE_URL, f"kg:{tenant_id}:{event_type}:{source_id}")


def _emit(tid: UUID, event_type: str, source_id: str, payload: dict[str, Any]) -> str:
    return process_kg_event(
        KgEvent(_eid(tid, event_type, source_id), event_type, tid, payload)
    )


def backfill_tenant(tenant_id: UUID | str) -> dict[str, int]:
    """Backfill one tenant's KG from canonical history. Returns per-type counts."""
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    counts: dict[str, int] = {}

    def _bump(t: str) -> None:
        counts[t] = counts.get(t, 0) + 1

    # 1. tenant_created (+ locality / business_type if present on the row).
    with tenant_connection(tid) as conn:
        trow = conn.execute(
            "SELECT business_name, "
            "to_jsonb(tenants.*) AS all_cols FROM tenants WHERE id = %s",
            (str(tid),),
        ).fetchone()
    if trow is not None:
        cols = trow["all_cols"] if isinstance(trow, dict) else trow[1]
        _emit(tid, KgEventType.TENANT_CREATED, str(tid), {
            "business_name": cols.get("business_name"),
            "locality": cols.get("locality") or cols.get("city"),
            "business_type": cols.get("business_type"),
        })
        _bump(KgEventType.TENANT_CREATED)

    # 2. customers (VT-72 wrapper — dogfood layer-2 + satisfy the lint).
    for c in CustomersWrapper().list_for_tenant(tid, limit=10000):
        _emit(tid, KgEventType.CUSTOMER_CREATED, str(c["id"]), {
            "customer_id": str(c["id"]),
            "phone_e164": c.get("phone_e164"),
        })
        _bump(KgEventType.CUSTOMER_CREATED)

    # 3. transactions (imported_transactions — not VT-72-gated; direct read).
    with tenant_connection(tid) as conn:
        txns = conn.execute(
            "SELECT id, customer_id, amount_paise, txn_date FROM imported_transactions "
            "WHERE tenant_id = %s ORDER BY txn_date",
            (str(tid),),
        ).fetchall()
    for t in txns:
        td = dict(t)
        _emit(tid, KgEventType.TRANSACTION_CREATED, str(td["id"]), {
            "transaction_id": str(td["id"]),
            "customer_id": str(td["customer_id"]) if td.get("customer_id") else None,
            "amount_paise": td.get("amount_paise"),
            "txn_date": str(td.get("txn_date") or ""),
        })
        _bump(KgEventType.TRANSACTION_CREATED)

    # 4. campaigns (VT-72 wrapper).
    for c in CampaignsWrapper().list_for_tenant(tid, limit=10000):
        _emit(tid, KgEventType.CAMPAIGN_CREATED, str(c["id"]), {
            "campaign_id": str(c["id"]),
            "status": c.get("status"),
        })
        _bump(KgEventType.CAMPAIGN_CREATED)

    # 5. attributions (not VT-72-gated; direct read). Columns are mapped
    #    defensively (schema variance across mig 023).
    with tenant_connection(tid) as conn:
        attrs = conn.execute(
            "SELECT to_jsonb(attributions.*) AS a FROM attributions WHERE tenant_id = %s",
            (str(tid),),
        ).fetchall()
    for r in attrs:
        a = (r["a"] if isinstance(r, dict) else r[0]) or {}
        camp = a.get("campaign_id")
        if not camp:
            continue
        _emit(tid, KgEventType.ATTRIBUTION_CREATED, str(a.get("id") or camp), {
            "campaign_id": str(camp),
            "transaction_id": str(a["transaction_id"]) if a.get("transaction_id") else None,
            "arrr_paise": a.get("arrr_paise") or a.get("recovered_paise"),
        })
        _bump(KgEventType.ATTRIBUTION_CREATED)

    logger.info("VT-65 backfill tenant=%s counts=%s", tid, counts)
    return counts


__all__ = ["backfill_tenant"]
