"""VT-711 default-off rollout modes and automatic rollback tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("pydantic")

from orchestrator.knowledge.rollout import (  # noqa: E402
    AutoRollbackEvaluator,
    RequesterKind,
    RollbackPolicy,
    RollbackReason,
    RolloutConfig,
    RolloutMode,
    RolloutRequest,
    RolloutRouter,
    RolloutTelemetry,
    RolloutTransition,
)

_NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


def _policy() -> RollbackPolicy:
    return RollbackPolicy(
        maximum_o11_regression=0.05,
        maximum_hedge_rate=0.3,
        maximum_refusal_rate=0.2,
        maximum_p95_latency_ms=500.0,
        maximum_cost_per_decision=0.25,
        minimum_observations=100,
    )


def _request(*, requester_id: str = "agent:sales", kind: RequesterKind = RequesterKind.AGENT):
    return RolloutRequest(
        requester_id=requester_id,
        requester_kind=kind,
        tenant_ref="tenant-secret-not-output",
    )


def _config(mode: RolloutMode, **updates) -> RolloutConfig:
    values = {
        "mode": mode,
        "corpus_version_id": "corpus-v1",
        "grant_ref": "fazal:future-explicit-grant",
        "approved_by": "fazal",
        "rollback_policy": _policy(),
    }
    values.update(updates)
    return RolloutConfig(**values)


def _telemetry(**updates) -> RolloutTelemetry:
    values = {
        "observation_count": 150,
        "money_incidents": 0,
        "regulatory_incidents": 0,
        "consent_incidents": 0,
        "cross_tenant_evidence_exposures": 0,
        "provenance_losses": 0,
        "o11_baseline_score": 0.8,
        "o11_current_score": 0.8,
        "hedge_rate": 0.1,
        "refusal_rate": 0.1,
        "p95_latency_ms": 200.0,
        "cost_per_decision": 0.1,
        "incident_card_version_refs": (),
    }
    values.update(updates)
    return RolloutTelemetry(**values)


def test_default_config_is_strictly_off() -> None:
    config = RolloutConfig()
    decision = RolloutRouter.decide(config, _request())
    assert config.mode is RolloutMode.OFF
    assert decision.retrieve is False
    assert decision.inject_into_reasoning is False
    assert decision.record_shadow_evidence is False
    assert decision.corpus_version_id is None


def test_off_cannot_hide_dormant_activation_authority() -> None:
    with pytest.raises(ValueError, match="off mode cannot carry"):
        RolloutConfig(mode=RolloutMode.OFF, corpus_version_id="dormant-corpus")


def test_non_off_requires_fazal_grant_complete_policy_and_corpus() -> None:
    with pytest.raises(ValueError, match="requires corpus_version_id"):
        RolloutConfig(mode=RolloutMode.SHADOW)
    with pytest.raises(ValueError, match="requires Fazal approval"):
        RolloutConfig(
            mode=RolloutMode.SHADOW,
            corpus_version_id="corpus-v1",
            grant_ref="someone-else",
            approved_by="someone-else",
            rollback_policy=_policy(),
        )


def test_shadow_retrieves_and_logs_without_injecting_context() -> None:
    decision = RolloutRouter.decide(_config(RolloutMode.SHADOW), _request())
    assert decision.retrieve is True
    assert decision.record_shadow_evidence is True
    assert decision.inject_into_reasoning is False


def test_vtr_canary_injects_only_for_allowlisted_vtr() -> None:
    config = _config(RolloutMode.VTR_CANARY, vtr_canary_ids=frozenset({"vtr:fazal"}))
    allowed = RolloutRouter.decide(
        config, _request(requester_id="vtr:fazal", kind=RequesterKind.VTR)
    )
    wrong_kind = RolloutRouter.decide(
        config, _request(requester_id="vtr:fazal", kind=RequesterKind.AGENT)
    )
    other = RolloutRouter.decide(
        config, _request(requester_id="vtr:other", kind=RequesterKind.VTR)
    )
    assert allowed.inject_into_reasoning is True
    assert wrong_kind.inject_into_reasoning is False
    assert other.inject_into_reasoning is False
    assert wrong_kind.record_shadow_evidence is True


def test_active_mode_is_explicit_and_injects_approved_corpus() -> None:
    decision = RolloutRouter.decide(_config(RolloutMode.ACTIVE), _request())
    assert decision.mode is RolloutMode.ACTIVE
    assert decision.corpus_version_id == "corpus-v1"
    assert decision.retrieve is True and decision.inject_into_reasoning is True


def test_zero_tolerance_incident_rolls_back_before_sample_floor() -> None:
    telemetry = _telemetry(
        observation_count=1,
        consent_incidents=1,
        incident_card_version_refs=("card-v2", "card-v1", "card-v2"),
    )
    decision = AutoRollbackEvaluator.evaluate(
        current_mode=RolloutMode.VTR_CANARY,
        telemetry=telemetry,
        policy=_policy(),
    )
    assert decision.rollback is True
    assert decision.target_mode is RolloutMode.OFF
    assert decision.reasons == (RollbackReason.CONSENT_INCIDENT,)
    assert decision.emergency_quarantine_card_refs == ("card-v1", "card-v2")


def test_operational_thresholds_wait_for_sample_then_trigger_together() -> None:
    below_sample = AutoRollbackEvaluator.evaluate(
        current_mode=RolloutMode.SHADOW,
        telemetry=_telemetry(
            observation_count=99,
            o11_current_score=0.5,
            hedge_rate=0.9,
            refusal_rate=0.9,
            p95_latency_ms=900.0,
            cost_per_decision=1.0,
        ),
        policy=_policy(),
    )
    assert below_sample.rollback is False

    enough = AutoRollbackEvaluator.evaluate(
        current_mode=RolloutMode.SHADOW,
        telemetry=_telemetry(
            observation_count=100,
            o11_current_score=0.5,
            hedge_rate=0.9,
            refusal_rate=0.9,
            p95_latency_ms=900.0,
            cost_per_decision=1.0,
        ),
        policy=_policy(),
    )
    assert enough.rollback is True
    assert set(enough.reasons) == {
        RollbackReason.MATERIAL_O11_REGRESSION,
        RollbackReason.ABNORMAL_HEDGE_RATE,
        RollbackReason.ABNORMAL_REFUSAL_RATE,
        RollbackReason.LATENCY_CEILING,
        RollbackReason.COST_CEILING,
    }


def test_cross_tenant_and_provenance_loss_are_independent_rollback_triggers() -> None:
    decision = AutoRollbackEvaluator.evaluate(
        current_mode=RolloutMode.ACTIVE,
        telemetry=_telemetry(
            observation_count=0,
            cross_tenant_evidence_exposures=1,
            provenance_losses=2,
        ),
        policy=_policy(),
    )
    assert decision.rollback is True
    assert set(decision.reasons) == {
        RollbackReason.CROSS_TENANT_EVIDENCE,
        RollbackReason.PROVENANCE_LOSS,
    }


def test_rollout_transitions_cannot_skip_stages_and_rollback_requires_evidence() -> None:
    with pytest.raises(ValueError, match="cannot skip"):
        RolloutTransition(
            from_mode=RolloutMode.OFF,
            to_mode=RolloutMode.ACTIVE,
            grant_ref="fazal:skip",
            approved_by="fazal",
            occurred_at=_NOW,
        )
    rollback = RolloutTransition(
        from_mode=RolloutMode.ACTIVE,
        to_mode=RolloutMode.OFF,
        grant_ref="auto-rollback:incident",
        approved_by="fazal",
        occurred_at=_NOW,
        rollback_reasons=(RollbackReason.CONSENT_INCIDENT,),
    )
    assert rollback.to_mode is RolloutMode.OFF


def test_off_mode_never_rolls_back_again() -> None:
    decision = AutoRollbackEvaluator.evaluate(
        current_mode=RolloutMode.OFF,
        telemetry=_telemetry(money_incidents=1),
        policy=_policy(),
    )
    assert decision.rollback is False
    assert decision.reasons == ()
