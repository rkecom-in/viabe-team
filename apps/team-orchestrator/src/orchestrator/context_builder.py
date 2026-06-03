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

WIRING STATUS (2026-06-03, Sprint 7 build-map) — this module is LIVE, not an
orphan. ``build_sales_recovery_context`` is called on the live path at SPECIALIST
HANDOFF: ``runner.webhook_pipeline_run`` -> ``dispatch_brain`` -> orchestrator-agent
``spawn_sales_recovery`` tool -> ``handoffs._build_sales_recovery_update`` (line ~133).
It is DISTINCT from ``orchestrator.knowledge.l1.assemble_context_bundle``, which is
the SEPARATE orchestrator-prompt L1-enrichment seam injected unconditionally at
``dispatch.py`` (~line 186). Two seams, two consumers — do NOT merge them. Of the
``_build_*`` sections, recent_campaigns / pending_owner_inputs / recovery_target_config
read live substrate today (mig 016/018 campaigns, mig 020 owner_inputs); ledger_summary (L2)
reads the live ``episodic_events`` substrate (mig 083, VT-67) — empty-but-live until VT-309
wires the threshold emit sites; business_profile (L1) and attribution_snapshot remain CL-190
safe-empty pending their substrates (Sprint-7 build waves). The CL-190 note above is partially
superseded: the campaigns + owner_inputs + episodic_events tables now EXIST and are read.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

import yaml
from psycopg.types.json import Jsonb

from orchestrator._tenant_guard import assert_tenant_scoped, emit_pipeline_step
from orchestrator.db import tenant_connection
from orchestrator.knowledge import l2_query
from orchestrator.knowledge.l2_types import L2EventType
from orchestrator.templates_registry import approved_template_names
from orchestrator.types.trigger_reason import TriggerReason

logger = logging.getLogger(__name__)

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


_L3_NO_PRIOR_NOTE = (
    "no L3 prior available — reason from this tenant's own data without "
    "cross-tenant priors"
)


@dataclass(frozen=True, slots=True)
class L3Priors:
    """VT-69 — cross-tenant L3 priors for the tenant's cohort, or the structured
    no-prior marker. ``patterns`` are aggregates only (no PII, no tenant id).
    ``available=False`` + ``note`` is the marker for quarantine / no-match — NEVER
    fabricated defaults (Pillar 4)."""

    available: bool = False
    patterns: list[dict[str, Any]] = field(default_factory=list)
    note: str = _L3_NO_PRIOR_NOTE


_L4_NO_SKILLS_NOTE = (
    "no L4 domain-knowledge documents available — reason from first principles "
    "and this tenant's own data"
)


@dataclass(frozen=True, slots=True)
class L4Skills:
    """VT-70 — lightweight L4 corpus pointers for the tenant's query (title +
    excerpt + score). The agent pulls FULL doc bodies on demand via the
    ``retrieve_l4_skills`` MCP tool; the bundle stays small. ``available=False`` +
    ``note`` is the marker when the corpus is empty / retrieval unavailable —
    NEVER fabricated knowledge (Pillar 4)."""

    available: bool = False
    skills: list[dict[str, Any]] = field(default_factory=list)
    note: str = _L4_NO_SKILLS_NOTE


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
    """One owner-supplied input — derived classification only (VT-146).

    Carries the structured ``intent / segment / occasion`` produced by
    the Component-2 extraction writer; the raw message text is NOT a
    field on this dataclass and is not stored in ``owner_inputs``. Brief
    locks the derived-only shape so retention does not regress
    VT-144's body-redaction fix.
    """

    input_id: UUID
    received_at: datetime
    intent: str
    segment: str | None = None
    occasion: str | None = None


@dataclass(frozen=True, slots=True)
class ContextMeta:
    # token_count is the sum of the five content sections only — it excludes
    # the meta + slack reservations in context_budgets.yaml. A downstream
    # reader comparing token_count to 8000 is comparing a subset to the total.
    token_count: int
    build_timestamp: datetime
    cursor_info: dict[str, Any]


