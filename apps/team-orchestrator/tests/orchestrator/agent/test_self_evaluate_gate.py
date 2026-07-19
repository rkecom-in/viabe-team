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
    GradeTier,
    SelfEvaluateFeedback,
    SelfEvaluateGate,
    SelfEvaluateOutcome,
    SelfEvaluateVerdict,
    _filter_arrr_only,
    _is_arrr_critique,
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


def _insufficient_data_dict(*, tenant_id: str, run_id: str) -> dict[str, Any]:
    """A v1.0-valid CampaignPlanInsufficientData dict — the legal
    "in-scope but not enough context" terminal SR emits when data is
    genuinely missing (VT-491 regression for runs 44f12ad2 / 0844b0ff)."""
    now = datetime.now(UTC)
    return {
        "version": "1.0",
        "status": "insufficient_data",
        "tenant_id": tenant_id,
        "run_id": run_id,
        "generated_at": now.isoformat(),
        "self_evaluate_status": "not_yet_evaluated",
        "missing_data": [
            {
                "category": "dormant_cohort",
                "description": "no dormant customers in the order history yet",
                "suggested_remediation": "connect POS / wait for orders to land",
            },
        ],
    }


def _out_of_scope_dict(*, tenant_id: str, run_id: str) -> dict[str, Any]:
    """A v1.0-valid CampaignPlanOutOfScope dict — the sibling
    non-proposed terminal (same VT-491 bug class)."""
    now = datetime.now(UTC)
    return {
        "version": "1.0",
        "status": "out_of_scope",
        "tenant_id": tenant_id,
        "run_id": run_id,
        "generated_at": now.isoformat(),
        "self_evaluate_status": "not_yet_evaluated",
        "out_of_scope_reason": "request is a reputation issue, not sales recovery",
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


def _verdict(outcome: SelfEvaluateOutcome, **fb: list[str]) -> SelfEvaluateVerdict:
    """Build a verdict. v1.1: each kwarg is a ``list[str]`` of distinct
    critique strings for that category (matches the widened
    SelfEvaluateFeedback contract)."""
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
        SalesRecoveryContext(
            tenant_id=tenant_id, run_id=run_id, user_request="test request"
        ),
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
            _verdict(SelfEvaluateOutcome.REVISE, pillar=["invented number"]),
            _verdict(SelfEvaluateOutcome.PASS),
        ]
    )
    result = run_sales_recovery_agent(
        SalesRecoveryContext(
            tenant_id=tenant_id, run_id=run_id, user_request="test request"
        ),
        evaluator=evaluator,
    )

    assert result.status == "completed"
    assert result.output is not None
    assert result.output["self_evaluate_status"] == SelfEvaluateStatus.PASSED.value
    assert evaluator.calls == 2
    assert fake.messages.create.call_count == 2


# ---------- 3. Revise twice → ships failed_after_revisions -------------------


def test_gate_preserves_multiple_distinct_violations_within_one_category(monkeypatch):
    """v1.1: SelfEvaluateFeedback.pillar (etc.) is ``list[str] | None``.
    When a category carries 2+ distinct violations, ALL entries
    survive end-to-end: evaluator → gate → AgentResult.

    Locked because the widening exists for this case — a single
    summary collapse would defeat the structured-retry contract."""
    router = _patch_router(monkeypatch)
    tenant_id, run_id = _ctx_ids()
    drafts = [
        _valid_plan_dict(tenant_id=tenant_id, run_id=run_id),
        _valid_plan_dict(tenant_id=tenant_id, run_id=run_id),
    ]
    _patch_anthropic(monkeypatch, drafts)
    monkeypatch.setenv("VIABE_ENV", "test")

    evaluator = FakeSelfEvaluator(
        verdicts=[
            _verdict(
                SelfEvaluateOutcome.REVISE,
                pillar=["invented 30% return rate", "pressure language 'last chance'"],
                consistency=["cohort_label mismatches attribution_snapshot"],
            ),
            _verdict(
                SelfEvaluateOutcome.REVISE,
                pillar=["another invented number", "second pressure phrase"],
            ),
        ]
    )
    run_sales_recovery_agent(
        SalesRecoveryContext(
            tenant_id=tenant_id, run_id=run_id, user_request="test request"
        ),
        evaluator=evaluator,
    )

    # The rejection routes one FailureRecord; the metadata.reasons
    # carries the lists from the FINAL REVISE verdict — 2 pillar
    # entries preserved (not collapsed).
    assert router.call_count == 1
    failure_arg = router.call_args.args[0]
    reasons = failure_arg.metadata["reasons"]
    assert reasons["pillar"] == [
        "another invented number",
        "second pressure phrase",
    ]
    assert reasons["consistency"] is None


