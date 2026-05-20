"""VT-36 — self-evaluate gate tests.

All six scenarios run against a mocked self_evaluate seam
(``FakeSelfEvaluator``). The real seam (VT-50) is backlog; an
end-to-end real-API integration test is deferred. CI never burns API
quota here.

The six scenarios:

  1. Pass first try
  2. Revise once, then pass
  3. Revise twice → ships failed_after_revisions
  4. Bypass prevention (gate runs regardless of agent transcript)
  5. Seam error → routed as agent_invalid_output
  6. Hard-limit precedence: at 24/25 the 25th call (gate's) succeeds;
     at 25/25 the cap fires BEFORE the seam runs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("yaml")

from orchestrator.agent.limits.coordinator import CancellationContext  # noqa: E402
from orchestrator.agent.limits.tool_counter import (  # noqa: E402
    TOOL_CALL_HARD_LIMIT,
    ToolCounter,
)
from orchestrator.agent.sales_recovery import (  # noqa: E402
    SalesRecoveryContext,
    run_sales_recovery_agent,
)
from orchestrator.agent.schemas.campaign_plan import SelfEvaluateStatus  # noqa: E402
from orchestrator.agent.self_evaluate import (  # noqa: E402
    EVALUATION_CRITERIA,
    FakeSelfEvaluator,
    GateAction,
    GateConfig,
    SelfEvaluateFeedback,
    SelfEvaluateGate,
    SelfEvaluateOutcome,
    SelfEvaluateVerdict,
)
from orchestrator.failures import HardLimitAxis  # noqa: E402


# ---------- helpers -----------------------------------------------------------


def _valid_plan_dict(*, tenant_id: str, run_id: str) -> dict[str, Any]:
    """Build a v1.0-valid CampaignPlanProposed dict for the model's
    canned response. evidence_refs marker consistency holds (E1 in both
    prose fields; one claim_id E1 declared)."""
    now = datetime.now(UTC)
    cid = str(uuid4())
    return {
        "version": "1.0",
        "status": "proposed",
        "tenant_id": tenant_id,
        "run_id": run_id,
        "generated_at": now.isoformat(),
        "self_evaluate_status": "not_yet_evaluated",
        "campaign_window": {
            "start": (now + timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(days=7)).isoformat(),
        },
        "target_cohort": {
            "customer_ids": [cid],
            "cohort_label": "60-90 day dormants",
            "cohort_size": 1,
            "selection_reason": "dormant cohort [E1].",
        },
        "expected_arrr": {
            "low_paise": 1000000,
            "high_paise": 3000000,
            "confidence": "medium",
            "basis": "prior winback yields [E1].",
        },
        "evidence_refs": [
            {
                "claim_id": "E1",
                "source_kind": "tool_call",
                "source_id": "test",
                "note": None,
            },
        ],
        "message_plan": {
            "template_id": "team_winback_v1",
            "template_params": {"first_name": "Owner", "discount": "10"},
            "language": "en",
            "personalization": "owner-first-name.",
        },
        "exclusion_list": [],
        "exclusion_reasons": {},
        "escalation_conditions": [],
    }


def _end_turn_response(text: str, *, input_tokens: int = 50, output_tokens: int = 50) -> Any:
    class _TextBlock(SimpleNamespace):
        def model_dump(self) -> dict[str, Any]:
            return {"type": "text", "text": self.text}

    return SimpleNamespace(
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        content=[_TextBlock(type="text", text=text)],
        stop_reason="end_turn",
    )


def _patch_anthropic(monkeypatch, drafts: list[dict[str, Any]]) -> MagicMock:
    """Pre-cans one assistant response per draft. Each draft is a CampaignPlan dict."""
    fake = MagicMock()
    fake.messages.create.side_effect = [
        _end_turn_response(json.dumps(d)) for d in drafts
    ]
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake
    )
    return fake


def _patch_router(monkeypatch) -> MagicMock:
    router = MagicMock()
    monkeypatch.setattr("orchestrator.agent.sales_recovery.route_failure", router)
    return router


def _verdict(outcome: SelfEvaluateOutcome, **fb: str) -> SelfEvaluateVerdict:
    feedback = SelfEvaluateFeedback(**fb) if fb else None
    return SelfEvaluateVerdict(outcome=outcome, feedback=feedback)


def _ctx_ids() -> tuple[str, str]:
    return str(uuid4()), str(uuid4())


# ---------- 1. Pass first try -------------------------------------------------


def test_gate_passes_on_first_try(monkeypatch):
    """Single model draft + a PASS verdict → AgentResult.output.self_evaluate_status='passed'."""
    _patch_router(monkeypatch)
    tenant_id, run_id = _ctx_ids()
    _patch_anthropic(monkeypatch, [_valid_plan_dict(tenant_id=tenant_id, run_id=run_id)])
    monkeypatch.setenv("VIABE_ENV", "test")

    evaluator = FakeSelfEvaluator(verdicts=[_verdict(SelfEvaluateOutcome.PASS)])
    result = run_sales_recovery_agent(
        SalesRecoveryContext(tenant_id=tenant_id, run_id=run_id),
        evaluator=evaluator,
    )

    assert result.status == "completed"
    assert result.output is not None
    assert result.output["self_evaluate_status"] == SelfEvaluateStatus.PASSED.value
    assert evaluator.calls == 1


# ---------- 2. Revise once, then pass ----------------------------------------


def test_gate_revise_then_pass(monkeypatch):
    """One REVISE + one PASS → ships passed, two model drafts produced."""
    _patch_router(monkeypatch)
    tenant_id, run_id = _ctx_ids()
    draft1 = _valid_plan_dict(tenant_id=tenant_id, run_id=run_id)
    draft2 = _valid_plan_dict(tenant_id=tenant_id, run_id=run_id)
    fake = _patch_anthropic(monkeypatch, [draft1, draft2])
    monkeypatch.setenv("VIABE_ENV", "test")

    evaluator = FakeSelfEvaluator(
        verdicts=[
            _verdict(SelfEvaluateOutcome.REVISE, pillar="invented number"),
            _verdict(SelfEvaluateOutcome.PASS),
        ]
    )
    result = run_sales_recovery_agent(
        SalesRecoveryContext(tenant_id=tenant_id, run_id=run_id),
        evaluator=evaluator,
    )

    assert result.status == "completed"
    assert result.output is not None
    assert result.output["self_evaluate_status"] == SelfEvaluateStatus.PASSED.value
    assert evaluator.calls == 2
    assert fake.messages.create.call_count == 2


# ---------- 3. Revise twice → ships failed_after_revisions -------------------


def test_gate_revise_twice_ships_failed_after_revisions(monkeypatch):
    """Both drafts get REVISE → ship the second with FAILED_AFTER_REVISIONS."""
    _patch_router(monkeypatch)
    tenant_id, run_id = _ctx_ids()
    drafts = [
        _valid_plan_dict(tenant_id=tenant_id, run_id=run_id),
        _valid_plan_dict(tenant_id=tenant_id, run_id=run_id),
    ]
    _patch_anthropic(monkeypatch, drafts)
    monkeypatch.setenv("VIABE_ENV", "test")

    evaluator = FakeSelfEvaluator(
        verdicts=[
            _verdict(SelfEvaluateOutcome.REVISE, pillar="invented number"),
            _verdict(SelfEvaluateOutcome.REVISE, consistency="targeting mismatch"),
        ]
    )
    result = run_sales_recovery_agent(
        SalesRecoveryContext(tenant_id=tenant_id, run_id=run_id),
        evaluator=evaluator,
    )

    assert result.status == "completed"
    assert result.output is not None
    assert (
        result.output["self_evaluate_status"]
        == SelfEvaluateStatus.FAILED_AFTER_REVISIONS.value
    )
    assert evaluator.calls == 2


# ---------- 4. Bypass prevention ---------------------------------------------


def test_gate_runs_even_when_agent_never_called_self_evaluate(monkeypatch):
    """Pillar 8 — the agent's transcript carries NO self_evaluate
    tool-use (the loop's tool registry is empty), yet the gate still
    runs at terminal. The agent cannot bypass."""
    _patch_router(monkeypatch)
    tenant_id, run_id = _ctx_ids()
    _patch_anthropic(monkeypatch, [_valid_plan_dict(tenant_id=tenant_id, run_id=run_id)])
    monkeypatch.setenv("VIABE_ENV", "test")

    evaluator = FakeSelfEvaluator(verdicts=[_verdict(SelfEvaluateOutcome.PASS)])
    result = run_sales_recovery_agent(
        SalesRecoveryContext(tenant_id=tenant_id, run_id=run_id),
        evaluator=evaluator,
    )

    # The transcript has zero self_evaluate tool_use blocks (no tools
    # registered), but the evaluator was called anyway.
    assert evaluator.calls == 1
    assert all(
        all(
            block.get("type") != "tool_use" or block.get("name") != "self_evaluate"
            for block in m.get("content", [])
            if isinstance(block, dict)
        )
        for m in result.raw_messages
    )


# ---------- 5. Seam error → routed as agent_invalid_output -------------------


def test_gate_seam_error_routes_as_agent_invalid_output(monkeypatch):
    """A seam-raised exception → status='invalid' + FailureRecord(
    AGENT_INVALID_OUTPUT) routed via VT-3.6."""
    router = _patch_router(monkeypatch)
    tenant_id, run_id = _ctx_ids()
    _patch_anthropic(monkeypatch, [_valid_plan_dict(tenant_id=tenant_id, run_id=run_id)])
    monkeypatch.setenv("VIABE_ENV", "test")

    evaluator = FakeSelfEvaluator(
        raise_on_call=RuntimeError("seam network failure")
    )
    result = run_sales_recovery_agent(
        SalesRecoveryContext(tenant_id=tenant_id, run_id=run_id),
        evaluator=evaluator,
    )

    assert result.status == "invalid"
    assert router.call_count == 1
    failure_arg = router.call_args.args[0]
    assert failure_arg.failure_type.value == "agent_invalid_output"
    assert "seam network failure" in failure_arg.message
    assert failure_arg.metadata["source"] == "self_evaluate_gate"


# ---------- 6. Hard-limit precedence -----------------------------------------


def test_gate_succeeds_when_seam_call_is_the_25th_tool(monkeypatch):
    """At count=24, the gate's increment makes it 25 (within budget) →
    seam runs. No hard-limit cancel."""
    ctx = CancellationContext()
    counter = ToolCounter(ctx)
    counter.count = TOOL_CALL_HARD_LIMIT - 1  # = 24
    evaluator = FakeSelfEvaluator(verdicts=[_verdict(SelfEvaluateOutcome.PASS)])

    gate = SelfEvaluateGate(
        evaluator=evaluator,
        ctx=ctx,
        tool_counter=counter,
        config=GateConfig(max_revisions=2),
    )
    # We need a valid draft to pass in. Use the helper directly.
    from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan

    tenant_id, run_id = _ctx_ids()
    draft = parse_campaign_plan(_valid_plan_dict(tenant_id=tenant_id, run_id=run_id))

    outcome = gate.run(draft)
    assert outcome.action is GateAction.SHIP
    assert outcome.self_evaluate_status is SelfEvaluateStatus.PASSED
    assert counter.count == TOOL_CALL_HARD_LIMIT  # exactly 25
    assert not ctx.is_cancelled


def test_hard_limit_fires_before_gate_when_already_at_cap(monkeypatch):
    """At count=25, the gate's increment makes it 26 → hard-limit
    cancel fires BEFORE the seam is called. Gate returns ABORTED."""
    ctx = CancellationContext()
    counter = ToolCounter(ctx)
    counter.count = TOOL_CALL_HARD_LIMIT  # = 25 (already at the cap)
    evaluator = FakeSelfEvaluator(verdicts=[_verdict(SelfEvaluateOutcome.PASS)])

    gate = SelfEvaluateGate(
        evaluator=evaluator,
        ctx=ctx,
        tool_counter=counter,
        config=GateConfig(max_revisions=2),
    )
    from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan

    tenant_id, run_id = _ctx_ids()
    draft = parse_campaign_plan(_valid_plan_dict(tenant_id=tenant_id, run_id=run_id))

    outcome = gate.run(draft)
    assert outcome.action is GateAction.ABORTED
    # The seam was NOT called — hard limit precedence is the load-bearing rule.
    assert evaluator.calls == 0
    assert ctx.is_cancelled
    assert ctx.cancelled_by is HardLimitAxis.TOOL_CALLS


# ---------- Sanity: evaluation criteria are the four documented --------------


def test_evaluation_criteria_are_the_four_documented():
    """Lock the four-criteria contract — Fazal sign-off on these."""
    assert EVALUATION_CRITERIA == ["schema", "pillar", "consistency", "legal"]
