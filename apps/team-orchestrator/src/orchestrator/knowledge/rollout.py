"""VT-711 O8 rollout modes and automatic rollback policy (unwired, default OFF).

Importing this module cannot read environment flags or activate retrieval.  Callers must construct
an explicit non-off config carrying a grant reference, corpus version and complete rollback policy.
No production module imports this file in checkpoint C.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RolloutRejected(RuntimeError):
    """A rollout configuration or transition failed closed."""


class RolloutMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    VTR_CANARY = "vtr_canary"
    ACTIVE = "active"


class RequesterKind(StrEnum):
    AGENT = "agent"
    MANAGER = "manager"
    VTR = "vtr"
    HARNESS = "harness"


class RollbackReason(StrEnum):
    MONEY_INCIDENT = "money_incident"
    REGULATORY_INCIDENT = "regulatory_incident"
    CONSENT_INCIDENT = "consent_incident"
    CROSS_TENANT_EVIDENCE = "cross_tenant_evidence"
    MATERIAL_O11_REGRESSION = "material_o11_regression"
    ABNORMAL_HEDGE_RATE = "abnormal_hedge_rate"
    ABNORMAL_REFUSAL_RATE = "abnormal_refusal_rate"
    LATENCY_CEILING = "latency_ceiling"
    COST_CEILING = "cost_ceiling"
    PROVENANCE_LOSS = "provenance_loss"


class RollbackPolicy(BaseModel):
    """Unratified numerical ceilings are all required; there are no guessed defaults."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    maximum_o11_regression: float = Field(ge=0.0, le=1.0)
    maximum_hedge_rate: float = Field(ge=0.0, le=1.0)
    maximum_refusal_rate: float = Field(ge=0.0, le=1.0)
    maximum_p95_latency_ms: float = Field(gt=0.0)
    maximum_cost_per_decision: float = Field(ge=0.0)
    minimum_observations: int = Field(ge=1)


class RolloutConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    mode: RolloutMode = RolloutMode.OFF
    corpus_version_id: str | None = None
    grant_ref: str | None = None
    approved_by: str | None = None
    vtr_canary_ids: frozenset[str] = frozenset()
    rollback_policy: RollbackPolicy | None = None

    @model_validator(mode="after")
    def _non_off_requires_explicit_grant_and_policy(self) -> "RolloutConfig":
        if self.mode is RolloutMode.OFF:
            if any(
                (
                    self.corpus_version_id,
                    self.grant_ref,
                    self.approved_by,
                    self.vtr_canary_ids,
                    self.rollback_policy,
                )
            ):
                raise ValueError("off mode cannot carry dormant activation authority")
            return self
        if not all(
            (self.corpus_version_id, self.grant_ref, self.approved_by, self.rollback_policy)
        ):
            raise ValueError(
                "non-off rollout requires corpus_version_id, grant_ref, approved_by and rollback_policy"
            )
        if self.approved_by != "fazal":
            raise ValueError("non-off O8 rollout requires Fazal approval")
        if self.mode is RolloutMode.VTR_CANARY and not self.vtr_canary_ids:
            raise ValueError("vtr_canary mode requires an explicit VTR allowlist")
        if self.mode is not RolloutMode.VTR_CANARY and self.vtr_canary_ids:
            raise ValueError("VTR allowlist is valid only in vtr_canary mode")
        return self


class RolloutRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requester_id: str
    requester_kind: RequesterKind
    tenant_ref: str = Field(exclude=True)


class RolloutDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: RolloutMode
    corpus_version_id: str | None
    retrieve: bool
    inject_into_reasoning: bool
    record_shadow_evidence: bool
    reason: str


class RolloutRouter:
    """Pure mode semantics.  It is intentionally not registered with any live broker."""

    @staticmethod
    def decide(config: RolloutConfig, request: RolloutRequest) -> RolloutDecision:
        if config.mode is RolloutMode.OFF:
            return RolloutDecision(
                mode=config.mode,
                corpus_version_id=None,
                retrieve=False,
                inject_into_reasoning=False,
                record_shadow_evidence=False,
                reason="knowledge rollout is off",
            )
        if config.mode is RolloutMode.SHADOW:
            return RolloutDecision(
                mode=config.mode,
                corpus_version_id=config.corpus_version_id,
                retrieve=True,
                inject_into_reasoning=False,
                record_shadow_evidence=True,
                reason="shadow retrieves and records but cannot change agent context",
            )
        if config.mode is RolloutMode.VTR_CANARY:
            admitted = (
                request.requester_kind is RequesterKind.VTR
                and request.requester_id in config.vtr_canary_ids
            )
            return RolloutDecision(
                mode=config.mode,
                corpus_version_id=config.corpus_version_id,
                retrieve=True,
                inject_into_reasoning=admitted,
                record_shadow_evidence=True,
                reason=(
                    "allowlisted VTR canary receives knowledge context"
                    if admitted
                    else "non-allowlisted requester remains shadow-only"
                ),
            )
        return RolloutDecision(
            mode=config.mode,
            corpus_version_id=config.corpus_version_id,
            retrieve=True,
            inject_into_reasoning=True,
            record_shadow_evidence=True,
            reason="active mode injects the explicitly approved corpus",
        )


