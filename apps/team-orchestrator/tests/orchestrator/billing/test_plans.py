"""VT-330 #5 — plans.yaml price-consistency. Nothing else catches a typo in the plan amounts
(a wrong price can't charge until LIVE — plan-id env is NEEDS-FAZAL — and VT-330 lands before
LIVE, so this is the right home for the guard)."""

from __future__ import annotations

import pytest

# The billing package import chain pulls psycopg → skip in the dep-less smoke (VT-337 lesson);
# runs in the full orchestrator suite. resolve_plan itself is DB-free (plans.yaml + env).
pytest.importorskip("psycopg")

import orchestrator.billing.plans as plans_mod  # noqa: E402
from orchestrator.billing.plans import (  # noqa: E402
    PlanIdNotConfiguredError,
    TierNotOfferedError,
    UnknownPlanError,
    assert_tier_offered,
    offered_tiers,
    resolve_plan,
)


@pytest.fixture(autouse=True)
def _plan_id_env(monkeypatch):
    # resolve_plan reads the Razorpay plan-id env (NEEDS-FAZAL for LIVE); set test values so
    # the amount-consistency assertions aren't masked by PlanIdNotConfiguredError.
    monkeypatch.setenv("FOUNDING_RZP_PLAN_ID", "plan_test_founding")
    monkeypatch.setenv("STANDARD_RZP_PLAN_ID", "plan_test_standard")
    monkeypatch.setenv("PRO_RZP_PLAN_ID", "plan_test_pro")


@pytest.mark.parametrize(
    ("tier", "amount_paise"),
    [("founding", 249900), ("standard", 499900), ("pro", 1499900)],
)
def test_resolve_plan_amounts(tier, amount_paise) -> None:
    plan = resolve_plan(tier)
    assert plan.plan_tier == tier
    assert plan.amount_paise == amount_paise  # the canonical price (Pillar 7 — from config)


def test_resolve_plan_unknown_tier() -> None:
    with pytest.raises(UnknownPlanError):
        resolve_plan("enterprise")


def test_resolve_plan_unconfigured_plan_id(monkeypatch) -> None:
    monkeypatch.delenv("FOUNDING_RZP_PLAN_ID", raising=False)
    with pytest.raises(PlanIdNotConfiguredError):
        resolve_plan("founding")


# --- VT-429 — offered_tiers launch-gate allowlist (fail-closed) ---------------------------------
def test_offered_tiers_launch_config_is_standard_only() -> None:
    """The committed plans.yaml offers STANDARD ONLY at launch; founding + pro are DEFINED but
    NOT offered."""
    assert offered_tiers() == frozenset({"standard"})
    # The non-offered tiers are still real plans (resolve_plan works) — just not offered.
    assert resolve_plan("founding").plan_tier == "founding"
    assert resolve_plan("pro").plan_tier == "pro"


def test_assert_tier_offered_passes_for_standard() -> None:
    assert_tier_offered("standard")  # offered → no raise


@pytest.mark.parametrize("tier", ["founding", "pro", "enterprise"])
def test_assert_tier_offered_rejects_non_offered(tier) -> None:
    """A defined-but-not-offered tier (founding/pro) AND an unknown tier (enterprise) both raise —
    the gate is an allowlist, not a denylist."""
    with pytest.raises(TierNotOfferedError):
        assert_tier_offered(tier)


def test_offered_tiers_absent_config_defaults_to_deny(monkeypatch) -> None:
    """FAIL-CLOSED: an ABSENT offered_tiers key → empty set (offer nothing), NEVER offer-all."""
    monkeypatch.setattr(plans_mod, "_config", lambda: {"plans": {"standard": {}}})
    assert offered_tiers() == frozenset()
    with pytest.raises(TierNotOfferedError):
        assert_tier_offered("standard")  # even standard is denied when the config is absent


def test_offered_tiers_empty_list_defaults_to_deny(monkeypatch) -> None:
    """FAIL-CLOSED: an EMPTY offered_tiers list → empty set, every tier denied."""
    monkeypatch.setattr(plans_mod, "_config", lambda: {"offered_tiers": []})
    assert offered_tiers() == frozenset()
    with pytest.raises(TierNotOfferedError):
        assert_tier_offered("standard")


def test_offered_tiers_malformed_config_defaults_to_deny(monkeypatch) -> None:
    """FAIL-CLOSED: a non-list offered_tiers (e.g. a bare string) → offer nothing, never widen."""
    monkeypatch.setattr(plans_mod, "_config", lambda: {"offered_tiers": "standard"})
    assert offered_tiers() == frozenset()
    with pytest.raises(TierNotOfferedError):
        assert_tier_offered("standard")
