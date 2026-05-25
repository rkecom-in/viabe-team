"""Shared types for the observability surface (VT-102 + VT-103).

``PipelineLogEvent`` is the read-side dataclass returned by every function in
``query.py``. ``TenantCostBreakdown`` / ``WorkspaceCostSummary`` /
``TenantUnitEconomics`` / ``CostAnomaly`` / ``CostRunaway`` are returned by
the cost-dashboard module. Lives in a sibling module so ``log.py`` /
``query.py`` / ``cost_dashboard.py`` can import without forming a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class PipelineLogEvent:
    """One row of ``pipeline_log`` materialised for Python consumers.

    ``tenant_id`` is ``None`` for workspace-level events. ``duration_ms`` is
    ``None`` when the originating call site doesn't measure duration (most
    non-RPC events).
    """

    id: UUID
    run_id: UUID
    tenant_id: UUID | None
    event_type: str
    severity: str
    component: str
    payload: dict[str, Any]
    duration_ms: int | None
    created_at: datetime


@dataclass(frozen=True)
class TenantCostBreakdown:
    """Per-tenant cost aggregate over a time window (VT-103).

    ``by_category`` maps a cost-category string (`llm` / `twilio` /
    `razorpay` / `apify` / `infra_allocated` / `other`) to total paise.
    """

    tenant_id: UUID
    since: datetime
    until: datetime
    total_paise: int
    by_category: dict[str, int] = field(default_factory=dict)
    event_count: int = 0


@dataclass(frozen=True)
class WorkspaceCostSummary:
    """Cross-tenant cost summary returned by ``get_workspace_cost_summary``."""

    since: datetime
    until: datetime
    workspace_total_paise: int
    top_tenants: list[tuple[UUID, int]] = field(default_factory=list)


@dataclass(frozen=True)
class TenantUnitEconomics:
    """ARRR / cost ratio for a tenant in a window (VT-103)."""

    tenant_id: UUID
    arrr_paise: int
    cost_paise: int
    ratio: float


@dataclass(frozen=True)
class CostAnomaly:
    """Tenant flagged by ``detect_cost_anomalies``."""

    tenant_id: UUID
    reference_avg_per_day_paise: int
    window_avg_per_day_paise: int
    multiplier_observed: float


@dataclass(frozen=True)
class CostRunaway:
    """Tenant flagged by ``runaway_alert_candidates``."""

    tenant_id: UUID
    window_cost_paise: int
    plan_monthly_paise: int
    pct_observed: float


__all__ = [
    "CostAnomaly",
    "CostRunaway",
    "PipelineLogEvent",
    "TenantCostBreakdown",
    "TenantUnitEconomics",
    "WorkspaceCostSummary",
]
