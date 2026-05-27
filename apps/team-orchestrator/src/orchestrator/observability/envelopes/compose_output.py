"""VT-179 envelope: ``compose_output`` (VT-193 brain-wired unified-output).

Emitted once per brain-dispatched run AFTER the supervisor graph
terminates, carrying the composed owner-facing payload produced by
``orchestrator.output_composer.compose_owner_output``.

Per CL-19: typed envelopes carry per-field payload shape; the
unified-output composer (VT-30) writes the full ComposedOutput
dataclass projection here so Ops Console replay can render the
send-ready envelope (template_name + content_sid + variables + body
preview).

Per VT-193: this envelope is the dispatch EXIT marker. The dispatch
ENTRY is the existing ``agent_invocation`` envelope (whose semantic
shifts from placeholder to real dispatch).
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class ComposeOutputInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_or_trigger: str
    terminal_path: str  # "terminal" | "collapse" | "escalated"


class ComposeOutputOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    template_name: str | None = None
    content_sid: str | None = None
    body_preview: str | None = None
    variables: dict[str, Any] | None = None
    envelope_hash: str | None = None


class ComposeOutputEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "compose_output"

    input_envelope: ComposeOutputInput
    output_envelope: ComposeOutputOutput


__all__ = [
    "ComposeOutputInput",
    "ComposeOutputOutput",
    "ComposeOutputEnvelope",
]
