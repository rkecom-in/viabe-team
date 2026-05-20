"""VT-32 — sales_recovery agent skeleton tests.

Three surfaces:

1. ``AgentResult`` contract: shape, defaults, ``terminated_by`` accepts
   every ``HardLimitAxis`` member.
2. ``run_sales_recovery_agent`` with a MOCKED ``anthropic.Anthropic``
   client — zero real API calls in CI. Exercises the placeholder happy
   path, raw_messages capture, cost attribution, status mapping.
3. ``sales_recovery_node`` translates an ``AgentResult`` into a
   reducer-friendly LangGraph state update.

A real-API canary test against ``claude-haiku-4-5`` lives at the bottom,
env-gated by ``VIABE_RUN_AGENT_CANARY=1`` and ``ANTHROPIC_API_KEY`` so it
DOES NOT run in CI (CI must not burn API quota — hard rule, VT-32).
Fazal triggers it manually once before merge.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("yaml")

from orchestrator.agent.cost import RATES, compute_cost_paise  # noqa: E402
from orchestrator.agent.sales_recovery import (  # noqa: E402
    SalesRecoveryContext,
    run_sales_recovery_agent,
)
from orchestrator.agent.sales_recovery_node import sales_recovery_node  # noqa: E402
from orchestrator.agent.types import AgentResult  # noqa: E402
from orchestrator.failures import HardLimitAxis  # noqa: E402


# --- 1. AgentResult contract -------------------------------------------------


def test_agent_result_defaults_are_safe():
    """A freshly constructed AgentResult has zero-spend, empty trace, no
    terminated state. Required so callers can build it incrementally
    without leaking junk numbers into telemetry."""
    result = AgentResult(status="completed")
    assert result.terminated_by is None
    assert result.output is None
    assert result.tokens_used == 0
    assert result.tool_calls_made == 0
    assert result.wallclock_ms == 0
    assert result.cost_paise == 0
    assert result.raw_messages == []
    assert result.terminated_reason is None


@pytest.mark.parametrize("axis", list(HardLimitAxis))
def test_agent_result_accepts_every_hard_limit_axis(axis: HardLimitAxis):
    """terminated_by reuses the failures.HardLimitAxis enum (CL-242).
    VT-35's enforcers will populate this field; the dataclass must
    accept every value the enum defines without translation."""
    result = AgentResult(
        status="terminated",
        terminated_by=axis,
        terminated_reason=f"{axis.value} budget exceeded",
    )
    assert result.terminated_by is axis


# --- 2. run_sales_recovery_agent with mocked Anthropic ----------------------


def _fake_response(
    *,
    text: str,
    input_tokens: int = 10,
    output_tokens: int = 5,
    stop_reason: str = "end_turn",
) -> Any:
    """Build a SimpleNamespace shaped like an Anthropic Message response."""

    class _TextBlock(SimpleNamespace):
        def model_dump(self) -> dict[str, Any]:
            return {"type": "text", "text": self.text}

    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens
        ),
        content=[_TextBlock(type="text", text=text)],
        stop_reason=stop_reason,
    )


def _patched_client(response: Any) -> Any:
    """Make Anthropic() return a client whose messages.create returns ``response``."""
    fake = MagicMock()
    fake.messages.create.return_value = response
    return fake


def test_run_sales_recovery_agent_placeholder_happy_path(monkeypatch):
    """Placeholder prompt → model returns the placeholder JSON →
    status='placeholder', output is the parsed dict, raw_messages
    captures the assistant turn, cost is non-zero, no terminated_by.

    Token counts are deliberately not tiny: the paise-per-token table is
    coarse, so a 10/5 split would round to 0 paise; use realistic
    placeholder-turn counts so the cost-accumulation path is asserted."""
    response = _fake_response(
        text='{"status": "placeholder"}', input_tokens=2000, output_tokens=200
    )
    fake_client = _patched_client(response)

    monkeypatch.setenv("VIABE_ENV", "test")  # → Haiku
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )

    result = run_sales_recovery_agent(
        SalesRecoveryContext(tenant_id="t1", run_id="r1")
    )

    assert result.status == "placeholder"
    assert result.output == {"status": "placeholder"}
    assert result.terminated_by is None
    assert result.tokens_used == 2200  # 2000 input + 200 output
    assert result.tool_calls_made == 0
    assert result.wallclock_ms >= 0
    assert result.cost_paise > 0  # Phase-1 rates are positive for Haiku
    # raw_messages has the seeded "begin" user turn + one assistant turn.
    assert any(
        m["role"] == "assistant"
        and any(
            block.get("text") == '{"status": "placeholder"}'
            for block in m["content"]
        )
        for m in result.raw_messages
    )


def test_run_sales_recovery_agent_uses_resolved_model_from_env(monkeypatch):
    """VIABE_ENV='production' → Opus; default → Haiku. The model id is
    read from config/models.yaml, never hardcoded in the runner."""
    response = _fake_response(text='{"status": "placeholder"}')
    fake_client = _patched_client(response)
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )

    monkeypatch.setenv("VIABE_ENV", "production")
    run_sales_recovery_agent(SalesRecoveryContext(tenant_id="t1", run_id="r1"))
    assert fake_client.messages.create.call_args.kwargs["model"] == "claude-opus-4-7"

    fake_client.messages.create.reset_mock()
    monkeypatch.setenv("VIABE_ENV", "test")
    run_sales_recovery_agent(SalesRecoveryContext(tenant_id="t1", run_id="r1"))
    assert fake_client.messages.create.call_args.kwargs["model"] == "claude-haiku-4-5"


def test_run_sales_recovery_agent_passes_brief_required_params(monkeypatch):
    """Per-response output cap (NOT the VT-35 run-level hard limit),
    extended thinking on, empty tools. ``max_tokens`` here is the
    per-call response cap; the 80K run-level token ceiling is a
    documented constant the VT-35 token meter enforces, never passed
    to ``messages.create``. Pin the call shape so a regression is loud."""
    from orchestrator.agent.sales_recovery import (
        _MAX_OUTPUT_TOKENS_PER_TURN,
        _RUN_LEVEL_TOKEN_HARD_LIMIT,
    )

    response = _fake_response(text='{"status": "placeholder"}')
    fake_client = _patched_client(response)
    monkeypatch.setenv("VIABE_ENV", "test")
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )

    run_sales_recovery_agent(SalesRecoveryContext(tenant_id="t1", run_id="r1"))
    call = fake_client.messages.create.call_args
    assert call.kwargs["max_tokens"] == _MAX_OUTPUT_TOKENS_PER_TURN
    assert call.kwargs["max_tokens"] != _RUN_LEVEL_TOKEN_HARD_LIMIT, (
        "messages.create max_tokens must NOT be the run-level 80K ceiling"
    )
    assert call.kwargs["thinking"]["type"] == "enabled"
    assert call.kwargs["tools"] == []
    # System prompt must be exactly the placeholder text (Type-3 commit).
    assert "placeholder agent" in call.kwargs["system"]
    assert '"status": "placeholder"' in call.kwargs["system"]


def test_run_sales_recovery_agent_status_invalid_when_output_unparseable(monkeypatch):
    """Non-JSON model output → status='invalid', output=None."""
    response = _fake_response(text="this is not json")
    fake_client = _patched_client(response)
    monkeypatch.setenv("VIABE_ENV", "test")
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )

    result = run_sales_recovery_agent(
        SalesRecoveryContext(tenant_id="t1", run_id="r1")
    )
    assert result.status == "invalid"
    assert result.output is None


def test_run_sales_recovery_agent_cost_uses_compute_cost_paise(monkeypatch):
    """The agent's cost_paise matches the cost.py table for the resolved model."""
    response = _fake_response(text='{"status": "placeholder"}', input_tokens=1000, output_tokens=200)
    fake_client = _patched_client(response)
    monkeypatch.setenv("VIABE_ENV", "test")  # Haiku
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )

    result = run_sales_recovery_agent(
        SalesRecoveryContext(tenant_id="t1", run_id="r1")
    )

    expected = compute_cost_paise(
        model="claude-haiku-4-5", input_tokens=1000, output_tokens=200
    )
    assert result.cost_paise == expected


