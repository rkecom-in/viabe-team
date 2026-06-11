"""VT-374 — run-control registry sanity (dep-less; plan §4/F14, contract §registry).

Imports the REAL registry package — ``orchestrator.run_control`` (and ``registry`` /
``gate_manifest``) is stdlib-only at import time by contract, so this file runs in the
dep-less CI smoke. The import ITSELF is the F14 proof: ``registry.py`` raises
``RuntimeError`` at import when any controllable entry maps to a gate-manifest module,
so collection succeeding means no send/consent surface is registered controllable.
The tests below pin the v1 shape so a drift (a new pure_return entry, a widened
allowed_keys set, a kind escaping the rerun policy) fails HERE, not in review.
"""

from __future__ import annotations

from orchestrator.run_control.gate_manifest import GATE_MODULES
from orchestrator.run_control.registry import (
    KIND_RERUN_POLICY,
    REGISTRY,
    RERUNNABLE,
    STEP_IMPL_MODULES,
    WORKFLOW_KINDS,
)

# I8/F11 — kinds whose side-effect inventory (STEP-0 §3.3) forbids re-dispatch.
_FORBIDDEN_KINDS = ("webhook_inbound", "trial_sweep", "campaign_send")


def test_import_proves_no_controllable_entry_in_gate_manifest():
    """F14: the registry import already raises on a gate-module registration; assert it
    explicitly anyway so the invariant survives a refactor of the import-time check."""
    for key, entry in REGISTRY.items():
        if entry.tier == "controllable":
            assert STEP_IMPL_MODULES[key] not in GATE_MODULES, (
                f"controllable step {key!r} maps to gate module {STEP_IMPL_MODULES[key]!r} "
                "— send/consent/approval surfaces are structurally non-controllable"
            )


def test_every_entry_has_an_impl_module_and_known_kind():
    assert set(STEP_IMPL_MODULES) == set(REGISTRY)
    for (kind, step), entry in REGISTRY.items():
        assert kind in WORKFLOW_KINDS, f"({kind}, {step}) uses an unknown workflow_kind"
        assert entry.tier in ("controllable", "observed")


def test_pause_only_dispatch_brain_has_no_override_surface():
    """N3: the pre-dispatch_brain boundary is pause-ONLY — empty allowed_keys + no
    pure_return means no override write can ever validate for it."""
    entry = REGISTRY[("webhook_inbound", "dispatch_brain")]
    assert entry.tier == "controllable"
    assert entry.allowed_keys == frozenset()
    assert entry.pure_return is False


def test_question_brain_compose_demoted_observed_pause_deny():
    """STEP-0 demotion: the owner-inbound hot path (fail-open except at journey.py) is
    observed-tier and pause-denied — a hold there would stall or silently fall through."""
    entry = REGISTRY[("webhook_inbound", "question_brain_compose")]
    assert entry.tier == "observed"
    assert entry.pause_deny is True
    assert entry.allowed_keys == frozenset()


def test_v1_registry_has_zero_pure_return_entries():
    """Contract pin: pinned_output is legal ONLY for pure_return steps and v1 registers
    NONE — every pinned_output write must therefore 422 at the API."""
    assert not [k for k, e in REGISTRY.items() if e.pure_return], (
        "v1 must register zero pure_return steps"
    )


def test_allowed_keys_are_exactly_the_contract_pins():
    """I7: the full allow-list surface, pinned exactly. Only 3 steps carry keys, all
    config/ID-class — adding a key (or a step with keys) is a deliberate contract edit."""
    with_keys = {key: entry.allowed_keys for key, entry in REGISTRY.items() if entry.allowed_keys}
    assert with_keys == {
        ("agent_dispatch", "candidate_build"): frozenset({"limit"}),
        ("agent_dispatch", "compose_drafts"): frozenset({"model"}),
        ("auto_discovery", "source_fetch"): frozenset({"skip_sources"}),
    }, f"allowed_keys drifted: {with_keys}"


def test_rerun_policy_covers_every_kind_in_registry():
    """A registered kind without a rerun policy would make /rerun's refusal arm
    undefined — coverage is total, and the policy keys are exactly WORKFLOW_KINDS."""
    assert {kind for kind, _ in REGISTRY} <= set(KIND_RERUN_POLICY)
    assert set(KIND_RERUN_POLICY) == set(WORKFLOW_KINDS)
    assert set(KIND_RERUN_POLICY.values()) <= {"reuse", "re-emit", "forbidden"}


def test_forbidden_kinds_are_forbidden_and_not_rerunnable():
    """I8/F11: webhook_inbound (MessageSid ledger), trial_sweep (warn-path has no send
    ledger), campaign_send (KG outbox dup) refuse re-dispatch; RERUNNABLE is exactly
    the complement."""
    for kind in _FORBIDDEN_KINDS:
        assert KIND_RERUN_POLICY[kind] == "forbidden", f"{kind} must be forbidden"
        assert kind not in RERUNNABLE
    assert RERUNNABLE == frozenset(
        kind for kind, policy in KIND_RERUN_POLICY.items() if policy != "forbidden"
    )
