"""VT-352 — Razorpay webhook dead-letter queue: observability + programmatic replay (Option B).

VT-330 record-and-drop commits a parse-error event (the raw stays in
``razorpay_webhook_events.payload.raw`` for reconciliation) + the ingress enqueues a PII-free row
here. This module is the durable, programmatic replay form for the LIVE money path:

- :func:`list_pending` — the still-stuck drops an operator/sweep must act on (observability).
- :func:`replay` — re-feed a CORRECTED event through the ingress so its fee applies (F1). The
  ingress re-processes the un-applied drop ATOMICALLY and flips this row to ``replayed``.

Service-role only (the dead-letter table is deny-all RLS); the bare pool mirrors the ingress.
"""

from __future__ import annotations

import os
from typing import Any

from psycopg.rows import dict_row

from orchestrator.graph import get_pool


def list_pending(limit: int = 50) -> list[dict[str, Any]]:
    """The still-stuck dropped events (status='pending'), oldest first — what to replay."""
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT event_id, event_type, error_reason, retry_count, first_seen, last_retry "
            "FROM razorpay_webhook_dead_letter WHERE status = 'pending' "
            "ORDER BY first_seen LIMIT %s",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def replay(event_id: str, corrected_payload: dict[str, Any]) -> dict[str, Any]:
    """Re-feed a CORRECTED event through the ingress so the dropped charge's fee NOW applies
    (VT-352 F1). The corrected payload is operator-supplied (e.g. the fixed integer amount); the
    ingress re-processes the un-applied drop atomically and flips the dead-letter row to
    'replayed'. Returns the ingress result dict. Raises if no dead-letter row exists for the id."""
    from orchestrator.api.razorpay_ingress import RazorpayIngressBody, razorpay_ingress

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT event_type FROM razorpay_webhook_dead_letter WHERE event_id = %s",
            (event_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"no dead-letter row for event_id={event_id}")
    body = RazorpayIngressBody(
        event_id=event_id, event_type=row["event_type"], payload=corrected_payload
    )
    # Pass the secret EXPLICITLY — calling the route fn directly leaves the Header() default as a
    # FieldInfo, not the value, so _verify_internal_secret would fail (FastAPI direct-call trap).
    return razorpay_ingress(body, x_internal_secret=os.environ.get("INTERNAL_API_SECRET"))