# --- compute_cost_paise table sanity -----------------------------------------


def test_cost_table_covers_both_haiku_and_opus():
    """Cost table MUST carry both production (Opus) and test (Haiku) rates
    (CL-242 — cost attribution can't go dark for either model)."""
    assert "claude-opus-4-7" in RATES
    assert "claude-haiku-4-5" in RATES


def test_cost_haiku_input_one_million_tokens_is_8500_paise():
    """₹1 = $85 conv; Haiku input = $1/M. 1M Haiku-input tokens = ₹85 = 8500 paise."""
    assert (
        compute_cost_paise(
            model="claude-haiku-4-5",
            input_tokens=1_000_000,
            output_tokens=0,
        )
        == 8500
    )


def test_cost_opus_output_one_million_tokens_is_637500_paise():
    """Opus output = $75/M; 1M output × ₹85/USD × 100 paise/INR = 637,500 paise."""
    assert (
        compute_cost_paise(
            model="claude-opus-4-7",
            input_tokens=0,
            output_tokens=1_000_000,
        )
        == 637_500
    )


def test_cost_unknown_model_raises():
    with pytest.raises(KeyError):
        compute_cost_paise(model="claude-sonnet-9-9", input_tokens=1, output_tokens=1)


def test_cost_rejects_negative_token_counts():
    with pytest.raises(ValueError):
        compute_cost_paise(model="claude-opus-4-7", input_tokens=-1, output_tokens=0)


