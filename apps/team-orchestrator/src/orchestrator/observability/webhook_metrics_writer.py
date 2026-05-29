"""VT-226 — webhook_metrics DBOS workflow writer.

Replaces the inline metrics path in team-web's Twilio webhook handler.
The team-web edge route fires a POST to the orchestrator admin endpoint
(see `api/admin/webhook_metrics.py`); the endpoint enqueues this
workflow; DBOS handles retry on transient DB failures.

VT-215 pattern: workflow decoration applied explicitly via
`register_webhook_metrics_workflow()`; no @DBOS.workflow at import.
"""

from __future__ import annotations

import logging

from dbos import DBOS

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)


def write_webhook_metric_workflow(
    *,
    source: str,
    event: str,
    message_sid: str | None,
    source_ip: str,
    response_status: int,
) -> dict[str, object]:
    """Insert one row into webhook_metrics. Returns shape for observability.

    Never raises into the caller — DBOS handles retry on transient
    failures; permanent failures are logged.
    """
    try:
        pool = get_pool()
        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO webhook_metrics
                    (source, event, message_sid, source_ip, response_status)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (source, event, message_sid, source_ip, response_status),
            )
        return {"status": "written", "source": source, "event": event}
    except Exception as exc:  # noqa: BLE001 — workflow must not crash scheduler
        logger.exception(
            "write_webhook_metric_workflow failed (source=%s, event=%s)",
            source, event,
        )
        return {"status": "error", "reason": repr(exc)[:200]}


def register_webhook_metrics_workflow() -> None:
    """Apply @DBOS.workflow to the writer.

    Called from main.py lifespan before launch_dbos. No scheduled
    decoration (this is invoked imperatively via DBOS.start_workflow
    from the admin endpoint).
    """
    DBOS.workflow()(write_webhook_metric_workflow)


__all__ = [
    "write_webhook_metric_workflow",
    "register_webhook_metrics_workflow",
]
