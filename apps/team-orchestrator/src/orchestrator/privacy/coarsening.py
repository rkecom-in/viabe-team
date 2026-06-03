"""VT-75 — locality coarsening (city → city_tier).

Deterministic coarsening so only a COARSE tier ever crosses the tenant boundary
(Pillar 6/7; the k-anon predicate allowlist forbids raw city/locality, CL-390).
``coarsen_locality`` DROPS the locality entirely — only the tier is returned.
``set_tenant_city_tier`` coarsens an incoming city + stores ONLY the tier on
``tenants.city_tier`` (the raw city is discarded, never persisted) — the column
VT-74's k-anon gate + VT-68 L3 construction read.

tier_1 = 8 metros (Type-3 locked). tier_2 = census-top ~50 (Type-1, Fazal
sign-off). Unknown → tier_3 (most conservative) + a `city_unknown` ops log.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

import yaml

logger = logging.getLogger(__name__)

CityTier = Literal["tier_1", "tier_2", "tier_3"]

_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config" / "city_tiers.yaml"


@lru_cache(maxsize=1)
def _cfg() -> dict:
    with _CONFIG_PATH.open() as f:
        return yaml.safe_load(f) or {}


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def _canonical(city: str) -> str:
    c = _norm(city)
    return _cfg().get("variants", {}).get(c, c)


def coarsen_city(city: str, state: str | None = None) -> CityTier:
    """City string → coarse tier. Normalizes + resolves variants first. Same-name
    cities outside India resolve to tier_3 unless an in-region state is given.
    Unknown city → tier_3 + a `city_unknown` ops log (raw city is the input being
    triaged, not tenant-linked)."""
    c = _canonical(city)
    if not c:
        return "tier_3"

    disamb = _cfg().get("disambiguation", {}).get(c)
    if disamb and state is not None:
        allowed = {_norm(s) for s in disamb.get("allowed_states", [])}
        if _norm(state) not in allowed:
            return "tier_3"  # known city, out-of-region state → conservative bucket

    if c in {_norm(x) for x in _cfg().get("tier_1", [])}:
        return "tier_1"
    if c in {_norm(x) for x in _cfg().get("tier_2", [])}:
        return "tier_2"
    logger.info("city_unknown: %r → tier_3 (consider adding to tier_2)", city)
    return "tier_3"


def coarsen_locality(locality: str | None, city: str, state: str | None = None) -> CityTier:
    """Coarsen by CITY only — the ``locality`` is DROPPED entirely (never read,
    never returned, never stored). Returns the city's tier."""
    _ = locality  # explicitly discarded — locality must not influence the result
    return coarsen_city(city, state)


def set_tenant_city_tier(tenant_id: UUID | str, city: str, state: str | None = None) -> CityTier:
    """Coarsen ``city`` and store ONLY the resulting tier on tenants.city_tier.
    The raw city is discarded (never persisted) — this is the privacy-preserving
    write the k-anon gate + L3 construction depend on. Returns the stored tier."""
    from orchestrator.db import tenant_connection

    tier = coarsen_city(city, state)
    with tenant_connection(tenant_id) as conn:
        conn.execute(
            "UPDATE tenants SET city_tier = %s WHERE id = %s",
            (tier, str(tenant_id)),
        )
    return tier


__all__ = ["CityTier", "coarsen_city", "coarsen_locality", "set_tenant_city_tier"]