def test_evaluator_none_default_skips_gate(monkeypatch):
    """Test-injection seam: ``run_sales_recovery_agent(..., evaluator=None)``
    skips the gate entirely. status='completed' (loop default for a
    parseable non-placeholder dict), no router call. Locks the
    unit-test injection path the brief preserves."""
    router = _patch_router(monkeypatch)
    tenant_id, run_id = _ctx_ids()
    _patch_anthropic(
        monkeypatch, [_valid_plan_dict(tenant_id=tenant_id, run_id=run_id)]
    )
    monkeypatch.setenv("VIABE_ENV", "test")

    result = run_sales_recovery_agent(
        SalesRecoveryContext(
            tenant_id=tenant_id, run_id=run_id, user_request="test request"
        ),
        evaluator=None,
    )

    assert result.status == "completed"
    # Gate didn't run → no SELF_EVAL_REJECTED, no AGENT_INVALID_OUTPUT.
    router.assert_not_called()


def test_gate_emits_self_evaluate_gate_per_call(monkeypatch):
    """Telemetry: every gate.run() must emit a pipeline_steps row.
    Mock _emit_self_evaluate_gate and assert it's called once per
    evaluator call, with the right attempt_number + outcome."""
    _patch_router(monkeypatch)
    tenant_id, run_id = _ctx_ids()
    drafts = [
        _valid_plan_dict(tenant_id=tenant_id, run_id=run_id),
        _valid_plan_dict(tenant_id=tenant_id, run_id=run_id),
    ]
    _patch_anthropic(monkeypatch, drafts)
    monkeypatch.setenv("VIABE_ENV", "test")

    emitter = MagicMock()
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery._emit_self_evaluate_gate",
        emitter,
    )

    evaluator = FakeSelfEvaluator(
        verdicts=[
            _verdict(SelfEvaluateOutcome.REVISE, pillar=["bad"]),
            _verdict(SelfEvaluateOutcome.PASS),
        ]
    )
    run_sales_recovery_agent(
        SalesRecoveryContext(
            tenant_id=tenant_id, run_id=run_id, user_request="test request"
        ),
        evaluator=evaluator,
    )

    assert emitter.call_count == 2
    first_kwargs = emitter.call_args_list[0].kwargs
    second_kwargs = emitter.call_args_list[1].kwargs
    assert first_kwargs["attempt_number"] == 1
    assert first_kwargs["outcome"] is SelfEvaluateOutcome.REVISE
    assert second_kwargs["attempt_number"] == 2
    assert second_kwargs["outcome"] is SelfEvaluateOutcome.PASS


def test_gate_revise_twice_rejects_and_routes_self_eval_rejected(monkeypatch):
    """v1.1 locked contract: two consecutive REVISE verdicts → run is
    REJECTED. NO ship-with-flag. ``AgentResult.status == 'rejected'`` +
    ``self_evaluate_status='failed_after_revisions'`` (the persistent
    enum value, set on the draft for observability) + a
    ``FailureRecord(SELF_EVAL_REJECTED)`` routed to the error_router
    for escalation."""
    router = _patch_router(monkeypatch)
    tenant_id, run_id = _ctx_ids()
    drafts = [
        _valid_plan_dict(tenant_id=tenant_id, run_id=run_id),
        _valid_plan_dict(tenant_id=tenant_id, run_id=run_id),
    ]
    _patch_anthropic(monkeypatch, drafts)
    monkeypatch.setenv("VIABE_ENV", "test")

    evaluator = FakeSelfEvaluator(
        verdicts=[
            _verdict(SelfEvaluateOutcome.REVISE, pillar=["invented number"]),
            _verdict(
                SelfEvaluateOutcome.REVISE,
                consistency=["targeting mismatch"],
                legal=["pressure language"],
            ),
        ]
    )
    result = run_sales_recovery_agent(
        SalesRecoveryContext(
            tenant_id=tenant_id, run_id=run_id, user_request="test request"
        ),
        evaluator=evaluator,
    )

    assert result.status == "rejected"
    assert result.output is not None
    assert (
        result.output["self_evaluate_status"]
        == SelfEvaluateStatus.FAILED_AFTER_REVISIONS.value
    )
    assert evaluator.calls == 2

    # Loop must have routed exactly one SELF_EVAL_REJECTED failure
    # for escalation (router escalates HIGH severity to Fazal). Two
    # routings happened: the per-attempt self_evaluate_gate
    # telemetry helper writes pipeline_steps DIRECTLY (no router), so
    # router.call_count counts only the rejected failure.
    assert router.call_count == 1
    failure_arg = router.call_args.args[0]
    assert failure_arg.failure_type.value == "self_eval_rejected"
    # Final REVISE's reasons preserved on the FailureRecord metadata.
    reasons = failure_arg.metadata["reasons"]
    assert reasons["consistency"] == ["targeting mismatch"]
    assert reasons["legal"] == ["pressure language"]
    assert reasons["pillar"] is None
    assert failure_arg.metadata["attempt_number"] == 2


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
        SalesRecoveryContext(
            tenant_id=tenant_id, run_id=run_id, user_request="test request"
        ),
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
        SalesRecoveryContext(
            tenant_id=tenant_id, run_id=run_id, user_request="test request"
        ),
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


