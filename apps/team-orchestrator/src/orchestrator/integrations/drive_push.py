"""VT-222 Drive Push DBOS workflows + scheduler (per CL-421).

Three workflows:
- ``pull_sheet_delta_workflow`` — invoked per notification; pulls
  sheet rows since the channel's last_notification_at and routes
  through field-mapping + dedupe.
- ``renew_expiring_drive_channels_body`` — scheduled every 6h; renews
  channels expiring within 48h.
- ``poll_unwatched_sheets_body`` — scheduled every 10min; pulls rows
  for tenants with no active push channel OR stale push notifications
  (>30 min since last_notification_at).

``register_drive_push_scheduler`` is called by ``main.py`` lifespan
BEFORE ``launch_dbos`` — same shape as ``register_purge_scheduler``
(VT-200) and ``register_ingestion_scheduler`` (VT-210/215).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from dbos import DBOS

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)

_RENEW_CRON = "0 */6 * * *"      # every 6 hours
_POLL_CRON = "*/10 * * * *"      # every 10 minutes
_RENEW_WINDOW = timedelta(hours=48)
_STALE_PUSH_THRESHOLD = timedelta(minutes=30)


def pull_sheet_delta_workflow(
    tenant_id: str, connector_id: str, resource_id: str
) -> dict[str, object]:
    """Pull rows since the channel's last_notification_at and LAND them.

    VT-417 PR-2: the pulled rows are now mapped (``ingest.sheet_row_to_canonical``)
    and persisted via ``ingest_customer_rows(acquired_via='drive_sheet')`` — real
    ``customers`` + ``sale`` ledger rows. Previously this counted rows and
    discarded them (the comment claimed a downstream write that never happened —
    ``pull_full`` returned data-less envelopes). ``tenant_id`` is the workflow
    argument (resolved server-side from the verified Drive channel), NEVER from a
    row payload (P3).

    Plain function; ``register_drive_push_scheduler`` applies
    ``@DBOS.workflow()`` explicitly to keep import-time side-effect-free
    (same shape as VT-210's ``ingest_one_connector``).
    """
    from orchestrator.integrations.connectors.google_sheet import (
        GoogleSheetConnector,
    )
    from orchestrator.integrations.ingest import (
        ingest_customer_rows,
        sheet_row_to_canonical,
    )

    try:
        connector = GoogleSheetConnector()
        raw_rows = connector.pull_full(UUID(tenant_id), resource_id)
        canonical = [
            c
            for r in (raw_rows or [])
            if isinstance(r, dict) and (c := sheet_row_to_canonical(r)) is not None
        ]
        summary = ingest_customer_rows(
            UUID(tenant_id), canonical, acquired_via="drive_sheet"
        )
        return {
            "status": "ok",
            "row_count": len(raw_rows) if raw_rows is not None else 0,
            "committed": summary.committed,
            "sales_written": summary.sales_written,
            "tenant_id": tenant_id,
            "connector_id": connector_id,
        }
    except Exception as exc:  # noqa: BLE001 — workflow must not crash scheduler
        logger.exception(
            "pull_sheet_delta_workflow failed (tenant=%s, resource=%s)",
            tenant_id,
            resource_id,
        )
        return {"status": "error", "error": repr(exc)[:200]}


def renew_expiring_drive_channels_body(
    scheduled_time: datetime, actual_time: datetime
) -> None:
    """Find channels expiring within 48h; renew each."""
    cutoff = datetime.now(UTC) + _RENEW_WINDOW
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id, connector_id, resource_id, channel_id, "
            "       channel_token, expires_at "
            "FROM tenant_drive_channels WHERE expires_at <= %s",
            (cutoff,),
        )
        rows = cur.fetchall()

    if not rows:
        return

    from orchestrator.integrations.connectors.google_sheet import (
        GoogleSheetConnector,
    )

    connector = GoogleSheetConnector()
    for row in rows:
        row_dict = dict(row) if not isinstance(row, dict) else row
        try:
            new = connector.renew_drive_push_channel(row_dict)
            logger.info(
                "drive channel renewed: tenant=%s old=%s new=%s",
                row_dict["tenant_id"],
                row_dict["channel_id"],
                new["channel_id"],
            )
        except Exception:  # noqa: BLE001 — renew failures are per-row, not per-tick
            logger.exception(
                "drive channel renewal failed (channel=%s)",
                row_dict["channel_id"],
            )


def poll_unwatched_sheets_body(
    scheduled_time: datetime, actual_time: datetime
) -> None:
    """Pull rows for tenants without active push channels OR with stale notifications.

    Finds (tenant, resource) pairs where:
      - tenant_connector_status row exists AND enabled, AND
      - no tenant_drive_channels row (never registered, OR stopped), OR
      - last_notification_at older than 30 minutes
    Calls pull_sheet_delta_workflow for each.
    """
    stale_before = datetime.now(UTC) - _STALE_PUSH_THRESHOLD
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT tcs.tenant_id::text AS tenant_id,
                   tcs.connector_id,
                   tcs.last_sync_at
            FROM tenant_connector_status tcs
            WHERE tcs.enabled
              AND tcs.connector_id = 'google_sheet'
              AND NOT EXISTS (
                  SELECT 1 FROM tenant_drive_channels tdc
                  WHERE tdc.tenant_id = tcs.tenant_id
                    AND tdc.connector_id = tcs.connector_id
                    AND (
                        tdc.last_notification_at IS NULL
                        OR tdc.last_notification_at > %s
                    )
              )
            """,
            (stale_before,),
        )
        targets = cur.fetchall()

    if not targets:
        return

    for target in targets:
        tenant_id = (
            target["tenant_id"] if isinstance(target, dict) else target[0]
        )
        connector_id = (
            target["connector_id"] if isinstance(target, dict) else target[1]
        )
        # Look up the resource_id (spreadsheet_id) — Phase-1 assumption is
        # one Sheet per tenant; multi-Sheet is out of scope per brief.
        # The tenant_connector_status row's config payload holds it;
        # for now we fetch from oauth row's last_resource_id (added by
        # VT-222 substrate if available) or skip if unknown.
        with pool.connection() as conn2, conn2.cursor() as cur2:
            cur2.execute(
                "SELECT resource_id FROM tenant_drive_channels "
                "WHERE tenant_id = %s AND connector_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (tenant_id, connector_id),
            )
            r2 = cur2.fetchone()
        resource_id = (
            r2["resource_id"] if isinstance(r2, dict) else (r2[0] if r2 else None)
        )
        if not resource_id:
            # Phase-1 unwatched-but-never-registered tenants need
            # operator intervention to register; skip silently here.
            continue
        try:
            DBOS.start_workflow(
                pull_sheet_delta_workflow,
                tenant_id,
                connector_id,
                resource_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "poll_unwatched_sheets: enqueue failed (tenant=%s)",
                tenant_id,
            )


def register_drive_push_scheduler() -> None:
    """Apply @DBOS.workflow + @DBOS.scheduled decorations.

    Called by main.py lifespan BEFORE launch_dbos(). Mirrors
    register_purge_scheduler / register_ingestion_scheduler ordering
    (workflow FIRST, scheduled SECOND — VT-200 / VT-215 lessons).
    """
    DBOS.workflow()(pull_sheet_delta_workflow)
    DBOS.workflow()(renew_expiring_drive_channels_body)
    DBOS.workflow()(poll_unwatched_sheets_body)
    DBOS.scheduled(_RENEW_CRON)(renew_expiring_drive_channels_body)
    DBOS.scheduled(_POLL_CRON)(poll_unwatched_sheets_body)


__all__ = [
    "pull_sheet_delta_workflow",
    "renew_expiring_drive_channels_body",
    "poll_unwatched_sheets_body",
    "register_drive_push_scheduler",
]
