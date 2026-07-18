"""VT-528 (B5) — the capability-truth registry.

Every capability the system can perform declares its CONTRACT here — mode, effect class,
policy rail, verifier, rollback, prerequisites, and the environments it may run in. Two theses
from the Phase-1 plan are enforced *at import*, fail-closed (a bad declaration crashes the
worker at boot, like ``run_control/registry.py``):

  1. **No specialist self-certifies.** Any capability with a real EFFECT (send / db_mutation /
     connector / campaign) MUST name a VERIFIER that is registered in ``VERIFIER_REGISTRY`` — an
     external check of the evidence, not the specialist's own word. ``verify()`` is the gate the
     manager runs before marking a task step done ("evidence before completion").
  2. **Every effect is owner-policy-gated.** An effectful capability MUST set ``policy_rail=True``
     (the OC1 owner-policy check applies before the effect fires).

WHY A CODE REGISTRY (not a DB table) — the SAME call as ``agents/activation_registry.py`` and
``integrations/registry.py``: a capability contract is part of the PRODUCT's behavioral surface,
ships with the code, is diffable + unit-testable at boot, and has no runtime-edit use case. A DB
table would add an RLS surface, a migration, and a deploy-vs-data-skew gap for zero live-ops
benefit. Environment availability is resolved against the caller's ``EXPECTED_ENV`` (passed in),
not a per-tenant DB toggle. CL-390: declarations only — NO tenant data lives here.

Extending: add a ``CapabilitySpec`` to ``CAPABILITY_REGISTRY``. Concrete per-capability verifiers
(and the connector ``health_check`` verifier) land as their capabilities graduate — see the tail.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from orchestrator.agents.activation_registry import REGISTRY as _ACTIVATION_REGISTRY

# ── Taxonomies ───────────────────────────────────────────────────────────────
EffectClass = Literal["send", "db_mutation", "connector", "campaign", "advisory"]
# VT-681 phase 1 — the mode axis is the LAUNCH OPERATING MODE of the capability (the B-track
# contract's tri-state), replacing VT-528's autonomy postures (concierge/supervised_auto/
# full_auto), which conflated "how autonomously it runs" with "whether it may be promised":
#   live      — executes end-to-end (through its gates); the Manager may promise it.
#   advisory  — Manager-held prepare/propose/analyse only; described as such, never as
#               an autonomous action ("I'll prepare X for you", never "I'll run X").
#   disabled  — declared so the Manager can be HONEST about it (the D2 ad-boost class);
#               never available in any environment until the mode flips.
CapabilityMode = Literal["live", "advisory", "disabled"]

EFFECT_CLASSES: frozenset[str] = frozenset(
    {"send", "db_mutation", "connector", "campaign", "advisory"}
)
CAPABILITY_MODES: frozenset[str] = frozenset({"live", "advisory", "disabled"})
KNOWN_ENVS: frozenset[str] = frozenset({"dev", "prod"})
# Every effect class EXCEPT advisory is a real-world effect → must be verified + policy-gated.
_EFFECTFUL: frozenset[str] = EFFECT_CLASSES - {"advisory"}


@dataclass(frozen=True)
class VerifierResult:
    ok: bool
    reason: str = ""


# A verifier is a pure predicate over the evidence a capability produced. Deterministic-first
# (mirrors vtr_classifier / approval_reply) — NOT an LLM self-eval call per capability.
Verifier = Callable[[Mapping[str, Any]], VerifierResult]


@dataclass(frozen=True)
class CapabilitySpec:
    """The declared contract of one capability. Data only — no behaviour, no tenant facts."""

    key: str
    lane: str                                   # owning roster lane (e.g. 'sales_recovery')
    effect_class: EffectClass
    mode: CapabilityMode                        # current default autonomy posture
    policy_rail: bool                           # owner-policy checked before the effect (OC1)
    summary: str
    verifier: str | None = None                 # name into VERIFIER_REGISTRY (None only for advisory)
    rollback: str | None = None                 # named rollback, or None if irreversible/none
    prerequisites: str | None = None            # activation_registry key gating it (reuse, don't fork)
    environments: frozenset[str] = field(default_factory=lambda: KNOWN_ENVS)


# ── Reference verifiers (deterministic) ──────────────────────────────────────
_MOCK_SID_PREFIX = "MKDEV"  # VT-476 dev_send_guard mock marker (a mocked send is NOT a real send)


def _verify_real_send_evidence(evidence: Mapping[str, Any]) -> VerifierResult:
    """A send is verified by its TRANSPORT RECEIPT, not the specialist's claim: evidence must
    carry a real Twilio message_sid (present, and not a ``MKDEV…`` dev-mock SID)."""
    sid = evidence.get("message_sid")
    if not isinstance(sid, str) or not sid:
        return VerifierResult(False, "no message_sid in send evidence")
    if sid.startswith(_MOCK_SID_PREFIX):
        return VerifierResult(False, "message_sid is a dev-mock (MKDEV) SID, not a real send")
    return VerifierResult(True, "real transport receipt present")


def _verify_connector_health_evidence(evidence: Mapping[str, Any]) -> VerifierResult:
    """A connector is verified by its persisted health row (tenant_connector_status), not the
    specialist's claim: evidence must name the connector and carry last_status='ok'."""
    connector_id = evidence.get("connector_id")
    if not isinstance(connector_id, str) or not connector_id:
        return VerifierResult(False, "no connector_id in connector evidence")
    if evidence.get("last_status") != "ok":
        return VerifierResult(False, f"connector {connector_id!r} last_status != 'ok'")
    return VerifierResult(True, "connector health row ok")


