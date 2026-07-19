"""VT-68/69 — L3 cross-tenant pattern types.

Durable vocabulary for the L3 layer (same posture as kg_vocab / l2_types): the
4 Phase-1 pattern types, the recency-band bucketing, the canonical cohort_key,
and the L3Pattern read model. Adding a pattern type is Type-2 governance.

L3 carries NO PII and NO per-tenant identity (Pillar 7): cohort_key is coarse
(business_type | city_tier | recency_band) and patterns are aggregates over a
k-anon-admitted (≥10) contributing-tenant set.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class PatternType:
    COHORT_RESPONSE_RATE: Final = "cohort_response_rate"
    ATTRIBUTION_RATE_BY_RECENCY: Final = "attribution_rate_by_recency"
    TEMPLATE_EFFECTIVENESS: Final = "template_effectiveness"
    TIME_OF_SEND_EFFECTIVENESS: Final = "time_of_send_effectiveness"


PATTERN_TYPES: Final = (
    PatternType.COHORT_RESPONSE_RATE,
    PatternType.ATTRIBUTION_RATE_BY_RECENCY,
    PatternType.TEMPLATE_EFFECTIVENESS,
    PatternType.TIME_OF_SEND_EFFECTIVENESS,
)

# Coarse recency bands (days since the customer's last inbound at send time).
# Coarse-only — never an exact day count in the cohort_key (re-id risk).
RECENCY_BANDS: Final = ("0_30d", "30_60d", "60_90d", "90d_plus")

# VT-632 cleanup: the numeric cut points are DERIVED from the band labels above (the upper bound of
# each finite band: 30/60/90), never re-typed inline — so RECENCY_BANDS is the ONE source and the
# bucketing can never silently drift from the labels (privacy/k-anon: the band string IS the
# published cohort_key dimension).
_RECENCY_CUTS: Final = tuple(int(b.split("_")[1].rstrip("d")) for b in RECENCY_BANDS[:-1])


def recency_band(days_since_last_inbound: int | None) -> str:
    """Bucket a day-count into a coarse band. ``None`` (never contacted) → the
    most-dormant band. Boundaries come from ``_RECENCY_CUTS`` (derived from ``RECENCY_BANDS``)."""
    if days_since_last_inbound is None:
        return RECENCY_BANDS[-1]
    for band, cut in zip(RECENCY_BANDS, _RECENCY_CUTS):
        if days_since_last_inbound < cut:
            return band
    return RECENCY_BANDS[-1]


def cohort_key(business_type: str, city_tier: str, band: str) -> str:
    """Canonical coarse cohort identifier. Coarse fields ONLY (Pillar 6/7)."""
    return f"{business_type}|{city_tier}|{band}"


def confidence_band(n_campaigns: int) -> str:
    """low / medium / high from sample size (n_campaigns)."""
    if n_campaigns >= 100:
        return "high"
    if n_campaigns >= 30:
        return "medium"
    return "low"


class L3Pattern(BaseModel):
    """One L3 pattern row (read model). Aggregates only — no PII, no tenant id."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    pattern_type: str
    cohort_key: str
    n_tenants: int
    n_campaigns: int
    metrics: dict[str, Any]
    confidence_band: str | None
    constructed_at: datetime
    expires_at: datetime


__all__ = [
    "L3Pattern", "PATTERN_TYPES", "PatternType", "RECENCY_BANDS",
    "cohort_key", "confidence_band", "recency_band",
]
