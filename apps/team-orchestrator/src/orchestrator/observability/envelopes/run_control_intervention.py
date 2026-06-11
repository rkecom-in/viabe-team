"""VT-374 envelope: ``run_control_intervention`` (run-control seam timeline row).

Written by ``pipeline_observability.record_intervention`` whenever a
controllable-seam hold released, a hold parked a run (max-hold/held), or a
one-shot override was consumed. The envelope carries IDs/enums ONLY (CL-390);
the structural data lives in the mig-131 ``pipeline_steps.override_id`` /
``paused_ms`` COLUMNS — the vtr_step_timeline view shows this kind keys-only
by default and the columns carry the data.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class RunControlInterventionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # 'released' = a pause hold ended and the step proceeded; 'held' = the seam
    # parked the run instead of proceeding (supervisor fan-out hold, runner
    # max-hold); 'override_consumed' = a step_overrides row was claimed.
    action: Literal["released", "held", "override_consumed"]
    workflow_kind: str
    step_name: str


class RunControlInterventionEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "run_control_intervention"

    input_envelope: RunControlInterventionInput
    output_envelope: None = None


__all__ = [
    "RunControlInterventionInput",
    "RunControlInterventionEnvelope",
]
