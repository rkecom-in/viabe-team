"""VT-42 — get_recent_campaigns standalone tool.

Deterministic per-tenant recent-campaign rollup. Pydantic IO; standalone
callable. NOT wired to an Agent yet (VT-4 SDK skeleton still Backlog).

Substrate map (post-mig-018 — VT-256 reconciled to the landed schema)
- campaign_id ← campaigns.id
- sent_at ← campaigns.generated_at (mig 018 renamed proposed_at→generated_at)
- template_id ← campaigns.plan_json -> 'message_plan' ->> 'template_id'
  (mig 018 dropped the standalone template_id column; the template lives in
  the CampaignPlan v1.0 plan_json blob now). '' for variants with no
  message_plan (out_of_scope / insufficient_data).
- status ← campaigns.status
- recipients_count ← always 1 per row (campaign row = one subscriber);
  callers may roll up by template_id externally
- response_count ← COUNT(attributions WHERE campaign_id = X)
  per-campaign aggregate

NO PII (CL-390): aggregate counts only — never per-recipient identifiers
(no customer_id, no razorpay_payment_id leak).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.db.wrappers import CampaignsWrapper

logger = logging.getLogger(__name__)


class CampaignRollup(BaseModel):
    """One campaign row aggregated with response count."""

    model_config = ConfigDict(frozen=True)

    campaign_id: str
    sent_at: datetime
    template_id: str
    recipients_count: int = Field(..., ge=0)
    response_count: int = Field(..., ge=0)
    status: str


class GetRecentCampaignsInput(BaseModel):
    """Tenant + days_back + limit."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., min_length=1)
    days_back: int = Field(default=7, ge=1, le=365)
    limit: int = Field(default=20, ge=1, le=200)


class GetRecentCampaignsOutput(BaseModel):
    """List of campaign rollups, newest first."""

    model_config = ConfigDict(frozen=True)

    campaigns: list[CampaignRollup]


def get_recent_campaigns(
    payload: GetRecentCampaignsInput,
    *,
    pool: Any | None = None,
) -> GetRecentCampaignsOutput:
    """Read recent campaigns + per-campaign response counts.

    Empty list returned gracefully when the campaigns table is absent
    (forward-compat — table is in main as of migration 016).
    """
    # VT-306: the campaigns⋈attributions read (tenant-matched join, generated_at
    # window, plan_json template_id COALESCE) is encapsulated by the wrapper.
    # ``pool`` is now vestigial (the wrapper owns its tenant_connection) — kept on
    # the signature for caller stability; a follow-up can drop it.
    _ = pool
    try:
        raw = CampaignsWrapper().list_recent_with_responses(
            payload.tenant_id,
            days_back=payload.days_back,
            limit=payload.limit,
        )
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ != "UndefinedTable":
            raise
        logger.info(
            "get_recent_campaigns: campaigns/attributions absent "
            "(tenant=%s); returning empty",
            payload.tenant_id,
        )
        return GetRecentCampaignsOutput(campaigns=[])

    def _col(r: Any, key: str, idx: int) -> Any:
        return r[key] if isinstance(r, dict) else r[idx]

    rollups = [
        CampaignRollup(
            campaign_id=str(_col(r, "campaign_id", 0)),
            sent_at=_col(r, "sent_at", 1),
            template_id=str(_col(r, "template_id", 2)),
            status=str(_col(r, "status", 3)),
            recipients_count=1,
            response_count=int(_col(r, "response_count", 4) or 0),
        )
        for r in raw
    ]
    logger.info(
        "get_recent_campaigns: tenant=%s campaigns=%d",
        payload.tenant_id, len(rollups),
    )
    return GetRecentCampaignsOutput(campaigns=rollups)


__all__ = [
    "CampaignRollup",
    "GetRecentCampaignsInput",
    "GetRecentCampaignsOutput",
    "get_recent_campaigns",
]
