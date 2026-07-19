"""VT-528 (B5) + VT-681 phase 1 — capability registry: contract resolution, the verify gate,
the live/advisory/disabled launch-mode taxonomy, and the fail-closed import-time invariants
(no self-certify + every effect policy-gated + advisory-mode-never-effectful + the
disabled-graduation ratchet)."""

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


def test_disabled_capability_available_nowhere():
    """VT-681 — the D2 ad-boost class: declared so the Manager can decline honestly, available in
    NO environment until the mode flips. VT-685 — compliance.return_filing is the SAME class
    (declared-disabled honesty entry for GST return filing, not yet graduated)."""
    for key in ("marketing.paid_ad_boost", "compliance.return_filing"):
        assert cap.mode_of(key) == "disabled", key
        assert cap.is_available(key, env="dev") is False, key
        assert cap.is_available(key, env="prod") is False, key


def test_launch_roster_modes():
    """The O10 roster labels, registry-backed: live agents live, advisory functions advisory."""
    for key in ("sales_recovery.winback_send", "onboarding.conduct_journey",
                "integration.google_sheet_ingest", "integration.shopify_connect",
                "integration.gst_verify", "manager.customer_list_export"):
        assert cap.mode_of(key) == "live", key
    # VT-685 — compliance.gstr_readiness joins the advisory (prepare-only) set.
    for key in ("marketing.campaign_prepare", "finance.advice", "accounting.prepare",
                "tech.owner_authorized_help", "cost_opt.advice", "compliance.gstr_readiness"):
        assert cap.mode_of(key) == "advisory", key
        assert cap.resolve(key).effect_class == "advisory", key


def test_verify_send_requires_real_transport_receipt():
    assert cap.verify("sales_recovery.winback_send", {"message_sid": "SMabc123"}).ok is True
    # a dev-mock (MKDEV) SID is NOT a real send → must fail (no self-certify by a mocked send)
    assert cap.verify("sales_recovery.winback_send", {"message_sid": "MKDEVdead"}).ok is False
    assert cap.verify("sales_recovery.winback_send", {}).ok is False


def test_advisory_verify_ok_no_effect():
    assert cap.verify("sales_recovery.advice", {}).ok is True


def test_verify_connector_health_evidence():
    ok = cap.verify("integration.google_sheet_ingest",
                    {"connector_id": "google_sheet", "last_status": "ok"})
    assert ok.ok is True
    assert cap.verify("integration.google_sheet_ingest",
                      {"connector_id": "google_sheet", "last_status": "error"}).ok is False
    assert cap.verify("integration.google_sheet_ingest", {}).ok is False


def test_verify_gstin_and_journey_and_export_evidence():
    assert cap.verify("integration.gst_verify", {"verification_status": "gstin_verified"}).ok is True
    assert cap.verify("integration.gst_verify", {"verification_status": "pending"}).ok is False
    assert cap.verify("onboarding.conduct_journey", {"journey_status": "complete"}).ok is True
    assert cap.verify("onboarding.conduct_journey", {"journey_status": "missing"}).ok is False
    good = {"audit_kind": "customer_list_exported", "row_count": 42}
    assert cap.verify("manager.customer_list_export", good).ok is True
    assert cap.verify("manager.customer_list_export",
                      {"audit_kind": "customer_list_exported", "row_count": True}).ok is False
    assert cap.verify("manager.customer_list_export", {"row_count": 42}).ok is False


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
        key="x.y", lane="l", effect_class="advisory", mode="live",
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


def test_invariant_empty_envs_only_for_disabled():
    with pytest.raises(RuntimeError, match="environments empty"):
        _validate(_spec(environments=frozenset()))
    _validate(_spec(mode="disabled", environments=frozenset()))  # legal


def test_invariant_advisory_mode_never_effectful():
    """An advisory-MODE function may not declare a real effect class — that would launder an
    effect past the roster posture."""
    with pytest.raises(RuntimeError, match="advisory"):
        _validate(_spec(mode="advisory", effect_class="send",
                        policy_rail=True, verifier="real_send_evidence"))


