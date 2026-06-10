"""Frozen dataclasses for the deterministic billing surface (VT-175)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


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
]