_DEFAULT_TARGET_RECOVERED_PAISE: int = 50_000  # default per-tenant recovery-target floor (paise)
_DEFAULT_RECOVERY_TARGET_MULTIPLIER: float = 1.1  # default per-tenant recovery-target multiplier

_DEFAULT_SECTION_KEYS = (
    "business_profile",
    "customer_ledger_summary",
    "recent_campaigns",
    "attribution_snapshot",
    "pending_owner_inputs",
    "l3_priors",
    "l4_skills",
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
    l3_priors: L3Priors = field(default_factory=L3Priors)
    l4_skills: L4Skills = field(default_factory=L4Skills)
    meta: ContextMeta = field(default_factory=_default_meta)
    # CL-190: True = real data; False = safe-empty fallback (substrate absent
    # or no rows for tenant). Keys are the five section names below.
    data_completeness: dict[str, bool] = field(
        default_factory=_default_data_completeness
    )
    # VT-164: per-tenant recovery-target config — read from tenants table in
    # build_sales_recovery_context; defaults = module constants so a missing
    # DB read never silently changes the computed target.
    recovery_target_multiplier: float = _DEFAULT_RECOVERY_TARGET_MULTIPLIER
    recovery_target_floor_paise: int = _DEFAULT_TARGET_RECOVERED_PAISE


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


# Cap on L2 threshold events read per ledger build (clamped again by l2_query).
_LEDGER_EVENT_CAP = 100


def _build_ledger_summary(tenant_id: UUID) -> tuple[LedgerSummary, bool]:
    """L2 episodic read (VT-67) — derives the customer ledger summary LIVE from
    L2 threshold events (``customer_high_value_threshold_crossed`` /
    ``customer_dormant_threshold_crossed``), the agent's "what happened" log.

    No longer CL-190 safe-empty: this reads the real ``episodic_events``
    substrate via ``l2_query`` (tenant-scoped + assert_tenant_scoped inside).
    Completeness flag is ``True`` whenever the read runs — the data is real L2
    (no placeholder field, unlike recent_campaigns' recovered_paise). It is
    legitimately empty until VT-309 wires the threshold emit sites on the live
    path; an empty-but-live ledger is the honest current state.

    - ``top_spenders``: distinct customers from recent high-value events (newest first).
    - ``dormant_cohorts``: cohort -> count from recent dormancy events.
    - ``total_customers``: distinct customers L2 has seen cross either threshold.
    """
    high_value = l2_query.recent_events(
        tenant_id,
        limit=_LEDGER_EVENT_CAP,
        event_types=[L2EventType.CUSTOMER_HIGH_VALUE_THRESHOLD_CROSSED],
    )
    dormant = l2_query.recent_events(
        tenant_id,
        limit=_LEDGER_EVENT_CAP,
        event_types=[L2EventType.CUSTOMER_DORMANT_THRESHOLD_CROSSED],
    )

    top_spenders: list[UUID] = []
    seen: set[UUID] = set()
    for ev in high_value:
        cid = ev.referenced_entity_id
        if cid is not None and cid not in seen:
            seen.add(cid)
            top_spenders.append(cid)

    dormant_cohorts: dict[str, int] = {}
    dormant_customers: set[UUID] = set()
    for ev in dormant:
        cohort = str(ev.payload.get("cohort") or "unknown")
        dormant_cohorts[cohort] = dormant_cohorts.get(cohort, 0) + 1
        if ev.referenced_entity_id is not None:
            dormant_customers.add(ev.referenced_entity_id)

    total_customers = len(seen | dormant_customers)
    summary = LedgerSummary(
        total_customers=total_customers,
        dormant_cohorts=dormant_cohorts,
        top_spenders=top_spenders,
    )
    return summary, True


def _build_l3_priors(tenant_id: UUID, run_id: UUID) -> tuple[L3Priors, bool]:
    """L3 cross-tenant prior read (VT-69). Looks up ``cohort_response_rate``
    priors for the tenant's (business_type, city_tier) across recency bands.

    The 180-day quarantine + the no-match case both yield the structured
    no-prior marker (available=False + note) — NEVER fabricated defaults
    (Pillar 4). Completeness flag = True only when a real prior was found.
    """
    from orchestrator.knowledge.l3_query import lookup_pattern
    from orchestrator.knowledge.l3_types import PatternType, RECENCY_BANDS
    from orchestrator.knowledge.l3_types import cohort_key as _cohort_key

    with tenant_connection(tenant_id) as conn:
        raw = conn.execute(
            "SELECT business_type, city_tier FROM tenants WHERE id = %s",
            (str(tenant_id),),
        ).fetchone()
    row = cast("dict[str, Any]", raw) if raw else {}
    bt, tier = row.get("business_type"), row.get("city_tier")
    if not bt or not tier:
        return L3Priors(available=False, note=_L3_NO_PRIOR_NOTE), False

    patterns: list[dict[str, Any]] = []
    for band in RECENCY_BANDS:
        p = lookup_pattern(
            tenant_id, PatternType.COHORT_RESPONSE_RATE,
            _cohort_key(bt, tier, band), run_id=run_id,
        )
        if p is not None:
            patterns.append({
                "pattern_type": p.pattern_type, "cohort_key": p.cohort_key,
                "metrics": p.metrics, "confidence_band": p.confidence_band,
                "n_tenants": p.n_tenants,
            })
    if not patterns:
        return L3Priors(available=False, note=_L3_NO_PRIOR_NOTE), False
    return L3Priors(available=True, patterns=patterns, note=""), True


def _build_l4_skills(tenant_id: UUID, user_request: str) -> tuple[L4Skills, bool]:
    """L4 corpus retrieval (VT-70) — embeds the owner request, returns the top
    applicable domain-knowledge docs as LIGHTWEIGHT pointers (title + excerpt +
    score); the agent pulls full bodies via the ``retrieve_l4_skills`` MCP tool.

    Best-effort: L4 is enrichment — a missing VOYAGE_API_KEY, a voyage outage, or
    an empty corpus yields the structured no-skills marker (NOT fabricated
    knowledge, Pillar 4) and never breaks the bundle. Completeness flag = True
    only when real docs are returned.
    """
    from orchestrator.knowledge.l4_query import retrieve_documents

    try:
        with tenant_connection(tenant_id) as conn:
            raw = conn.execute(
                "SELECT business_type, city_tier FROM tenants WHERE id = %s",
                (str(tenant_id),),
            ).fetchone()
        row = cast("dict[str, Any]", raw) if raw else {}
        docs = retrieve_documents(
            user_request,
            business_type=row.get("business_type"),
            city_tier=row.get("city_tier"),
            top_k=5,
        )
    except Exception:  # noqa: BLE001 — L4 is best-effort enrichment; never break dispatch
        logger.warning("L4 retrieval failed (tenant=%s); proceeding without", tenant_id)
        return L4Skills(available=False, note=_L4_NO_SKILLS_NOTE), False

    if not docs:
        return L4Skills(available=False, note=_L4_NO_SKILLS_NOTE), False
    skills = [
        {
            "id": str(d.id),  # for the VT-71 composition audit (l4_doc_ids)
            "title": d.title,
            "tags": d.tags,
            "priority": d.priority,
            "score": round(d.score, 4) if d.score is not None else None,
            "excerpt": d.body[:300],
        }
        for d in docs
    ]
    return L4Skills(available=True, skills=skills, note=""), True


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


_PENDING_OWNER_INPUTS_LIMIT = 10


def _build_pending_owner_inputs(tenant_id: UUID) -> tuple[list[OwnerInput], bool]:
    """owner_inputs-table read (VT-146).

    Reads the live ``owner_inputs`` table via ``tenant_connection``
    (RLS + GUC scoped); returns the pending rows (``consumed_at IS
    NULL``) ordered most-recent-first, capped at
    ``_PENDING_OWNER_INPUTS_LIMIT``. Pending semantics live in the
    schema, not in this code — the partial index on
    ``(tenant_id, created_at DESC) WHERE consumed_at IS NULL``
    keeps the hot-path read cheap as the table grows over the tenant
    relationship's lifetime.

    Completeness flag: ``True`` when at least one row is returned,
    ``False`` on empty. No placeholder columns on this section (unlike
    VT-138's ``recovered_paise = 0`` placeholder in ``recent_campaigns``)
    so empty-substrate vs. populated-substrate is the right contract.

    Belt-and-braces over RLS (CL-71 / CL-190): the raw rows pass through
    ``assert_tenant_scoped`` before mapping — RLS should make a
    cross-tenant row impossible, but the assertion logs + raises if it
    ever happens.
    """
    with tenant_connection(tenant_id) as conn:
        raw_rows = conn.execute(
            "SELECT id, tenant_id, intent, segment, occasion, created_at "
            "FROM owner_inputs "
            "WHERE consumed_at IS NULL "
            "ORDER BY created_at DESC "
            "LIMIT %s",
            (_PENDING_OWNER_INPUTS_LIMIT,),
        ).fetchall()
    rows = cast("list[dict[str, Any]]", raw_rows)
    assert_tenant_scoped(rows, tenant_id)
    inputs = [
        OwnerInput(
            input_id=row["id"],
            received_at=row["created_at"],
            intent=row["intent"],
            segment=row["segment"],
            occasion=row["occasion"],
        )
        for row in rows
    ]
    return inputs, bool(inputs)


def _build_recovery_target_config(tenant_id: UUID) -> tuple[float, int]:
    """Read per-tenant recovery-target config from the tenants table (VT-164).

    Returns ``(multiplier, floor_paise)`` sourced from the DB.  On any read
    failure (no row, exception) falls back to the module-level defaults so the
    computed target is UNCHANGED from pre-VT-164 behaviour (CL-191 safe-empty
    / fallback contract).

    RLS note: tenant_connection sets ``app.current_tenant`` GUC so the SELECT
    can only return the one row whose ``id = app_current_tenant()``.  The
    explicit ``WHERE id = %s`` is belt-and-braces (plan: not using
    ``assert_tenant_scoped`` because the tenants self-read row key is ``id``,
    not ``tenant_id`` — would be a field mismatch; inline assertion instead).
    """
    try:
        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT id, recovery_target_multiplier, recovery_target_floor_paise "
                "FROM tenants WHERE id = %s",
                (str(tenant_id),),
            ).fetchone()
    except Exception:  # noqa: BLE001 — any DB error → fallback, logged below
        row = None

    if not row:
        return (_DEFAULT_RECOVERY_TARGET_MULTIPLIER, _DEFAULT_TARGET_RECOVERED_PAISE)

    # Belt-and-braces: the RLS GUC already scopes the row, but assert the id
    # matches what we asked for (catches any future policy misconfiguration).
    assert row["id"] == tenant_id or str(row["id"]) == str(tenant_id), (
        f"_build_recovery_target_config: tenant_id mismatch "
        f"(asked {tenant_id!r}, got {row['id']!r})"
    )
    return (float(row["recovery_target_multiplier"]), int(row["recovery_target_floor_paise"]))


