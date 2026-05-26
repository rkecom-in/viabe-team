"""Frozen dataclasses for the deterministic billing surface (VT-175)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID


Day39VerdictKind = Literal["continue", "refund_triggered", "not_eligible"]


@dataclass(frozen=True)
class Day39Verdict:
    """Day-39 evaluation result for one tenant.

    ``verdict`` semantics:
    - ``continue`` — ARRR >= 2× cumulative_fees; subscription continues.
    - ``refund_triggered`` — ARRR < 2× cumulative_fees; refund flow fires.
    - ``not_eligible`` — tenant's ``paid_conversion_at + 39 days`` is in
      the future, or ``paid_conversion_at`` is NULL. No evaluation
      performed; no pipeline_log event emitted.

    ``already_decided`` flips True when the canary / re-run path replays
    a previously-emitted verdict instead of re-evaluating + re-emitting.
    Idempotency guard.
    """

    tenant_id: UUID
    verdict: Day39VerdictKind
    arrr_paise: int
    cumulative_fees_paise: int
    decided_at: datetime
    already_decided: bool = False


@dataclass(frozen=True)
class AttributionCloseResult:
    """Attribution close result for one campaign.

    ``already_closed`` flips True when the campaign was already closed
    on a prior invocation; the function short-circuits and returns the
    previously-written aggregate without re-updating or re-emitting.
    """

    campaign_id: UUID
    total_arrr_paise: int
    closed_at: datetime
    already_closed: bool = False
    attribution_row_count: int = 0


__all__ = [
    "AttributionCloseResult",
    "Day39Verdict",
    "Day39VerdictKind",
]
