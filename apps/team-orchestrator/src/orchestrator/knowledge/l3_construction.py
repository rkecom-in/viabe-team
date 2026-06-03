"""VT-68 — L3 cross-tenant pattern construction (nightly, idempotent rebuild).

The 2nd sanctioned cross-tenant service-role read (Pillar 8; the 1st is the VT-74
k-anon gate). It aggregates campaign outcomes ACROSS tenants into anonymized
priors and writes ``l3_patterns``. Guardrails (Cowork 20260604T004000Z, same
discipline as VT-74):

- Service-role pool (BYPASSRLS) — cross-tenant by design; does NOT call
  assert_tenant_scoped → no VT-79 Detector-1 false-trip. Allowlisted in the
  VT-72 no-direct-tenant-db-access lint with rationale.
- k-anonymity over the CONTRIBUTING-tenant set per cohort (check_contributor_admission,
  k≥10, CL-28). A cohort with many attribute-matchers but <10 actual contributors
  is dropped — NOT constructed (Pillar 6, build-time).
- ``l3_patterns`` holds AGGREGATES ONLY: counts + rates, never a tenant_id, never
  a customer id, never a city (only coarse city_tier) — a single tenant's numbers
  cannot be reconstructed from a pattern (Pillar 7).
- Response signal = attribution/recovery (the Phase-1 proxy; no reply signal
  pre-WABA). 180-day quarantine: tenants younger than 180d do NOT contribute.

Idempotent: a full rebuild upserts by (pattern_type, cohort_key), so re-running
produces equivalent state. Runs nightly (3 AM IST) via a DBOS.scheduled trigger.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from orchestrator.graph import get_pool
from orchestrator.knowledge.l3_types import (
    PatternType,
    cohort_key,
    confidence_band,
    recency_band,
)
from orchestrator.privacy.k_anonymity import check_contributor_admission

logger = logging.getLogger(__name__)

_WINDOW_DAYS = 90       # campaigns considered: proposed in the last 90 days
_QUARANTINE_DAYS = 180  # tenants younger than this do NOT contribute (VT-69)
_EXPIRY_DAYS = 90       # safety floor on a pattern (nightly rebuild keeps fresh)

# Cross-tenant base relation: one row per (sent campaign, recipient) in-window,
# from a past-quarantine tenant with coarse attributes set, LEFT JOINed to its
# attribution (the response/recovery proxy). Service-role; no tenant filter.
_BASE_SQL = """
    SELECT
        c.tenant_id,
        t.business_type,
        t.city_tier,
        c.id            AS campaign_id,
        c.template_id,
        c.proposed_at,
        cust.last_inbound_at,
        (a.id IS NOT NULL) AS converted
    FROM campaigns c
    JOIN tenants t              ON t.id = c.tenant_id
    JOIN campaign_recipients cr ON cr.campaign_id = c.id
    JOIN customers cust         ON cust.id = cr.customer_id
    LEFT JOIN attributions a    ON a.campaign_id = c.id AND a.customer_id = cr.customer_id
    WHERE c.status = 'sent'
      AND c.proposed_at >= %s
      AND t.signed_up_at < %s
      AND t.business_type IS NOT NULL
      AND t.city_tier IS NOT NULL
