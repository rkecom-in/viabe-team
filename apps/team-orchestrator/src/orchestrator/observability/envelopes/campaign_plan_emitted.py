"""VT-179 envelope: ``campaign_plan_emitted`` (orchestrator terminal verdict).

Replaces legacy step_kind ``campaign_plan_terminal`` (VT-179 Option A
canonical rename — collapse.py writes this kind). Variant + reason fields
match the CL-294 collapse-node output contract.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


CampaignPlanVariant = Literal[
    "proposed",
    "out_of_scope",
    "insufficient_data",
]


class CampaignPlanEmittedInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_source: Literal["agent", "fallback_deterministic"]


class CampaignPlanEmittedOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    variant: CampaignPlanVariant
    version: str
    out_of_scope_reason: str | None = None
    suggested_specialist: str | None = None
    missing_data: list[dict[str, Any]] | None = None


class CampaignPlanEmittedEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "campaign_plan_emitted"

    input_envelope: CampaignPlanEmittedInput
    output_envelope: CampaignPlanEmittedOutput


__all__ = [
    "CampaignPlanVariant",
    "CampaignPlanEmittedInput",
    "CampaignPlanEmittedOutput",
    "CampaignPlanEmittedEnvelope",
]