# --- VT-71 composition audit -------------------------------------------------


def _write_composition_audit(
    *,
    tenant_id: UUID,
    run_id: UUID,
    cohort_key: str | None,
    section_token_counts: dict[str, int],
    total_token_count: int,
    truncated_sections: list[str],
    l3_cohort_keys: list[str],
    l4_doc_ids: list[str],
) -> None:
    """Write one composition_audits row (Pillar-7 traceability). Best-effort —
    a failure logs + is swallowed; the agent's bundle must never be blocked by
    its own audit. Tenant-scoped (RLS via the GUC); lifetime retention (CL-416)."""
    try:
        with tenant_connection(tenant_id) as conn:
            conn.execute(
                "INSERT INTO composition_audits "
                "(tenant_id, run_id, cohort_key, section_token_counts, "
                " total_token_count, truncated_sections, l3_cohort_keys, l4_doc_ids) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(tenant_id), str(run_id), cohort_key,
                    Jsonb(section_token_counts), total_token_count,
                    truncated_sections, l3_cohort_keys,
                    [UUID(x) for x in l4_doc_ids],
                ),
            )
    except Exception:  # noqa: BLE001 — audit is best-effort; never break the bundle
        logger.warning(
            "VT-71 composition audit write failed (tenant=%s run=%s)", tenant_id, run_id
        )


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
    l3_priors, l3_ok = _build_l3_priors(tenant_id, run_id)
    l4_skills, l4_ok = _build_l4_skills(tenant_id, user_request)
    recovery_target_multiplier, recovery_target_floor_paise = _build_recovery_target_config(tenant_id)

    # VT-71 cross-layer dedup: an L4 doc explicitly tagged with a live L3
    # cohort_key is redundant with that prior's DATA — drop it (the L3 number
    # supersedes; other L4 heuristics stay; no content collision). Conservative:
    # only an EXACT cohort_key tag match dedups (generic tags like 'cafe' don't).
    _l3_cohorts = {p["cohort_key"] for p in l3_priors.patterns}
    if _l3_cohorts and l4_skills.skills:
        _kept = [s for s in l4_skills.skills if not (set(s.get("tags") or []) & _l3_cohorts)]
        if len(_kept) != len(l4_skills.skills):
            l4_skills = L4Skills(
                available=bool(_kept), skills=_kept,
                note="" if _kept else _L4_NO_SKILLS_NOTE,
            )

    data_completeness = {
        "business_profile": bp_ok,
        "customer_ledger_summary": ls_ok,
        "recent_campaigns": rc_ok,
        "attribution_snapshot": as_ok,
        "pending_owner_inputs": oi_ok,
        "l3_priors": l3_ok,
        "l4_skills": l4_ok,
    }

    def _total() -> int:
        return (
            _estimate_tokens(business_profile)
            + _estimate_tokens(ledger_summary)
            + _estimate_tokens(recent_campaigns)
            + _estimate_tokens(attribution_snapshot)
            + _estimate_tokens(pending_owner_inputs)
            + _estimate_tokens(l3_priors)
            + _estimate_tokens(l4_skills)
        )

    # Truncation order (VT-71, Cowork 20260604T015000Z): PROTECT the moat layers
    # (L3 priors + L4 skills) — trim the per-tenant sections FIRST so a large L2
    # (ledger top_spenders) cannot starve L3/L4. Order: oldest owner inputs ->
    # campaigns down to 3 -> top_spenders 20/10/5 -> drop business hours -> L4
    # skills -> L3 priors (last resort) -> overflow. (Previously L4/L3 dropped
    # first — that starved the moat; reversed.)
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
        # Moat layers — only after the per-tenant sections are exhausted.
        if l4_skills.skills:
            l4_skills = L4Skills(available=False, note=_L4_NO_SKILLS_NOTE)
            truncated.append("l4_skills")
            continue
        if l3_priors.patterns:
            l3_priors = L3Priors(available=False, note=_L3_NO_PRIOR_NOTE)
            truncated.append("l3_priors")
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

    # VT-71 composition audit (Pillar 7) — one row per compose so ops can
    # reconstruct what the agent saw. Best-effort: an audit failure must never
    # break the bundle the agent needs.
    _write_composition_audit(
        tenant_id=tenant_id,
        run_id=run_id,
        cohort_key=None,
        section_token_counts={
            "business_profile": _estimate_tokens(business_profile),
            "customer_ledger_summary": _estimate_tokens(ledger_summary),
            "recent_campaigns": _estimate_tokens(recent_campaigns),
            "attribution_snapshot": _estimate_tokens(attribution_snapshot),
            "pending_owner_inputs": _estimate_tokens(pending_owner_inputs),
            "l3_priors": _estimate_tokens(l3_priors),
            "l4_skills": _estimate_tokens(l4_skills),
        },
        total_token_count=meta.token_count,
        truncated_sections=list(dict.fromkeys(truncated)),
        l3_cohort_keys=[p["cohort_key"] for p in l3_priors.patterns],
        l4_doc_ids=[s["id"] for s in l4_skills.skills if s.get("id")],
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
        l3_priors=l3_priors,
        l4_skills=l4_skills,
        meta=meta,
        data_completeness=data_completeness,
        recovery_target_multiplier=recovery_target_multiplier,
        recovery_target_floor_paise=recovery_target_floor_paise,
    )


