"""CampaignPlan v0.1 contract — locked per CL-177.

Extension policy: adding/removing fields, status enum changes, and field
type changes are all Type 2 (joint Clau + Fazal). Do not modify without a
Decision entry superseding CL-177.

Downstream consumers (lock prevents drift):
- VT-4 (Sales Recovery Agent) produces instances
- VT-6 (Owner Surface) displays for approval, flips status
- VT-5 (Outbound MCP) reads approved, sends, flips status
- Billing reconciliation reads sent/failed for ARRR attribution
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

CampaignStatus = Literal["proposed", "approved", "rejected", "sent", "failed"]


class CampaignPlan(BaseModel):
    """v0.1 contract per CL-177.

    Agents only emit status='proposed'. Owner approval (VT-6) flips to
    approved/rejected. Sender (VT-5) flips to sent/failed.
    """

    tenant_id: UUID
    subscriber_id: UUID
    template_id: str = Field(..., description="Internal template name, e.g. 'team_winback_v1'")
    body_params: dict[str, str] = Field(..., description="WhatsApp template variable values")
    status: CampaignStatus = "proposed"
    proposed_at: datetime = Field(..., description="UTC, timezone-aware")
    proposed_by: str = Field(..., description="Agent identifier, e.g. 'sales_recovery_agent'")

    model_config = {"extra": "forbid"}

    @field_validator("proposed_at")
    @classmethod
    def _proposed_at_must_be_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("proposed_at must be timezone-aware (use datetime.now(UTC))")
        return v