# ---------- VT-491: non-proposed variants short-circuit (no LLM grade) --------


def _gate_with_raising_seam() -> tuple[SelfEvaluateGate, FakeSelfEvaluator, ToolCounter]:
    """A gate whose seam ASSERTS if consulted — the determinism crux.
    If the gate ever calls the LLM seam on a non-proposed variant, the
    AssertionError surfaces and the test fails. calls==0 proves the seam
    was never reached."""
    ctx = CancellationContext()
    counter = ToolCounter(ctx)
    evaluator = FakeSelfEvaluator(
        raise_on_call=AssertionError("self_evaluate seam must NOT be called "
                                     "on a non-proposed variant")
    )
    gate = SelfEvaluateGate(
        evaluator=evaluator,
        ctx=ctx,
        tool_counter=counter,
        config=GateConfig(max_revisions=2),
    )
    return gate, evaluator, counter


def test_insufficient_data_short_circuits_accept_without_grading():
    """VT-491 regression (runs 44f12ad2 / 0844b0ff): an
    insufficient_data terminal is ACCEPTED (SHIP) deterministically —
    the LLM seam is NEVER consulted (calls==0, the determinism proof),
    no tool-budget slot is charged (counter stays 0), no grading attempt
    (attempt_number==0). The plan flows on UNCHANGED to the downstream
    data-remediation terminal."""
    from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan

    gate, evaluator, counter = _gate_with_raising_seam()
    tenant_id, run_id = _ctx_ids()
    draft = parse_campaign_plan(
        _insufficient_data_dict(tenant_id=tenant_id, run_id=run_id)
    )

    outcome = gate.run(draft)

    assert outcome.action is GateAction.SHIP
    assert evaluator.calls == 0          # the seam was NEVER consulted
    assert counter.count == 0            # no record_dispatch → no budget charged
    assert gate.evaluator_calls == 0
    assert outcome.attempt_number == 0
    assert outcome.outcome is None
    # status field is cosmetic for this variant (record_terminal_verdict
    # never reads it); left at the GateOutcome default.
    assert outcome.self_evaluate_status is SelfEvaluateStatus.NOT_YET_EVALUATED


def test_out_of_scope_short_circuits_accept_without_grading():
    """VT-491 sibling case: out_of_scope (also non-proposed) takes the
    same deterministic short-circuit — seam not called, no budget."""
    from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan

    gate, evaluator, counter = _gate_with_raising_seam()
    tenant_id, run_id = _ctx_ids()
    draft = parse_campaign_plan(
        _out_of_scope_dict(tenant_id=tenant_id, run_id=run_id)
    )

    outcome = gate.run(draft)

    assert outcome.action is GateAction.SHIP
    assert evaluator.calls == 0
    assert counter.count == 0
    assert outcome.attempt_number == 0


def test_proposed_plan_is_still_fully_graded():
    """The gate is NOT weakened: a real proposed plan STILL goes to the
    LLM seam (calls==1), gets a verdict, and one tool-budget slot is
    charged. Short-circuit applies to non-proposed variants only."""
    from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan

    ctx = CancellationContext()
    counter = ToolCounter(ctx)
    evaluator = FakeSelfEvaluator(verdicts=[_verdict(SelfEvaluateOutcome.PASS)])
    gate = SelfEvaluateGate(
        evaluator=evaluator,
        ctx=ctx,
        tool_counter=counter,
        config=GateConfig(max_revisions=2),
    )
    tenant_id, run_id = _ctx_ids()
    draft = parse_campaign_plan(_valid_plan_dict(tenant_id=tenant_id, run_id=run_id))

    outcome = gate.run(draft)

    assert outcome.action is GateAction.SHIP
    assert outcome.self_evaluate_status is SelfEvaluateStatus.PASSED
    assert evaluator.calls == 1          # the seam WAS consulted
    assert counter.count == 1            # one dispatch charged
    assert outcome.attempt_number == 1