def _verify_gstin_verified_evidence(evidence: Mapping[str, Any]) -> VerifierResult:
    """GST verification is verified by the persisted tenants.verification_status value."""
    if evidence.get("verification_status") != "gstin_verified":
        return VerifierResult(False, "verification_status != 'gstin_verified'")
    return VerifierResult(True, "gstin_verified persisted")


def _verify_journey_progress_evidence(evidence: Mapping[str, Any]) -> VerifierResult:
    """An onboarding step is verified by the persisted journey row state, never the turn text."""
    status = evidence.get("journey_status")
    if status not in ("active", "complete"):
        return VerifierResult(False, f"journey_status {status!r} not active/complete")
    return VerifierResult(True, "journey row state present")


def _verify_export_audit_evidence(evidence: Mapping[str, Any]) -> VerifierResult:
    """A customer-list export is verified by its tm_audit trail (VT-676: counts/tokens are
    audited — content/URL never are), not by the reply claiming a file was sent."""
    if evidence.get("audit_kind") != "customer_list_exported":
        return VerifierResult(False, "no customer_list_exported audit in evidence")
    row_count = evidence.get("row_count")
    if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
        return VerifierResult(False, f"row_count {row_count!r} not a non-negative int")
    return VerifierResult(True, "export audit trail present")


VERIFIER_REGISTRY: dict[str, Verifier] = {
    "real_send_evidence": _verify_real_send_evidence,
    "connector_health_evidence": _verify_connector_health_evidence,
    "gstin_verified_evidence": _verify_gstin_verified_evidence,
    "journey_progress_evidence": _verify_journey_progress_evidence,
    "export_audit_evidence": _verify_export_audit_evidence,
}


