"""Deterministic error router (VT-29).

``route_failure`` picks a recovery ``Strategy`` for a classified
``FailureRecord``. Pillar 1: deterministic, no LLM, no reasoning. The
mapping is just policy lookup with a retry-count override.

Routing rule
------------
1. If the failure's ``run_state`` shows the retry count for this failure
   type has hit ``escalation_threshold``, override the default strategy
   with an escalation (owner or Fazal, per severity).
2. Otherwise return ``spec.default_strategy``.
3. ``unknown_error`` ALWAYS escalates — its spec already says
   ``ESCALATE_TO_FAZAL``, but rule (1) is short-circuited so it cannot
   accidentally retry even with retry_count == 0.

Logging
-------
Each routing decision lands in ``pipeline_steps`` (the VT-12.2 column
list — error_envelope captures the failure, output_envelope captures
the chosen strategy + rationale). Logging goes through
``tenant_connection`` so RLS is enforced (CL-122 / Pillar 3).

If ``failure.tenant_id`` / ``failure.run_id`` are absent (e.g. a
webhook_signature_failure rejected before tenant resolution) the
routing decision is returned but NOT persisted — there is no tenant to
scope the row to. The caller is expected to log the rejection via a
non-RLS mechanism (FastAPI logger, etc.).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, cast

from psycopg.types.json import Jsonb

from orchestrator.db import tenant_connection
from orchestrator.failures import (
    SPECS,
    FailureRecord,
    FailureType,
    Severity,
)
from orchestrator.strategies import Strategy

logger = logging.getLogger(__name__)


def _escalation_target(severity: Severity) -> Strategy:
    """High/critical severity → Fazal; medium → owner. Low never reaches here
    because nothing in SPECS has Severity.LOW today; future entries may.
    """
    if severity in (Severity.HIGH, Severity.CRITICAL):
        return Strategy.ESCALATE_TO_FAZAL
    return Strategy.ESCALATE_TO_OWNER


def route_failure(
    failure: FailureRecord,
    run_state: Mapping[str, object] | None = None,
) -> Strategy:
    """Return the ``Strategy`` for ``failure``.

    ``run_state`` is the live ``SubscriberState`` (or a Mapping with the
    same keys). When ``run_state["history"]`` contains a
    ``{"event": "failure", "failure_type": ...}`` record, the prior
    retry count for the same failure type is used to compare against
    ``escalation_threshold``. A missing or non-list ``history`` means
    "first occurrence".

    The decision is persisted to ``pipeline_steps`` when both
    ``failure.tenant_id`` and ``failure.run_id`` are present. Logging
    failures do NOT raise — observability must not break recovery.
    """
    spec = SPECS[failure.failure_type]

    # Rule 3: unknown_error is short-circuited before any retry-count override.
    if failure.failure_type == FailureType.UNKNOWN_ERROR:
        strategy = spec.default_strategy
    else:
        retry_count = _retry_count(failure.failure_type, run_state)
        if retry_count >= spec.escalation_threshold:
            strategy = _escalation_target(spec.severity)
        else:
            strategy = spec.default_strategy

    _log_decision(failure, strategy)
    return strategy


def _retry_count(failure_type: FailureType, run_state: Mapping[str, object] | None) -> int:
    """Count prior occurrences of ``failure_type`` in ``run_state["history"]``."""
    if run_state is None:
        return 0
    history = run_state.get("history")
    if not isinstance(history, list):
        return 0
    return sum(
        1
        for entry in history
        if isinstance(entry, dict)
        and entry.get("event") == "failure"
        and entry.get("failure_type") == failure_type.value
    )


def _log_decision(failure: FailureRecord, strategy: Strategy) -> None:
    """Persist the routing decision to ``pipeline_steps`` (best-effort)."""
    if failure.tenant_id is None or failure.run_id is None:
        logger.info(
            "error_router: %s -> %s (not persisted — no tenant/run context)",
            failure.failure_type.value,
            strategy.value,
        )
        return
    try:
        with tenant_connection(failure.tenant_id) as conn, conn.transaction():
            # dict_row factory is configured on the pool (graph.py); mypy can't
            # see it through psycopg's generic Row type, so cast at the seam.
            raw = conn.execute(
                "SELECT COALESCE(MAX(step_index), 0) + 1 AS next "
                "FROM pipeline_steps WHERE run_id = %s",
                (str(failure.run_id),),
            ).fetchone()
            row = cast("dict[str, Any]", raw)
            next_index = int(row["next"])
            conn.execute(
                """
                INSERT INTO pipeline_steps
                    (run_id, tenant_id, step_index, step_kind,
                     output_envelope, error_envelope, rationale)
                VALUES (%s, %s, %s, 'error_router_decision', %s, %s, %s)
                """,
                (
                    str(failure.run_id),
                    str(failure.tenant_id),
                    next_index,
                    Jsonb({"strategy": strategy.value}),
                    Jsonb(
                        {
                            "failure_type": failure.failure_type.value,
                            "message": failure.message,
                            "vendor": failure.vendor,
                            "metadata": failure.metadata,
                            "occurred_at": failure.occurred_at.isoformat(),
                        }
                    ),
                    f"{failure.failure_type.value} -> {strategy.value}",
                ),
            )
    except Exception:
        # Observability must not break recovery. Surface via logs, not raise.
        logger.exception("error_router: failed to persist decision")


__all__ = ["route_failure"]
