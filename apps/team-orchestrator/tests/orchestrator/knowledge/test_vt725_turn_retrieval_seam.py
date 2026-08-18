"""VT-725 scope 1 + 5 — the retrieval seam: dark by default, shadow-only, fail-soft, and wired.

The engine has worked since VT-723 and had ZERO callers in `src/`. These tests cover the seam that
gives it callers, and they are deliberately built around the two ways this could go wrong quietly:

1. **It lands dark and stays dark by accident** — so the shadow-path tests assert the exact kwargs
   reaching the engine, not merely that something was called.
2. **It lands wired and nobody notices it never runs** — so the wiring is pinned at both call sites.
   That pin proves the CALL EXISTS in the dispatch path; it cannot prove a real turn produces a
   trace. That is exit gate (a) and it needs deployed dev with `TEAM_KNOWLEDGE_SERVING=shadow`,
   which waits on VT-749 scope 1 scoping the 63 unscoped cards.
"""

from __future__ import annotations

import inspect
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.knowledge import card_serving, turn_retrieval  # noqa: E402


@pytest.fixture
def shadow(monkeypatch):
    monkeypatch.setenv("TEAM_KNOWLEDGE_SERVING", "shadow")


@pytest.fixture
def calls(monkeypatch):
    """Capture what reaches the engine, and return a content-free stand-in for its result."""
    captured: list[dict] = []

    class _Result:
        candidates = 3
        conflicts = 0
        degraded_reason = None
        evidence_links_written = 3
        elapsed_ms = 12.5
        selected_card_refs = ("ref-a", "ref-b")

    def _fake(**kwargs):
        captured.append(kwargs)
        return _Result()

    monkeypatch.setattr(card_serving, "retrieve_cards_for_turn", _fake)
    return captured


def _recorder(seen: list[dict]):
    """A stand-in that RECORDS instead of raising.

    A raising stand-in is useless against this seam and the first draft of these tests used one: the
    fail-soft `except Exception` catches the tripwire's own AssertionError and returns None, so the
    test passed even with the serving gate forced open. Proven by forcing it. Never assert
    "unreachable" with an exception against a function whose contract is to swallow exceptions.
    """

    def _fake(**kwargs):
        seen.append(kwargs)
        return None

    return _fake


# --- dark by default ----------------------------------------------------------------------


def test_dark_by_default_never_reaches_the_engine(monkeypatch):
    """With `TEAM_KNOWLEDGE_SERVING` unset the seam costs one env read and touches no database.

    This is what makes it safe to wire the call sites BEFORE VT-749 scopes the corpus: retrieval
    writes `decision_evidence_links` on every call, and collecting the first causality evidence we
    have while 63 of 100 eligible cards match every context would manufacture a baseline we would
    then have to distrust.
    """
    seen: list[dict] = []
    monkeypatch.delenv("TEAM_KNOWLEDGE_SERVING", raising=False)
    monkeypatch.setattr(card_serving, "retrieve_cards_for_turn", _recorder(seen))

    assert turn_retrieval.retrieve_for_manager_turn(
        tenant_id=uuid4(), run_id=uuid4(), objective="win back dormant customers"
    ) is None
    assert turn_retrieval.retrieve_for_specialist(
        tenant_id=uuid4(),
        run_id=uuid4(),
        identity="sales_recovery_agent",
        objective="win back dormant customers",
    ) is None
    assert seen == [], "the engine must not be reached at all while serving is off"


