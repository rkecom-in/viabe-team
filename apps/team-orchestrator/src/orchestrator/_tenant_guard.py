"""Tenant-scoping guard for the Context Composer (VT-3.4 PR 2/3).

Belt-and-braces over RLS (CL-71 / migration 015): even with `app_role` + the
`app.current_tenant` GUC enforcing Row-Level Security, every raw DB read in
``context_builder`` is additionally asserted here. Defence in depth — a tenant
mismatch is a security failure, surfaced loud.

Fail-loud on a missing tenant_id (CL-195)
-----------------------------------------
A graph invocation that fails to set ``tenant_id`` will have
``state.get('tenant_id') is None``. ``expected_tenant_id=None`` then mismatches
every real DB row (no row has ``tenant_id = None``), and this guard raises
``TenantIsolationError`` on every read.

This is INTENTIONAL fail-loud behaviour. A missing tenant_id at runtime is a
security-relevant bug; a silent fallback would be worse than loud failure. Do
NOT "fix" the guard to permit ``None`` — fix the upstream node that did not
populate state.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger("orchestrator.tenant_guard")


class TenantIsolationError(RuntimeError):
    """Raised when a tenant-scoped query returns a row for another tenant."""


def emit_pipeline_step(*, step_kind: str, severity: str, payload: dict[str, Any]) -> None:
    """Emit a structured observability event.

    VT-122 owns the durable observability writer (the ``@observability.step``
    decorator that persists to the ``pipeline_steps`` table — see the TODO in
    transitions.py). Until VT-122 lands, this emits the event as a structured
    log record only; the DB write is deferred. Callers and tests treat it as
    the event-emission seam regardless of backing store.

    Log level tracks severity: 'high' -> ERROR, 'info' -> INFO, else WARNING.
    """
    level = {"high": logging.ERROR, "info": logging.INFO}.get(
        severity, logging.WARNING
    )
    logger.log(
        level,
        "pipeline_step event: kind=%s severity=%s payload=%s",
        step_kind,
        severity,
        payload,
        extra={"step_kind": step_kind, "severity": severity, "observability": True},
    )


def assert_tenant_scoped(
    rows: list[dict[str, Any]], expected_tenant_id: UUID | None
) -> None:
    """Assert every row's ``tenant_id`` equals ``expected_tenant_id``.

    Empty ``rows`` passes vacuously. A row with a mismatched (or missing)
    ``tenant_id`` is a security failure: the breach is logged at ERROR and
    emitted as a high-severity ``tenant_isolation_breach`` observability event
    BEFORE the raise, so it is captured even if an upstream caller swallows the
    exception.

    ``expected_tenant_id=None`` (upstream did not populate tenant_id) mismatches
    every real row by design — see the module docstring.
    """
    if not rows:
        return
    # A row missing the tenant_id key entirely is also a breach — never let a
    # keyless row pass (it would slip through when expected_tenant_id is None).
    bad = [
        r
        for r in rows
        if "tenant_id" not in r or r.get("tenant_id") != expected_tenant_id
    ]
    if bad:
        logger.error(
            "TENANT_ISOLATION_BREACH: tenant-scoped query returned rows with a "
            "mismatched tenant_id",
            extra={
                "expected_tenant_id": str(expected_tenant_id),
                "breach_count": len(bad),
                "security_tag": True,
            },
        )
        emit_pipeline_step(
            step_kind="tenant_isolation_breach",
            severity="high",
            payload={
                "expected": str(expected_tenant_id),
                "breach_count": len(bad),
            },
        )
        raise TenantIsolationError(
            f"Query returned {len(bad)} rows with tenant_id != {expected_tenant_id}"
        )
