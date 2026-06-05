"""Soft schema validation for pipeline_log payloads (VT-102).

Per the brief: invalid payloads are still written, with a
``payload_validation_failed: true`` annotation injected by the writer. The
validator returns the failure detail; the writer decides what to do with it.

No pydantic — the writer is on the hot path and pydantic's startup +
per-validation cost is meaningful. A flat dict of callable validators is
cheaper, transparent, and the failure mode is the same: soft warning, no
hard reject.

The list of event types is intentionally finite — Pillar 8 (one taxonomy).
Adding a new event type goes through code review.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


# A field validator returns ``None`` on success or a string error on failure.
Validator = Callable[[Any], str | None]


def _required_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return f"expected str, got {type(value).__name__}"
    if not value:
        return "expected non-empty str"
    return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return _required_str(value)


def _required_int(value: Any) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return f"expected int, got {type(value).__name__}"
    return None


def _required_uuid_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return f"expected uuid str, got {type(value).__name__}"
    # Cheap shape check — full uuid parse is overkill on the hot path.
    if value.count("-") != 4 or len(value) != 36:
        return "expected canonical uuid format (8-4-4-4-12)"
    return None


def _required_dict(value: Any) -> str | None:
    if not isinstance(value, dict):
        return f"expected dict, got {type(value).__name__}"
    return None


# Each schema maps required-field-name → Validator. Optional fields are
# present-but-allowed-to-be-None; absent fields don't fail validation. The
# writer copies the payload verbatim modulo redaction, so additional fields
# are tolerated by design.
EVENT_SCHEMAS: dict[str, dict[str, Validator]] = {
    "webhook_received": {
        "channel": _required_str,
        "message_sid": _optional_str,
    },
    "webhook_signature_verified": {
        "channel": _required_str,
        "ok": lambda v: None if isinstance(v, bool) else f"expected bool, got {type(v).__name__}",
    },
    "agent_dispatched": {
        "agent_name": _required_str,
    },
    "tool_invoked": {
        "tool_name": _required_str,
    },
    "tool_completed": {
        "tool_name": _required_str,
        "ok": lambda v: None if isinstance(v, bool) else f"expected bool, got {type(v).__name__}",
    },
    "db_write": {
        "table_name": _required_str,
        "operation_type": lambda v: (
            None
            if v in ("insert", "update", "delete")
            else f"expected one of insert/update/delete, got {v!r}"
        ),
    },
    "external_api_call": {
        "vendor": _required_str,
        "endpoint": _required_str,
        # OPTIONAL fields (VT-103 convention; not enforced, no validator):
        #   cost_paise: int — cost of this call in paise (1 INR = 100 paise)
        #   cost_category: str — one of `llm`, `twilio`, `razorpay`, `apify`,
        #     `infra_allocated`. Cost-dashboard aggregator falls back to
        #     bucketing by `vendor` when this field is absent.
    },
    "external_api_response": {
        "vendor": _required_str,
        "status_code": _required_int,
    },
    "error": {
        "error_class": _required_str,
        # error_message + stack_trace are PII-redacted by the writer; we
        # don't require them here — many error events carry only the class.
    },
    "phase_transition": {
        "from_phase": _required_str,
        "to_phase": _required_str,
    },
    "scheduled_trigger_fired": {
        "trigger_name": _required_str,
    },
    "delivery_attempted": {
        "channel": _required_str,
        "recipient_handle": _optional_str,
    },
    "payment_event": {
        "event_kind": _required_str,
    },
    "consent_event": {
        "event_kind": _required_str,
    },
    # Used by the Rule-#15 canary; harmless in prod.
    "canary_test": {
        "k": _optional_str,
    },
    # VT-104 reasoning-trace event types (forward-pointing: function-as-
    # tool boundary; VT-4 agent SDK PR wires the call sites).
    "agent_reasoning_step": {
        "step_name": _required_str,
        # Optional fields: content (str, redacted), metadata (dict).
    },
    "tool_call_args": {
        "tool_name": _required_str,
        # Optional fields: args (dict, redacted).
    },
    "tool_call_result": {
        "tool_name": _required_str,
        "ok": lambda v: None if isinstance(v, bool) else f"expected bool, got {type(v).__name__}",
        # Optional fields: result (any, redacted), error (str, redacted).
    },
    # VT-28 scheduled-trigger event types. Three SHELL events (plumbing-
    # mode per CL-274 + phantom-Done prevention per CL-318/319/380) +
    # one full-implementation weekly_cadence event. The corresponding
    # completion event names (`attribution_closed`, `day39_evaluated`,
    # `monthly_impact_started`) are RESERVED for VT-176 and intentionally
    # NOT registered here.
    "weekly_cadence_fired": {
        "trigger_reason": _required_str,
    },
    "attribution_close_shell": {
        "status": _required_str,
        "trigger_reason": _required_str,
    },
    "day39_shell": {
        "status": _required_str,
        "trigger_reason": _required_str,
    },
    "monthly_impact_shell": {
        "status": _required_str,
        "trigger_reason": _required_str,
    },
    # VT-175 released event types (formerly reserved by VT-28). The
    # canonical completion events for attribution-close + day-39 — no
    # longer SHELL forms now that the schema substrate + deterministic
    # evaluators ship in this row. Production emission via
    # `orchestrator.billing.attribution_close.close_attribution` +
    # `orchestrator.billing.day39_evaluator.evaluate_day39`. The shell
    # event types above stay registered so VT-176's body-swap rollout
    # can ship without an intermediate schema break.
    "attribution_closed": {
        "campaign_id": _required_str,
        "tenant_id": _required_str,
        "total_arrr_paise": _required_int,
    },
    "day39_continue": {
        "tenant_id": _required_str,
        "verdict": _required_str,
        "arrr_paise": _required_int,
        "cumulative_fees_paise": _required_int,
    },
    "day39_refund_triggered": {
        "tenant_id": _required_str,
        "verdict": _required_str,
        "arrr_paise": _required_int,
        "cumulative_fees_paise": _required_int,
    },
    # VT-93 refund execution audit (pipeline_log; the immutable copy goes to
    # privacy_audit_log). Emitted by billing/refund_executor on completion.
    # TAXONOMY (Cowork PB1, 20260605T095500Z): 'refund_executed' is the CLEAN
    # terminal audit event for an EXECUTED refund. The phase-machine event
    # 'day39_refund_triggered' (transitions.py) keeps its ORIGINAL meaning — it
    # fires the paid_active/paid_at_risk -> refunded transition; it is NOT
    # overloaded to mean "executed". 'refund_partial_failed' goes only to
    # privacy_audit_log (its CHECK), not here. VT-85 owns the fuller
    # day39_refund_offered / day39_refund_decision taxonomy.
    "refund_executed": {
        "tenant_id": _required_str,
        "refund_reason": _required_str,
        "total_refund_paise": _required_int,
    },
    # VT-85 day-39 refund-conversation taxonomy. 'day39_refund_offered' replaces
    # the old auto-fire 'day39_refund_triggered' EMIT on the refund verdict — the
    # evaluator now makes an OFFER (no auto-refund). 'day39_refund_decision' records
    # the owner's reply (or the 48h timeout default).
    "day39_refund_offered": {
        "tenant_id": _required_str,
        "verdict": _required_str,
        "arrr_paise": _required_int,
        "cumulative_fees_paise": _required_int,
    },
    "day39_refund_decision": {
        "tenant_id": _required_str,
        "decision": _required_str,  # refund | continue | discuss
        "source": _required_str,  # reply | timeout
    },
    # VT-176 released event type (downstream PDF generator from VT-9.6
    # successor consumes this). Schema is intentionally minimal: only
    # the routing fields are required (tenant_id + target_month). The
    # PDF generator pulls cost/attribution context itself.
    "monthly_impact_started": {
        "tenant_id": _required_str,
        "target_month": _required_str,
    },
    # VT-30 composer-invocation audit event. Emitted when the orchestrator-
    # agent dispatches the composer tool. Payload carries the
    # ComposedOutput envelope (post-redaction at the writer boundary).
    "composer_invoked": {
        "intent_or_trigger": _required_str,
        "message_type": _required_str,
    },
}


def validate(event_type: str, payload: Any) -> tuple[bool, list[str]]:
    """Return ``(ok, errors)`` for ``payload`` against ``event_type``'s schema.

    Unknown event_types pass validation with a single warning so observability
    isn't blocked by code drift; the writer still writes the row.
    """
    errors: list[str] = []

    schema = EVENT_SCHEMAS.get(event_type)
    if schema is None:
        errors.append(f"unknown event_type {event_type!r} (not in EVENT_SCHEMAS)")
        return False, errors

    payload_err = _required_dict(payload)
    if payload_err is not None:
        errors.append(f"payload: {payload_err}")
        return False, errors

    for field, validator in schema.items():
        if field not in payload:
            errors.append(f"missing required field {field!r}")
            continue
        msg = validator(payload[field])
        if msg is not None:
            errors.append(f"{field}: {msg}")

    return (not errors), errors


__all__ = ["EVENT_SCHEMAS", "validate"]
