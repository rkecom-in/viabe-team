"""VT-43 — get_attribution_data standalone tool.

Deterministic, read-only attribution snapshot. Pydantic IO; standalone
callable (NOT wired to an Agent yet — VT-4 SDK skeleton Backlog). The
day-39 evaluator (billing) consumes this; output MUST be reproducible —
identical inputs → byte-identical `model_dump_json()`.

Pillars: 2 (pure SQL aggregation, no LLM), 4 (data + deterministic notes,
no judgment fields), 7 (honest caveats; degraded fields are None not 0).

Option A graceful-degrade (Cowork review 2026-05-30): the legacy spec's
attribution_method / attribution_confidence breakdown + cohort_size have
NO substrate on main (attributions mig 023 lacks those columns; no
`transactions`/`customers` tables — VT-170 Backlog). Those fields return
None + a `completeness` flag + one deterministic forward-target note.
Supported now from the real schema:
- attribution_status ← campaigns.attribution_closed_at (NULL → pending)
- attribution_close_at ← campaigns.attribution_close_at
- transacting_count ← COUNT(DISTINCT contributor) over attributions
- arrr_paise ← live SUM(attributions.attributed_paise) (preferred over
  the cached campaigns.total_arrr_paise; mismatch emits a note)

Reproducibility: integer paise only (no float), ORDER BY on every
aggregation, notes sorted before return.

NO PII (CL-390): aggregate counts + paise only; no customer_id /
razorpay_payment_id / phone in the output.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestrator.db.wrappers import CampaignsWrapper

logger = logging.getLogger(__name__)

# One stable forward-target note string (deterministic — same bytes every run).
_DEGRADE_NOTE = (
    "breakdown/confidence/cohort substrate absent — gated on VT-170 "
    "(customers) + VT-240 (attribution method/confidence write-path); "
    "attribution_breakdown, attribution_confidence, cohort_size, "
    "attribution_rate omitted"
)


class CampaignAttributionSnapshot(BaseModel):
    """Attribution snapshot for a single campaign."""

    model_config = ConfigDict(frozen=True)

    campaign_id: str
    attribution_status: str  # 'pending' | 'closed'
    attribution_close_at: datetime | None
    transacting_count: int = Field(..., ge=0)
    arrr_paise: int = Field(..., ge=0)
    # Forward-target (Option A): no substrate yet → None, not 0.
    cohort_size: int | None = None
    attribution_rate: float | None = None
    attribution_breakdown: dict[str, Any] | None = None
    attribution_confidence: float | None = None


class CampaignAttributionSummary(BaseModel):
    """One-line per-campaign summary used in window mode."""

    model_config = ConfigDict(frozen=True)

    campaign_id: str
    attribution_status: str
    transacting_count: int = Field(..., ge=0)
    arrr_paise: int = Field(..., ge=0)


class WindowAttributionSnapshot(BaseModel):
    """Aggregate attribution across campaigns closing in a window."""

    model_config = ConfigDict(frozen=True)

    window_start: datetime
    window_end: datetime
    campaign_count: int = Field(..., ge=0)
    total_transacting_count: int = Field(..., ge=0)
    total_arrr_paise: int = Field(..., ge=0)
    # Forward-target.
    total_cohort_size: int | None = None
    aggregate_attribution_rate: float | None = None
    aggregate_attribution_breakdown: dict[str, Any] | None = None
    per_campaign_summary: list[CampaignAttributionSummary] = Field(
        default_factory=list
    )


class GetAttributionDataInput(BaseModel):
    """campaign_id XOR (window_start, window_end). Exactly one mode."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., min_length=1)
    campaign_id: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> GetAttributionDataInput:
        has_campaign = self.campaign_id is not None
        has_window = self.window_start is not None or self.window_end is not None
        if has_campaign and has_window:
            raise ValueError(
                "provide campaign_id OR a window range, not both"
            )
        if not has_campaign and not has_window:
            raise ValueError(
                "provide exactly one of campaign_id or (window_start, "
                "window_end)"
            )
        if has_window and (
            self.window_start is None or self.window_end is None
        ):
            raise ValueError(
                "window mode requires both window_start and window_end"
            )
        if (
            self.window_start is not None
            and self.window_end is not None
            and self.window_end < self.window_start
        ):
            raise ValueError("window_end must be >= window_start")
        return self