# ── The registry — one entry per PROMISE-RELEVANT capability (VT-681 phase 1) ─
# The set the Manager can meaningfully promise (or must honestly decline) at Phase-1 launch,
# joined from the O10 roster: 3 live agents (Manager embedded / Sales Recovery / Onboarding
# Conductor), the live Tools, the 5 advisory functions (Manager-held, prepare-only), and the
# one DECLARED-DISABLED action the D2 net already discloses by hand. Per-tenant resolution
# (activation × entitlement × freeze) is phase 2's resolve_for; these are the ENV-level defaults.
CAPABILITY_REGISTRY: dict[str, CapabilitySpec] = {
    # ── Sales Recovery (LIVE agent; Concierge roster, first eligible to graduate) ──
    "sales_recovery.winback_send": CapabilitySpec(
        key="sales_recovery.winback_send",
        lane="sales_recovery",
        effect_class="send",
        mode="live",
        policy_rail=True,
        summary="Send an owner-approved win-back message to a lapsed-customer cohort.",
        verifier="real_send_evidence",
        rollback=None,                          # a send is irreversible — no rollback
        prerequisites="sales_recovery",         # activation_registry bar
        environments=KNOWN_ENVS,
    ),
    "sales_recovery.advice": CapabilitySpec(
        key="sales_recovery.advice",
        lane="sales_recovery",
        effect_class="advisory",
        mode="live",
        policy_rail=False,                       # advice has no external effect to gate
        summary="Give the owner grounded win-back advice (no send, no mutation).",
        verifier=None,
        rollback=None,
        prerequisites=None,
        environments=KNOWN_ENVS,
    ),
    # ── Onboarding Conductor (LIVE agent) ──
    "onboarding.conduct_journey": CapabilitySpec(
        key="onboarding.conduct_journey",
        lane="onboarding_conductor",
        effect_class="db_mutation",              # writes journey answers + business profile
        mode="live",
        policy_rail=True,                        # every write is the owner's own conversational input
        summary="Run the WhatsApp onboarding journey: confirm/collect profile fields, pace the "
                "post-profile flows, complete the journey.",
        verifier="journey_progress_evidence",
        rollback="re-open journey (answers are re-editable until complete)",
        prerequisites=None,                      # onboarding IS the prerequisite-builder
        environments=KNOWN_ENVS,
    ),
    # ── Integration Tools (dissolved into Tools per ACF; LIVE) ──
    "integration.google_sheet_ingest": CapabilitySpec(
        key="integration.google_sheet_ingest",
        lane="integration",
        effect_class="connector",
        mode="live",
        policy_rail=True,
        summary="Zero-paste Google Sheets connect + ledger ingestion (CL-421).",
        verifier="connector_health_evidence",
        rollback="disable connector (tenant_connector_status.enabled=false)",
        prerequisites="integration_agent",
        environments=KNOWN_ENVS,
    ),
    "integration.shopify_connect": CapabilitySpec(
        key="integration.shopify_connect",
        lane="integration",
        effect_class="connector",
        mode="live",
        policy_rail=True,
        summary="Shopify OAuth connect + catalog/order ingestion.",
        verifier="connector_health_evidence",
        rollback="disable connector (tenant_connector_status.enabled=false)",
        prerequisites="integration_agent",
        environments=KNOWN_ENVS,
    ),
    "integration.gst_verify": CapabilitySpec(
        key="integration.gst_verify",
        lane="integration",
        effect_class="db_mutation",              # flips tenants.verification_status
        mode="live",
        policy_rail=True,
        summary="Verify the owner's GSTIN and persist verification_status (a correctness gate — "
                "never bent to make a flow pass).",
        verifier="gstin_verified_evidence",
        rollback=None,                           # verification is a ratchet, not undone in-product
        prerequisites=None,
        environments=KNOWN_ENVS,
    ),
    "integration.knowyourgst_discovery": CapabilitySpec(
        key="integration.knowyourgst_discovery",
        lane="integration",
        effect_class="advisory",                 # read-only business discovery
        mode="live",
        policy_rail=False,
        summary="Discover business facts from a GSTIN (knowyourgst) to pre-fill the profile draft.",
        verifier=None,
        rollback=None,
        prerequisites=None,
        environments=KNOWN_ENVS,
    ),
    # ── Manager-held owner service (LIVE; VT-676) ──
    "manager.customer_list_export": CapabilitySpec(
        key="manager.customer_list_export",
        lane="team_manager",
        effect_class="send",                     # sends a media message to the OWNER
        mode="live",
        policy_rail=True,
        summary="Export the tenant's customer list as a CSV attachment to the verified owner "
                "(private bucket, 300s signed URL, counts-only audit).",
        verifier="export_audit_evidence",
        rollback=None,                           # the signed URL self-expires (300s TTL)
        prerequisites=None,
        environments=KNOWN_ENVS,
    ),
    # ── Advisory functions (Manager-held tools; NEVER described as autonomous) ──
    "marketing.campaign_prepare": CapabilitySpec(
        key="marketing.campaign_prepare",
        lane="marketing",
        effect_class="advisory",
        mode="advisory",
        policy_rail=False,
        summary="Prepare + propose campaign plans and content drafts; any resulting send goes "
                "through the Manager's approval-gated send rails, never this function.",
        verifier=None, rollback=None, prerequisites=None, environments=KNOWN_ENVS,
    ),
    "finance.advice": CapabilitySpec(
        key="finance.advice",
        lane="finance",
        effect_class="advisory",
        mode="advisory",
        policy_rail=False,
        summary="Cash-flow / receivables / pricing-margin analysis; payment-reminder PROPOSALS only.",
        verifier=None, rollback=None, prerequisites=None, environments=KNOWN_ENVS,
    ),
    "accounting.prepare": CapabilitySpec(
        key="accounting.prepare",
        lane="accounting",
        effect_class="advisory",
        mode="advisory",
        policy_rail=False,
        summary="Prepare-only accounting outputs (v1 charter: nothing filed, nothing mutated).",
        verifier=None, rollback=None, prerequisites=None, environments=KNOWN_ENVS,
    ),
    "tech.owner_authorized_help": CapabilitySpec(
        key="tech.owner_authorized_help",
        lane="tech",
        effect_class="advisory",
        mode="advisory",
        policy_rail=False,
        summary="Technical guidance; any action only on the owner's explicit authorization, "
                "described as assistance, never as an autonomous agent.",
        verifier=None, rollback=None, prerequisites=None, environments=KNOWN_ENVS,
    ),
    "cost_opt.advice": CapabilitySpec(
        key="cost_opt.advice",
        lane="cost_opt",
        effect_class="advisory",
        mode="advisory",
        policy_rail=False,
        summary="Cost-optimisation analysis and recommendations (advisory only).",
        verifier=None, rollback=None, prerequisites=None, environments=KNOWN_ENVS,
    ),
    # ── Compliance (VT-685 — Codex-onboarding kit; the FIRST Codex-built specialist target) ──
    # Phase-1 posture is ADVISORY/PREPARE-ONLY (docs/agent-framework/CODEX-ONBOARDING.md §1): the
    # ``compliance_tools`` module reads + analyses + prepares GSTR-1/3B filing-READINESS; it never
    # files, sends, spends, or mutates business state. Actual return filing is a LATER graduation
    # through this registry (flip ``compliance.return_filing`` from disabled once Fazal grants it a
    # real verifier + a filing effect path) — declaring it now, disabled, is the D2-class honesty
    # entry so the Manager can decline a "file my GST return" ask truthfully from day one.
    "compliance.gstr_readiness": CapabilitySpec(
        key="compliance.gstr_readiness",
        lane="compliance",
        effect_class="advisory",
        mode="advisory",
        policy_rail=False,                       # advice/prep has no external effect to gate
        summary="Read + analyse the sales ledger and GST verification status to PREPARE a "
                "GSTR-1/3B return-filing readiness snapshot (checklist only — nothing filed).",
        verifier=None,
        rollback=None,
        prerequisites=None,
        environments=KNOWN_ENVS,
    ),
    "compliance.return_filing": CapabilitySpec(
        key="compliance.return_filing",
        lane="compliance",
        effect_class="db_mutation",              # would mutate filing state once it graduates
        mode="disabled",                         # NOT supported — the honesty entry (D2 class)
        policy_rail=True,                        # the rail is declared now so graduation can
                                                  # never drop it (Thesis 2 applies to disabled too)
        summary="File a GSTR return with the GST portal on the owner's behalf. NOT supported — "
                "the Manager must disclose the limit and offer the readiness prep instead (D2).",
        verifier=None,                           # permitted ONLY because mode='disabled'
        rollback=None, prerequisites=None,
        environments=frozenset(),                # available nowhere until the mode flips
    ),
    # ── Declared DISABLED (honesty entries — the D2 class, now registry-backed) ──
    "marketing.paid_ad_boost": CapabilitySpec(
        key="marketing.paid_ad_boost",
        lane="marketing",
        effect_class="campaign",
        mode="disabled",                         # the ONE unsupported paid action the D2 net
        policy_rail=True,                        # discloses by hand today; graduating it re-arms
        summary="Run a paid ad boost on an external platform (Instagram/Facebook/Google). NOT "
                "supported — the Manager must disclose the limit and pivot (D2).",
        verifier=None,                           # permitted ONLY because mode='disabled'
        rollback=None, prerequisites=None,
        environments=frozenset(),                # available nowhere until the mode flips
    ),
}