# --- bundle → prompt serializer (VT-4 ship-thin / VT-163 registry wiring) ----
#
# VT-163: replaced the ship-thin _PHASE1_APPROVED_TEMPLATES placeholder with
# a live call to templates_registry.approved_template_names("en"). The default
# is computed at call time (not import time) so the registry's 60s TTL cache
# applies — a yaml data edit is picked up without a restart.
#
# Back-compat: serialize_bundle_for_prompt still accepts a caller-supplied
# templates_available override (test seam unchanged).

def _default_templates_available() -> tuple[str, ...]:
    """Live read of agent-selectable template names from the registry."""
    return approved_template_names("en")


def serialize_bundle_for_prompt(
    context: SalesRecoveryContext,
    *,
    templates_available: tuple[str, ...] | None = None,
    target_recovered_paise: int | None = None,
) -> str:
    """Render the SalesRecoveryContext bundle as a markdown context block
    suitable for prepending to the agent's first user message.

    The agent loop's input is the Anthropic Messages API. The bundle is
    a Python dataclass — the LLM only sees what we put in the message
    content. This function renders the bundle's sections plus the
    ship-thin scaffolding (templates_available + target_recovered_paise)
    into a single structured block.

    Identity fields (``tenant_id``, ``run_id``) are deliberately omitted
    — the agent has no use for them and the orchestrator owns identity
    injection at output coercion. ``data_completeness`` IS included so
    the model knows which sections are substrate-backed vs safe-empty.

    The ``user_request`` is appended at the end of the block, so the
    caller can use the returned string directly as the first user
    message content.
    """
    if templates_available is None:
        templates_available = _default_templates_available()
    if target_recovered_paise is None:
        # VT-164: use per-tenant config from context (populated by
        # _build_recovery_target_config in build_sales_recovery_context).
        # Falls back to the module-level defaults when context fields are
        # their defaults — so a missing DB read never changes the number.
        target_recovered_paise = max(
            round(
                context.attribution_snapshot.last_7d_recovered_paise
                * context.recovery_target_multiplier
            ),
            context.recovery_target_floor_paise,
        )

    parts: list[str] = ["# Sales Recovery Context"]

    bp = context.business_profile
    parts.append("\n## Business profile")
    parts.append(
        f"- name: {bp.business_name or '(unknown)'}\n"
        f"- type: {bp.business_type or '(unknown)'}\n"
        f"- locality: {bp.locality or '(unknown)'}\n"
        f"- current_phase: {bp.current_phase or '(unknown)'}\n"
        f"- founding_tier_flag: {bp.founding_tier_flag}\n"
        f"- substrate_populated: "
        f"{context.data_completeness.get('business_profile', False)}"
    )

    ls = context.customer_ledger_summary
    parts.append("\n## Customer ledger summary")
    cohort_lines = (
        "\n".join(f"  - {name}: {count} customers" for name, count in ls.dormant_cohorts.items())
        if ls.dormant_cohorts
        else "  - (none recorded)"
    )
    parts.append(
        f"- total_customers: {ls.total_customers}\n"
        f"- dormant_cohorts:\n{cohort_lines}\n"
        f"- top_spenders (count): {len(ls.top_spenders)}\n"
        f"- substrate_populated: "
        f"{context.data_completeness.get('customer_ledger_summary', False)}"
    )

    parts.append("\n## Recent campaigns")
    if context.recent_campaigns:
        campaign_lines = "\n".join(
            f"  - campaign_id={c.campaign_id} status={c.status} "
            f"recovered_paise={c.recovered_paise} "
            f"proposed_at={c.proposed_at.isoformat()}"
            for c in context.recent_campaigns
        )
        parts.append(
            f"- count: {len(context.recent_campaigns)}\n{campaign_lines}\n"
            f"- substrate_populated: "
            f"{context.data_completeness.get('recent_campaigns', False)}"
        )
    else:
        parts.append(
            "- count: 0 (no prior recovery campaigns recorded)\n"
            f"- substrate_populated: "
            f"{context.data_completeness.get('recent_campaigns', False)}"
        )

    att = context.attribution_snapshot
    parts.append("\n## Attribution snapshot")
    parts.append(
        f"- cumulative_recovered_paise: {att.cumulative_recovered_paise}\n"
        f"- last_7d_recovered_paise: {att.last_7d_recovered_paise}\n"
        f"- last_30d_recovered_paise: {att.last_30d_recovered_paise}\n"
        f"- substrate_populated: "
        f"{context.data_completeness.get('attribution_snapshot', False)}"
    )

    parts.append("\n## Pending owner inputs")
    if context.pending_owner_inputs:
        owner_lines = "\n".join(
            f"  - intent={oi.intent} segment={oi.segment or '(none)'} "
            f"occasion={oi.occasion or '(none)'} "
            f"received_at={oi.received_at.isoformat()}"
            for oi in context.pending_owner_inputs
        )
        parts.append(
            f"- count: {len(context.pending_owner_inputs)}\n{owner_lines}\n"
            f"- substrate_populated: "
            f"{context.data_completeness.get('pending_owner_inputs', False)}"
        )
    else:
        parts.append(
            "- count: 0\n"
            f"- substrate_populated: "
            f"{context.data_completeness.get('pending_owner_inputs', False)}"
        )

    l3 = context.l3_priors
    parts.append("\n## L3 cross-tenant priors (anonymized, k>=10)")
    if l3.available and l3.patterns:
        prior_lines = "\n".join(
            f"  - {p['cohort_key']}: response_rate="
            f"{p.get('metrics', {}).get('response_rate', '?')} "
            f"(n_tenants={p['n_tenants']}, confidence={p['confidence_band']})"
            for p in l3.patterns
        )
        parts.append(
            f"- priors:\n{prior_lines}\n"
            "- These are anonymized cross-tenant aggregates — directional priors, "
            "not this tenant's data. Weigh them against this tenant's own signals."
        )
    else:
        parts.append(f"- {l3.note}")

    l4 = context.l4_skills
    parts.append("\n## L4 domain-knowledge skills (retrieved)")
    if l4.available and l4.skills:
        skill_lines = "\n".join(
            f"  - {s['title']} (tags: {', '.join(s.get('tags') or []) or 'none'}; "
            f"score: {s.get('score')}): {s.get('excerpt', '')}"
            for s in l4.skills
        )
        parts.append(
            f"- relevant docs:\n{skill_lines}\n"
            "- Excerpts only — call ``retrieve_l4_skills`` for the full text of a "
            "doc before relying on it."
        )
    else:
        parts.append(f"- {l4.note}")

    parts.append("\n## Available WhatsApp templates (orchestrator-approved)")
    parts.append(
        "\n".join(f"- {tid}" for tid in templates_available)
        + "\n\nUse exactly one of the template_ids above in your "
        "``message_plan.template_id``. Inventing a template_id is a "
        "contract violation; if none of the listed templates fits the "
        "cohort, return ``status='insufficient_data'``."
    )

    parts.append("\n## Expected outcome")
    parts.append(
        f"- target_recovered_paise: {target_recovered_paise}\n"
        "- Use this figure to size your ``expected_arrr`` range. The "
        "range MUST be a low/high band, not a point estimate; the "
        "midpoint should sit near this target."
    )

    parts.append(f"\n## Trigger reason\n- {context.trigger_reason}")

    parts.append(f"\n## Owner request\n{context.user_request}")

    return "\n".join(parts)
