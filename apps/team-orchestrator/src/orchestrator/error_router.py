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
list — ``error`` captures the failure, ``output_envelope`` captures
the chosen strategy + ``decision_rationale`` — both renamed under
VT-187 / CL-417 schema normalization). VT-379: the write goes through
``pipeline_observability.write_redacted_step_row`` so the failure
message / metadata (free text, verbatim model output) are PII-redacted
at write — the previous direct INSERT wrote them raw. RLS is enforced
via ``tenant_connection`` inside the helper (CL-122 / Pillar 3).

If ``failure.tenant_id`` / ``failure.run_id`` are absent (e.g. a
webhook_signature_failure rejected before tenant resolution) the
routing decision is returned but NOT persisted — there is no tenant to
scope the row to. The caller is expected to log the rejection via a
non-RLS mechanism (FastAPI logger, etc.).
"""

from __future__ import annotations

import logging
from typing import Mapping

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


# VT-501 — the alert sweep (alerts/triggers.py error_envelope detector) surfaces
# ``pipeline_steps.step_name`` for each 'error' row, falling back to the literal
# string ``"unknown"`` when it is NULL. ``_log_decision`` historically wrote NO
# step_name, so EVERY error envelope read as "unknown" — the page was un-actionable
# (a CampaignPlan schema rejection looked identical to a genuine DB error). This map
# refines the ONE over-broad failure_type (``agent_invalid_output`` lumps four
# distinct causes carried in ``metadata['source']``) into an actionable code, so a
# schema miss reads as ``schema_rejection`` — distinct from a genuine error. All
# other failure_types already have a self-describing ``.value`` (database_error,
# unknown_error, …) and pass through unchanged.
_INVALID_OUTPUT_SOURCE_CODES: dict[str, str] = {
    "agent_schema_rejection": "schema_rejection",
    "agent_terminal_no_dict": "invalid_output_no_json",
    "agent_variant_discriminator_invalid": "invalid_variant_discriminator",
    "self_evaluate_gate": "self_evaluate_seam_error",
}


def _failure_code(failure: FailureRecord) -> str:
    """A non-PII, actionable code for the error row's ``step_name`` (VT-501).

    Base = the classified ``failure_type.value`` (already self-describing for
    every type except ``agent_invalid_output``). For ``agent_invalid_output`` —
    which lumps schema rejection / non-JSON / bad-discriminator / gate-seam under
    one type — refine via the ``metadata['source']`` the SR emit-sites already set
    (``sales_recovery._emit_invalid_output``), so a schema miss is distinguishable
    from a genuine unhandled error. Enum-derived strings only; never free text /
    a value, so it carries no PII and survives the write unredacted.
    """
    base = failure.failure_type.value
    if failure.failure_type is FailureType.AGENT_INVALID_OUTPUT:
        source = failure.metadata.get("source")
        if isinstance(source, str):
            return _INVALID_OUTPUT_SOURCE_CODES.get(source, base)
    return base


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
        # VT-379: route through the shared redacting writer — failure.message
        # is free text and failure.metadata can carry verbatim model output
        # (e.g. dropped_values); the redactor + tenant name registry run at
        # write. Row semantics (step_kind/seq/columns) preserved exactly.
        from orchestrator.observability.pipeline_observability import (
            write_redacted_step_row,
        )

        write_redacted_step_row(
            run_id=failure.run_id,
            tenant_id=failure.tenant_id,
            step_kind="error",
            # VT-501: carry the actionable failure code so the error_envelope alert
            # surfaces (e.g.) 'schema_rejection' instead of the NULL→'unknown' fallback.
            step_name=_failure_code(failure),
            output_envelope={"strategy": strategy.value},
            error={
                "failure_type": failure.failure_type.value,
                "message": failure.message,
                "vendor": failure.vendor,
                "metadata": failure.metadata,
                "occurred_at": failure.occurred_at.isoformat(),
            },
            decision_rationale=f"{failure.failure_type.value} -> {strategy.value}",
        )
    except Exception:
        # Observability must not break recovery. Surface via logs, not raise.
        logger.exception("error_router: failed to persist decision")


__all__ = ["route_failure"]
