"""VT-464 D4 — the brain observability envelopes must validate against schema.

The live re-drive saw both brain envelopes soft-fail validation
(``payload_validation_failed=True``), degrading Ops replay:

  * ``agent_invocation`` — the writer (agent/dispatch._write_dispatch_entry)
    put ``reason`` in output_envelope and packed undeclared keys into
    input_envelope, while the schema REQUIRES ``agent_role`` + ``reason`` in a
    strict (extra="forbid") input_envelope and pins output_envelope to None.
  * ``agent_reasoning_step`` — the writer (observability/agent_callback) emits
    the Context Composer bundle fields, but the schema declared ONLY
    ``prompt_token_count`` (which the writer did not even emit) and forbade
    extras.

These tests construct the envelopes with the EXACT payload the writers now
emit and assert they validate (no pydantic ValidationError). They are the
regression guard: a future writer/schema drift re-introduces the soft-fail and
fails here.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from datetime import datetime, timezone  # noqa: E402

from orchestrator.observability.envelopes.agent_invocation import (  # noqa: E402
    AgentInvocationEnvelope,
)
from orchestrator.observability.envelopes.agent_reasoning_step import (  # noqa: E402
    AgentReasoningStepEnvelope,
)


def _base_kwargs() -> dict:
    return {
        "run_id": uuid4(),
        "tenant_id": uuid4(),
        "step_seq": 0,
        "started_at": datetime.now(timezone.utc),
    }


def test_agent_invocation_envelope_validates_with_writer_payload() -> None:
    """The dispatch-entry writer's payload must satisfy AgentInvocationInput."""
    env = AgentInvocationEnvelope(
        **_base_kwargs(),
        step_name="brain_dispatch_entry",
        status="running",
        input_envelope={
            "agent_role": "orchestrator",
            "reason": "substantive owner message — needs orchestrator-agent reasoning",
        },
        output_envelope=None,
    )
    assert env.input_envelope.agent_role == "orchestrator"
    assert "owner message" in env.input_envelope.reason


def test_agent_invocation_rejects_missing_required_fields() -> None:
    """Guard: the strict schema still REJECTS a payload missing agent_role/reason
    (the pre-fix shape) — so the fix is the writer conforming, not a loosened gate."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentInvocationEnvelope(
            **_base_kwargs(),
            input_envelope={"trigger": "owner_substantive_message"},
            output_envelope=None,
        )


def test_agent_reasoning_step_envelope_validates_with_writer_payload() -> None:
    """The agent_callback writer's input_envelope must satisfy the schema —
    prompt_token_count plus the Context Composer bundle fields."""
    env = AgentReasoningStepEnvelope(
        **_base_kwargs(),
        step_name="agent_turn",
        status="completed",
        input_envelope={
            "prompt_token_count": 1234,
            "context_bundle_hash": "abc123",
            "context_bundle_components": ["business_profile", "customer_ledger_summary"],
            "context_bundle_token_count": 800,
            "prior_tool_calls_count": 2,
            "prior_tool_calls_summary": [{"tool": "compose_output", "ok": True}],
        },
        output_envelope={
            "think_text": "redacted",
            "action": "compose_output",
            "action_args": {"target": "x", "summary": "y"},
            "logfire_trace_id": None,
        },
    )
    assert env.input_envelope.prompt_token_count == 1234
    assert env.input_envelope.context_bundle_token_count == 800
