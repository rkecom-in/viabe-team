"""VT-179 envelope: ``state_transition`` (LangGraph Command mirror).

Per CL-175 — every langgraph Command/state-update lands as a step row
with the from/to state pair captured.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class StateTransitionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    from_state: str
    to_state: str
    langgraph_command: dict[str, Any]


class StateTransitionEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "state_transition"

    input_envelope: StateTransitionInput
    output_envelope: None = None


__all__ = ["StateTransitionInput", "StateTransitionEnvelope"]
