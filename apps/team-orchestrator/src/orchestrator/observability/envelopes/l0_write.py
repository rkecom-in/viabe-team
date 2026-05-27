"""VT-179 envelope: ``l0_write`` (orchestrator-agent L0 memory write).

Emitted on every ``observability/l0_memory.write_l0_fragment`` call (the
@tool_step-decorated wrapper in ``agent/orchestrator_agent.py``). Records
the cohort_key + fragment_type the orchestrator-agent intended to observe,
plus the resulting observation_count after the UPSERT.

Per CL-220: tool-call envelopes (l0_write/l0_query) are step-kind-distinct
from ``mcp_tool_call`` so downstream replay can filter L0-memory activity
without scanning every tool call.
Per CL-390: ``cohort_key`` is the cohort identifier (NOT tenant_id);
``content`` is the JSON payload that passed PII reject.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class L0WriteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fragment_type: Literal[
        "routing_decision", "specialist_outcome", "trigger_pattern"
    ]
    cohort_key: str
    content: dict[str, Any]


class L0WriteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fragment_id: str
    observation_count: int
    inserted: bool


class L0WriteEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "l0_write"

    input_envelope: L0WriteInput
    output_envelope: L0WriteOutput


__all__ = ["L0WriteInput", "L0WriteOutput", "L0WriteEnvelope"]
