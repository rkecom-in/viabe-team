"""VT-210 — Recurring ingestion scheduler (single fan-out DBOS scheduled).

Q1/Q2/Q3/Q4 Option A locked per Cowork plan-review 2026-05-28.

Mechanism mirrors ``dbos_purge.purge_workflow_inputs_scheduled``: the
function body is a plain function in this module (no import-time
decoration), and ``register_ingestion_scheduler()`` applies
``@DBOS.scheduled('*/5 * * * *')`` explicitly. ``main.py`` lifespan
calls it once before ``launch_dbos()``. Keeping the decoration off
import-time keeps ``DBOSRegistry.compute_app_version`` stable for any
test or admin path that imports this module purely for
``run_due_ingestions`` / ``ingest_one_connector``.

Cadence: ``*/5 * * * *`` — every 5 minutes, scan
``tenant_connector_status WHERE enabled AND next_scheduled_run <= now()``
and dispatch one ``ingest_one_connector`` workflow per due row.
``pull_cadence`` is per-row; Phase-1 supports daily ``"M H * * *"``
patterns (e.g. ``"0 9 * * *"`` = 9:00 IST daily) — any other shape
raises so a misconfigured cadence is visible at registration time.

Failure escalation: 3 consecutive ``ingest_one_connector`` failures
write ``token_expired_reconnect`` into
``tenant_integration_state.pending_owner_input`` (per Q2 lock). The
agent polls that JSONB on its next dispatch.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

from dbos import DBOS

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)

_SCHEDULER_CRON = "*/5 * * * *"
_FAILURE_ESCALATION_THRESHOLD = 3


def _paused(tenant_id: UUID) -> bool:
    """VT-374 pause check (kind 'ingestion'). SKIP semantics: a paused tenant's due row
    stays due (``next_scheduled_run`` untouched, no fail-count bump), so the 5-minute
    scheduler naturally resumes the pull after /release — no blocking hold needed.
    check_pause never raises (F9 two-tier)."""
    from orchestrator.run_control import check_pause

    return check_pause(tenant_id, "ingestion")


def _parse_daily_cron(expr: str) -> tuple[int, int]:
    """Parse a Phase-1 ``"M H * * *"`` daily-at-time cron expression.

    Returns ``(hour, minute)``. Raises ``ValueError`` on anything outside
    this shape — the column has a CHECK at the application boundary
    (registration), not the DB; broader cron support comes when a real
    parser dependency lands.
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"pull_cadence must be 5-field cron, got: {expr!r}")
    minute_s, hour_s, dom, mon, dow = parts
    if dom != "*" or mon != "*" or dow != "*":
        raise ValueError(
            "Phase-1 pull_cadence supports only daily 'M H * * *' patterns; "
            f"got: {expr!r}"
        )
    minute = int(minute_s)
    hour = int(hour_s)
    if not (0 <= minute <= 59 and 0 <= hour <= 23):
        raise ValueError(f"pull_cadence out of range: {expr!r}")
    return hour, minute


def _compute_next_run(pull_cadence: str, after: datetime) -> datetime:
    """Compute the next fire-time strictly after ``after``.

    Stores in UTC. Owners express cadence in IST (UTC+5:30); since
    Phase-1 uses ``"0 H * * *"`` daily, we compute the next instant of
    HH:MM IST after the ``after`` moment and convert to UTC.
    """
    hour_ist, minute_ist = _parse_daily_cron(pull_cadence)
    ist_offset = timedelta(hours=5, minutes=30)
    after_ist = after.astimezone(UTC) + ist_offset
    target_today_ist = datetime.combine(
        after_ist.date(), time(hour=hour_ist, minute=minute_ist), tzinfo=UTC
    )
    if target_today_ist <= after_ist.replace(tzinfo=UTC):
        target_today_ist = target_today_ist + timedelta(days=1)
    return target_today_ist - ist_offset


