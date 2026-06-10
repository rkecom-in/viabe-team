"""VT-366 Gap-2a — unit tests for the Auto-Discovery Engine (``auto_discovery_run``).

The engine is exercised with INJECTED fake sources (plain callables returning ``SourceResult``s),
so no real source / network / DB is touched. The observability ``log_event`` is monkeypatched to a
spy — the engine records cost best-effort, and we both keep it hermetic and assert it fired.

Guards:
- ``pytest.importorskip("psycopg")`` — engine pulls the source adapters → draft_profile → psycopg.
- ``pytest.importorskip("dbos")`` — ``auto_discovery`` does ``from dbos import DBOS`` at module top
  (for the @workflow wrapper); the dep-less smoke job may not have dbos installed.

We test ``auto_discovery_run`` (plain), NOT ``auto_discovery_workflow`` (the DBOS wrapper).
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

from orchestrator.onboarding.auto_discovery import auto_discovery_run
from orchestrator.onboarding.auto_discovery_sources import SourceResult

TENANT = uuid.uuid4()


@pytest.fixture(autouse=True)
def cost_spy(monkeypatch):
    """Stub the observability sink so the best-effort cost record stays in-memory.

    ``_record_cost`` does a local ``from orchestrator.observability.log import log_event``, so we
    patch the attribute on that module. Returns the recorded-call list for optional assertion.
    """
    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)

    log_mod = pytest.importorskip("orchestrator.observability.log")
    monkeypatch.setattr(log_mod, "log_event", _spy)
    return calls


def _named(name):
    """Build a fake ``discover_*`` source with a real ``__name__`` (the engine derives the summary
    key from ``fn.__name__`` minus ``discover_``)."""

    def deco(fn):
        fn.__name__ = name
        return fn

    return deco


# --------------------------------------------------------------- GBP → website chain


def test_gbp_website_chain_threads_website_into_seed():
    seen_seed: dict = {}

    @_named("discover_gbp")
    def fake_gbp(tenant_id, seed):
        return SourceResult("gbp", "ok", cost_usd=0.004, website="https://found.example")

    @_named("discover_website")
    def fake_website(tenant_id, seed):
        # the engine must have injected the GBP-discovered website into the seed before calling us
        seen_seed["website"] = seed.get("website")
        return SourceResult("website", "ok", cost_usd=0.001)

    out = auto_discovery_run(
        TENANT, {"business_name": "Sharma Sweets"}, sources=[fake_gbp, fake_website]
    )

    assert seen_seed["website"] == "https://found.example"
    assert out["sources"] == {"gbp": "ok", "website": "ok"}


def test_chain_does_not_clobber_seed_supplied_website():
    seen_seed: dict = {}

    @_named("discover_gbp")
    def fake_gbp(tenant_id, seed):
        return SourceResult("gbp", "ok", cost_usd=0.004, website="https://gbp.example")

    @_named("discover_website")
    def fake_website(tenant_id, seed):
        seen_seed["website"] = seed.get("website")
        return SourceResult("website", "ok", cost_usd=0.001)

    auto_discovery_run(
        TENANT,
        {"business_name": "Sharma Sweets", "website": "https://owner.example"},
        sources=[fake_gbp, fake_website],
    )
    # seed already had a website → GBP's must NOT overwrite it
    assert seen_seed["website"] == "https://owner.example"


def test_engine_does_not_mutate_caller_seed():
    @_named("discover_gbp")
    def fake_gbp(tenant_id, seed):
        return SourceResult("gbp", "ok", cost_usd=0.0, website="https://found.example")

    original = {"business_name": "Sharma Sweets"}
    auto_discovery_run(TENANT, original, sources=[fake_gbp])
    # the engine copies the seed locally; the chain mutation must not leak back to the caller
    assert "website" not in original


# ------------------------------------------------------------------------ fail-soft


def test_failsoft_source_raising_marks_error_and_run_continues():
    ran: list[str] = []

    @_named("discover_gbp")
    def raising(tenant_id, seed):
        ran.append("gbp")
        raise RuntimeError("apify 500")

    @_named("discover_website")
    def later(tenant_id, seed):
        ran.append("website")
        return SourceResult("website", "ok", cost_usd=0.001)

    out = auto_discovery_run(TENANT, {"business_name": "x"}, sources=[raising, later])

    assert ran == ["gbp", "website"]  # the later source still ran
    assert out["sources"]["gbp"] == "error"
    assert out["sources"]["website"] == "ok"
    assert out["aborted"] is False
    # the raising source contributed no cost
    assert out["spent_usd"] == 0.001


def test_failsoft_raising_source_contributes_no_cost():
    @_named("discover_gbp")
    def raising(tenant_id, seed):
        raise ValueError("boom")

    out = auto_discovery_run(TENANT, {"business_name": "x"}, sources=[raising])
    assert out["sources"] == {"gbp": "error"}
    assert out["spent_usd"] == 0.0
    assert out["aborted"] is False


# --------------------------------------------------------------------- cost ceiling


def test_cost_ceiling_aborts_before_next_source():
    ran: list[str] = []

    @_named("discover_gbp")
    def pricey_a(tenant_id, seed):
        ran.append("gbp")
        return SourceResult("gbp", "ok", cost_usd=0.010)

    @_named("discover_website")
    def pricey_b(tenant_id, seed):
        ran.append("website")
        return SourceResult("website", "ok", cost_usd=0.010)  # cumulative 0.020 > 0.018

    @_named("discover_serper")
    def should_not_run(tenant_id, seed):
        ran.append("serper")
        return SourceResult("serper", "ok", cost_usd=0.010)

    out = auto_discovery_run(
        TENANT, {"business_name": "x"}, sources=[pricey_a, pricey_b, should_not_run]
    )

    # third source is gated out — spent (0.020) exceeds the 0.018 ceiling before it runs
    assert ran == ["gbp", "website"]
    assert "serper" not in out["sources"]
    assert out["aborted"] is True
    assert out["spent_usd"] == 0.020


def test_under_ceiling_does_not_abort():
    @_named("discover_gbp")
    def a(tenant_id, seed):
        return SourceResult("gbp", "ok", cost_usd=0.004)

    @_named("discover_website")
    def b(tenant_id, seed):
        return SourceResult("website", "ok", cost_usd=0.001)

    out = auto_discovery_run(TENANT, {"business_name": "x"}, sources=[a, b])
    assert out["aborted"] is False
    assert out["spent_usd"] == 0.005
    assert out["sources"] == {"gbp": "ok", "website": "ok"}


def test_ceiling_checked_before_source_not_after():
    """The breaker trips on the NEXT iteration: a single source that alone exceeds the ceiling
    still runs (cost is checked BEFORE each source), but a following source is blocked."""
    ran: list[str] = []

    @_named("discover_gbp")
    def huge(tenant_id, seed):
        ran.append("gbp")
        return SourceResult("gbp", "ok", cost_usd=0.050)  # alone over ceiling

    @_named("discover_website")
    def after(tenant_id, seed):
        ran.append("website")
        return SourceResult("website", "ok", cost_usd=0.001)

    out = auto_discovery_run(TENANT, {"business_name": "x"}, sources=[huge, after])
    assert ran == ["gbp"]  # huge ran (no pre-cost), after was gated
    assert out["aborted"] is True
    assert out["spent_usd"] == 0.050


# ------------------------------------------------------------------- summary shape


def test_summary_shape_and_rounding(cost_spy):
    @_named("discover_gbp")
    def a(tenant_id, seed):
        return SourceResult("gbp", "ok", cost_usd=0.0011115)

    @_named("discover_website")
    def b(tenant_id, seed):
        return SourceResult("website", "empty", cost_usd=0.0)

    out = auto_discovery_run(TENANT, {"business_name": "x"}, sources=[a, b])

    assert set(out.keys()) == {"tenant_id", "spent_usd", "aborted", "sources"}
    assert out["tenant_id"] == str(TENANT)  # stringified
    assert out["spent_usd"] == round(0.0011115, 4)  # rounded to 4dp
    assert out["aborted"] is False
    assert out["sources"] == {"gbp": "ok", "website": "empty"}
    # the engine recorded cost to observability (best-effort) exactly once
    assert len(cost_spy) == 1
    assert cost_spy[0]["event_type"] == "auto_discovery_cost"
    assert cost_spy[0]["payload"]["cost_usd"] == round(0.0011115, 4)


def test_status_defaults_to_error_when_result_lacks_status(cost_spy):
    """A source returning an object without a ``status`` attr → engine reads 'error' (defensive)."""

    class Weird:
        cost_usd = 0.0
        website = None

    @_named("discover_gbp")
    def weird(tenant_id, seed):
        return Weird()

    out = auto_discovery_run(TENANT, {"business_name": "x"}, sources=[weird])
    assert out["sources"]["gbp"] == "error"
    assert out["aborted"] is False


def test_cost_record_failure_does_not_break_run(monkeypatch):
    """Observability is best-effort: if ``log_event`` raises, the engine still returns its summary."""
    log_mod = pytest.importorskip("orchestrator.observability.log")

    def _raise(**kwargs):
        raise RuntimeError("pipeline_log down")

    monkeypatch.setattr(log_mod, "log_event", _raise)

    @_named("discover_gbp")
    def a(tenant_id, seed):
        return SourceResult("gbp", "ok", cost_usd=0.004)

    out = auto_discovery_run(TENANT, {"business_name": "x"}, sources=[a])
    assert out["sources"] == {"gbp": "ok"}
    assert out["spent_usd"] == 0.004