# ── Import-time invariants (fail-closed, mirrors run_control/registry) ────────
def _validate_spec(
    spec: CapabilitySpec,
    *,
    verifiers: Mapping[str, Verifier],
    activation_keys: frozenset[str],
) -> None:
    """Raise ``RuntimeError`` on any contract violation. Called for every registry entry at import
    (so a bad declaration crashes boot) and directly by the tests (so the invariants are covered)."""
    if spec.effect_class not in EFFECT_CLASSES:
        raise RuntimeError(f"capability {spec.key!r}: unknown effect_class {spec.effect_class!r}")
    if spec.mode not in CAPABILITY_MODES:
        raise RuntimeError(f"capability {spec.key!r}: unknown mode {spec.mode!r}")
    # VT-681: an empty environments set is legal ONLY for a declared-disabled capability (it is
    # available nowhere by definition); everything else must claim a subset of the known envs.
    if not spec.environments and spec.mode != "disabled":
        raise RuntimeError(f"capability {spec.key!r}: environments empty but mode != 'disabled'")
    if not spec.environments <= KNOWN_ENVS:
        raise RuntimeError(
            f"capability {spec.key!r}: environments {sorted(spec.environments)} not ⊆ "
            f"{sorted(KNOWN_ENVS)}"
        )
    # VT-681: an advisory-MODE capability prepares/proposes only — declaring a real effect class
    # under advisory mode would launder an effect past the roster posture.
    if spec.mode == "advisory" and spec.effect_class != "advisory":
        raise RuntimeError(
            f"capability {spec.key!r}: mode 'advisory' but effect_class {spec.effect_class!r} "
            "(an advisory-mode function may only declare effect_class='advisory')"
        )
    if spec.effect_class in _EFFECTFUL:
        # Thesis 1 — no self-certify: an effect must name a verifier. A DISABLED effect may
        # defer its verifier (nothing can execute); flipping the mode to live re-arms this
        # invariant at boot — the graduation ratchet.
        if spec.verifier is None and spec.mode != "disabled":
            raise RuntimeError(
                f"capability {spec.key!r}: effectful ({spec.effect_class}) but declares no verifier "
                "(no specialist self-certifies an effect)"
            )
        # Thesis 2 — every effect is owner-policy-gated (disabled included: the declaration of
        # intent must already carry the rail so graduation can never drop it).
        if not spec.policy_rail:
            raise RuntimeError(
                f"capability {spec.key!r}: effectful ({spec.effect_class}) but policy_rail=False "
                "(every effect is checked against the owner policy)"
            )
    if spec.verifier is not None and spec.verifier not in verifiers:
        raise RuntimeError(
            f"capability {spec.key!r}: verifier {spec.verifier!r} not in VERIFIER_REGISTRY"
        )
    if spec.prerequisites is not None and spec.prerequisites not in activation_keys:
        raise RuntimeError(
            f"capability {spec.key!r}: prerequisites {spec.prerequisites!r} not an activation key "
            f"(available: {sorted(activation_keys)})"
        )


