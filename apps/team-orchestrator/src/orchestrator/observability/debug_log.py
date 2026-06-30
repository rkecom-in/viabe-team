"""VT-515 — structured debug/failure event log.

Single entry point: :func:`emit_debug_event`. Call from any failure branch
(discovery, verify, create, OTP) to persist a first-class, PII-redacted
record to ``debug_events`` so the team-web viewer (Supabase Realtime) can
surface failures within ~2s of occurrence — including the silent-degrades
(no_key / scrape_error / zero_results) that previously fell through unlogged.

Design invariants
-----------------
- **Fail-soft** — the emit NEVER raises into the caller. A DB write failure
  writes a ``log.warning`` breadcrumb and drops the row. The calling code
  path (signup, discovery, OTP) is never disrupted by a debug-log failure.
- **PII-redacted at write** — ``error_message`` and ``context`` are passed
  through :func:`orchestrator.privacy.pii_redactor.redact` before the
  INSERT. Raw phone / GSTIN / PAN / Aadhaar values must not reach the table.
  ``error_stack`` is similarly redacted (the stack may carry sensitive values
  in local-variable repr).
- **Stack capture** — when ``error`` is an :class:`Exception`, the traceback
  is captured automatically, redacted, and stored in ``error_stack``.
- **Service-role write** — uses ``get_pool()`` (the RLS-bypassing pool),
  matching the log.py null-tenant path. The table is deny-all for every
  other role.

Failure types (stable vocabulary — the viewer groups by this)
--------------------------------------------------------------
``exception`` · ``timeout`` · ``vendor_error`` · ``network`` ·
``validation`` · ``crash`` · ``silent_degrade``

Severity
--------
``warning`` (recoverable / degraded) · ``error`` (hard failure) ·
``critical`` (data-loss / invariant violation)

Impact
------
``blocked_signup`` · ``degraded_to_manual`` · ``degraded_to_X`` ·
``failed_safe`` · ``None`` (no actionable impact tag)
"""

from __future__ import annotations

import logging
import sys
import traceback
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


def emit_debug_event(
    *,
    failure_type: str,
    component: str,
    operation: str | None = None,
    error: BaseException | str | None = None,
    context: dict[str, Any] | None = None,
    severity: str = "error",
    impact: str | None = None,
    tenant_id: UUID | str | None = None,
    trace_id: str | None = None,
    vendor: str | None = None,
    vendor_status: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """Insert one ``debug_events`` row. Never raises — fail-soft by design.

    Parameters
    ----------
    failure_type:
        One of the stable vocabulary tokens:
        ``exception`` / ``timeout`` / ``vendor_error`` / ``network`` /
        ``validation`` / ``crash`` / ``silent_degrade``.
    component:
        The subsystem that originated the failure:
        ``signup`` / ``discovery`` / ``verify`` / ``create`` / ``otp`` /
        ``knowyourgst`` / ``sandbox`` / ``twilio`` / ``scrapingbee`` /
        ``anthropic`` / …
    operation:
        Optional sub-operation label (e.g. ``scrape_knowyourgst``,
        ``send_otp``, ``entity_confirm``, ``create_tenant``).
    error:
        Either the live exception (stack is captured automatically) or a
        plain string message. Both are PII-redacted before storage.
    context:
        Caller-supplied dict of relevant (non-PII) context. PII-redacted.
    severity:
        ``warning`` / ``error`` / ``critical``.
    impact:
        ``blocked_signup`` / ``degraded_to_manual`` / ``degraded_to_X`` /
        ``failed_safe`` / ``None``.
    tenant_id:
        The tenant UUID when one exists (NULL for pre-tenant failures such
        as pre-create GST-gate rejections).
    trace_id:
        Correlation key: ``discovery_id`` during the discovery leg;
        ``str(tenant_id)`` post-create; ``verification_sid`` for OTP.
    vendor:
        External vendor name if applicable (``sandbox``, ``twilio``,
        ``scrapingbee``, ``anthropic``, …).
    vendor_status:
        Vendor-returned HTTP status code or error label.
    latency_ms:
        Duration of the failed call (ms), when available.
    """
    try:
        _insert(
            failure_type=failure_type,
            component=component,
            operation=operation,
            error=error,
            context=context,
            severity=severity,
            impact=impact,
            tenant_id=tenant_id,
            trace_id=trace_id,
            vendor=vendor,
            vendor_status=vendor_status,
            latency_ms=latency_ms,
        )
    except BaseException as exc:  # noqa: BLE001 — must never propagate
        _warn(
            f"debug_events insert failed: "
            f"failure_type={failure_type!r} component={component!r} "
            f"exc={type(exc).__name__}: {exc}"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _insert(
    *,
    failure_type: str,
    component: str,
    operation: str | None,
    error: BaseException | str | None,
    context: dict[str, Any] | None,
    severity: str,
    impact: str | None,
    tenant_id: UUID | str | None,
    trace_id: str | None,
    vendor: str | None,
    vendor_status: str | None,
    latency_ms: int | None,
) -> None:
    from psycopg.types.json import Jsonb

    from orchestrator.graph import get_pool
    from orchestrator.privacy.pii_redactor import redact

    # --- error_message ---
    raw_msg: str | None
    raw_stack: str | None
    if isinstance(error, BaseException):
        raw_msg = f"{type(error).__name__}: {error}"
        raw_stack = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    elif isinstance(error, str):
        raw_msg = error or None
        raw_stack = None
    else:
        raw_msg = None
        raw_stack = None

    # Redact both the message and the stack — they may carry phone numbers,
    # GSTINs, or other sensitive values in argument repr.
    error_message: str | None = None
    if raw_msg:
        redacted_msg = redact(raw_msg)
        error_message = str(redacted_msg)[:4000] if redacted_msg else None

    error_stack: str | None = None
    if raw_stack:
        redacted_stack = redact(raw_stack)
        error_stack = str(redacted_stack)[:8000] if redacted_stack else None

    # --- context ---
    redacted_context: dict[str, Any] | None = None
    if context:
        rc = redact(context)
        redacted_context = rc if isinstance(rc, dict) else {"_raw": str(rc)}

    tenant_id_str = str(tenant_id) if tenant_id is not None else None

    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO debug_events
                (tenant_id, trace_id, failure_type, component, operation,
                 error_message, error_stack, context, severity, impact,
                 vendor, vendor_status, latency_ms)
            VALUES
                (%s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s,
                 %s, %s, %s)
            """,
            (
                tenant_id_str,
                trace_id,
                failure_type,
                component,
                operation,
                error_message,
                error_stack,
                Jsonb(redacted_context) if redacted_context is not None else None,
                severity,
                impact,
                vendor,
                vendor_status,
                latency_ms,
            ),
        )


def _warn(msg: str) -> None:
    """One-line stderr + logger breadcrumb. Never raises."""
    try:
        logger.warning("[debug_log] %s", msg)
        print(f"[observability/debug_log] {msg}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


__all__ = ["emit_debug_event"]
