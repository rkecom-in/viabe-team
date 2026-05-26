"""VT-179 envelope: ``attribution_match`` (recovered-revenue row close).

Per VT-175. One row per matched attribution_close + customer payment.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from .base import StepEnvelope


class AttributionMatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: str
    customer_token: str


class AttributionMatchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attribution_id: str
    arrr_paise: int
    matched_at_utc: str


class AttributionMatchEnvelope(StepEnvelope):
    step_kind: ClassVar[str] = "attribution_match"

    input_envelope: AttributionMatchInput
    output_envelope: AttributionMatchOutput


__all__ = [
    "AttributionMatchInput",
    "AttributionMatchOutput",
    "AttributionMatchEnvelope",
]