# --- 3. LangGraph node wrapper -----------------------------------------------


def test_sales_recovery_node_returns_agent_result_under_agent_result_key(monkeypatch):
    """The node translates AgentResult → state update under 'agent_result'."""
    response = _fake_response(text='{"status": "placeholder"}')
    fake_client = _patched_client(response)
    monkeypatch.setenv("VIABE_ENV", "test")
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )

    update = sales_recovery_node({"tenant_id": "t1", "run_id": "r1"})
    assert "agent_result" in update
    assert update["agent_result"]["status"] == "placeholder"
    assert update["agent_result"]["output"] == {"status": "placeholder"}


def test_sales_recovery_node_fail_loud_on_missing_tenant_id():
    from orchestrator._tenant_guard import TenantIsolationError

    with pytest.raises(TenantIsolationError):
        sales_recovery_node({"run_id": "r1"})


def test_sales_recovery_node_fail_loud_on_missing_run_id():
    from orchestrator._tenant_guard import TenantIsolationError

    with pytest.raises(TenantIsolationError):
        sales_recovery_node({"tenant_id": "t1"})


# --- Canary: real API, env-gated, NEVER runs in CI ---------------------------


@pytest.mark.skipif(
    os.environ.get("VIABE_RUN_AGENT_CANARY") != "1"
    or not os.environ.get("ANTHROPIC_API_KEY"),
    reason="canary skipped — needs VIABE_RUN_AGENT_CANARY=1 + ANTHROPIC_API_KEY",
)
def test_canary_real_haiku_run_returns_placeholder_status(monkeypatch):
    """One real Messages-API call against claude-haiku-4-5 to prove the SDK
    plumbing works end-to-end. Fazal runs this manually once before
    merge. CI must NEVER reach here (VIABE_RUN_AGENT_CANARY unset)."""
    monkeypatch.setenv("VIABE_ENV", "test")  # forces Haiku
    result = run_sales_recovery_agent(
        SalesRecoveryContext(tenant_id="canary", run_id="canary")
    )
    assert result.status == "placeholder", asdict(result)
    assert result.output == {"status": "placeholder"}
    assert result.tokens_used > 0
    assert result.cost_paise > 0