def _ingest_pulled_rows(
    tenant_id: UUID, connector_id: str, pulled: list, *, field_mapping: dict[str, str] | None = None
) -> int:
    """Map a connector's ``pull_full`` rows → ``CanonicalRow`` and land them via
    ``ingest_customer_rows``. Returns the committed-customer count.

    Connector-aware mapping (the pull shapes differ):
      * ``google_sheet`` — rows are ``{column -> cell}`` dicts → ``sheet_row_to_canonical``.
        ``field_mapping`` (VT-608 fix round CRITICAL 2) — the owner-CONFIRMED mapping persisted in
        ``tenant_connector_status.field_mapping`` (migration 168) — is threaded through so a
        RECURRING pull honours the SAME confirmed mapping the initial commit used, not just the
        alias-guess fallback. ``None`` (no mapping confirmed, or a non-Sheets connector) keeps the
        exact pre-existing alias-table behavior.
      * ``shopify`` — ``pull_full`` returns CUSTOMER dicts (identity only; NO orders/
        sales — the order-of-record substrate arrives via the webhook + ``backfill_orders``).
        We land identity (phone/email/name); no sale is fabricated from a customer row.

    tenant_id is the scheduler's server-derived argument (P3), never a row field.
    A connector with no recognized pull shape lands nothing (returns 0) — never a
    silent wrong-shape write.
    """
    from orchestrator.integrations.ingest import (
        CanonicalRow,
        ingest_customer_rows,
        sheet_row_to_canonical,
    )

    rows: list[CanonicalRow] = []
    if connector_id == "google_sheet":
        rows = [
            c
            for r in pulled
            if isinstance(r, dict)
            and (c := sheet_row_to_canonical(r, mapping=field_mapping)) is not None
        ]
        acquired_via = "google_sheet"
    elif connector_id == "shopify":
        from orchestrator.integrations.connectors.shopify import _normalize_e164

        for r in pulled:
            if not isinstance(r, dict):
                continue
            phone = _normalize_e164(r.get("phone"))
            email_raw = r.get("email")
            email = (
                str(email_raw).strip().lower()
                if email_raw and str(email_raw).strip() else None
            )
            name = (
                f"{r.get('first_name', '') or ''} {r.get('last_name', '') or ''}".strip()
                or None
            )
            if phone or email or name:
                rows.append(
                    CanonicalRow(
                        phone_e164=phone, email=email, display_name=name,
                        sales=(), consent=None,  # identity-only; no sale from a customer row
                    )
                )
        acquired_via = "shopify"
    else:
        # Unknown pull shape — do NOT guess a mapping (would risk wrong-shape writes).
        logger.info(
            "_ingest_pulled_rows: connector=%s has no pull mapper — rows not landed",
            connector_id,
        )
        return 0

    if not rows:
        return 0
    summary = ingest_customer_rows(tenant_id, rows, acquired_via=acquired_via)
    return summary.committed