_ACTIVATION_KEYS = frozenset(_ACTIVATION_REGISTRY.keys())
for _key, _spec in CAPABILITY_REGISTRY.items():
    if _key != _spec.key:
        raise RuntimeError(f"capability registry: key {_key!r} != spec.key {_spec.key!r}")
    _validate_spec(_spec, verifiers=VERIFIER_REGISTRY, activation_keys=_ACTIVATION_KEYS)


# ── Resolution / verify API ──────────────────────────────────────────────────
def resolve(key: str) -> CapabilitySpec:
    """The declared contract for a capability. Fail-closed: ``KeyError`` on an unknown key (an
    undeclared capability is never silently treated as available)."""
    if key not in CAPABILITY_REGISTRY:
        raise KeyError(
            f"capability {key!r} not declared; available: {sorted(CAPABILITY_REGISTRY.keys())}"
        )
    return CAPABILITY_REGISTRY[key]


def is_available(key: str, *, env: str) -> bool:
    """Whether the capability may run in ``env`` (the caller's EXPECTED_ENV). Fail-closed on an
    unknown env value. VT-681: a declared-DISABLED capability is available NOWHERE regardless of
    env — that's the honesty entry the promise seam (phase 3) reads to decline truthfully."""
    spec = resolve(key)
    return spec.mode != "disabled" and env in spec.environments


