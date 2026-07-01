"""VT-470 — Finance lane (ADVISORY ALWAYS) tests.

Locks the lane's hard contract (design §8, VT-470 charter):
  1. ADVISORY: the tool surface holds NO money-movement / send / ledger-write capability —
     it passes the VT-268 fail-closed guardrail (``assert_agent_tools_safe``), AND a
     standalone scan proves no spend/charge/pay/transfer/settle/refund tool exists.
  2. Reminders route through the EXISTING send rail: ``propose_payment_reminder`` RETURNS A
     PROPOSAL (sent=False, persisted=False, routes_through the customer-send choke) — it
     does NOT send and does NOT persist a draft itself.
  3. The lane is a registry-pluggable ``SpecialistSpec`` (``SPECIALIST_SPEC``) mirroring the
     integration / onboarding_conductor entries (sub-graph, wrap_node=False, edge_to=None,
     prereq=None) — the coordinator registers it centrally on the roster spine (VT-465).
  4. Two-way handoff: ``finance_pushback`` exists and returns a structured pushback envelope.

Disjoint module: this test exercises ONLY ``orchestrator.agent.finance_lane`` + the shared
VT-268 guardrail. The READ tools' DB behavior is covered with an injected fake connection so
the test is dep-less on a live DB (the SQL shape against ``customer_ledger_entries`` /
``imported_transactions`` is the EXISTING substrate, not rebuilt here).
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain")


def _names(tools):
    return {getattr(t, "name", type(t).__name__) for t in tools}


# The exact ADVISORY tool surface — a NEW tool (addition OR removal) fails this pin, forcing
# review that the new capability is not a send/write/money-movement breach (VT-268 discipline).
FINANCE_LANE_EXPECTED = {
    "analyze_cash_flow",
    "analyze_receivables",
    "pricing_margin_input",
    "propose_payment_reminder",
    "finance_pushback",
    "finance_escalate_to_fazal",
}


# --- 1. ADVISORY: no money-movement / send / ledger-write capability --------------------


def test_finance_lane_tool_allowlist_pinned():
    from orchestrator.agent.finance_lane import FINANCE_LANE_TOOLS

    assert _names(FINANCE_LANE_TOOLS) == FINANCE_LANE_EXPECTED


def test_finance_lane_passes_vt268_guardrail():
    """The Finance surface holds NO forbidden send/write/spend capability — guard does not raise."""
    from orchestrator.agent.finance_lane import FINANCE_LANE_TOOLS
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

    assert_agent_tools_safe(FINANCE_LANE_TOOLS, surface="finance_lane")


def test_finance_lane_has_no_money_movement_capability():
    """ADVISORY ALWAYS — NO tool that moves money exists in the module (the lane's hard rail).

    Asserts directly (not only via the VT-268 substring guard) that no tool NAME denotes
    EXECUTING a money movement. NEVER moves money is permanent per the charter; this pins
    it. Checked at WORD-token granularity so the advisory ``propose_payment_reminder``
    (proposing a reminder ABOUT a payment — it does not pay) is correctly NOT flagged,
    while a real ``make_payment`` / ``execute_spend`` / ``charge_card`` would be."""
    from orchestrator.agent.finance_lane import FINANCE_LANE_TOOLS

    tokens: set[str] = set()
    for name in _names(FINANCE_LANE_TOOLS):
        tokens.update(name.lower().split("_"))
    # Money-MOVEMENT verb TOKENS the advisory lane must never expose as a standalone word.
    # ('analyze'/'propose'/'pricing'/'pushback'/'reminder'/'payment' are advisory tokens.)
    forbidden_verbs = {
        "pay", "charge", "transfer", "settle", "refund", "remit",
        "spend", "withdraw", "disburse", "payout", "execute", "send", "move",
    }
    leak = tokens & forbidden_verbs
    assert not leak, f"finance lane exposes a money-movement/send verb token: {sorted(leak)}"


def test_finance_lane_build_rejects_a_money_movement_tool():
    """Fail-closed at build: handing the builder a spend/pay tool RAISES (never silently wired)."""
    from langchain_core.tools import tool

    from orchestrator.agent.finance_lane import _MODEL, build_finance_lane_agent
    from orchestrator.agent.tool_guardrail import ToolGuardrailViolation

    @tool
    def make_payment_evil(amount_paise: int) -> str:
        """A would-be money-movement tool that must never reach the advisory Finance lane."""
        return str(amount_paise)

    with pytest.raises(ToolGuardrailViolation):
        build_finance_lane_agent(_MODEL, extra_tools=[make_payment_evil])


def test_finance_lane_build_rejects_a_send_tool():
    """Fail-closed at build: a direct customer-send tool RAISES (reminders go via the rail only)."""
    from langchain_core.tools import tool

    from orchestrator.agent.finance_lane import _MODEL, build_finance_lane_agent
    from orchestrator.agent.tool_guardrail import ToolGuardrailViolation

    @tool
    def send_to_customer_evil(customer_id: str) -> str:
        """A would-be direct send tool that must never reach the advisory Finance lane."""
        return customer_id

    with pytest.raises(ToolGuardrailViolation):
        build_finance_lane_agent(_MODEL, extra_tools=[send_to_customer_evil])


# --- 2. Reminders route through the existing send rail ----------------------------------


def test_propose_payment_reminder_does_not_send_or_persist():
    """The reminder tool RETURNS A PROPOSAL routed through the send rail — it does NOT act.

    sent=False + persisted=False + routes_through the customer-send choke is the contract: the
    Finance lane proposes; ``agents/customer_send.agent_send_draft`` (consent/caps + the VT-474
    decaying checkpoint) is the ONLY thing that sends, server-side, never an agent tool."""
    from uuid import uuid4

    from orchestrator.agent.finance_lane import propose_payment_reminder

    tid = str(uuid4())
    cid = str(uuid4())
    result = propose_payment_reminder.invoke(
        {
            "tenant_id": tid,
            "customer_id": cid,
            "reason": "₹5,000 outstanding 45 days",
            "reminder_text": "Friendly reminder about your pending balance.",
        }
    )
    assert result["kind"] == "payment_reminder_proposal"
    assert result["sent"] is False
    assert result["persisted"] is False
    assert result["routes_through"] == "agents.customer_send.agent_send_draft"
    assert result["tenant_id"] == tid
    assert result["customer_id"] == cid


def test_finance_lane_holds_no_send_or_draft_write_tool():
    """No tool name implies sending or persisting a draft — only PROPOSE/READ verbs exist."""
    from orchestrator.agent.finance_lane import FINANCE_LANE_TOOLS

    names = _names(FINANCE_LANE_TOOLS)
    for forbidden in ("send", "agent_send_draft", "persist_draft", "write_draft", "dispatch"):
        assert not any(forbidden in n.lower() for n in names), forbidden


# --- 3. Registry-pluggable SpecialistSpec (VT-465) --------------------------------------


def test_finance_lane_exports_specialist_spec():
    from orchestrator.agent.finance_lane import SPECIALIST_SPEC
    from orchestrator.agent.roster import SpecialistSpec

    assert isinstance(SPECIALIST_SPEC, SpecialistSpec)
    assert SPECIALIST_SPEC.name == "finance_lane"
    assert SPECIALIST_SPEC.agent_name == "finance_lane"
    assert SPECIALIST_SPEC.spawn_tool_name == "spawn_finance_lane"
    assert SPECIALIST_SPEC.route_key == "spawn_finance_lane"
    # Mirrors integration / onboarding_conductor: compiled sub-graph (not function-wrapped),
    # flows to END, and the ADVISORY lane has no activation bar (prereq=None).
    assert SPECIALIST_SPEC.wrap_node is False
    assert SPECIALIST_SPEC.edge_to is None
    assert SPECIALIST_SPEC.prereq is None


def test_finance_lane_spec_node_builder_builds_a_subgraph():
    """The spec's node_builder yields the finance sub-graph (the roster iterates this at build)."""
    from orchestrator.agent.finance_lane import SPECIALIST_SPEC, _MODEL

    node = SPECIALIST_SPEC.node_builder(_MODEL)
    assert node is not None
    # The spec's spawn tool builds without error (registry-pluggable end to end).
    spawn = SPECIALIST_SPEC.make_spawn()
    assert spawn.name == "spawn_finance_lane"


# --- 4. Two-way handoff: pushback -------------------------------------------------------


def test_finance_pushback_returns_structured_envelope():
    from orchestrator.agent.finance_lane import finance_pushback

    result = finance_pushback.invoke(
        {
            "desired_outcome": "collect overdue receivables",
            "reason": "no receivables are currently overdue",
            "proposed_outcome": "monitor; revisit in 30 days",
        }
    )
    assert result["pushback"] is True
    assert result["proposed_outcome"] == "monitor; revisit in 30 days"


def test_finance_pushback_runs_manager_decision_observe_only(monkeypatch):
    """VT-549 (B3-wiring 2): the finance pushback runs the manager decision loop observe-only via the
    same bridge as sales — the envelope is unchanged (backward-compat) and the wire fires tagged
    'finance'."""
    import orchestrator.agent.specialist_return as sr
    from orchestrator.agent.finance_lane import finance_pushback

    seen: list = []
    monkeypatch.setattr(
        sr, "observe_specialist_return", lambda env, *, agent: seen.append((env, agent))
    )
    result = finance_pushback.invoke(
        {"desired_outcome": "x", "reason": "y", "proposed_outcome": "monitor 30d"}
    )
    assert result["pushback"] is True  # envelope byte-for-byte (backward-compat)
    assert len(seen) == 1 and seen[0][1] == "finance"
    assert seen[0][0]["proposed_outcome"] == "monitor 30d"


# --- READ tools: aggregate-only, PII-free, best-effort ----------------------------------


class _FakeRow(dict):
    """A psycopg-dict-like row."""


class _FakeCursor:
    def __init__(self, results):
        self._results = list(results)
        self._last = None

    def execute(self, _sql, _params=None):
        self._last = self._results.pop(0) if self._results else None
        return self

    def fetchone(self):
        return self._last if not isinstance(self._last, list) else (self._last[0] if self._last else None)

    def fetchall(self):
        return self._last if isinstance(self._last, list) else []


class _FakeConn:
    def __init__(self, results):
        self._cursor = _FakeCursor(results)

    def execute(self, sql, params=None):
        return self._cursor.execute(sql, params)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_analyze_cash_flow_aggregate_and_pii_free(monkeypatch):
    """Cash-flow returns totals/counts (no raw rows / PII); outstanding = sales − payments."""
    from uuid import uuid4

    from orchestrator.agent import finance_lane

    ledger_row = _FakeRow(inflow=500000, collected=300000, sale_count=10, payment_count=6)
    txn_row = _FakeRow(credit=320000, debit=50000)
    fake = _FakeConn([ledger_row, txn_row])
    monkeypatch.setattr(finance_lane, "tenant_connection", lambda _tid: fake)

    out = finance_lane.analyze_cash_flow.invoke({"tenant_id": str(uuid4())})
    assert out["inflow_paise"] == 500000
    assert out["collected_paise"] == 300000
    assert out["outstanding_paise"] == 200000  # 500000 - 300000
    assert out["credit_paise"] == 320000
    assert out["net_paise"] == 320000 - 50000
    # PII-free: only numeric aggregates + counts, no phone/email/name keys.
    assert all(isinstance(v, int) for k, v in out.items() if k.endswith("_paise"))


def test_analyze_cash_flow_degrades_to_zeros_on_read_error(monkeypatch):
    """Advisory read is best-effort: a DB failure returns zeros, never raises (advise on what exists)."""
    from uuid import uuid4

    from orchestrator.agent import finance_lane

    def _boom(_tid):
        raise RuntimeError("db down")

    monkeypatch.setattr(finance_lane, "tenant_connection", _boom)
    out = finance_lane.analyze_cash_flow.invoke({"tenant_id": str(uuid4())})
    assert out["inflow_paise"] == 0
    assert out["outstanding_paise"] == 0


def test_analyze_receivables_returns_customer_ids_only(monkeypatch):
    """Receivables returns customer_id UUIDs + aggregate paise — NEVER phone/email/name (CL-390)."""
    from uuid import uuid4

    from orchestrator.agent import finance_lane

    c1, c2 = str(uuid4()), str(uuid4())
    rows = [
        _FakeRow(customer_id=c1, outstanding_paise=150000, days_since_last_sale=45),
        _FakeRow(customer_id=c2, outstanding_paise=80000, days_since_last_sale=60),
    ]
    fake = _FakeConn([rows])
    monkeypatch.setattr(finance_lane, "tenant_connection", lambda _tid: fake)

    out = finance_lane.analyze_receivables.invoke({"tenant_id": str(uuid4())})
    assert out["overdue_count"] == 2
    assert out["total_outstanding_paise"] == 230000
    assert set(out["overdue_customer_ids"]) == {c1, c2}
