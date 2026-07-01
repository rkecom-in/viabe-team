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

# ── Taxonomies (net-new; the plan's effect-class set) ────────────────────────
EffectClass = Literal["send", "db_mutation", "connector", "campaign", "advisory"]
CapabilityMode = Literal["concierge", "supervised_auto", "full_auto"]

EFFECT_CLASSES: frozenset[str] = frozenset(
    {"send", "db_mutation", "connector", "campaign", "advisory"}
)
CAPABILITY_MODES: frozenset[str] = frozenset({"concierge", "supervised_auto", "full_auto"})
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


VERIFIER_REGISTRY: dict[str, Verifier] = {
    "real_send_evidence": _verify_real_send_evidence,
}


# ── The registry — one entry per declared capability ─────────────────────────
CAPABILITY_REGISTRY: dict[str, CapabilitySpec] = {
    "sales_recovery.winback_send": CapabilitySpec(
        key="sales_recovery.winback_send",
        lane="sales_recovery",
        effect_class="send",
        mode="concierge",
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
        mode="concierge",
        policy_rail=False,                       # advice has no external effect to gate
        summary="Give the owner grounded win-back advice (no send, no mutation).",
        verifier=None,
        rollback=None,
        prerequisites=None,
        environments=KNOWN_ENVS,
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
    if not spec.environments or not spec.environments <= KNOWN_ENVS:
        raise RuntimeError(
            f"capability {spec.key!r}: environments {sorted(spec.environments)} not ⊆ "
            f"{sorted(KNOWN_ENVS)} (or empty)"
        )
    if spec.effect_class in _EFFECTFUL:
        # Thesis 1 — no self-certify: an effect must name a verifier.
        if spec.verifier is None:
            raise RuntimeError(
                f"capability {spec.key!r}: effectful ({spec.effect_class}) but declares no verifier "
                "(no specialist self-certifies an effect)"
            )
        # Thesis 2 — every effect is owner-policy-gated.
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
    unknown env value."""
    return env in resolve(key).environments


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
    "all_capabilities",
    "is_available",
    "requires_policy_rail",
    "resolve",
    "verify",
]