def test_thin_proposed_plan_is_still_rejected():
    """The gate is NOT weakened: a proposed plan that the seam REVISEs
    twice STILL gets REJECTED (calls==2) — the short-circuit does not
    let a bad proposed plan skip the grade."""
    from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan

    ctx = CancellationContext()
    counter = ToolCounter(ctx)
    evaluator = FakeSelfEvaluator(
        verdicts=[
            _verdict(SelfEvaluateOutcome.REVISE, pillar=["invented number"]),
            _verdict(SelfEvaluateOutcome.REVISE, legal=["pressure language"]),
        ]
    )
    gate = SelfEvaluateGate(
        evaluator=evaluator,
        ctx=ctx,
        tool_counter=counter,
        config=GateConfig(max_revisions=2),
    )
    tenant_id, run_id = _ctx_ids()
    draft = parse_campaign_plan(_valid_plan_dict(tenant_id=tenant_id, run_id=run_id))

    # First REVISE → RETRY; caller re-runs the gate with the next draft.
    first = gate.run(draft)
    assert first.action is GateAction.RETRY
    second = gate.run(draft)

    assert second.action is GateAction.REJECTED
    assert second.self_evaluate_status is SelfEvaluateStatus.FAILED_AFTER_REVISIONS
    assert evaluator.calls == 2


def test_run_sales_recovery_insufficient_data_completes_no_escalation(monkeypatch):
    """Integration (gate-on, mocked Anthropic): the model emits an
    insufficient_data terminal; the gate is wired with a raise-on-call
    seam. The run COMPLETES deterministically — output preserves
    insufficient_data + missing_data verbatim; the seam is never called
    (calls==0); NO SELF_EVAL_REJECTED and NO AGENT_INVALID_OUTPUT routed
    (router untouched). This is the end-to-end VT-491 fix: a legitimate
    "not enough data" terminal no longer coin-flips into a Fazal page."""
    router = _patch_router(monkeypatch)
    tenant_id, run_id = _ctx_ids()
    _patch_anthropic(
        monkeypatch, [_insufficient_data_dict(tenant_id=tenant_id, run_id=run_id)]
    )
    monkeypatch.setenv("VIABE_ENV", "test")

    evaluator = FakeSelfEvaluator(
        raise_on_call=AssertionError("seam must not be called on insufficient_data")
    )
    result = run_sales_recovery_agent(
        SalesRecoveryContext(
            tenant_id=tenant_id, run_id=run_id, user_request="test request"
        ),
        evaluator=evaluator,
    )

    assert result.status == "completed"
    assert result.output is not None
    assert result.output["status"] == "insufficient_data"
    assert result.output["missing_data"][0]["category"] == "dormant_cohort"
    assert evaluator.calls == 0          # the seam was never consulted
    # No escalation, no invalid-output routing — the run landed cleanly.
    router.assert_not_called()


# ---------- Sanity: evaluation criteria are the four documented --------------


def test_evaluation_criteria_are_the_four_documented():
    """Lock the four-criteria contract — Fazal sign-off on these."""
    assert EVALUATION_CRITERIA == ["schema", "pillar", "consistency", "legal"]


# ============================================================================
# VT-500 — calibrate the grounding gate to CLAIMS + SCALE (adversarial-verify)
# ============================================================================
#
# The calibration RELAXES exactly ONE axis — the ``expected_arrr`` ROI/ARRR
# business-justification — and ONLY on the narrow SIMPLE lane (allow-listed
# ``team_winback_simple``, non-money-bearing, cohort <= L3_AUTO_MAX_BATCH). The
# adversarial proof below is that the relaxation is PROVABLY one-directional: a
# fabrication / PII / cohort-grounding / legal critique is NEVER stripped on the
# simple tier, the strict (offer/large/disabled/unresolved) path is unchanged,
# and the threshold + kill-switch bite. No real LLM: a FakeSelfEvaluator injects
# the exact critique sets so the GATE's deterministic filter is what's tested.


