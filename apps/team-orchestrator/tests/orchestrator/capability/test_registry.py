"""VT-528 (B5) — capability registry: contract resolution, the verify gate, and the
fail-closed import-time invariants (no self-certify + every effect policy-gated)."""

from __future__ import annotations

import pytest

from orchestrator.capability import registry as cap


def test_resolve_known_and_unknown():
    spec = cap.resolve("sales_recovery.winback_send")
    assert spec.effect_class == "send"
    assert spec.policy_rail is True
    assert spec.verifier == "real_send_evidence"
    with pytest.raises(KeyError):
        cap.resolve("nope.nope")


def test_is_available_env_gating():
    assert cap.is_available("sales_recovery.winback_send", env="dev") is True
    assert cap.is_available("sales_recovery.winback_send", env="prod") is True
    assert cap.is_available("sales_recovery.winback_send", env="staging") is False


def test_verify_send_requires_real_transport_receipt():
    assert cap.verify("sales_recovery.winback_send", {"message_sid": "SMabc123"}).ok is True
    # a dev-mock (MKDEV) SID is NOT a real send → must fail (no self-certify by a mocked send)
    assert cap.verify("sales_recovery.winback_send", {"message_sid": "MKDEVdead"}).ok is False
    assert cap.verify("sales_recovery.winback_send", {}).ok is False


def test_advisory_verify_ok_no_effect():
    assert cap.verify("sales_recovery.advice", {}).ok is True


def test_requires_policy_rail():
    assert cap.requires_policy_rail("sales_recovery.winback_send") is True
    assert cap.requires_policy_rail("sales_recovery.advice") is False


def test_all_capabilities_declared():
    caps = cap.all_capabilities()
    assert "sales_recovery.winback_send" in caps
    assert "sales_recovery.advice" in caps


# ── Import-time invariants (exercised via _validate_spec with crafted bad specs) ──
def _spec(**over) -> cap.CapabilitySpec:
    base = dict(
        key="x.y", lane="l", effect_class="advisory", mode="concierge",
        policy_rail=False, summary="s", verifier=None, rollback=None,
        prerequisites=None, environments=cap.KNOWN_ENVS,
    )
    base.update(over)
    return cap.CapabilitySpec(**base)


def _validate(spec: cap.CapabilitySpec) -> None:
    cap._validate_spec(
        spec, verifiers=cap.VERIFIER_REGISTRY, activation_keys=frozenset({"sales_recovery"})
    )


def test_invariant_effectful_needs_verifier():
    with pytest.raises(RuntimeError, match="no verifier"):
        _validate(_spec(effect_class="send", policy_rail=True, verifier=None))


def test_invariant_effectful_needs_policy_rail():
    with pytest.raises(RuntimeError, match="policy_rail"):
        _validate(_spec(effect_class="send", policy_rail=False, verifier="real_send_evidence"))


def test_invariant_unknown_verifier():
    with pytest.raises(RuntimeError, match="not in VERIFIER_REGISTRY"):
        _validate(_spec(effect_class="advisory", verifier="ghost"))


def test_invariant_unknown_prerequisite():
    with pytest.raises(RuntimeError, match="not an activation key"):
        _validate(_spec(prerequisites="not_an_agent"))


def test_invariant_bad_env():
    with pytest.raises(RuntimeError, match="environments"):
        _validate(_spec(environments=frozenset({"dev", "moon"})))


def test_live_registry_all_valid():
    """Every shipped entry passes its own invariants (the boot guard, re-asserted)."""
    keys = frozenset(cap._ACTIVATION_KEYS)
    for spec in cap.CAPABILITY_REGISTRY.values():
        cap._validate_spec(spec, verifiers=cap.VERIFIER_REGISTRY, activation_keys=keys)
