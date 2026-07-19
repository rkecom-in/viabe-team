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
        return SourceResult("gbp", "ok", cost_usd=0.035)

    @_named("discover_website")
    def pricey_b(tenant_id, seed):
        ran.append("website")
        return SourceResult("website", "ok", cost_usd=0.035)  # cumulative 0.070 > 0.060 ceiling

    @_named("discover_serper")
    def should_not_run(tenant_id, seed):
        ran.append("serper")
        return SourceResult("serper", "ok", cost_usd=0.035)

    out = auto_discovery_run(
        TENANT, {"business_name": "x"}, sources=[pricey_a, pricey_b, should_not_run]
    )

    # third source is gated out — spent (0.070) exceeds the 0.060 ceiling before it runs
    assert ran == ["gbp", "website"]
    assert "serper" not in out["sources"]
    assert out["aborted"] is True
    assert out["spent_usd"] == 0.070


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
        return SourceResult("gbp", "ok", cost_usd=0.070)  # alone over the 0.060 ceiling

    @_named("discover_website")
    def after(tenant_id, seed):
        ran.append("website")
        return SourceResult("website", "ok", cost_usd=0.001)

    out = auto_discovery_run(TENANT, {"business_name": "x"}, sources=[huge, after])
    assert ran == ["gbp"]  # huge ran (no pre-cost), after was gated
    assert out["aborted"] is True
    assert out["spent_usd"] == 0.070


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
    # the engine recorded cost to observability (best-effort) exactly once. Filter by
    # event_type: the VT-374 per-source pause checks legitimately emit
    # run_control_degraded alerts in this DB-less environment (fail-OPEN posture, F9).
    cost_events = [c for c in cost_spy if c["event_type"] == "auto_discovery_cost"]
    assert len(cost_events) == 1
    assert cost_events[0]["payload"]["cost_usd"] == round(0.0011115, 4)


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


# ------------------------------------------------------------- VT-568 reorder + seed anchors


def test_default_source_order_is_gst_before_gbp(monkeypatch):
    """VT-568 — GST runs FIRST so its verified anchors seed GBP's identity adjudication."""
    order: list[str] = []
    import orchestrator.onboarding.auto_discovery_sources as srcmod

    def _mk(name):
        @_named(name)
        def fn(tenant_id, seed):
            order.append(name.replace("discover_", ""))
            return SourceResult(name.replace("discover_", ""), "skipped", cost_usd=0.0)

        return fn

    for n in ("discover_gst", "discover_gbp", "discover_website", "discover_serper"):
        monkeypatch.setattr(srcmod, n, _mk(n))

    auto_discovery_run(TENANT, {"business_name": "x"})  # sources=None → the default list
    assert order == ["gst", "gbp", "website", "serper"]


def test_seed_updates_feed_downstream_source():
    """A source's ``seed_updates`` (GST identity anchors) reach a later source via the seed."""
    seen: dict = {}

    @_named("discover_gst")
    def fake_gst(tenant_id, seed):
        return SourceResult(
            "gst", "ok", cost_usd=0.0,
            seed_updates={"gst_trade_name": "RKECOM", "gst_legal_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED"},
        )

    @_named("discover_gbp")
    def fake_gbp(tenant_id, seed):
        seen["trade"] = seed.get("gst_trade_name")
        seen["legal"] = seed.get("gst_legal_name")
        return SourceResult("gbp", "rejected", cost_usd=0.029)

    auto_discovery_run(TENANT, {"business_name": "RKECOM"}, sources=[fake_gst, fake_gbp])
    assert seen["trade"] == "RKECOM"
    assert seen["legal"] == "RKECOM SERVICES (OPC) PRIVATE LIMITED"


def test_seed_updates_do_not_clobber_existing_seed_key():
    seen: dict = {}

    @_named("discover_gst")
    def fake_gst(tenant_id, seed):
        return SourceResult("gst", "ok", cost_usd=0.0, seed_updates={"gst_trade_name": "FROM_GST"})

    @_named("discover_gbp")
    def fake_gbp(tenant_id, seed):
        seen["trade"] = seed.get("gst_trade_name")
        return SourceResult("gbp", "rejected", cost_usd=0.0)

    auto_discovery_run(
        TENANT, {"business_name": "x", "gst_trade_name": "PREEXISTING"}, sources=[fake_gst, fake_gbp]
    )
    assert seen["trade"] == "PREEXISTING"  # setdefault: never overwrite an anchor the seed already carries


# ------------------------------------------------------------------------ VT-568 redrive


def test_redrive_resets_draft_before_rerun(monkeypatch):
    import orchestrator.onboarding.auto_discovery as ad

    calls: list[tuple] = []
    monkeypatch.setattr(ad, "_reset_draft", lambda tid: calls.append(("reset", str(tid))))
    monkeypatch.setattr(
        ad, "auto_discovery_run", lambda tid, seed: calls.append(("run", str(tid), seed)) or {"ok": True}
    )

    seed = {"business_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED", "gstin": "27AAKCR3738B1ZE"}
    out = ad.redrive_discovery(TENANT, seed=seed)

    assert out == {"ok": True}
    assert [c[0] for c in calls] == ["reset", "run"]  # draft cleared BEFORE the rerun
    assert calls[1][2] == seed


def test_rebuild_seed_prefers_verified_name(monkeypatch):
    import orchestrator.onboarding.auto_discovery as ad

    class _Conn:
        def __init__(self, row):
            self._row = row

        def execute(self, _q, _params):
            return self

        def fetchone(self):
            return self._row

    class _Ctx:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            return self._conn

        def __exit__(self, *_a):
            return False

    class _Pool:
        def __init__(self, row):
            self._row = row

        def connection(self):
            return _Ctx(_Conn(self._row))

    row = {
        "business_name": "RKECOM Services PVT LTD",
        "verified_business_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED",
        "gstin": "27AAKCR3738B1ZE",
        "business_type": "services",
    }
    monkeypatch.setattr("orchestrator.graph.get_pool", lambda: _Pool(row))

    seed = ad._rebuild_seed(TENANT)
    # the SERVER-VERIFIED name anchors the redrive (not the raw typed signup name)
    assert seed == {
        "business_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED",
        "gstin": "27AAKCR3738B1ZE",
        "business_type": "services",
    }