def _winback_plan_dict(
    *,
    tenant_id: str,
    run_id: str,
    template_id: str = "team_winback_simple",
    cohort_size: int = 5,
) -> dict[str, Any]:
    """A v1.0-valid CampaignPlanProposed dict with a parametrised
    ``message_plan.template_id`` + cohort size (cohort_size == len(customer_ids),
    the schema invariant). Marker consistency holds (E1 in both prose fields)."""
    now = datetime.now(UTC)
    cids = [str(uuid4()) for _ in range(cohort_size)]
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
            "customer_ids": cids,
            "cohort_label": "60-90 day dormants",
            "cohort_size": cohort_size,
            "selection_reason": "genuinely-lapsed dormant cohort [E1].",
        },
        "expected_arrr": {
            "low_paise": 1000000,
            "high_paise": 3000000,
            "confidence": "medium",
            "basis": "prior winback yields [E1].",
        },
        "evidence_refs": [
            {"claim_id": "E1", "source_kind": "tool_call", "source_id": "t", "note": None},
        ],
        "message_plan": {
            "template_id": template_id,
            "template_params": {"customer_name": "Owner", "business_name": "Cafe"},
            "language": "en",
            "personalization": "owner-first-name.",
        },
        "exclusion_list": [],
        "exclusion_reasons": {},
        "escalation_conditions": [],
    }


def _simple_tier_gate(
    evaluator: FakeSelfEvaluator,
    *,
    enabled: bool = True,
    templates: tuple[str, ...] = ("team_winback_simple",),
    max_revisions: int = 2,
) -> tuple[SelfEvaluateGate, ToolCounter]:
    ctx = CancellationContext()
    counter = ToolCounter(ctx)
    gate = SelfEvaluateGate(
        evaluator=evaluator,
        ctx=ctx,
        tool_counter=counter,
        config=GateConfig(
            max_revisions=max_revisions,
            simple_tier_enabled=enabled,
            simple_templates=templates,
        ),
    )
    return gate, counter


def _winback_draft(
    *, template_id: str = "team_winback_simple", cohort_size: int = 5
) -> Any:
    from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan

    tenant_id, run_id = _ctx_ids()
    return parse_campaign_plan(
        _winback_plan_dict(
            tenant_id=tenant_id,
            run_id=run_id,
            template_id=template_id,
            cohort_size=cohort_size,
        )
    )


# ---- Case 0: the relaxation WORKS — ARRR-only REVISE on simple → SHIPS -------


def test_simple_winback_weak_arrr_only_ships():
    """A genuinely-lapsed, grounded simple win-back whose ONLY critique is on
    the ``expected_arrr`` axis SHIPS: the gate drops the ARRR critique, nothing
    else fails it → REVISE collapses to PASS. This is the calibration's point."""
    evaluator = FakeSelfEvaluator(
        verdicts=[
            _verdict(
                SelfEvaluateOutcome.REVISE,
                pillar=[
                    "expected_arrr.basis overstates confidence: claims it 'will "
                    "recover ₹50K' rather than citing the low/high range."
                ],
                consistency=[
                    "expected_arrr.high_paise=3000000 is implausibly large for "
                    "target_cohort.cohort_size=5."
                ],
            ),
        ]
    )
    gate, counter = _simple_tier_gate(evaluator)
    outcome = gate.run(_winback_draft(cohort_size=5))

    assert outcome.action is GateAction.SHIP
    assert outcome.self_evaluate_status is SelfEvaluateStatus.PASSED
    assert outcome.outcome is SelfEvaluateOutcome.PASS
    assert evaluator.last_tier is GradeTier.SIMPLE  # classified simple
    assert evaluator.calls == 1
    assert counter.count == 1  # one dispatch charged — the grade still ran


# ---- Case 1: fabricated customer fact on simple → STILL REJECTED -------------


