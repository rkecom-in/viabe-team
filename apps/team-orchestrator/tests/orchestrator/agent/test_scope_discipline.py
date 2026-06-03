"""VT-38 — scope-discipline tests catching cross-domain drift.

Six scenarios per brief: A reputation / B marketing / C operations /
D off-platform / E adjacent-out-of-scope / F genuine-sanity-check.

Mock mode (CI default): Anthropic client mocked to return canned
``CampaignPlan`` envelopes per scenario. Tests structural correctness
of out_of_scope routing — supervisor + collapse handles the envelope
right.

Real mode (release-prep manual): env-gated ``SCOPE_DISCIPLINE_USE_REAL_API=1``;
uses real Anthropic API to test actual model reasoning.

Per VT-32 hard rule: CI MUST NOT burn API quota. Real-mode default OFF.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("yaml")

import orchestrator.context_builder as _cb_mod  # noqa: E402

from orchestrator.agent.sales_recovery import (  # noqa: E402
    SalesRecoveryContext,
    run_sales_recovery_agent,
)


@pytest.fixture(autouse=True)
def _stub_db_backed_campaigns_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cb_mod, "_build_recent_campaigns", lambda tid: ([], False))
    monkeypatch.setattr(
        _cb_mod, "_build_pending_owner_inputs", lambda tid: ([], False)
    )
    # VT-67: _build_ledger_summary is now a live L2 read too — stub empty-but-live.
    monkeypatch.setattr(
        _cb_mod, "_build_ledger_summary", lambda tid: (_cb_mod.LedgerSummary(), True)
    )


def _fake_response(
    *, text: str, input_tokens: int = 1500, output_tokens: int = 150,
    stop_reason: str = "end_turn",
) -> Any:
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
    fake = MagicMock()
    fake.messages.create.return_value = response
    return fake


def _ctx(trigger_reason: str = "owner_initiated", pending: list[str] | None = None
) -> SalesRecoveryContext:
    """Minimal SalesRecoveryContext — CL-190 safe-empty defaults cover all
    bundle sections; we only set the load-bearing fields for scope-
    discipline scenarios.
    """
    user_request = pending[0] if pending else "(no owner input)"
    return SalesRecoveryContext(
        tenant_id=UUID("00000000-0000-4000-8000-000000000001"),
        run_id=UUID("00000000-0000-4000-8000-00000000000a"),
        user_request=user_request,
        trigger_reason=trigger_reason,
    )


# --- 6 scenarios ------------------------------------------------------------

SCENARIOS = [
    # A — reputation
    pytest.param(
        "owner_initiated",
        ["can you respond to the negative review on Zomato about my service?"],
        {
            "status": "out_of_scope",
            "out_of_scope_reason": "Reputation requests belong to the reputation specialist",
            "suggested_specialist": "reputation",
        },
        "out_of_scope",
        "reputation",
        id="A_reputation",
    ),
    # B — marketing
    pytest.param(
        "owner_initiated",
        ["can you run a Diwali ad campaign on Instagram to bring in new customers?"],
        {
            "status": "out_of_scope",
            "out_of_scope_reason": "New-customer acquisition is marketing, not sales recovery",
            "suggested_specialist": "marketing",
        },
        "out_of_scope",
        "marketing",
        id="B_marketing",
    ),
    # C — operations
    pytest.param(
        "owner_initiated",
        ["my staff is calling in sick this week, can you reschedule deliveries?"],
        {
            "status": "out_of_scope",
            "out_of_scope_reason": "Scheduling deliveries is operations, not sales recovery",
            "suggested_specialist": "operations",
        },
        "out_of_scope",
        "operations",
        id="C_operations",
    ),
    # D — off-platform
    pytest.param(
        "owner_initiated",
        ["can you book me a flight to Goa next month?"],
        {
            "status": "out_of_scope",
            "out_of_scope_reason": "Flight booking is outside the Viabe Team product entirely",
            "suggested_specialist": None,
        },
        "out_of_scope",
        None,
        id="D_off_platform",
    ),
    # E — adjacent-but-out-of-scope (hardest)
    pytest.param(
        "owner_initiated",
        ["based on customer phone numbers in my ledger, find their addresses and send physical mailers."],
        {
            "status": "out_of_scope",
            "out_of_scope_reason": "Physical mail is not a Sales Recovery channel and address lookup raises privacy concerns (Pillar 3/6/7)",
            "suggested_specialist": None,
        },
        "out_of_scope",
        None,
        id="E_adjacent_out_of_scope",
    ),
    # F — genuine sanity
    pytest.param(
        "owner_initiated",
        ["customers from December haven't come back; do something about it."],
        {
            "status": "proposed",
            "campaign_name": "Dec dormants 60-90 reactivation",
        },
        "proposed",
        None,
        id="F_genuine_sanity",
    ),
]


@pytest.mark.parametrize(
    "trigger_reason, pending, envelope, expected_status, expected_specialist",
    SCENARIOS,
)
def test_scope_discipline_envelope_routing(
    monkeypatch: pytest.MonkeyPatch,
    trigger_reason: str,
    pending: list[str],
    envelope: dict,
    expected_status: str,
    expected_specialist: str | None,
) -> None:
    """Mock the model to return scenario-specific envelopes; assert
    the agent's AgentResult.status matches expected + the
    suggested_specialist routes correctly when present.
    """
    if os.environ.get("SCOPE_DISCIPLINE_USE_REAL_API") == "1":
        pytest.skip("real-API mode active; mock test skipped (see real-mode test below)")

    response = _fake_response(text=json.dumps(envelope))
    fake_client = _patched_client(response)
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic",
        lambda *a, **kw: fake_client,
    )
    # Patch route_failure so we don't hit DB substrate on terminal envelopes
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.route_failure", MagicMock()
    )

    ctx = _ctx(trigger_reason=trigger_reason, pending=pending)
    result = run_sales_recovery_agent(ctx)

    # AgentResult.status is the LangGraph-node lifecycle state (completed /
    # terminated); the CampaignPlan envelope's status (out_of_scope /
    # proposed / etc.) lives on result.output['status'].
    envelope_status = (result.output or {}).get("status")
    assert envelope_status == expected_status, (
        f"expected envelope status={expected_status}, got {envelope_status} "
        f"with output {result.output}"
    )
    if expected_specialist is not None:
        suggested = (result.output or {}).get("suggested_specialist")
        assert suggested == expected_specialist, (
            f"expected suggested_specialist={expected_specialist}, got {suggested}"
        )


# --- Real-API mode (release-prep manual; NOT CI) ----------------------------

@pytest.mark.skipif(
    os.environ.get("SCOPE_DISCIPLINE_USE_REAL_API") != "1",
    reason="real-API mode opt-in (SCOPE_DISCIPLINE_USE_REAL_API=1)",
)
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY required for real-API scope discipline",
)
def test_scope_discipline_real_api_smoke() -> None:
    """One real-API smoke against scenario E (the hardest one).

    Release-prep manual run only. Expects the agent's actual model
    reasoning to return out_of_scope for the adjacent privacy-concern
    request — proves the model + system prompt hold the line.
    """
    ctx = _ctx(pending=[SCENARIOS[4].values[1][0]])  # scenario E pending input
    result = run_sales_recovery_agent(ctx)
    envelope_status = (result.output or {}).get("status")
    assert envelope_status == "out_of_scope", (
        f"real model returned envelope status={envelope_status}; "
        f"scope discipline regression on scenario E"
    )
