"""Migration-173 ledger: cost wiring, tenant vs platform routing, fail-soft.

Dep-less: the DB seams (``_insert_tenant`` / ``_insert_platform``) are monkeypatched,
so no psycopg / DBOS import fires. ``compute_cost_usd`` is monkeypatched to a fixed
value so the cost that lands on the event params is asserted deterministically.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

# The orchestrator.llm package __init__ eagerly imports provider.py (langchain_core),
# so importing ANY submodule transitively needs it — skip cleanly in the dep-less smoke.
pytest.importorskip("langchain_core")

from orchestrator.llm import ledger as ledger_mod  # noqa: E402
from orchestrator.llm.ledger import record_llm_call  # noqa: E402


def _fix_cost(monkeypatch, value="0.001234"):
    monkeypatch.setattr(ledger_mod, "compute_cost_usd", lambda *a, **k: Decimal(value))


def test_tenant_call_inserts_event_and_rolls_up(monkeypatch):
    _fix_cost(monkeypatch, "0.05")
    captured = {}

    def _spy_tenant(tenant_id, params, *, agent, tokens_in, tokens_out):
        captured["tenant_id"] = tenant_id
        captured["params"] = params
        captured["rollup"] = (agent, tokens_in, tokens_out)

    monkeypatch.setattr(ledger_mod, "_insert_tenant", _spy_tenant)
    monkeypatch.setattr(
        ledger_mod, "_insert_platform", lambda *a, **k: (_ for _ in ()).throw(AssertionError("platform path"))
    )

    tid = uuid4()
    record_llm_call(
        tenant_id=tid,
        agent="team_manager",
        call_site="dispatch_brain",
        provider="anthropic",
        model="claude-sonnet-5",
        tokens_in=1200,
        tokens_out=340,
        request_id="msg_abc123",
    )

    params = captured["params"]
    # Column order: tenant_id, agent, call_site, provider, model, service_tier,
    #               tokens_in, tokens_out, cost_usd, request_id.
    assert params[0] == str(tid)
    assert params[1] == "team_manager"
    assert params[2] == "dispatch_brain"
    assert params[3] == "anthropic"
    assert params[4] == "claude-sonnet-5"
    assert params[5] == "standard"  # default tier
    assert params[6] == 1200
    assert params[7] == 340
    assert params[8] == Decimal("0.05")
    assert params[9] == "msg_abc123"
    # The VT-619 rollup is fed this call's tenant/agent/tokens.
    assert captured["rollup"] == ("team_manager", 1200, 340)


def test_cached_tokens_fold_into_total_input_and_cost(monkeypatch):
    # compute_cost_usd must receive the cached count; persisted tokens_in = total.
    seen = {}

    def _spy_cost(model, tier, t_in, t_out, cached=0):
        seen["args"] = (model, tier, t_in, t_out, cached)
        return Decimal("0.02")

    monkeypatch.setattr(ledger_mod, "compute_cost_usd", _spy_cost)
    captured = {}
    monkeypatch.setattr(
        ledger_mod,
        "_insert_tenant",
        lambda tenant_id, params, *, agent, tokens_in, tokens_out: captured.update(
            params=params, rollup_in=tokens_in
        ),
    )

    record_llm_call(
        tenant_id=uuid4(),
        agent="team_manager",
        call_site="dispatch_brain",
        provider="anthropic",
        model="claude-sonnet-5",
        tokens_in=1000,
        tokens_out=200,
        cached_tokens_in=400,
    )
    # Cost computed on the split (full 1000 + cached 400).
    assert seen["args"] == ("claude-sonnet-5", "standard", 1000, 200, 400)
    # Ledger + rollup persist TOTAL input (1000 + 400).
    assert captured["params"][6] == 1400
    assert captured["params"][8] == Decimal("0.02")
    assert captured["rollup_in"] == 1400


def test_platform_call_uses_null_tenant_and_no_rollup(monkeypatch):
    _fix_cost(monkeypatch, "0.01")
    captured = {}

    monkeypatch.setattr(ledger_mod, "_insert_platform", lambda params: captured.update(params=params))
    monkeypatch.setattr(
        ledger_mod, "_insert_tenant", lambda *a, **k: (_ for _ in ()).throw(AssertionError("tenant path"))
    )

    record_llm_call(
        tenant_id=None,
        agent="judge",
        call_site="blind_judge",
        provider="anthropic",
        model="claude-opus-4-8",
        service_tier="flex",
        tokens_in=900,
        tokens_out=100,
    )

    params = captured["params"]
    assert params[0] is None  # NULL tenant_id (platform/tenantless row)
    assert params[1] == "judge"
    assert params[5] == "flex"
    assert params[8] == Decimal("0.01")
    assert params[9] is None  # request_id defaulted


def test_failsoft_swallows_db_error(monkeypatch):
    _fix_cost(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(ledger_mod, "_insert_tenant", _boom)
    # Must NOT raise — metering never breaks a turn (CL-122).
    record_llm_call(
        tenant_id=uuid4(),
        agent="team_manager",
        call_site="dispatch_brain",
        provider="anthropic",
        model="claude-sonnet-5",
        tokens_in=10,
        tokens_out=10,
    )


def test_failsoft_swallows_cost_computation_error(monkeypatch):
    monkeypatch.setattr(
        ledger_mod, "compute_cost_usd", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad"))
    )
    called = {"tenant": False, "platform": False}
    monkeypatch.setattr(ledger_mod, "_insert_tenant", lambda *a, **k: called.__setitem__("tenant", True))
    monkeypatch.setattr(ledger_mod, "_insert_platform", lambda *a, **k: called.__setitem__("platform", True))

    record_llm_call(
        tenant_id=uuid4(),
        agent="team_manager",
        call_site="dispatch_brain",
        provider="anthropic",
        model="claude-sonnet-5",
        tokens_in=10,
        tokens_out=10,
    )
    # Cost blew up before any insert — no write attempted, no exception surfaced.
    assert called == {"tenant": False, "platform": False}
