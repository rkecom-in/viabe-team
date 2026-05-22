"""Context Composer — SalesRecoveryContext bundle constructor (VT-3.4 PR 2/3 / VT-34).

Pillar 1: this lives in the orchestrator, not the agent. Agent code MUST NOT
import this module (lint-enforced — see ci.yml's agent-import gate).

Pillar 3: every raw DB read is tenant-scoped, asserted via
``_tenant_guard.assert_tenant_scoped`` (belt-and-braces over RLS).

CL-190 — substrate absence: L1 KG (VT-7.1), L2 episodic memory, the campaigns
table, and the owner_inputs table do not exist yet. Every ``_build_*`` function
therefore returns a safe-empty fallback + ``False`` completeness flag. The
bundle CONTRACT ships now; real data fills in when the substrates land — with
no change to this module's public surface.

CL-183 VERIFICATION TARGET (deferred): bundle plumbing (the constructor's
per-section dispatch, completeness integration, budget enforcement) is tested
via monkeypatched ``_build_*`` functions. The real read paths only get
exercised once the substrates exist — that integration test is deferred to the
VT-7.1-bundling PR. Do not claim cross-tenant isolation is tested end-to-end;
what is tested is the dispatcher + the ``assert_tenant_scoped`` guard.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

import yaml

from orchestrator._tenant_guard import assert_tenant_scoped, emit_pipeline_step
from orchestrator.db import tenant_connection
from orchestrator.types.trigger_reason import TriggerReason

_BUDGETS_PATH = Path(__file__).parent.parent.parent / "config" / "context_budgets.yaml"

# No tokenizer exists in the orchestrator (verified §2.1g). Per §3.3, use a
# char-count approximation (~4 chars/token) with a 20% safety margin: the
# effective cap is 80% of total_cap. Replace with a real tokenizer when one
# lands (open Tech Debt).
_CHARS_PER_TOKEN = 4
_SAFETY_MARGIN = 0.8


class ContextOverflowError(RuntimeError):
    """Raised when a bundle exceeds the token cap even after maximum truncation."""


# --- bundle dataclasses (VT-34 / CL-177-adjacent contract) -------------------


@dataclass(frozen=True, slots=True)
class BusinessProfile:
    business_name: str = ""
    business_type: str = ""
    locality: str = ""
    hours: dict[str, Any] = field(default_factory=dict)
    owner_name: str = ""
    current_phase: str = ""
    founding_tier_flag: bool = False


@dataclass(frozen=True, slots=True)
class LedgerSummary:
    total_customers: int = 0
    dormant_cohorts: dict[str, int] = field(default_factory=dict)
    top_spenders: list[UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CampaignSnapshot:
    campaign_id: UUID
    status: Literal[
        "proposed", "approved", "rejected", "sent", "failed", "pending_attribution"
    ]
    recovered_paise: int
    proposed_at: datetime


@dataclass(frozen=True, slots=True)
class AttributionSnapshot:
    cumulative_recovered_paise: int = 0
    last_7d_recovered_paise: int = 0
    last_30d_recovered_paise: int = 0
    attribution_rate_trend: list[float] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class OwnerInput:
    input_id: UUID
    received_at: datetime
    content: str


@dataclass(frozen=True, slots=True)
class ContextMeta:
    # token_count is the sum of the five content sections only — it excludes
    # the meta + slack reservations in context_budgets.yaml. A downstream
    # reader comparing token_count to 8000 is comparing a subset to the total.
    token_count: int
    build_timestamp: datetime
    cursor_info: dict[str, Any]


_DEFAULT_SECTION_KEYS = (
    "business_profile",
    "customer_ledger_summary",
    "recent_campaigns",
    "attribution_snapshot",
    "pending_owner_inputs",
)


def _default_data_completeness() -> dict[str, bool]:
    return {key: False for key in _DEFAULT_SECTION_KEYS}


def _default_meta() -> ContextMeta:
    return ContextMeta(token_count=0, build_timestamp=datetime.now(UTC), cursor_info={})


@dataclass(frozen=True, slots=True)
class SalesRecoveryContext:
    # Identity + task — required, no default. ``user_request`` is the
    # orchestrator-supplied owner message that triggered the dispatch
    # (Exec-6.85: bundle now carries it so the specialist receives the
    # full task context instead of a minimal wedge).
    tenant_id: UUID
    run_id: UUID
    user_request: str
    # Bundle sections + provenance — all default to CL-190 safe-empty so
    # tests + lightweight call sites can construct a minimal bundle without
    # filling every substrate. ``build_sales_recovery_context`` overrides
    # these with the real per-section builders.
    trigger_reason: TriggerReason = "weekly_cadence"
    business_profile: BusinessProfile = field(default_factory=BusinessProfile)
    customer_ledger_summary: LedgerSummary = field(default_factory=LedgerSummary)
    recent_campaigns: list[CampaignSnapshot] = field(default_factory=list)
    attribution_snapshot: AttributionSnapshot = field(
        default_factory=AttributionSnapshot
    )
    pending_owner_inputs: list[OwnerInput] = field(default_factory=list)
    meta: ContextMeta = field(default_factory=_default_meta)
    # CL-190: True = real data; False = safe-empty fallback (substrate absent
    # or no rows for tenant). Keys are the five section names below.
    data_completeness: dict[str, bool] = field(
        default_factory=_default_data_completeness
    )


# --- token estimation --------------------------------------------------------


@lru_cache(maxsize=1)
def _load_budgets() -> dict[str, Any]:
    with _BUDGETS_PATH.open() as f:
        return dict(yaml.safe_load(f))


def _estimate_tokens(obj: Any) -> int:
    """Char-approximation token estimate (~4 chars/token). Deterministic."""
    if obj is None:
        return 0
    if isinstance(obj, list):
        return sum(_estimate_tokens(item) for item in obj)
    payload = asdict(obj) if is_dataclass(obj) and not isinstance(obj, type) else obj
    return len(json.dumps(payload, default=str)) // _CHARS_PER_TOKEN


# --- per-section builders (CL-190 safe-empty; substrates absent) -------------
#
# CL-183 VERIFICATION TARGET (deferred): each _build_* will read its real
# substrate (L1 KG / L2 episodic / campaigns / pipeline_steps / owner_inputs)
# once those exist. None exist today, so each returns a safe-empty fallback +
# False. When the substrate lands, the read goes here, wrapped in a *concrete*
# exception catch (never broad `except Exception` — CL-191) and guarded with
# assert_tenant_scoped on every raw row set.


def _build_business_profile(tenant_id: UUID) -> tuple[BusinessProfile, bool]:
    """L1 KG read — deferred to VT-7.1. Safe-empty until then."""
    return BusinessProfile(), False


def _build_ledger_summary(tenant_id: UUID) -> tuple[LedgerSummary, bool]:
    """L2 episodic read — deferred to VT-7.1. Safe-empty until then."""
    return LedgerSummary(), False


def _build_recent_campaigns(tenant_id: UUID) -> tuple[list[CampaignSnapshot], bool]:
    """campaigns-table read (VT-138).

    Reads the live ``campaigns`` table via ``tenant_connection`` (RLS +
    GUC scoped); returns the most-recent ``LIMIT 5`` rows mapped to
    ``CampaignSnapshot``. ``id``, ``status`` and ``generated_at`` map
    directly; ``recovered_paise`` is set to ``0`` because the per-
    campaign attribution substrate does not exist yet (CL blocker
    367387c2-cc5a-81a7-aa37-e6e23c222357 — Option 2, completeness-
    flag-honest).

    The completeness flag is ``False`` whenever this builder runs —
    even when real rows return — because ``recovered_paise`` is a
    placeholder. The flag will flip to ``True`` only when a future
    ``campaign_attribution`` substrate populates the real recovered-
    paise figure; that substrate is its own VT row and is OUT of scope
    here.

    Belt-and-braces over RLS (CL-71 / CL-190): the raw rows are passed
    through ``assert_tenant_scoped`` before mapping — RLS should make a
    cross-tenant row impossible, but the assertion logs + raises if it
    ever happens.
    """
    with tenant_connection(tenant_id) as conn:
        raw_rows = conn.execute(
            "SELECT id, tenant_id, status, generated_at FROM campaigns "
            "ORDER BY generated_at DESC LIMIT 5"
        ).fetchall()
    rows = cast("list[dict[str, Any]]", raw_rows)
    assert_tenant_scoped(rows, tenant_id)
    snapshots = [
        CampaignSnapshot(
            campaign_id=row["id"],
            status=row["status"],
            recovered_paise=0,
            proposed_at=row["generated_at"],
        )
        for row in rows
    ]
    return snapshots, False


def _build_attribution_snapshot(tenant_id: UUID) -> tuple[AttributionSnapshot, bool]:
    """pipeline_steps + campaigns read — deferred (campaigns table absent)."""
    return AttributionSnapshot(), False


def _build_pending_owner_inputs(tenant_id: UUID) -> tuple[list[OwnerInput], bool]:
    """owner_inputs-table read — deferred (table not yet created)."""
    return [], False


# --- the bundle constructor --------------------------------------------------


def build_sales_recovery_context(
    tenant_id: UUID,
    run_id: UUID,
    trigger_reason: TriggerReason,
    user_request: str,
) -> SalesRecoveryContext:
    """Sole constructor for SalesRecoveryContext bundles.

    Builds each section via its ``_build_*`` function, records the per-section
    completeness flags, enforces the 8K-token cap (per-section truncation in a
    fixed order), and assembles the bundle. Raises ``ContextOverflowError`` if
    the bundle still exceeds the cap after maximum truncation.

    ``user_request`` (Exec-6.85): the orchestrator-supplied owner message
    that triggered the dispatch. Required, must be non-empty — the
    specialist cannot be spawned without one.
    """
    if not isinstance(user_request, str) or not user_request.strip():
        raise ValueError(
            "build_sales_recovery_context: user_request must be a non-empty"
            " string (orchestrator must supply the owner message before"
            " dispatch)"
        )
    budgets = _load_budgets()
    effective_cap = int(int(budgets["total_cap"]) * _SAFETY_MARGIN)

    business_profile, bp_ok = _build_business_profile(tenant_id)
    ledger_summary, ls_ok = _build_ledger_summary(tenant_id)
    recent_campaigns, rc_ok = _build_recent_campaigns(tenant_id)
    attribution_snapshot, as_ok = _build_attribution_snapshot(tenant_id)
    pending_owner_inputs, oi_ok = _build_pending_owner_inputs(tenant_id)

    data_completeness = {
        "business_profile": bp_ok,
        "customer_ledger_summary": ls_ok,
        "recent_campaigns": rc_ok,
        "attribution_snapshot": as_ok,
        "pending_owner_inputs": oi_ok,
    }

    def _total() -> int:
        return (
            _estimate_tokens(business_profile)
            + _estimate_tokens(ledger_summary)
            + _estimate_tokens(recent_campaigns)
            + _estimate_tokens(attribution_snapshot)
            + _estimate_tokens(pending_owner_inputs)
        )

    # Truncation order (§3.3): oldest owner inputs -> campaigns down to 3 ->
    # top_spenders 20/10/5 -> drop business hours -> overflow.
    truncated: list[str] = []
    while _total() > effective_cap:
        if pending_owner_inputs:
            pending_owner_inputs = pending_owner_inputs[1:]
            truncated.append("pending_owner_inputs")
            continue
        if len(recent_campaigns) > 3:
            recent_campaigns = recent_campaigns[: len(recent_campaigns) - 1]
            truncated.append("recent_campaigns")
            continue
        if len(ledger_summary.top_spenders) > 5:
            n = len(ledger_summary.top_spenders)
            keep = 20 if n > 20 else 10 if n > 10 else 5
            ledger_summary = replace(
                ledger_summary, top_spenders=ledger_summary.top_spenders[:keep]
            )
            truncated.append("customer_ledger_summary")
            continue
        if business_profile.hours:
            business_profile = replace(business_profile, hours={})
            truncated.append("business_profile")
            continue
        raise ContextOverflowError(
            f"bundle for tenant {tenant_id} exceeds {effective_cap} tokens "
            "after maximum truncation"
        )

    for section in dict.fromkeys(truncated):
        emit_pipeline_step(
            step_kind="context_truncation",
            severity="info",
            payload={"section": section},
        )

    meta = ContextMeta(
        token_count=_total(),
        build_timestamp=datetime.now(UTC),
        cursor_info={},
    )
    return SalesRecoveryContext(
        tenant_id=tenant_id,
        run_id=run_id,
        user_request=user_request,
        trigger_reason=trigger_reason,
        business_profile=business_profile,
        customer_ledger_summary=ledger_summary,
        recent_campaigns=recent_campaigns,
        attribution_snapshot=attribution_snapshot,
        pending_owner_inputs=pending_owner_inputs,
        meta=meta,
        data_completeness=data_completeness,
    )
