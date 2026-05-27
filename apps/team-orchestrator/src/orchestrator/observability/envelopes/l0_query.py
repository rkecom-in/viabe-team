"""VT-179 envelope: ``l0_query`` (orchestrator-agent L0 memory read).

Emitted on every ``observability/l0_memory.query_l0`` call. Records the
cohort_key + fragment_type queried plus how many fragments came back
(RLS gates at observation_count >= 10 per CL-28).

Per CL-220: distinct step_kind from ``mcp_tool_call`` for L0-memory
replay filtering.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class L0QueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fragment_type: Literal[
        "routing_decision", "specialist_outcome", "trigger_pattern"
    ]
    cohort_key: str
    k: int = 5


class L0QueryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fragments: list[dict[str, Any]]
    matched_count: int


class L0QueryEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "l0_query"

    input_envelope: L0QueryInput
    output_envelope: L0QueryOutput


__all__ = ["L0QueryInput", "L0QueryOutput", "L0QueryEnvelope"]
