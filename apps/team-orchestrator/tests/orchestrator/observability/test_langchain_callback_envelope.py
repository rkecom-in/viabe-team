"""VT-464 D4-redo — the LIVE langchain reasoning-step writer must validate.

The earlier D4 fix patched ``observability/agent_callback.py`` (step_name
``agent_turn``), but the LIVE brain path runs through
``observability/langchain_callback.py`` (step_name ``orchestrator_agent_turn``).
That writer's ``input_envelope`` omitted the REQUIRED ``prompt_token_count``
field of ``AgentReasoningStepInput`` (extra="forbid") → every deployed brain
run logged ``payload_validation_failed`` and Ops replay was degraded.

These tests drive the real ``OrchestratorReasoningCallback._write_reasoning_step``
with a spy on ``write_step`` and assert the emitted ``input_envelope`` validates
against the strict schema (prompt_token_count present, no extra-forbidden keys).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("pydantic")

from orchestrator.observability import langchain_callback as cb_mod  # noqa: E402
from orchestrator.observability.decorators import (  # noqa: E402
    observability_context,
)
from orchestrator.observability.envelopes.agent_reasoning_step import (  # noqa: E402
    AgentReasoningStepInput,
)
from orchestrator.observability.langchain_callback import (  # noqa: E402
    OrchestratorReasoningCallback,
)


class _NullDriver:
    def check_mid_invocation(self, *a, **k) -> None:  # noqa: D401, ANN002, ANN003
        return None


def _make_callback() -> OrchestratorReasoningCallback:
    usage = SimpleNamespace(
        tokens_input=0, tokens_output=0, cost_paise=0, tool_calls=1
    )
    return OrchestratorReasoningCallback(
        driver=_NullDriver(),  # type: ignore[arg-type]
        usage=usage,  # type: ignore[arg-type]
        run_id=uuid4(),
        tenant_id=uuid4(),
    )


def _fake_response(text: str = "thinking...") -> SimpleNamespace:
    """Minimal LLMResult-shaped object the callback's _first_text reads."""
    gen = SimpleNamespace(text=text, message=None)
    return SimpleNamespace(generations=[[gen]], llm_output={})


def test_live_reasoning_step_input_envelope_validates(monkeypatch) -> None:
    """The langchain writer's input_envelope must satisfy AgentReasoningStepInput
    — including the REQUIRED prompt_token_count — so no payload_validation_failed.
    """
    captured: dict[str, object] = {}

    def _spy_write_step(**kwargs):  # noqa: ANN003
        captured.update(kwargs)

    monkeypatch.setattr(cb_mod, "write_step", _spy_write_step)

    cb = _make_callback()
    usage_data = {"input_tokens": 1234, "output_tokens": 56, "model": "claude-opus-4-7"}

    with observability_context(run_id=uuid4(), tenant_id=uuid4()):
        cb._write_reasoning_step(_fake_response(), usage_data, status="completed")

    assert captured, "write_step was not called"
    assert captured["step_name"] == "orchestrator_agent_turn"

    input_env = captured["input_envelope"]
    # The required field is now emitted, sourced from usage input_tokens.
    assert input_env["prompt_token_count"] == 1234

    # Strict schema (extra="forbid") must accept the writer's payload verbatim —
    # this is the regression guard against the live payload_validation_failed.
    validated = AgentReasoningStepInput.model_validate(input_env)
    assert validated.prompt_token_count == 1234


def test_live_reasoning_step_prompt_token_defaults_zero_when_usage_empty(
    monkeypatch,
) -> None:
    """A turn with no usage data still emits a valid envelope (prompt_token_count=0),
    not a missing-field soft-fail.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cb_mod, "write_step", lambda **kw: captured.update(kw)
    )

    cb = _make_callback()
    with observability_context(run_id=uuid4(), tenant_id=uuid4()):
        cb._write_reasoning_step(_fake_response(), {}, status="completed")

    input_env = captured["input_envelope"]
    assert input_env["prompt_token_count"] == 0
    AgentReasoningStepInput.model_validate(input_env)  # no ValidationError
