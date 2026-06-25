"""VT-331 — Team subscription plan resolution (orchestrator-authoritative).

plan_tier -> {razorpay_plan_id, amount_paise}. Prices come from config/plans.yaml
(config, not code -> the gate-no-price-literals CI gate stays clean); the Razorpay plan
IDs come from env (NEEDS-FAZAL; none committed). team-web sends only plan_tier — the
money-authoritative mapping lives HERE, at the service-role layer (Cowork Q1), so a
team-web bug can never create a subscription at the wrong plan or price.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, NamedTuple

import yaml

_CONFIG = Path(__file__).resolve().parents[3] / "config" / "plans.yaml"


class ResolvedPlan(NamedTuple):
    plan_tier: str
    razorpay_plan_id: str
    amount_paise: int
    # VT-424 — billing cycles for razorpay.subscription.create (mandatory). Config-sourced
    # (plans.yaml), defaults to 120 if omitted so a missing field can't 500 the create.
    total_count: int = 120


class UnknownPlanError(ValueError):
    """plan_tier is not defined in plans.yaml."""


class PlanIdNotConfiguredError(RuntimeError):
    """The plan's Razorpay plan-id env var is unset (NEEDS-FAZAL for LIVE)."""


def _plans() -> dict[str, Any]:
    data = yaml.safe_load(_CONFIG.read_text()) or {}
    plans = data.get("plans")
    if not isinstance(plans, dict):
        raise RuntimeError(f"plans.yaml must define a 'plans' mapping; got {type(plans).__name__}")
    return plans


def resolve_plan(plan_tier: str) -> ResolvedPlan:
    """Resolve plan_tier -> (plan_id from env, amount_paise from config). Raises
    UnknownPlanError on a bad tier, PlanIdNotConfiguredError when the plan-id env var
    is unset (the LIVE plan IDs are NEEDS-FAZAL)."""
    spec = _plans().get(plan_tier)
    if spec is None:
        raise UnknownPlanError(f"unknown plan_tier: {plan_tier!r}")
    env_name = spec.get("plan_id_env")
    amount = spec.get("amount_paise")
    if not env_name or amount is None:
        raise UnknownPlanError(f"plan {plan_tier!r} is misconfigured in plans.yaml")
    plan_id = os.environ.get(str(env_name), "")
    if not plan_id:
        raise PlanIdNotConfiguredError(
            f"{env_name} unset — the Razorpay plan ID for {plan_tier!r} is NEEDS-FAZAL (LIVE)"
        )
    total_count = int(spec.get("total_count", 120))  # VT-424 — billing cycles for create
    return ResolvedPlan(plan_tier, plan_id, int(amount), total_count)