def test_invariant_disabled_defers_verifier_but_never_the_rail():
    """The graduation ratchet: a DISABLED effect may omit its verifier (nothing executes), but
    must already carry policy_rail; flipping mode to live without a verifier crashes boot."""
    _validate(_spec(mode="disabled", effect_class="campaign", policy_rail=True,
                    verifier=None, environments=frozenset()))  # legal while disabled
    with pytest.raises(RuntimeError, match="no verifier"):
        _validate(_spec(mode="live", effect_class="campaign", policy_rail=True, verifier=None))
    with pytest.raises(RuntimeError, match="policy_rail"):
        _validate(_spec(mode="disabled", effect_class="campaign", policy_rail=False,
                        verifier=None, environments=frozenset()))


def test_live_registry_all_valid():
    """Every shipped entry passes its own invariants (the boot guard, re-asserted)."""
    keys = frozenset(cap._ACTIVATION_KEYS)
    for spec in cap.CAPABILITY_REGISTRY.values():
        cap._validate_spec(spec, verifiers=cap.VERIFIER_REGISTRY, activation_keys=keys)


# ── VT-681 phase 2 — resolve_for (per-tenant tri-state join) ──────────────────


def test_resolve_for_disabled_short_circuits(monkeypatch):
    r = cap.resolve_for("marketing.paid_ad_boost", "t-1", env="dev", conn=object())
    assert r.available is False
    assert r.mode == "disabled"
    assert "decline honestly" in r.reasons[0]


def test_resolve_for_env_gate():
    r = cap.resolve_for("sales_recovery.winback_send", "t-1", env="staging", conn=object())
    assert r.available is False
    assert "env" in r.reasons[0]


def _stub_gate(monkeypatch, eligible: bool):
    import orchestrator.agents.onboarding_gate as og

    monkeypatch.setattr(og, "is_agent_eligible", lambda tid, agent, *, conn: eligible)


def test_resolve_for_activation_bar_unmet(monkeypatch):
    _stub_gate(monkeypatch, False)
    r = cap.resolve_for("sales_recovery.winback_send", "t-1", env="dev", conn=object())
    assert r.available is False
    assert r.reasons == ("activation bar unmet: 'sales_recovery'",)


def test_resolve_for_activation_error_fails_closed(monkeypatch):
    import orchestrator.agents.onboarding_gate as og

    def _boom(tid, agent, *, conn):
        raise RuntimeError("db down")

    monkeypatch.setattr(og, "is_agent_eligible", _boom)
    r = cap.resolve_for("sales_recovery.winback_send", "t-1", env="dev", conn=object())
    assert r.available is False
    assert "fail-closed" in r.reasons[0]


def test_resolve_for_available_carries_full_reason_trail(monkeypatch):
    """The soft-open entitlement + absent freeze subsystem are VISIBLE reasons on an available
    capability — the named honesty notes, never silently laundered as 'resolved'."""
    _stub_gate(monkeypatch, True)
    r = cap.resolve_for("sales_recovery.winback_send", "t-1", env="dev", conn=object())
    assert r.available is True
    joined = " | ".join(r.reasons)
    assert "activation bar met" in joined
    assert "entitlement" in joined            # soft-open or not-resolvable — either way visible
    assert "freeze/kill" in joined


def test_resolve_for_free_capability_no_prereq_no_module():
    r = cap.resolve_for("finance.advice", "t-1", env="dev", conn=object())
    assert r.available is True
    assert any("free capability" in x for x in r.reasons)


def test_resolve_all_for_covers_every_declared_key(monkeypatch):
    _stub_gate(monkeypatch, True)
    resolved = cap.resolve_all_for("t-1", env="dev", conn=object())
    assert sorted(r.key for r in resolved) == cap.all_capabilities()
    by_key = {r.key: r for r in resolved}
    assert by_key["marketing.paid_ad_boost"].available is False
    assert by_key["finance.advice"].available is True
