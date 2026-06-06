"""VT-330 #5 — plans.yaml price-consistency. Nothing else catches a typo in the plan amounts
(a wrong price can't charge until LIVE — plan-id env is NEEDS-FAZAL — and VT-330 lands before
LIVE, so this is the right home for the guard)."""

from __future__ import annotations

import pytest

# The billing package import chain pulls psycopg → skip in the dep-less smoke (VT-337 lesson);
# runs in the full orchestrator suite. resolve_plan itself is DB-free (plans.yaml + env).
pytest.importorskip("psycopg")

from orchestrator.billing.plans import (  # noqa: E402
    PlanIdNotConfiguredError,
    UnknownPlanError,
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