class GetAttributionDataOutput(BaseModel):
    """Discriminated by `mode`. One of campaign/window is populated."""

    model_config = ConfigDict(frozen=True)

    mode: str  # 'campaign' | 'window'
    campaign: CampaignAttributionSnapshot | None = None
    window: WindowAttributionSnapshot | None = None
    # True only when every field has real substrate. Always False under
    # Option A until VT-170 + VT-240 land.
    complete: bool = False
    notes: list[str] = Field(default_factory=list)


def _col(r: Any, key: str, idx: int) -> Any:
    return r[key] if isinstance(r, dict) else r[idx]


def _campaign_mode(cur: Any, payload: GetAttributionDataInput) -> GetAttributionDataOutput:
    notes: list[str] = [_DEGRADE_NOTE]

    # VT-306: campaign row via the wrapper on the caller's tenant-scoped cur.
    # The attributions aggregate below stays direct (attributions is NOT a hot
    # table). find_by_id returns the full row; we read the 3 attribution fields.
    # VT-306 (bounce fix): NO conn= — the surrounding cur is pool+set_config
    # (BYPASSRLS, no SET ROLE app_role), so passing it would defeat layer-1 RLS.
    # The wrapper opens its OWN tenant_connection (SET ROLE app_role + GUC).
    crow = CampaignsWrapper().find_by_id(payload.tenant_id, payload.campaign_id)
    if crow is None:
        # No such campaign for this tenant — honest empty (Pillar 7).
        notes.append("campaign not found for tenant")
        snap = CampaignAttributionSnapshot(
            campaign_id=str(payload.campaign_id),
            attribution_status="unknown",
            attribution_close_at=None,
            transacting_count=0,
            arrr_paise=0,
        )
        return GetAttributionDataOutput(
            mode="campaign", campaign=snap, complete=False,
            notes=sorted(notes),
        )

    close_at = _col(crow, "attribution_close_at", 0)
    closed_at = _col(crow, "attribution_closed_at", 1)
    cached_arrr = _col(crow, "total_arrr_paise", 2)
    status = "closed" if closed_at is not None else "pending"
    if status == "pending":
        notes.append(
            "attribution_status=pending; cohort + ARRR still settling, "
            "treat as indicative"
        )

    cur.execute(
        """
        SELECT
            COUNT(DISTINCT COALESCE(customer_id::text, razorpay_payment_id,
                                    id::text)) AS transacting_count,
            COALESCE(SUM(attributed_paise), 0) AS arrr_paise
        FROM attributions
        WHERE campaign_id = %s AND tenant_id = %s
        """,
        (payload.campaign_id, payload.tenant_id),
    )
    arow = cur.fetchone()
    transacting = int(_col(arow, "transacting_count", 0) or 0)
    arrr = int(_col(arow, "arrr_paise", 1) or 0)

    # Prefer live SUM; note any divergence from the cached column.
    if cached_arrr is not None and int(cached_arrr) != arrr:
        notes.append(
            f"cached campaigns.total_arrr_paise={int(cached_arrr)} differs "
            f"from live SUM={arrr}; using live SUM"
        )

    snap = CampaignAttributionSnapshot(
        campaign_id=str(payload.campaign_id),
        attribution_status=status,
        attribution_close_at=close_at,
        transacting_count=transacting,
        arrr_paise=arrr,
    )
    return GetAttributionDataOutput(
        mode="campaign", campaign=snap, complete=False, notes=sorted(notes),
    )