def ingest_one_connector(tenant_id: UUID, connector_id: str) -> dict[str, object]:
    """Run one pull for (tenant, connector); update status.

    Plain function (no @DBOS.workflow at import). Caller may invoke
    directly (tests / admin) or via ``DBOS.start_workflow`` from the
    scheduler. The wrapping decorator is applied in
    ``register_ingestion_scheduler``.

    Logic:
      1. Load row from ``tenant_connector_status``.
      2. Resolve connector via registry; call ``pull_full`` from
         ``last_sync_at`` cursor.
      3. On success: zero ``consecutive_fails``; ``last_status='ok'``;
         bump ``rows_ingested_today`` (reset on date roll); set
         ``next_scheduled_run`` via cron parse; ``updated_at = now``.
      4. On failure: increment ``consecutive_fails``; ``last_status='error'``;
         persist short ``last_error_message`` (first 200 chars). If new
         count >= 3, write the ``token_expired_reconnect`` envelope into
         ``tenant_integration_state.pending_owner_input``.
    """
    from orchestrator.integrations.registry import get_connector

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT pull_cadence, last_sync_at, consecutive_fails, "
            "rows_ingested_today, last_ingested_date, field_mapping "
            "FROM tenant_connector_status "
            "WHERE tenant_id = %s AND connector_id = %s",
            (str(tenant_id), connector_id),
        )
        raw = cur.fetchone()
    if raw is None:
        logger.warning(
            "ingest_one_connector: no status row for tenant=%s connector=%s",
            tenant_id,
            connector_id,
        )
        return {"status": "no_row"}

    if isinstance(raw, dict):
        row = raw
    else:
        cols = ["pull_cadence", "last_sync_at", "consecutive_fails",
                "rows_ingested_today", "last_ingested_date", "field_mapping"]
        row = dict(zip(cols, raw, strict=True))

    # VT-374 (ingestion, connector_pull) seam — defense-in-depth with the fan-out check
    # (the rerun arm enters here directly): a paused tenant's pull is skipped with the
    # status row untouched, so release resumes it on the next due tick.
    if _paused(tenant_id):
        logger.info(
            "ingest_one_connector: tenant=%s connector=%s paused by run-control — skipped",
            tenant_id,
            connector_id,
        )
        return {"status": "paused"}

    spec = get_connector(connector_id)
    connector_cls = _connector_class_for(connector_id)

    rows_pulled = 0
    rows_committed = 0
    error_message: str | None = None
    try:
        connector = connector_cls()
        # ConnectorBase Phase-1 contract: pull_full(tenant_id, since=...)
        # Cursor-aware connectors (google_sheet uses row-index) deviate
        # from datetime since; their adapters reconcile internally.
        pulled = connector.pull_full(tenant_id, since=row["last_sync_at"])
        rows_pulled = len(pulled) if pulled is not None else 0
        # VT-417 PR-2: LAND the pulled rows instead of counting+discarding them.
        # Map each connector-pull row → CanonicalRow (connector-aware) → real
        # customers + sale ledger via ingest_customer_rows. tenant_id is the
        # scheduler's server-derived argument (P3), never from a row.
        rows_committed = _ingest_pulled_rows(
            tenant_id, connector_id, pulled or [], field_mapping=row.get("field_mapping")
        )
    except Exception as exc:  # noqa: BLE001 — scheduler must not crash
        error_message = repr(exc)[:200]
        logger.exception(
            "ingest_one_connector failed: tenant=%s connector=%s",
            tenant_id,
            connector_id,
        )

    now = datetime.now(UTC)
    next_run = _compute_next_run(row["pull_cadence"], now)
    today = now.date()
    if row["last_ingested_date"] != today:
        rows_today = rows_pulled
    else:
        rows_today = int(row["rows_ingested_today"]) + rows_pulled

    if error_message is None:
        new_status = "ok"
        new_fails = 0
    else:
        new_status = "error"
        new_fails = int(row["consecutive_fails"]) + 1

    with pool.connection() as conn:
        conn.execute(
            """
            UPDATE tenant_connector_status SET
                last_sync_at = %s,
                last_status = %s,
                last_error_message = %s,
                consecutive_fails = %s,
                rows_ingested_today = %s,
                last_ingested_date = %s,
                next_scheduled_run = %s,
                updated_at = now()
            WHERE tenant_id = %s AND connector_id = %s
            """,
            (
                now, new_status, error_message, new_fails,
                rows_today, today, next_run,
                str(tenant_id), connector_id,
            ),
        )
        if new_fails >= _FAILURE_ESCALATION_THRESHOLD:
            envelope = {
                "phase_change_required": "token_expired_reconnect",
                "connector_id": connector_id,
                "last_error": error_message,
                "consecutive_fails": new_fails,
            }
            conn.execute(
                """
                UPDATE tenant_integration_state SET
                    pending_owner_input = %s::jsonb,
                    updated_at = now()
                WHERE tenant_id = %s
                """,
                (json.dumps(envelope), str(tenant_id)),
            )
            logger.warning(
                "escalated token_expired_reconnect: tenant=%s connector=%s "
                "consecutive_fails=%d",
                tenant_id,
                connector_id,
                new_fails,
            )

    _ = spec  # spec referenced for future per-connector dispatch knobs
    return {
        "status": new_status,
        "rows_pulled": rows_pulled,
        "rows_committed": rows_committed,
        "consecutive_fails": new_fails,
    }


