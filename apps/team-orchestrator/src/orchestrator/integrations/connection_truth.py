"""VT-756 — is there a customer-data source behind this tenant's numbers?

THE QUESTION THIS MODULE EXISTS TO ANSWER. A count of zero has two causes, and they are different
claims to an owner:

    nothing connected      →  "I don't have your customer data yet."
    connected, no rows     →  "Your ledger is connected and currently shows no customers."

`status_query.customer_count` used to answer neither — it counted rows and stated the number, so a
tenant with no source connected was told *"You currently have 0 customers in your ledger"*, which
reads as *we looked, and your business has none*. That is a measurement claim made where nothing was
measured.

WHY THIS IS A NEW MODULE RATHER THAN AN IMPORT. The fact already existed three times over, each
private to its caller and each with a different definition:

    journey._connected_integrations          phase_5_confirmed only — data has LANDED. Shopify-shaped
                                             (reads the single `current_connector_id`), journey-private.
    integrations.commit.is_connector_connected   a tenant_oauth_tokens row — per connector.
    connector_first_contact._connected_or_healthy  oauth row OR an enabled+ok status row, per
                                             connector. The two genuinely diverge: a status-only
                                             tenant answered "not connected" against a healthy,
                                             syncing connector (the reconnect_broken_sync fabrication
                                             residual, §2 judge 2026-07-11).

All three ask "is connector X connected?". The honesty question is "is ANY customer-data source
connected AT ALL?", which none of them answers and which no caller can assemble without knowing the
connector list. That is what `customer_data_source_connected` is.

FAIL-CLOSED, DELIBERATELY. Any read failure returns False — "connect a source" toward an owner who
already has one is a recoverable annoyance; a fabricated zero is a trust loss. This inverts the usual
fail-soft posture on purpose, and it is the direction VT-756 scope 2 specifies.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

__all__ = ["customer_data_source_connected"]


def customer_data_source_connected(tenant_id: UUID | str) -> bool:
    """True iff SOME customer-data source is connected for this tenant, by any source of record.

    Connected means any ONE of:

    * a ``tenant_oauth_tokens`` row exists (an OAuth install completed — the durable install truth
      ``is_connector_connected`` reads, here with the connector unpinned);
    * an ``enabled`` ``tenant_connector_status`` row whose ``last_status`` is ``'ok'`` (the VT-210
      operational truth — the half whose absence produced the reconnect_broken_sync fabrication);
    * the connector-onboarding state has reached ``phase_5_confirmed`` (rows actually ingested — the
      strictest of the three, and the one ``journey._connected_integrations`` uses).

    The union is deliberate. Each source is authoritative for a different moment in a connector's
    life, and an owner whose data is syncing does not care which table remembers it.

    Fail-CLOSED: any exception → False. See the module docstring.
    """
    try:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM tenant_oauth_tokens WHERE tenant_id = %(t)s
                ) OR EXISTS (
                    SELECT 1 FROM tenant_connector_status
                    WHERE tenant_id = %(t)s AND enabled AND last_status = 'ok'
                ) AS connected
                """,
                {"t": str(tenant_id)},
            ).fetchone()
        if _truthy(row, "connected"):
            return True
    except Exception:  # noqa: BLE001 — fail-CLOSED; the honest answer is the safe one
        logger.warning(
            "VT-756: connector-truth read failed (fail-closed -> not connected) tenant=%s", tenant_id
        )
        return False

    # The ingested-data half, read through the same seam journey uses so the two never diverge.
    try:
        from orchestrator.onboarding.shopify_onboarding import PHASE_CONFIRMED, read_integration_state

        state = read_integration_state(tenant_id)
        return bool(
            state and state.get("phase") == PHASE_CONFIRMED and state.get("current_connector_id")
        )
    except Exception:  # noqa: BLE001 — fail-CLOSED
        logger.warning(
            "VT-756: integration-state read failed (fail-closed -> not connected) tenant=%s", tenant_id
        )
        return False


def _truthy(row: Any, key: str) -> bool:
    """Row access that survives both row factories (tuple and dict) — the harness and the app open
    connections differently and this module is read from both."""
    if row is None:
        return False
    return bool(row[key] if isinstance(row, dict) else row[0])
