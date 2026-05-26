"""VT-179 base envelope class for typed pipeline_steps records.

Every step_kind envelope subclasses ``StepEnvelope``. The subclass carries
its own ``input_envelope`` / ``output_envelope`` Pydantic sub-models with
step-specific declared fields; the canonical-column-name fields (from
VT-187 / migration 025) live on the base class so every envelope serializes
into the canonical pipeline_steps shape.

Field names mirror the canonical pipeline_steps column names per CL-417 —
the writer (VT-180) maps envelope fields → columns 1:1 by name.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


StepStatus = Literal["running", "completed", "failed", "skipped"]


class StepEnvelope(BaseModel):
    """Base for all step_kind envelopes (VT-179).

    Subclasses MUST set ``step_kind`` to a ``Literal["<kind>"]`` so Pydantic
    can discriminate on it, and declare ``input_envelope`` / ``output_envelope``
    sub-models with their step-specific fields.

    The fields here map 1:1 onto canonical ``pipeline_steps`` columns from
    VT-187 / migration 025.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_kind: ClassVar[str] = ""

    run_id: UUID
    tenant_id: UUID
    step_seq: int
    step_name: str | None = None
    parent_step_id: UUID | None = None
    status: StepStatus = "completed"
    decision_rationale: str | None = None
    model_used: str | None = None
    tokens_input: int | None = None
    tokens_output: int | None = None
    tool_calls: list[dict[str, Any]] | None = None
    started_at: datetime
    ended_at: datetime | None = None
    error: dict[str, Any] | None = None

    input_envelope: BaseModel
    output_envelope: BaseModel | None = None


__all__ = ["StepEnvelope", "StepStatus"]
