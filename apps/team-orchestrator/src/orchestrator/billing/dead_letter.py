"""VT-352 — Razorpay webhook dead-letter queue: observability + programmatic replay (Option B).

VT-330 record-and-drop commits a parse-error event (the raw stays in
``razorpay_webhook_events.payload.raw`` for reconciliation) + the ingress enqueues a PII-free row
here. This module is the durable, programmatic replay form for the LIVE money path:

- :func:`list_pending` — the still-stuck drops an operator/sweep must act on (observability).
- :func:`replay` — re-feed a CORRECTED event through the ingress so its fee applies (F1). The
  ingress re-processes the un-applied drop ATOMICALLY and flips this row to ``replayed``.

Service-role only (the dead-letter table is deny-all RLS); the bare pool mirrors the ingress.

F7 (Cowork fold-in) — these have NO caller yet. Wiring them is a TEAM_RAZORPAY_LIVE CUTOVER
acceptance item (alongside the Idempotency-Key real-API canary): either a scheduled job that
``list_pending`` → alerts, or a runbook step. Tracked in the VT-352 row's pre-LIVE acceptance. Do
NOT auto-wire pre-LIVE (no real settlements yet).
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


def count_pending() -> int:
    """Count of still-stuck dropped events (status='pending') — the sweep's observability
    metric. Read-only (no money effect); the scheduled sweep alerts when this is > 0.

    Uses ``dict_row`` access (``row['n']``) because the graph pool sets
    ``row_factory=dict_row`` by default — a positional ``row[0]`` does a dict
    key lookup of ``0`` and KeyErrors."""
    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM razorpay_webhook_dead_letter WHERE status = 'pending'"
        )
        row = cur.fetchone()
        return int(row["n"]) if row else 0


def replay(event_id: str, corrected_payload: dict[str, Any]) -> dict[str, Any]:
    """Re-feed a CORRECTED event through the ingress so the dropped charge's fee NOW applies
    (VT-352 F1). The corrected payload is operator-supplied (e.g. the fixed integer amount); the
    ingress re-processes the un-applied drop atomically and flips the dead-letter row to
    'replayed'. Returns the ingress result dict. Raises if no dead-letter row exists for the id."""
    from orchestrator.api.razorpay_ingress import (
        RazorpayIngressBody,
        _subscription_id,
        razorpay_ingress,
    )

    with get_pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT event_type, event_payload FROM razorpay_webhook_dead_letter WHERE event_id = %s",
            (event_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"no dead-letter row for event_id={event_id}")
    # F2 (Cowork bounce): tenant-scope cross-check. The dropped charge belongs to a specific
    # subscription; a typo'd subscription_id in the corrected payload would apply the fee to a
    # DIFFERENT tenant (inflates their cumulative_fees → fee over-count). Reject
    # when the corrected subscription_id ≠ the dead-letter row's original (if the original had one).
    original_sub = (row["event_payload"] or {}).get("subscription_id")
    corrected_sub = _subscription_id(corrected_payload)
    if original_sub is not None and corrected_sub != original_sub:
        raise ValueError(
            f"corrected subscription_id {corrected_sub!r} != dead-letter original "
            f"{original_sub!r} (event_id={event_id}) — refusing cross-tenant replay"
        )
    body = RazorpayIngressBody(
        event_id=event_id, event_type=row["event_type"], payload=corrected_payload
    )
    # Pass the secret EXPLICITLY — calling the route fn directly leaves the Header() default as a
    # FieldInfo, not the value, so _verify_internal_secret would fail (FastAPI direct-call trap).
    return razorpay_ingress(body, x_internal_secret=os.environ.get("INTERNAL_API_SECRET"))