class RolloutTelemetry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_count: int = Field(ge=0)
    money_incidents: int = Field(ge=0)
    regulatory_incidents: int = Field(ge=0)
    consent_incidents: int = Field(ge=0)
    cross_tenant_evidence_exposures: int = Field(ge=0)
    provenance_losses: int = Field(ge=0)
    o11_baseline_score: float = Field(ge=0.0, le=1.0)
    o11_current_score: float = Field(ge=0.0, le=1.0)
    hedge_rate: float = Field(ge=0.0, le=1.0)
    refusal_rate: float = Field(ge=0.0, le=1.0)
    p95_latency_ms: float = Field(ge=0.0)
    cost_per_decision: float = Field(ge=0.0)
    incident_card_version_refs: tuple[str, ...] = ()


class AutoRollbackDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rollback: bool
    target_mode: Literal[RolloutMode.OFF] | None
    reasons: tuple[RollbackReason, ...]
    emergency_quarantine_card_refs: tuple[str, ...]


class AutoRollbackEvaluator:
    """Zero-tolerance privacy/safety triggers plus policy-supplied operational ceilings."""

    @staticmethod
    def evaluate(
        *,
        current_mode: RolloutMode,
        telemetry: RolloutTelemetry,
        policy: RollbackPolicy,
    ) -> AutoRollbackDecision:
        if current_mode is RolloutMode.OFF:
            return AutoRollbackDecision(
                rollback=False,
                target_mode=None,
                reasons=(),
                emergency_quarantine_card_refs=(),
            )
        reasons: list[RollbackReason] = []
        if telemetry.money_incidents:
            reasons.append(RollbackReason.MONEY_INCIDENT)
        if telemetry.regulatory_incidents:
            reasons.append(RollbackReason.REGULATORY_INCIDENT)
        if telemetry.consent_incidents:
            reasons.append(RollbackReason.CONSENT_INCIDENT)
        if telemetry.cross_tenant_evidence_exposures:
            reasons.append(RollbackReason.CROSS_TENANT_EVIDENCE)
        if telemetry.provenance_losses:
            reasons.append(RollbackReason.PROVENANCE_LOSS)

        if telemetry.observation_count >= policy.minimum_observations:
            if telemetry.o11_baseline_score - telemetry.o11_current_score > policy.maximum_o11_regression:
                reasons.append(RollbackReason.MATERIAL_O11_REGRESSION)
            if telemetry.hedge_rate > policy.maximum_hedge_rate:
                reasons.append(RollbackReason.ABNORMAL_HEDGE_RATE)
            if telemetry.refusal_rate > policy.maximum_refusal_rate:
                reasons.append(RollbackReason.ABNORMAL_REFUSAL_RATE)
            if telemetry.p95_latency_ms > policy.maximum_p95_latency_ms:
                reasons.append(RollbackReason.LATENCY_CEILING)
            if telemetry.cost_per_decision > policy.maximum_cost_per_decision:
                reasons.append(RollbackReason.COST_CEILING)

        rollback = bool(reasons)
        return AutoRollbackDecision(
            rollback=rollback,
            target_mode=RolloutMode.OFF if rollback else None,
            reasons=tuple(reasons),
            emergency_quarantine_card_refs=(
                tuple(sorted(set(telemetry.incident_card_version_refs))) if rollback else ()
            ),
        )


class RolloutTransition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    from_mode: RolloutMode
    to_mode: RolloutMode
    grant_ref: str
    approved_by: Literal["fazal"]
    occurred_at: datetime
    rollback_reasons: tuple[RollbackReason, ...] = ()

    @model_validator(mode="after")
    def _ordered_or_rollback(self) -> "RolloutTransition":
        if self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        forward = {
            RolloutMode.OFF: RolloutMode.SHADOW,
            RolloutMode.SHADOW: RolloutMode.VTR_CANARY,
            RolloutMode.VTR_CANARY: RolloutMode.ACTIVE,
        }
        if self.to_mode is RolloutMode.OFF:
            if self.from_mode is RolloutMode.OFF or not self.rollback_reasons:
                raise ValueError("rollback to off requires a non-off source and evidence reasons")
        elif forward.get(self.from_mode) is not self.to_mode:
            raise ValueError("rollout cannot skip graduation stages")
        elif self.rollback_reasons:
            raise ValueError("forward graduation cannot carry rollback reasons")
        return self


__all__ = [
    "AutoRollbackDecision",
    "AutoRollbackEvaluator",
    "RequesterKind",
    "RollbackPolicy",
    "RollbackReason",
    "RolloutConfig",
    "RolloutDecision",
    "RolloutMode",
    "RolloutRejected",
    "RolloutRequest",
    "RolloutRouter",
    "RolloutTelemetry",
    "RolloutTransition",
]