def test_simple_winback_fabricated_fact_still_rejected():
    """Anti-fabrication fires on the SIMPLE tier — the ARRR filter does NOT
    save a fabricated customer fact. Two REVISE verdicts each carrying a
    ``pillar`` invented-number critique (path ``target_cohort.selection_reason``,
    NOT expected_arrr) → first RETRY, second REJECTED. The fabrication critique
    survives the filter end-to-end onto the rejection feedback."""
    fab = (
        "target_cohort.selection_reason cites 'cafés have a 30% return rate' — "
        "invented per-vertical number, not in the bundle."
    )
    evaluator = FakeSelfEvaluator(
        verdicts=[
            _verdict(SelfEvaluateOutcome.REVISE, pillar=[fab]),
            _verdict(SelfEvaluateOutcome.REVISE, pillar=[fab]),
        ]
    )
    gate, _ = _simple_tier_gate(evaluator)
    draft = _winback_draft(cohort_size=5)

    first = gate.run(draft)
    assert first.action is GateAction.RETRY  # NOT shipped — fabrication survived
    assert evaluator.last_tier is GradeTier.SIMPLE
    # The surviving critique is in the retry feedback (not stripped):
    assert any("invented per-vertical" in m["content"] for m in first.feedback_messages)

    second = gate.run(draft)
    assert second.action is GateAction.REJECTED
    assert second.self_evaluate_status is SelfEvaluateStatus.FAILED_AFTER_REVISIONS
    assert second.rejection_feedback is not None
    assert second.rejection_feedback.pillar == [fab]  # carried, not dropped


# ---- Case 2: PII leak on simple → critique survives the ARRR filter ----------


def test_simple_winback_pii_leak_critique_survives_filter():
    """A PII-leak critique (literal phone in template_params, path
    ``message_plan.template_params`` under ``legal``) is NOT an expected_arrr
    path → the ARRR-drop does NOT strip it → the draft still fails on simple."""
    pii = (
        "message_plan.template_params contains the literal '+919321553267' — "
        "phone PII leaked into template params."
    )
    evaluator = FakeSelfEvaluator(
        verdicts=[
            # Mix a droppable ARRR critique alongside the PII one: only the ARRR
            # entry should vanish; the PII entry must still fail the draft.
            _verdict(
                SelfEvaluateOutcome.REVISE,
                legal=[pii],
                pillar=["expected_arrr.basis overstates confidence."],
            ),
        ]
    )
    gate, _ = _simple_tier_gate(evaluator)
    first = gate.run(_winback_draft(cohort_size=3))

    assert first.action is GateAction.RETRY  # PII critique survived → not shipped
    rendered = "\n".join(m["content"] for m in first.feedback_messages)
    assert "+919321553267" in rendered  # PII critique present
    assert "expected_arrr.basis" not in rendered  # ARRR critique dropped


# ---- Case 3: ungrounded cohort on simple → consistency-grounding still bites -


def test_simple_winback_ungrounded_cohort_still_revises():
    """The cohort-grounding sub-rule of ``consistency`` (NOT the ARRR sub-rule)
    survives the relaxation: targeting a bucket with zero real customers still
    REVISEs on simple."""
    crit = (
        "target_cohort.cohort_label '90-180 day dormants' but context_summary "
        "shows 0 customers in that bucket."
    )
    evaluator = FakeSelfEvaluator(
        verdicts=[_verdict(SelfEvaluateOutcome.REVISE, consistency=[crit])]
    )
    gate, _ = _simple_tier_gate(evaluator)
    first = gate.run(_winback_draft(cohort_size=4))

    assert first.action is GateAction.RETRY  # grounding critique survived
    assert evaluator.last_tier is GradeTier.SIMPLE


# ---- Case 4: THE one-directionality proof (direct filter unit test) ----------


def test_arrr_filter_is_provably_one_directional():
    """PROVE (don't assert) one-directionality: feed a MIXED critique set
    {expected_arrr.x, target_cohort.y, message_plan.z, legal.w} through the
    filter → ONLY the expected_arrr entry is dropped; the other 3 survive."""
    arrr = "expected_arrr.high_paise implausibly large for the cohort."
    cohort = "target_cohort.selection_reason cites an invented '30% return rate'."
    msg = "message_plan.template_params contains 'last chance' — pressure language."
    legal = "legal: misleading 'guaranteed savings' claim."  # legal-category, no path prefix

    fb = SelfEvaluateFeedback(
        schema=["evidence_refs not cited by any prose marker: ['E2']"],
        pillar=[arrr, cohort],
        consistency=[arrr],
        legal=[msg, legal],
    )
    out = _filter_arrr_only(fb)

    # The expected_arrr entries are the ONLY ones removed:
    assert out.pillar == [cohort]          # arrr dropped, cohort kept
    assert out.consistency is None         # was ARRR-only → empties to None
    assert out.legal == [msg, legal]       # neither is an expected_arrr path
    assert out.schema == fb.schema         # untouched
    assert not out.is_empty()              # 4 non-ARRR critiques remain

    # And the predicate itself: ONLY expected_arrr-leading strings match.
    assert _is_arrr_critique(arrr) is True
    assert _is_arrr_critique("expected_arrr.basis overstates confidence.") is True
    assert _is_arrr_critique("`expected_arrr.low_paise` too low.") is True
    assert _is_arrr_critique(cohort) is False
    assert _is_arrr_critique(msg) is False
    assert _is_arrr_critique(legal) is False
    # A critique that merely MENTIONS expected_arrr mid-sentence but cites a
    # different leading path is NOT dropped (the path is the leading token):
    assert (
        _is_arrr_critique(
            "target_cohort.cohort_size too small to justify expected_arrr.high_paise."
        )
        is False
    )