def _window_mode(cur: Any, payload: GetAttributionDataInput) -> GetAttributionDataOutput:
    notes: list[str] = [_DEGRADE_NOTE]

    # VT-306: the campaigns⋈attributions window rollup is encapsulated by the
    # wrapper (tenant-matched join), on the caller's tenant-scoped cur.
    # VT-306 (bounce fix): NO conn= — own tenant_connection (SET ROLE app_role).
    rows = CampaignsWrapper().attribution_window_summary(
        payload.tenant_id, payload.window_start, payload.window_end
    )

    summaries: list[CampaignAttributionSummary] = []
    total_transacting = 0
    total_arrr = 0
    for r in rows:
        cid = str(_col(r, "campaign_id", 0))
        closed_at = _col(r, "attribution_closed_at", 1)
        tcount = int(_col(r, "transacting_count", 2) or 0)
        arrr = int(_col(r, "arrr_paise", 3) or 0)
        total_transacting += tcount
        total_arrr += arrr
        summaries.append(
            CampaignAttributionSummary(
                campaign_id=cid,
                attribution_status="closed" if closed_at is not None else "pending",
                transacting_count=tcount,
                arrr_paise=arrr,
            )
        )

    assert payload.window_start is not None
    assert payload.window_end is not None
    snap = WindowAttributionSnapshot(
        window_start=payload.window_start,
        window_end=payload.window_end,
        campaign_count=len(summaries),
        total_transacting_count=total_transacting,
        total_arrr_paise=total_arrr,
        per_campaign_summary=summaries,  # already ORDER BY c.id ASC
    )
    return GetAttributionDataOutput(
        mode="window", window=snap, complete=False, notes=sorted(notes),
    )


def get_attribution_data(
    payload: GetAttributionDataInput,
    *,
    pool: Any | None = None,
) -> GetAttributionDataOutput:
    """Read attribution snapshot for a campaign or a close-window.

    Reproducible: identical inputs → byte-identical model_dump_json().
    RLS via SET LOCAL app.current_tenant. psycopg-free at import; an
    absent attributions/campaigns schema surfaces as graceful empty.
    """
    if pool is None:
        from orchestrator.graph import get_pool

        pool = get_pool()

    # VT-306 (bounce-2): the OUTER connection is a tenant_connection (SET ROLE
    # app_role + GUC) — NOT pool.connection()+set_config — so the direct
    # `attributions` read in _campaign_mode is RLS-enforced too (attributions has
    # FORCE RLS, mig 023, inert under the BYPASSRLS pool role). ``pool`` is now
    # vestigial. The campaigns reads still go through the wrapper (own conn).
    _ = pool
    from orchestrator.db import tenant_connection

    with tenant_connection(payload.tenant_id) as cur:
            try:
                if payload.campaign_id is not None:
                    out = _campaign_mode(cur, payload)
                else:
                    out = _window_mode(cur, payload)
            except Exception as exc:  # noqa: BLE001
                if type(exc).__name__ != "UndefinedTable":
                    raise
                # Forward-compat: attributions/campaigns absent → honest empty.
                logger.info(
                    "get_attribution_data: schema absent (tenant=%s); empty",
                    payload.tenant_id,
                )
                if payload.campaign_id is not None:
                    return GetAttributionDataOutput(
                        mode="campaign",
                        campaign=CampaignAttributionSnapshot(
                            campaign_id=str(payload.campaign_id),
                            attribution_status="unknown",
                            attribution_close_at=None,
                            transacting_count=0,
                            arrr_paise=0,
                        ),
                        complete=False,
                        notes=sorted([_DEGRADE_NOTE, "attributions schema absent"]),
                    )
                assert payload.window_start is not None
                assert payload.window_end is not None
                return GetAttributionDataOutput(
                    mode="window",
                    window=WindowAttributionSnapshot(
                        window_start=payload.window_start,
                        window_end=payload.window_end,
                        campaign_count=0,
                        total_transacting_count=0,
                        total_arrr_paise=0,
                    ),
                    complete=False,
                    notes=sorted([_DEGRADE_NOTE, "attributions schema absent"]),
                )

    logger.info(
        "get_attribution_data: tenant=%s mode=%s",
        payload.tenant_id, out.mode,
    )
    return out


__all__ = [
    "CampaignAttributionSnapshot",
    "CampaignAttributionSummary",
    "WindowAttributionSnapshot",
    "GetAttributionDataInput",
    "GetAttributionDataOutput",
    "get_attribution_data",
]
