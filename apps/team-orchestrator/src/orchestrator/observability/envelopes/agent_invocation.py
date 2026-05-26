"""VT-179 envelope: ``agent_invocation`` (agent/specialist called).

Replaces legacy step_kind ``awaiting_brain`` (VT-179 Option A canonical
rename — runner.record_brain_pending writes this kind).
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class AgentInvocationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_role: Literal["orchestrator", "sales_recovery", "owner_inputs"]
    reason: str


class AgentInvocationEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "agent_invocation"

    input_envelope: AgentInvocationInput
    output_envelope: None = None


__all__ = ["AgentInvocationInput", "AgentInvocationEnvelope"]