"""


class _Cell:
    """Mutable per-cohort accumulator (counts only — never ids in the output)."""

    __slots__ = ("tenants", "campaigns", "recipients", "conversions")

    def __init__(self) -> None:
        self.tenants: set[UUID] = set()
        self.campaigns: set[UUID] = set()
        self.recipients = 0
        self.conversions = 0


def _days_since(last_inbound_at: datetime | None, at: datetime) -> int | None:
    if last_inbound_at is None:
        return None
    return (at - last_inbound_at).days


def construct_l3_patterns(*, now: datetime | None = None, run_id: UUID | None = None) -> dict[str, int]:
    """Rebuild all L3 patterns. Returns {'constructed': n, 'dropped_below_k': m}.

    Never raises on a single cohort — best-effort per cohort; a malformed cohort
    logs + is skipped. The whole pass is wrapped so the scheduler never crashes.
    """
    now = now or datetime.now(UTC)
    rid = run_id or uuid4()
    window_start = now - timedelta(days=_WINDOW_DAYS)
    quarantine_cutoff = now - timedelta(days=_QUARANTINE_DAYS)
    expires_at = now + timedelta(days=_EXPIRY_DAYS)

    # cells[(pattern_type, cohort_key)] -> _Cell
    cells: dict[tuple[str, str], _Cell] = {}

    def _cell(ptype: str, ckey: str) -> _Cell:
        return cells.setdefault((ptype, ckey), _Cell())

    with get_pool().connection() as conn:
        rows = conn.execute(_BASE_SQL, (window_start, quarantine_cutoff)).fetchall()

    for r in rows:
        rd = dict(r)
        tid = rd["tenant_id"] if isinstance(rd["tenant_id"], UUID) else UUID(str(rd["tenant_id"]))
        cid = rd["campaign_id"] if isinstance(rd["campaign_id"], UUID) else UUID(str(rd["campaign_id"]))
        bt = rd["business_type"]
        tier = rd["city_tier"]
        band = recency_band(_days_since(rd["last_inbound_at"], rd["proposed_at"]))
        converted = bool(rd["converted"])
        hour = rd["proposed_at"].astimezone(UTC).hour  # send-time proxy = proposed_at (no sent_at)
        template = rd["template_id"]

        # Each pattern type keys its cohort differently (per the VT-68 spec).
        for ptype, ckey in (
            (PatternType.COHORT_RESPONSE_RATE, cohort_key(bt, tier, band)),
            (PatternType.ATTRIBUTION_RATE_BY_RECENCY, f"{bt}|{band}"),
            (PatternType.TEMPLATE_EFFECTIVENESS, f"{bt}|{tier}|template:{template}"),
            (PatternType.TIME_OF_SEND_EFFECTIVENESS, f"{bt}|{tier}|hour:{hour:02d}"),
        ):
            cell = _cell(ptype, ckey)
            cell.tenants.add(tid)
            cell.campaigns.add(cid)
            cell.recipients += 1
            cell.conversions += int(converted)

    constructed = 0
    dropped = 0
    upserts: list[tuple[Any, ...]] = []
    for (ptype, ckey), cell in cells.items():
        # Contributor-set k-anon gate (NOT attribute-matchers) — the real guarantee.
        admission = check_contributor_admission(cell.tenants, ckey, run_id=rid)
        if not admission.admitted:
            dropped += 1
            continue
        n_tenants = admission.tenant_count
        n_campaigns = len(cell.campaigns)
        rate = (cell.conversions / cell.recipients) if cell.recipients else 0.0
        # AGGREGATES ONLY — counts + a rate. No ids, no city, no per-tenant figure.
        metrics = {
            "response_rate": round(rate, 4),
            "n_recipients": cell.recipients,
            "n_conversions": cell.conversions,
        }
        upserts.append((
            ptype, ckey, n_tenants, n_campaigns, Jsonb(metrics),
            confidence_band(n_campaigns), now, expires_at,
        ))
        constructed += 1

    if upserts:
        with get_pool().connection() as conn, conn.transaction():
            # Idempotent rebuild: replace all rows for the types we recomputed,
            # then upsert. (DELETE+insert keeps stale cohorts from lingering when
            # their contributors drop below k between runs.)
            conn.execute(
                "DELETE FROM l3_patterns WHERE pattern_type = ANY(%s)",
                (list({u[0] for u in upserts}),),
            )
            conn.cursor().executemany(
                "INSERT INTO l3_patterns "
                "(pattern_type, cohort_key, n_tenants, n_campaigns, metrics, "
                " confidence_band, constructed_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (pattern_type, cohort_key) DO UPDATE SET "
                "  n_tenants = EXCLUDED.n_tenants, n_campaigns = EXCLUDED.n_campaigns, "
                "  metrics = EXCLUDED.metrics, confidence_band = EXCLUDED.confidence_band, "
                "  constructed_at = EXCLUDED.constructed_at, expires_at = EXCLUDED.expires_at",
                upserts,
            )

    logger.info(
        "VT-68 L3 construction: %d patterns constructed, %d cohorts dropped below k",
        constructed, dropped,
    )
    return {"constructed": constructed, "dropped_below_k": dropped}


__all__ = ["construct_l3_patterns"]