def mode_of(key: str) -> CapabilityMode:
    """The declared launch operating mode (live/advisory/disabled) — the label the Manager must
    describe the capability WITH (an advisory function is 'I can prepare…', never 'I'll run…')."""
    return resolve(key).mode


# ── Per-tenant resolution (VT-681 phase 2) ───────────────────────────────────

# capability lane → agent_framework module name, for the entitlement join. Lanes with no
# registered module are FREE capabilities (no SKU). Kept explicit — deriving it from
# prerequisites keys would silently break when either registry renames.
_LANE_MODULE: dict[str, str] = {
    "sales_recovery": "sales_recovery",
    "integration": "integration_tools",
    "onboarding_conductor": "onboarding_tools",
    "compliance": "compliance_tools",  # VT-685 — the Codex-built compliance_tools module
}


@dataclass(frozen=True)
class ResolvedCapability:
    """One capability's truth FOR ONE TENANT in ONE environment — what the promise seam
    (phase 3) reads before the Manager may promise anything. ``reasons`` is the full audit
    trail of the resolution (including non-blocking visibility notes), never just the blocker."""

    key: str
    mode: CapabilityMode
    available: bool
    reasons: tuple[str, ...]


def resolve_for(key: str, tenant_id: str, *, env: str, conn: Any) -> ResolvedCapability:
    """The per-tenant tri-state join: declared mode × environment × activation bar ×
    entitlement. Fail-closed at every uncertain edge (an error reads as unavailable, with the
    reason recorded). ``conn`` is the caller's RLS-scoped tenant connection — the SAME contract
    ``onboarding_gate.is_agent_eligible`` requires; resolution never opens its own.

    HONESTY NOTES (named, deliberate — never silently laundered):
      - Entitlement is SOFT-OPEN until billing wires (D-ENT): ``check_entitlement`` structurally
        returns True today. resolve_for still CALLS it (so the join is already live the day it
        can say no) and ALWAYS records the soft-open status in ``reasons``.
      - There is NO freeze/kill-switch subsystem in the tree today (audited 2026-07-18); when one
        exists it joins here. Recorded as a reason, not silently omitted.
    """
    spec = resolve(key)
    reasons: list[str] = []

    if spec.mode == "disabled":
        return ResolvedCapability(
            key=key, mode=spec.mode, available=False,
            reasons=("mode=disabled — declared unsupported; decline honestly (D2 class)",),
        )
    if env not in spec.environments:
        return ResolvedCapability(
            key=key, mode=spec.mode, available=False,
            reasons=(f"not available in env {env!r} (declared: {sorted(spec.environments)})",),
        )

    if spec.prerequisites is not None:
        try:
            from orchestrator.agents.onboarding_gate import is_agent_eligible  # lazy: DB deps

            if not is_agent_eligible(tenant_id, spec.prerequisites, conn=conn):
                return ResolvedCapability(
                    key=key, mode=spec.mode, available=False,
                    reasons=(f"activation bar unmet: {spec.prerequisites!r}",),
                )
            reasons.append(f"activation bar met: {spec.prerequisites!r}")
        except Exception as exc:  # noqa: BLE001 — fail-closed: an unreadable bar is an unmet bar
            return ResolvedCapability(
                key=key, mode=spec.mode, available=False,
                reasons=(f"activation check failed ({type(exc).__name__}) — fail-closed",),
            )

    module_name = _LANE_MODULE.get(spec.lane)
    if module_name is None:
        reasons.append("entitlement: free capability (no module SKU)")
    else:
        try:
            from orchestrator.agent_framework.entitlement import check_entitlement  # lazy
            from orchestrator.agent_framework.registration import get_registered  # lazy

            manifest = get_registered(module_name).manifest
            if not check_entitlement(manifest, tenant_id):
                return ResolvedCapability(
                    key=key, mode=spec.mode, available=False,
                    reasons=(*reasons, f"entitlement denied for module {module_name!r}"),
                )
            reasons.append(
                f"entitlement ok for module {module_name!r} "
                "(SOFT-OPEN until billing wires — D-ENT, never blocks today)"
            )
        except Exception as exc:  # noqa: BLE001 — an unregistered module is a FREE join, visibly
            reasons.append(
                f"entitlement: module {module_name!r} not resolvable "
                f"({type(exc).__name__}) — treated free, not blocked"
            )

    reasons.append("freeze/kill: no such subsystem exists yet (joins here when built)")
    return ResolvedCapability(
        key=key, mode=spec.mode, available=True, reasons=tuple(reasons),
    )


