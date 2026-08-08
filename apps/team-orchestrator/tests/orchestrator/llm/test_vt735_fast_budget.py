"""VT-735 — the per-tenant daily Fast budget actually binds, and fails on the right side.

The hook existed before this and nothing supplied it, so Fast was unbounded while the policy said
it was capped. These tests pin the three properties that make the cap real without making it
dangerous: it degrades to STANDARD (never Flex), it fails OPEN on a broken store, and it is not
re-queried on every call.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.llm import fast_budget  # noqa: E402
from orchestrator.llm.service_tier_policy import resolve_service_tier  # noqa: E402

FAST_SITE = "classify_owner_message"
BACKGROUND_SITE = "dispatch_brain"


@pytest.fixture(autouse=True)
def _clear_cache():
    fast_budget.reset_cache()
    yield
    fast_budget.reset_cache()


def test_a_tenant_within_budget_still_gets_fast() -> None:
    tier = resolve_service_tier(FAST_SITE, tenant_id=uuid4(), fast_budget_check=lambda _t: True)
    assert tier == "fast"


def test_an_exhausted_budget_degrades_to_standard_and_never_to_flex(monkeypatch) -> None:
    """The policy is explicit: a decisive moment never gets the slow tier.

    Asserted with TEAM_GPT_FLEX=all — the setting that makes flex the answer for everything else —
    so this proves the degrade target is chosen, not merely the default that happened to apply.
    """

    monkeypatch.setenv("TEAM_GPT_FLEX", "all")
    assert resolve_service_tier(BACKGROUND_SITE, tenant_id=uuid4()) == "flex"

    tier = resolve_service_tier(FAST_SITE, tenant_id=uuid4(), fast_budget_check=lambda _t: False)
    assert tier == "standard"


def test_a_broken_budget_store_fails_OPEN_rather_than_slowing_the_approval_path() -> None:
    """A cost control must never become a correctness risk (VT-734 duplicate-request race)."""

    def exploding(_tenant):
        raise RuntimeError("budget store is down")

    assert resolve_service_tier(FAST_SITE, tenant_id=uuid4(), fast_budget_check=exploding) == "fast"


def test_a_missing_budget_store_is_not_silently_a_zero_budget() -> None:
    """`None` means "not wired", which must read as allowed — the opposite would have made every
    Fast call degrade the moment someone forgot to pass the hook."""

    assert resolve_service_tier(FAST_SITE, tenant_id=uuid4(), fast_budget_check=None) == "fast"


def test_the_count_is_cached_so_the_safety_path_does_not_pay_a_query_per_call(monkeypatch) -> None:
    tenant = uuid4()
    calls: list[str] = []

    def fake_read(tenant_id):
        calls.append(str(tenant_id))
        return 0, 50

    monkeypatch.setattr(fast_budget, "_read", fake_read)
    for _ in range(5):
        assert fast_budget.fast_budget_check(tenant) is True
    assert len(calls) == 1, "the budget was re-read on a cached call"

    fast_budget.reset_cache()
    assert fast_budget.fast_budget_check(tenant) is True
    assert len(calls) == 2, "reset_cache must force a fresh read"


def test_exhaustion_is_decided_by_used_vs_limit_and_raises_the_vtr_flag(monkeypatch) -> None:
    flagged: list[tuple[int, int]] = []
    monkeypatch.setattr(
        fast_budget, "_flag_on_vtr",
        lambda tenant_id, *, used, limit: flagged.append((used, limit)),
    )

    monkeypatch.setattr(fast_budget, "_read", lambda _t: (49, 50))
    assert fast_budget.fast_budget_check(uuid4()) is True
    assert flagged == [], "a tenant one call under the cap must not be flagged"

    fast_budget.reset_cache()
    monkeypatch.setattr(fast_budget, "_read", lambda _t: (50, 50))
    assert fast_budget.fast_budget_check(uuid4()) is False
    assert flagged == [(50, 50)], "hitting the cap must flag exactly once with the real numbers"


def test_a_zero_limit_disables_fast_for_that_tenant(monkeypatch) -> None:
    """0 is a meaningful setting, not 'unset' — the migration's CHECK allows it deliberately."""

    monkeypatch.setattr(fast_budget, "_flag_on_vtr", lambda *a, **k: None)
    monkeypatch.setattr(fast_budget, "_read", lambda _t: (0, 0))
    assert fast_budget.fast_budget_check(uuid4()) is False


def test_a_tenantless_platform_call_is_not_charged_to_anybody_s_budget() -> None:
    assert fast_budget.fast_budget_check(None) is True


def test_a_vtr_flag_failure_never_breaks_the_degrade_it_is_reporting(monkeypatch) -> None:
    def exploding(*_a, **_k):
        raise RuntimeError("alerts are down")

    monkeypatch.setattr(fast_budget, "_read", lambda _t: (99, 50))
    monkeypatch.setattr("orchestrator.alerts.dispatch.dispatch_alert", exploding)
    # The budget answer still lands; the alert failure is swallowed inside _flag_on_vtr.
    assert fast_budget.fast_budget_check(uuid4()) is False


def test_the_env_default_is_used_when_a_tenant_declares_no_limit(monkeypatch) -> None:
    monkeypatch.setenv("TEAM_FAST_CALLS_PER_DAY", "7")
    assert fast_budget._env_default() == 7
    monkeypatch.setenv("TEAM_FAST_CALLS_PER_DAY", "not-a-number")
    assert fast_budget._env_default() == fast_budget.DEFAULT_MAX_FAST_CALLS_DAY
    monkeypatch.delenv("TEAM_FAST_CALLS_PER_DAY")
    assert fast_budget._env_default() == fast_budget.DEFAULT_MAX_FAST_CALLS_DAY