def test_simple_tier_mixed_revise_keeps_non_arrr_through_gate():
    """End-to-end through gate.run: a mixed REVISE on the simple tier surfaces
    the 3 non-ARRR critiques on RETRY and drops only the ARRR one."""
    evaluator = FakeSelfEvaluator(
        verdicts=[
            _verdict(
                SelfEvaluateOutcome.REVISE,
                pillar=[
                    "expected_arrr.basis overstates confidence.",
                    "target_cohort.selection_reason invented '30% return rate'.",
                ],
                consistency=["target_cohort.cohort_label mismatches the snapshot."],
                legal=["message_plan.template_params leaked '+9199...' PII."],
            )
        ]
    )
    gate, _ = _simple_tier_gate(evaluator)
    first = gate.run(_winback_draft(cohort_size=6))

    assert first.action is GateAction.RETRY
    rendered = "\n".join(m["content"] for m in first.feedback_messages)
    assert "expected_arrr.basis" not in rendered
    assert "target_cohort.selection_reason invented" in rendered
    assert "target_cohort.cohort_label mismatches" in rendered
    assert "message_plan.template_params leaked" in rendered


# ---- Case 5: OFFER template (money_bearing) → strict, never the simple lane --


def test_offer_template_uses_strict_grade_and_rejects_thin():
    """``team_winback_offer`` is money_bearing → STRICT regardless of size. An
    ARRR-only REVISE is NOT dropped (strict) → the thin/fabricated offer
    REVISEs, then REJECTs. It NEVER enters the simple lane."""
    arrr = "expected_arrr.basis overstates confidence."
    evaluator = FakeSelfEvaluator(
        verdicts=[
            _verdict(SelfEvaluateOutcome.REVISE, pillar=[arrr]),
            _verdict(SelfEvaluateOutcome.REVISE, pillar=[arrr]),
        ]
    )
    # Allow-list BOTH templates so the offer passes the name check and the
    # money_bearing clause is what forces strict (defence-in-depth proof).
    gate, _ = _simple_tier_gate(
        evaluator, templates=("team_winback_simple", "team_winback_offer")
    )
    draft = _winback_draft(template_id="team_winback_offer", cohort_size=5)

    first = gate.run(draft)
    assert first.action is GateAction.RETRY  # ARRR NOT dropped → strict
    assert evaluator.last_tier is GradeTier.STRICT  # money_bearing forced strict
    second = gate.run(draft)
    assert second.action is GateAction.REJECTED


# ---- Case 6: cohort > L3_AUTO_MAX_BATCH (>20) on simple template → strict ----


def test_simple_template_large_cohort_falls_to_strict():
    """A ``team_winback_simple`` draft with cohort_size=21 (> L3_AUTO_MAX_BATCH)
    falls to STRICT — the ARRR critique is NOT dropped, the draft REVISEs. The
    threshold bites; a 20-cohort would have shipped."""
    from orchestrator.agents.autonomy import L3_AUTO_MAX_BATCH

    arrr = "expected_arrr.high_paise implausibly large for the cohort."
    evaluator = FakeSelfEvaluator(
        verdicts=[_verdict(SelfEvaluateOutcome.REVISE, consistency=[arrr])]
    )
    gate, _ = _simple_tier_gate(evaluator)
    first = gate.run(_winback_draft(cohort_size=L3_AUTO_MAX_BATCH + 1))

    assert first.action is GateAction.RETRY  # strict — ARRR survived
    assert evaluator.last_tier is GradeTier.STRICT