def test_off_is_the_default_for_any_unrecognised_value(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setenv("TEAM_KNOWLEDGE_SERVING", "active")  # deliberately unreachable
    monkeypatch.setattr(card_serving, "retrieve_cards_for_turn", _recorder(seen))

    assert turn_retrieval.retrieve_for_manager_turn(
        tenant_id=uuid4(), run_id=uuid4(), objective="anything"
    ) is None
    assert seen == []


# --- the shadow path ----------------------------------------------------------------------


def test_manager_retrieval_declares_the_manager_identity_and_planning_stage(shadow, calls):
    run_id, tenant_id = uuid4(), uuid4()

    result = turn_retrieval.retrieve_for_manager_turn(
        tenant_id=tenant_id, run_id=run_id, objective="cashflow is tight this month",
        message_ref="SM0000000000000000000000000000abcd",
    )

    assert result is not None
    assert len(calls) == 1
    kw = calls[0]
    assert kw["identity"] == "team_manager"
    assert kw["stage"].value == "planning"
    assert kw["domain"].value == "management"
    assert kw["objective"] == "cashflow is tight this month"
    assert kw["decision_id"] == "manager_turn:SM0000000000000000000000000000abcd"
    assert kw["tenant_id"] == tenant_id
    assert kw["run_id"] == run_id


def test_specialist_retrieval_declares_its_own_identity_and_specialist_stage(shadow, calls):
    turn_retrieval.retrieve_for_specialist(
        tenant_id=uuid4(),
        run_id=uuid4(),
        identity="sales_recovery_agent",
        objective="win back the dormant cohort",
        task_ref="step-7",
    )

    kw = calls[0]
    assert kw["identity"] == "sales_recovery_agent"
    assert kw["stage"].value == "specialist"
    assert kw["domain"].value == "sales", "the lane's own domain, not the Manager's"
    assert kw["decision_id"] == "specialist:sales_recovery_agent:step-7"


def test_an_undeclared_identity_retrieves_nothing_rather_than_the_managers_breadth(shadow, calls):
    """`primary_domain_for` raises for an unknown identity and the seam degrades to None. A silent
    inheritance of the Manager's all-domains profile is the failure this refuses."""
    assert turn_retrieval.retrieve_for_specialist(
        tenant_id=uuid4(), run_id=uuid4(), identity="not_a_specialist", objective="anything"
    ) is None
    assert calls == [], "an undeclared identity must not reach the engine at all"


def test_an_empty_objective_never_retrieves(shadow, calls):
    """Retrieving against an empty string scores every card on recency and confidence alone and
    then calls the result relevance."""
    for objective in ("", "   ", None):
        assert turn_retrieval.retrieve_for_manager_turn(
            tenant_id=uuid4(), run_id=uuid4(), objective=objective or ""
        ) is None
    assert calls == []


# --- fail-soft ----------------------------------------------------------------------------


def test_any_engine_failure_degrades_to_none(shadow, monkeypatch):
    """The Manager worked without cards for months and must still."""

    def _boom(**kwargs):
        raise RuntimeError("embedding provider down")

    monkeypatch.setattr(card_serving, "retrieve_cards_for_turn", _boom)

    assert turn_retrieval.retrieve_for_manager_turn(
        tenant_id=uuid4(), run_id=uuid4(), objective="anything"
    ) is None


def test_injection_tripwire_refuses_to_serve(shadow, monkeypatch):
    """Shadow's safety argument is that a served result CANNOT reach a prompt. If that ever stops
    being true, the seam stops serving instead of quietly becoming the injection path."""
    reached: list[int] = []
    monkeypatch.setattr(
        card_serving, "retrieve_cards_for_turn", lambda **kw: reached.append(1)
    )
    monkeypatch.setattr(card_serving.CardServingResult, "INJECTS_INTO_PROMPT", True)

    assert turn_retrieval.retrieve_for_manager_turn(
        tenant_id=uuid4(), run_id=uuid4(), objective="anything"
    ) is None
    assert reached == [], "the tripwire must fire BEFORE the engine is asked for anything"


def test_the_shipped_result_is_not_injectable():
    """The tripwire above is only meaningful if the real value is False today."""
    assert card_serving.CardServingResult.INJECTS_INTO_PROMPT is False
    assert card_serving.CardServingResult.AUTHORIZES_EFFECTS is False


# --- attribution keys ---------------------------------------------------------------------


def test_decision_ids_are_stable_across_a_replay():
    """Migration 183's uniqueness is (tenant, run, decision, card, disposition) — that is what makes
    a replayed DBOS step idempotent instead of a double-count in the ablation data. A fresh uuid per
    call would silently turn every retry into new evidence."""
    run_id = uuid4()
    assert turn_retrieval.manager_turn_decision_id(run_id, "SMabc") == (
        turn_retrieval.manager_turn_decision_id(run_id, "SMabc")
    )
    assert turn_retrieval.manager_turn_decision_id(run_id, None) == f"manager_turn:{run_id}"
    assert turn_retrieval.specialist_decision_id("integration_agent", "step-1") == (
        turn_retrieval.specialist_decision_id("integration_agent", "step-1")
    )


# --- the two name sets that must not drift apart ------------------------------------------


def test_plan_specialist_names_and_retrieval_profiles_are_one_set():
    """A specialist named in the plan models but absent from the retrieval profiles would make
    `retrieve_for_specialist` return None for that lane forever, and nothing would say so."""
    from orchestrator.agent_framework.retrieval_profiles import (
        SPECIALIST_PRIMARY_DOMAIN,
        SPECIALIST_RETRIEVAL_PROFILES,
    )
    from orchestrator.manager.plan_models import _SPECIALISTS

    assert set(_SPECIALISTS) == set(SPECIALIST_RETRIEVAL_PROFILES)
    assert set(_SPECIALISTS) == set(SPECIALIST_PRIMARY_DOMAIN)


def test_every_primary_domain_is_one_the_profile_declares():
    from orchestrator.agent_framework.retrieval_profiles import (
        MANAGER_PRIMARY_DOMAIN,
        MANAGER_RETRIEVAL_PROFILE,
        SPECIALIST_PRIMARY_DOMAIN,
        SPECIALIST_RETRIEVAL_PROFILES,
    )

    assert MANAGER_PRIMARY_DOMAIN in MANAGER_RETRIEVAL_PROFILE.domains
    for identity, domain in SPECIALIST_PRIMARY_DOMAIN.items():
        assert domain in SPECIALIST_RETRIEVAL_PROFILES[identity].domains


# --- the wiring pin -----------------------------------------------------------------------


def test_both_call_sites_are_wired():
    """The engine's defect was never a bug in the engine — it was that nothing called it.

    The first version of this test grepped ``dispatch_brain`` for the call. It passed for the whole
    life of the feature while the corpus was consulted exactly never on dev, because
    ``dispatch_brain`` is the router that enforce mode's ``skip_legacy_dispatch`` skips. Pinning a
    call inside a function says nothing about whether that function is on the live path.

    So the pin moved with the call: the Manager-side retrieval belongs on the per-turn path in
    ``runner``, ahead of the router branch, where no mode can route around it.
    """
    import inspect

    from orchestrator import runner
    from orchestrator.manager import workflow

    src = inspect.getsource(runner)
    assert "retrieve_for_manager_turn(" in src, (
        "the Manager's per-turn path must call the retrieval seam"
    )
    assert "retrieve_for_specialist(" in inspect.getsource(workflow), (
        "specialist dispatch must call the retrieval seam"
    )


def test_manager_retrieval_is_not_behind_the_enforce_branch():
    """enforce mode skips ``dispatch_brain``; the corpus call must not be skipped with it.

    Structural, because the alternative is a live turn: the call must appear BEFORE the
    ``if not skip_legacy_dispatch:`` branch in the source, so both routers traverse it.
    """
    import inspect

    from orchestrator import runner

    src = inspect.getsource(runner)
    call_at = src.index("retrieve_for_manager_turn(\n")
    branch_at = src.index("if not skip_legacy_dispatch:")
    assert call_at < branch_at, (
        "retrieve_for_manager_turn must run before the skip_legacy_dispatch branch — behind it, "
        "enforce mode (dev's mode) never consults the card corpus at all"
    )
