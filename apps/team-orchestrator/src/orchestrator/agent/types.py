"""Agent result envelope (VT-32).

``AgentResult`` is the contract specialists return from the
specialist-dispatch seam. The orchestrator owns side effects (DB, WhatsApp,
LangGraph state mutation); the specialist hands back a typed result, the
orchestrator translates it.

Status / terminated_by alignment
--------------------------------
- ``status`` extends the obvious agent terminal states with two operational
  cases: ``terminated`` (a hard-limit enforcer cancelled the run — VT-35)
  and ``placeholder`` (the canary path returns this; the placeholder prompt
  emits ``{"status": "placeholder"}``).
- ``terminated_by`` reuses ``failures.HardLimitAxis`` (CL-242 / VT-29 PR
  #29) rather than defining a parallel literal set, so VT-35's enforcers
  emit and read the same enum on both sides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from orchestrator.failures import HardLimitAxis
from orchestrator.knowledge_contracts import (
    ConflictManifestEntry,
    EvidenceManifestEntry,
    GroundingStatus,
    KnowledgeVersion,
    validate_grounding_envelope,
)

AgentStatus = Literal[
    "completed",
    "terminated",
    "refused",
    "invalid",
    "placeholder",
    "rejected",  # self-evaluate gate exhausted retry; SELF_EVAL_REJECTED
]


@dataclass
class AgentResult:
    """Specialist result envelope (VT-32).

    Fields:
      - ``status`` — terminal state of the run. ``terminated`` ⇒ check
        ``terminated_by`` to discover which hard-limit axis fired.
        ``placeholder`` ⇒ the canary path, no real work performed.
      - ``terminated_by`` — populated iff ``status == 'terminated'``;
        ``None`` otherwise. The axis identifies which of the five
        ``HardLimitAxis`` budgets tripped (VT-35).
      - ``output`` — structured agent output. For the sales_recovery
        specialist this will eventually be a serialised CampaignPlan (a
        later subtask); placeholder runs return ``{"status": "placeholder"}``.
      - ``tokens_used`` — sum of input + output tokens across every turn
        in the run. Pre-cache (cache reads count once).
      - ``tool_calls_made`` — every tool dispatch attempt, including ones
        that errored. VT-35's tool counter increments at the same seam.
      - ``wallclock_ms`` — entry → exit, including time spent in tools.
      - ``cost_paise`` — accrued via ``orchestrator.agent.cost``;
        accumulates EVEN on a terminated run (hard-rule: terminations do
        not refund the spend they already incurred).
      - ``raw_messages`` — full anthropic Messages-API message list as
        sent/received (assistant + user + tool_result blocks). For trace
        / observability and as the source of truth when extracting
        ``output``.
      - ``terminated_reason`` — short human-readable string, populated
        iff ``status == 'terminated'``. Example: ``"wallclock exceeded
        300s budget"``.
    """

    status: AgentStatus
    terminated_by: HardLimitAxis | None = None
    output: dict[str, Any] | None = None
    tokens_used: int = 0
    tool_calls_made: int = 0
    wallclock_ms: int = 0
    cost_paise: int = 0
    raw_messages: list[dict[str, Any]] = field(default_factory=list)
    terminated_reason: str | None = None
    evidence_manifest: tuple[EvidenceManifestEntry, ...] = ()
    conflict_manifest: tuple[ConflictManifestEntry, ...] = ()
    knowledge_version: KnowledgeVersion | None = None
    grounding_status: GroundingStatus = GroundingStatus.NOT_APPLICABLE

    def __post_init__(self) -> None:
        validate_grounding_envelope(
            evidence_manifest=self.evidence_manifest,
            conflict_manifest=self.conflict_manifest,
            knowledge_version=self.knowledge_version,
            grounding_status=self.grounding_status,
        )


__all__ = ["AgentResult", "AgentStatus"]