def test_cohort_ceiling_boundary_is_l3_auto_max_batch():
    """Boundary: exactly L3_AUTO_MAX_BATCH ⇒ SIMPLE; one over ⇒ STRICT.
    The ceiling is the imported constant, not a hardcoded 20."""
    from orchestrator.agents.autonomy import L3_AUTO_MAX_BATCH

    gate, _ = _simple_tier_gate(FakeSelfEvaluator(verdicts=[]))
    at_ceiling = _winback_draft(cohort_size=L3_AUTO_MAX_BATCH)
    over_ceiling = _winback_draft(cohort_size=L3_AUTO_MAX_BATCH + 1)
    assert gate._classify_tier(at_ceiling) is GradeTier.SIMPLE
    assert gate._classify_tier(over_ceiling) is GradeTier.STRICT


# ---- Case 7: money_bearing-resolve error / registry drift → fail-closed ------


def test_unresolvable_template_fails_closed_to_strict():
    """An allow-listed template name that the registry cannot resolve (drift /
    typo / retired) → ``_resolve_money_bearing`` fail-closes to money-bearing →
    STRICT. The ARRR critique is NOT dropped. A drifted template can NEVER reach
    the relaxed lane."""
    arrr = "expected_arrr.basis overstates confidence."
    evaluator = FakeSelfEvaluator(
        verdicts=[_verdict(SelfEvaluateOutcome.REVISE, pillar=[arrr])]
    )
    # Put a GHOST template (not in the registry yaml) in the allow-list so it
    # passes the name check but fails the registry resolve.
    gate, _ = _simple_tier_gate(evaluator, templates=("team_winback_ghost_xyz",))
    draft = _winback_draft(template_id="team_winback_ghost_xyz", cohort_size=3)

    first = gate.run(draft)
    assert first.action is GateAction.RETRY  # fail-closed strict — ARRR survived
    assert evaluator.last_tier is GradeTier.STRICT


# ---- Case 8: kill-switch (simple_tier.enabled=false) → all-strict ------------


def test_kill_switch_off_forces_strict_for_every_draft():
    """``simple_tier_enabled=False`` reverts the gate to all-strict: even a
    canonical simple win-back (small cohort, non-money-bearing) takes the FULL
    grade and the ARRR critique is NOT dropped. A clean one-line revert."""
    arrr = "expected_arrr.basis overstates confidence."
    evaluator = FakeSelfEvaluator(
        verdicts=[_verdict(SelfEvaluateOutcome.REVISE, pillar=[arrr])]
    )
    gate, _ = _simple_tier_gate(evaluator, enabled=False)
    first = gate.run(_winback_draft(cohort_size=3))

    assert first.action is GateAction.RETRY  # strict — ARRR survived
    assert evaluator.last_tier is GradeTier.STRICT


# ---- Classification matrix + config wiring ----------------------------------


def test_classify_tier_matrix():
    """The full predicate matrix in one place."""
    gate, _ = _simple_tier_gate(FakeSelfEvaluator(verdicts=[]))
    assert (
        gate._classify_tier(_winback_draft(template_id="team_winback_simple", cohort_size=20))
        is GradeTier.SIMPLE
    )
    # Non-allow-listed template → strict.
    assert (
        gate._classify_tier(_winback_draft(template_id="team_winback_v1", cohort_size=5))
        is GradeTier.STRICT
    )
    # Offer (money_bearing) — not in this gate's allow-list → strict.
    assert (
        gate._classify_tier(_winback_draft(template_id="team_winback_offer", cohort_size=5))
        is GradeTier.STRICT
    )


def test_gate_config_loads_simple_tier_block_from_yaml():
    """Production wiring: GateConfig.load() reads the simple_tier block, and the
    allow-list is pinned to the executor's WINBACK_TEMPLATE_NAME (no drift)."""
    from orchestrator.agents.sales_recovery_executor import WINBACK_TEMPLATE_NAME

    cfg = GateConfig.load()
    assert cfg.simple_tier_enabled is True
    assert WINBACK_TEMPLATE_NAME in cfg.simple_templates


def test_strict_path_untouched_for_legacy_template():
    """STRICT proof: a legacy ``team_winback_v1`` draft (the existing-test
    fixture template) is graded STRICT and an ARRR-only REVISE is NOT dropped —
    the calibration only ADDS the simple lane; it never widens the legacy one."""
    arrr = "expected_arrr.basis overstates confidence."
    evaluator = FakeSelfEvaluator(
        verdicts=[_verdict(SelfEvaluateOutcome.REVISE, pillar=[arrr])]
    )
    gate, _ = _simple_tier_gate(evaluator)
    first = gate.run(_winback_draft(template_id="team_winback_v1", cohort_size=5))
    assert first.action is GateAction.RETRY  # not dropped → strict
    assert evaluator.last_tier is GradeTier.STRICT
