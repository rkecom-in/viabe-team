"""VT-466 — the Team-Manager's business-context READ + WRITE + slice seams.

Design §7 ("Business knowledge/context lives in the KG/business-profile — the
manager reads/writes it; specialists get scoped slices"). This is the manager's
coherent way to KNOW THE BUSINESS: read the cross-functional situation/profile +
the business OBJECTIVE the manager holds across turns, and WRITE the objective +
learnings back.

REUSE-FIRST (no parallel KG; Fazal standing). Everything here is a THIN
composition over the EXISTING L1 ``business_profile`` entity:

- READ  → ``assemble_context_bundle`` (the L1 system block already injected at
  ``dispatch.py``) + the structured ``business_profile`` entity attributes
  (identity / archetype / hours / integration map) + the tenant-row identity
  (verified business_name + GST status) + the manager-held ``business_objective``.
- WRITE → ``upsert_business_profile`` (RLS-scoped, MERGE-not-clobber, atomic)
  for the per-tenant ``business_objective`` record (objective / will / decisions
  / learnings the manager carries across turns); cohort-generalizable learnings
  still go to L0 (``write_l0_fragment``, k-anonymous) — that path is unchanged
  and is NOT duplicated here.
- SLICE → ``context_slice_for_lane`` produces the lane-scoped slice the manager
  hands a specialist (the VT-465 ``SpecialistHandoff.context_slice``): the
  objective + the lane-relevant profile keys ONLY. Never cross-tenant; the
  specialist holds no cross-functional strategy.

WHY ``business_profile`` ATTRIBUTE, NOT A NEW TABLE (Fazal preference, §7): the
objective IS business context — the moat is the ONE per-tenant context record.
A new table would add an RLS surface + a migration + a deploy-vs-data-skew gap
for zero benefit; the existing entity already has the partial-unique-index
one-row-per-tenant guarantee (mig 055) + the atomic top-level JSONB merge. The
objective rides as the ``business_objective`` key on the SAME entity.

RLS / tenant-scoping: every read/write flows through the EXISTING L1 functions,
which use ``tenant_connection`` (SET ROLE app_role + the ``app.current_tenant``
GUC) — so isolation is the same enforced second layer the rest of L1 relies on.
This module adds NO new raw DB access.

CL-390: ``business_objective`` is owner/manager-authored BUSINESS context (goals,
policy, decisions), NOT customer PII. Never log attribute values — only the
tenant_id + booleans.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from orchestrator.db import tenant_connection
from orchestrator.knowledge.l1 import (
    BUSINESS_PROFILE_ENTITY_TYPE,
    assemble_context_bundle,
    search_entities,
    upsert_business_profile,
)

logger = logging.getLogger(__name__)

# The single JSONB key on the ``business_profile`` entity under which the
# manager-held objective/will/decisions/learnings live. One key, MERGE-not-
# clobber (a write to it replaces the whole object — see _merge_objective).
BUSINESS_OBJECTIVE_KEY = "business_objective"

# The lane → relevant profile-attribute keys map (design §7 "specialists get
# scoped slices"). The manager holds the WHOLE cross-functional context; a
# specialist sees ONLY its lane's slice + the objective. Keys are read from the
# ``business_profile`` entity attributes. A lane not listed gets the identity-
# only slice (business_archetype) so a new lane is never accidentally handed the
# full context — default-deny on the slice surface.
_LANE_PROFILE_KEYS: dict[str, tuple[str, ...]] = {
    # Sales-Recovery — winback / lapsed customers / campaigns.
    "sales_recovery": (
        "business_archetype",
        "communication_prefs",
        "working_hours",
    ),
    # Integration / connect — data-source setup.
    "integration": (
        "business_archetype",
        "integration_map",
    ),
    # Onboarding-conductor — profile setup.
    "onboarding_conductor": (
        "business_archetype",
        "owner_persona",
        "working_hours",
    ),
    # The six business specialist lanes (VT-468..473). Each keyed on the lane's
    # ``SpecialistSpec.name`` (the token ``context_slice_for_lane`` receives). A
    # lane sees ONLY its slice + the objective; an unlisted name falls to the
    # identity-only default (default-deny on the slice surface).
    #
    # Sales (VT-468) — revenue from EXISTING customers (win-back / repeat / upsell
    # / re-engage); needs the archetype + how/when to reach customers.
    "sales_lane": (
        "business_archetype",
        "communication_prefs",
        "working_hours",
    ),
    # Marketing (VT-469) — campaigns / segments / festival offers / content;
    # needs the archetype + the owner's contactability prefs + hours (festival /
    # seasonal timing).
    "marketing": (
        "business_archetype",
        "communication_prefs",
        "working_hours",
    ),
    # Finance (VT-470) — cash-flow / receivables / margin/pricing (ADVISORY);
    # needs the archetype (revenue/margin shape) + escalation thresholds (the
    # owner's money-decision bounds the lane reasons within).
    "finance_lane": (
        "business_archetype",
        "escalation_thresholds",
    ),
    # Accounting (VT-471) — bookkeeping / GST-tax-summary / reconciliation
    # (PREPARE-only); needs the archetype to frame the books (the verified GST
    # identity rides in BusinessContext.identity, not the profile slice).
    "accounting": (
        "business_archetype",
    ),
    # Tech (VT-472) — store / listing / integration HEALTH; needs the archetype +
    # the integration map (which connectors/listings exist to diagnose).
    "tech": (
        "business_archetype",
        "integration_map",
    ),
    # Cost-Opt (VT-473) — wasteful-spend / subscription / ROI advice + resource
    # recalibration (ADVISE-only); needs the archetype + the integration map
    # (vendor/connector cost surface) + escalation thresholds (the owner's
    # cost-decision bounds).
    "cost_opt": (
        "business_archetype",
        "integration_map",
        "escalation_thresholds",
    ),
}

# Keys ALWAYS in a slice regardless of lane (identity anchor + objective is
# threaded separately). Identity-only so an unmapped lane still gets the anchor.
_SLICE_IDENTITY_KEYS: tuple[str, ...] = ("business_archetype",)


@dataclass(frozen=True, slots=True)
class BusinessContext:
    """What the Team-Manager reads to reason about the business (design §7
    "Manager = SITUATION + OUTCOME"). A pure value object — assembled from the
    EXISTING L1 substrate, never a new store.

    - ``l1_block``   — the rendered L1 system block (owner-stated profile +
                       agent-learned reflection) from ``assemble_context_bundle``.
                       ``None`` when the tenant carries no L1 content yet.
    - ``profile``    — the structured ``business_profile`` entity attributes
                       (archetype / hours / integration map / communication
                       prefs / owner persona). ``{}`` when no entity exists.
    - ``identity``   — the tenant-row identity the manager needs to reason about
                       legitimacy: verified ``business_name`` + ``gst_status`` +
                       ``phase`` + ``business_type``. Read-only; the manager does
                       NOT write these (the verify rails own them).
    - ``objective``  — the manager-held ``business_objective`` record (objective
                       / will / decisions / learnings). ``{}`` until the manager
                       records one. This is the cross-turn "what's good for this
                       business" the manager carries.
    """

    tenant_id: UUID
    l1_block: str | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    identity: dict[str, Any] = field(default_factory=dict)
    objective: dict[str, Any] = field(default_factory=dict)


def _read_profile_attributes(tenant_id: UUID) -> dict[str, Any]:
    """Read the tenant's single ``business_profile`` entity attributes (RLS-scoped
    via ``search_entities`` → ``tenant_connection``). ``{}`` when none exists."""
    rows = search_entities(
        tenant_id, entity_type=BUSINESS_PROFILE_ENTITY_TYPE, limit=1
    )
    return dict(rows[0].attributes or {}) if rows else {}


# The verification tiers that count as "≥ gstin_verified" — REUSE the canonical
# set the onboarding gate / activation registry assert against (do NOT redefine
# the verified-vs-not boundary). Imported lazily in _read_identity to avoid an
# agents import at module load.
def _verified_tiers() -> frozenset[str]:
    from orchestrator.agents.onboarding_gate import _VERIFIED_TIERS

    return _VERIFIED_TIERS


def _read_identity(tenant_id: UUID) -> dict[str, Any]:
    """Read the tenant-row identity the manager reasons about: the VERIFIED
    business name + verification status + phase + business_type + gstin-present.

    Reads the real ``tenants`` columns (``verification_status`` /
    ``verified_business_name`` / ``gstin`` — verified by schema introspection,
    not assumed). ``business_name`` is the owner-entered name; the manager
    reasons about legitimacy off the VERIFIED name (``verified_business_name``,
    set only by the GSTIN/VTR verify path) — surfaced distinctly so the manager
    never treats an unverified entered name as verified.

    RLS: ``tenant_connection`` sets the GUC so the self-read can only return the
    one row whose ``id = app_current_tenant()``; the explicit ``WHERE id = %s``
    is belt-and-braces (the tenants self-read key is ``id``, not ``tenant_id`` —
    same shape ``_build_recovery_target_config`` uses in context_builder).
    Best-effort: a read miss degrades to ``{}`` rather than breaking a manager
    turn. The GST/ownership verify RAILS own these columns — the manager only
    READS them here (never writes), so this seam cannot weaken a gate.
    """
    try:
        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT business_name, verified_business_name, business_type, "
                "phase, verification_status, gstin FROM tenants WHERE id = %s",
                (str(tenant_id),),
            ).fetchone()
    except Exception:  # noqa: BLE001 — identity is enrichment; a read miss degrades
        logger.warning(
            "business_context: tenant identity read failed (tenant=%s)", tenant_id
        )
        return {}
    if not row:
        return {}

    def _col(key: str, idx: int) -> Any:
        return row[key] if isinstance(row, dict) else row[idx]

    status = _col("verification_status", 4)
    verified = status in _verified_tiers()
    verified_name = _col("verified_business_name", 1)
    return {
        # The verified name when verified, else the owner-entered name — the
        # manager reasons off the strongest available identity, flagged below.
        "business_name": verified_name or _col("business_name", 0),
        "verified_business_name": verified_name,
        "business_type": _col("business_type", 2),
        "phase": _col("phase", 3),
        "gst_status": status,
        "gst_verified": verified,
        "gstin_present": bool(_col("gstin", 5)),
    }


def read_business_context(tenant_id: UUID | str) -> BusinessContext:
    """The manager READ seam (design §7) — assemble the coherent business context
    the Team-Manager reasons over: the L1 block + structured profile + tenant
    identity + the manager-held objective.

    A thin composition over the EXISTING L1 ``business_profile`` entity +
    ``assemble_context_bundle`` — NOT a new store. RLS-scoped throughout (every
    read flows through ``tenant_connection``). Best-effort per section: a read
    miss on any section degrades to its safe-empty default rather than breaking
    the manager turn (the L1 enrichment block is already best-effort at the
    dispatch call site).
    """
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))

    try:
        l1_block = assemble_context_bundle(tid)
    except Exception:  # noqa: BLE001 — L1 enrichment is best-effort (parity w/ dispatch)
        logger.warning(
            "business_context: L1 bundle assembly failed (tenant=%s)", tid
        )
        l1_block = None

    profile = _read_profile_attributes(tid)
    objective = dict(profile.get(BUSINESS_OBJECTIVE_KEY) or {})
    # The objective rides INSIDE the profile entity; surface it as its own field
    # and drop it from the profile-attrs view so the two are not double-rendered.
    profile_view = {k: v for k, v in profile.items() if k != BUSINESS_OBJECTIVE_KEY}

    return BusinessContext(
        tenant_id=tid,
        l1_block=l1_block,
        profile=profile_view,
        identity=_read_identity(tid),
        objective=objective,
    )


def render_business_context_block(ctx: BusinessContext) -> str | None:
    """Render the manager's business context as a ``## Business context`` system
    block for dispatch injection. ``None`` when there is nothing to surface (so
    dispatch injects nothing rather than an empty header — same contract as
    ``assemble_context_bundle``).

    The L1 block (owner-stated profile + agent reflection) is injected SEPARATELY
    by dispatch (the VT-195 seam), so it is NOT re-rendered here — this block adds
    the IDENTITY anchor + the manager-held OBJECTIVE the L1 block does not carry.
    CL-390: renders business context only; no customer PII.
    """
    lines: list[str] = []

    ident = ctx.identity
    if ident:
        name = ident.get("business_name") or "(unknown)"
        btype = ident.get("business_type") or "(unknown)"
        phase = ident.get("phase") or "(unknown)"
        gst = ident.get("gst_status") or "(unknown)"
        verified_name = ident.get("verified_business_name")
        lines.append("## Business context")
        name_line = f"- business: {name} ({btype})"
        if not verified_name:
            name_line += " — name NOT yet verified"
        lines.append(
            f"{name_line}\n"
            f"- phase: {phase}\n"
            f"- verification: {gst} (verified={bool(ident.get('gst_verified'))})"
        )

    obj = ctx.objective
    if obj:
        if not lines:
            lines.append("## Business context")
        lines.append("### Business objective (what you hold for this business)")
        for key in ("objective", "will", "policy", "decisions", "learnings"):
            val = obj.get(key)
            if val in (None, "", [], {}):
                continue
            lines.append(f"- {key}: {_render_value(val)}")

    return "\n".join(lines) if lines else None


def _render_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def _merge_objective(
    existing: dict[str, Any], patch: dict[str, Any]
) -> dict[str, Any]:
    """Top-level merge of an objective patch into the existing objective record.

    Mirrors ``upsert_business_profile``'s MERGE-not-clobber at the objective
    level: a patched key overwrites; an unpatched sibling key is PRESERVED. So
    the manager can record a single learning without clobbering the standing
    objective/will. ``None`` valued keys in the patch are DROPPED (an explicit
    "clear this field" is a future affordance, not the default merge)."""
    merged = dict(existing)
    for k, v in patch.items():
        if v is None:
            continue
        merged[k] = v
    return merged


def write_business_objective(
    tenant_id: UUID | str, objective_patch: dict[str, Any]
) -> dict[str, Any]:
    """The manager WRITE seam (design §7) — record/update the per-tenant business
    OBJECTIVE / will / decisions / learnings the manager holds across turns.

    REUSE: writes through ``upsert_business_profile`` (RLS-scoped, MERGE-not-
    clobber, atomic, one-row-per-tenant) — the ``business_objective`` key on the
    EXISTING ``business_profile`` entity. NOT a new table, NOT a new store.

    Two-level merge so a single learning never clobbers the standing objective:
      1. read the current objective object (RLS-scoped),
      2. top-level merge the patch into it (``_merge_objective``),
      3. write the WHOLE merged object back under the ``business_objective`` key
         (``upsert_business_profile``'s top-level merge replaces that one key,
         preserving every sibling profile attribute).

    Deterministic (no LLM at the write site). Returns the merged objective so the
    caller can read back / assert. Empty patch is a no-op (returns current).

    Cohort-generalizable learnings (that should reach OTHER tenants) go to L0
    (``write_l0_fragment``, k-anonymous) — that is a SEPARATE, unchanged path;
    this seam is the TENANT-SCOPED objective the manager carries for THIS
    business only.
    """
    tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    if not objective_patch:
        return dict(read_business_context(tid).objective)

    existing = _read_profile_attributes(tid).get(BUSINESS_OBJECTIVE_KEY) or {}
    merged = _merge_objective(dict(existing), objective_patch)
    upsert_business_profile(tid, {BUSINESS_OBJECTIVE_KEY: merged})
    logger.info(
        "business_context: business_objective recorded (tenant=%s keys=%d)",
        tid, len(merged),
    )
    return merged


def context_slice_for_lane(
    ctx: BusinessContext, lane: str
) -> dict[str, Any]:
    """The SLICE seam (design §7) — produce the lane-scoped ``context_slice`` the
    manager hands a specialist (the VT-465 ``SpecialistHandoff.context_slice``).

    The manager holds the WHOLE cross-functional context; a specialist sees ONLY:
      - the business OBJECTIVE (the manager's framed outcome context), and
      - the lane-relevant PROFILE keys (from ``_LANE_PROFILE_KEYS``; an unmapped
        lane gets the identity anchor only — default-deny, so a new lane is never
        accidentally handed the full context).

    The slice carries NO cross-tenant data (it is built from one tenant's
    ``BusinessContext``) and NO out-of-lane profile keys. It is a plain dict so
    it drops straight into ``SpecialistHandoff.context_slice``.
    """
    keys = _LANE_PROFILE_KEYS.get(lane, _SLICE_IDENTITY_KEYS)
    profile_slice = {
        k: ctx.profile[k] for k in keys if k in ctx.profile and ctx.profile[k] not in (None, "", [], {})
    }
    slice_payload: dict[str, Any] = {
        "lane": lane,
        "business_archetype": ctx.identity.get("business_type")
        or ctx.profile.get("business_archetype"),
        "profile": profile_slice,
    }
    if ctx.objective:
        slice_payload["objective"] = dict(ctx.objective)
    return slice_payload


__all__ = [
    "BUSINESS_OBJECTIVE_KEY",
    "BusinessContext",
    "context_slice_for_lane",
    "read_business_context",
    "render_business_context_block",
    "write_business_objective",
]