def _connector_class_for(connector_id: str) -> type:
    """Map ``connector_id`` → concrete connector class.

    Kept as a small registry function rather than added to
    ``ConnectorSpec`` so the spec stays a pure data record — the
    class-discovery seam is one place to import all concrete
    connectors and avoids registry-vs-class cyclical deps.
    """
    if connector_id == "google_sheet":
        from orchestrator.integrations.connectors.google_sheet import (
            GoogleSheetConnector,
        )

        return GoogleSheetConnector
    if connector_id == "shopify":
        from orchestrator.integrations.connectors.shopify import ShopifyConnector

        return ShopifyConnector
    raise ValueError(f"no concrete connector class for: {connector_id!r}")


def run_due_ingestions() -> int:
    """Scheduler body — fan out one workflow per due row.

    Returns the count of workflows dispatched (for telemetry / canary).
    Plain function so tests can drive it without the @DBOS.scheduled
    poller plumbing.
    """
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id, connector_id FROM tenant_connector_status "
            "WHERE enabled = TRUE AND next_scheduled_run <= now()"
        )
        rows = cur.fetchall()

    dispatched = 0
    for raw in rows:
        if isinstance(raw, dict):
            tenant_id_s = raw["tenant_id"]
            connector_id = raw["connector_id"]
        else:
            tenant_id_s, connector_id = raw[0], raw[1]
        tenant_id = UUID(str(tenant_id_s))
        # VT-374 — pause check before fan-out: a paused tenant is not dispatched at all
        # (cheaper than dispatching a workflow that would skip itself).
        if _paused(tenant_id):
            logger.info(
                "scheduler: tenant=%s connector=%s paused by run-control — dispatch skipped",
                tenant_id,
                connector_id,
            )
            continue
        try:
            DBOS.start_workflow(ingest_one_connector, tenant_id, connector_id)
            dispatched += 1
        except Exception:  # noqa: BLE001 — one bad row must not halt the sweep
            logger.exception(
                "scheduler: failed to dispatch tenant=%s connector=%s",
                tenant_id,
                connector_id,
            )
    return dispatched


def ingestion_scheduler_body(
    scheduled_time: datetime, actual_time: datetime
) -> None:
    """Plain scheduled-function body. ``register_ingestion_scheduler``
    applies ``@DBOS.scheduled`` explicitly so import-time has no DBOS
    side-effect (mirrors ``dbos_purge`` pattern).
    """
    try:
        dispatched = run_due_ingestions()
        if dispatched:
            logger.info(
                "ingestion scheduler dispatched %d workflow(s) at %s",
                dispatched,
                actual_time.isoformat(),
            )
    except Exception:  # noqa: BLE001 — scheduler must keep firing
        logger.exception("ingestion scheduler sweep raised; cadence continues")


def register_ingestion_scheduler() -> None:
    """Apply ``@DBOS.scheduled`` decoration + ``@DBOS.workflow`` decoration.

    Called from ``main.py`` lifespan BEFORE ``launch_dbos()``. Mirrors
    ``register_purge_scheduler`` so import-time stays side-effect-free.

    VT-215 hygiene fix: ``ingestion_scheduler_body`` gets
    ``@DBOS.workflow`` BEFORE ``@DBOS.scheduled``. Without it the
    scheduler poller emits ``DBOSWorkflowFunctionNotFoundError`` every
    cron tick (same bug VT-200 closed for the purge scheduler).
    """
    DBOS.workflow()(ingest_one_connector)
    DBOS.workflow()(ingestion_scheduler_body)
    DBOS.scheduled(_SCHEDULER_CRON)(ingestion_scheduler_body)


__all__ = [
    "ingest_one_connector",
    "ingestion_scheduler_body",
    "register_ingestion_scheduler",
    "run_due_ingestions",
]