def resolve_all_for(tenant_id: str, *, env: str, conn: Any) -> list[ResolvedCapability]:
    """Every declared capability resolved for one tenant — the promise seam's one-call read."""
    return [resolve_for(k, tenant_id, env=env, conn=conn) for k in all_capabilities()]


def requires_policy_rail(key: str) -> bool:
    return resolve(key).policy_rail


def verify(key: str, evidence: Mapping[str, Any]) -> VerifierResult:
    """Run the capability's declared verifier over its evidence — the "evidence before completion"
    gate. An advisory capability has no external effect to verify (``ok=True``). An effectful one
    always has a verifier (import invariant), looked up from ``VERIFIER_REGISTRY``."""
    spec = resolve(key)
    if spec.verifier is None:
        return VerifierResult(True, "advisory: no effect to verify")
    return VERIFIER_REGISTRY[spec.verifier](evidence)


def all_capabilities() -> list[str]:
    return sorted(CAPABILITY_REGISTRY.keys())


__all__ = [
    "CAPABILITY_MODES",
    "CAPABILITY_REGISTRY",
    "CapabilityMode",
    "CapabilitySpec",
    "EFFECT_CLASSES",
    "EffectClass",
    "KNOWN_ENVS",
    "VERIFIER_REGISTRY",
    "Verifier",
    "VerifierResult",
    "ResolvedCapability",
    "all_capabilities",
    "is_available",
    "mode_of",
    "requires_policy_rail",
    "resolve",
    "resolve_all_for",
    "resolve_for",
    "verify",
]
