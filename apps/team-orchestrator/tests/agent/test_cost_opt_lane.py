"""VT-473 — the Cost-Optimisation specialist lane (v1 ADVISE).

Pins the cost-opt lane WITHOUT a live Anthropic call:

  1. ADVISE-ONLY tool surface — the lane holds ONLY read/analyze tools (spend / unit-economics /
     anomaly / marketing-ROI / context); it holds NO send / write / spend-execute / commitment /
     config-write / act tool (VT-268 ``find_forbidden_tools`` + the fail-closed build guard).
  2. The tools DELEGATE to the existing cost/spend/ROI substrate (cost_dashboard + get_attribution_
     data) — no parallel aggregation; a delegated read flows through unchanged.
  3. The lane produces ADVICE/SUGGESTIONS only — ``CostOptAdvice.acted`` is pinned False; every
     suggestion is ``owner_gated`` — and the FUTURE-act seam is DOCUMENTED, not wired.
  4. ``SPECIALIST_SPEC`` is a valid ``SpecialistSpec`` the coordinator can append to ROSTER (a
     CompiledStateGraph sub-graph -> END), proving "adding a lane = a registry entry" (design §7).

This test is DISJOINT — it does NOT mutate ROSTER (the coordinator registers centrally); it only
asserts the exported spec is registration-ready.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")


# --- (1) ADVISE-only tool surface: read/analyze only, NO act/send/write -----------------------------

COST_OPT_EXPECTED = {
    "analyze_tenant_spend",
    "analyze_unit_economics",
    "identify_spend_anomaly",
    "analyze_marketing_roi",
    "read_cost_context",
}


def test_cost_opt_tool_allowlist_pinned() -> None:
    """Exact match: a NEW tool (esp. an act/send/write one) fails -> forces VT-268 review."""
    from orchestrator.agent.cost_opt_lane import COST_OPT_LANE_TOOLS

    names = {t.name for t in COST_OPT_LANE_TOOLS}
    assert names == COST_OPT_EXPECTED


def test_cost_opt_holds_no_act_send_or_write_tool() -> None:
    """The lane is ADVISE-only — the VT-268 forbidden-capability guard finds nothing."""
    from orchestrator.agent.cost_opt_lane import COST_OPT_LANE_TOOLS
    from orchestrator.agent.tool_guardrail import find_forbidden_tools

    assert find_forbidden_tools(COST_OPT_LANE_TOOLS) == []


def test_build_cost_opt_lane_rejects_act_tool() -> None:
    """Runtime fail-closed: handing the lane builder an act/spend tool raises at build — proving
    the ADVISE boundary cannot be opened even by a future careless wiring."""
    from langchain_core.tools import tool

    from orchestrator.agent.cost_opt_lane import _MODEL, build_cost_opt_lane_agent
    from orchestrator.agent.tool_guardrail import ToolGuardrailViolation

    @tool
    def execute_spend_cut(vendor: str) -> str:
        """A would-be act tool (cancels spend) that must NEVER reach the advise lane."""
        return vendor

    with pytest.raises(ToolGuardrailViolation):
        build_cost_opt_lane_agent(_MODEL, extra_tools=[execute_spend_cut])


@pytest.mark.parametrize(
    "act_name",
    [
        "execute_spend_reduction",
        "commit_spend_change",
        "cancel_subscription_make_commitment",  # contains make_commitment
        "apply_config_change_to_vendor",
        "send_whatsapp_message",
    ],
)
def test_guard_would_trip_on_a_cost_act_tool(act_name: str) -> None:
    """Any concrete cost-ACT capability name trips the guard — the ADVISE surface stays closed."""
    from types import SimpleNamespace

    from orchestrator.agent.tool_guardrail import (
        ToolGuardrailViolation,
        assert_agent_tools_safe,
    )

    with pytest.raises(ToolGuardrailViolation):
        assert_agent_tools_safe([SimpleNamespace(name=act_name)], surface="cost_opt_lane")


# --- (2) tools DELEGATE to the existing cost/spend/ROI substrate (no parallel aggregation) ----------


def test_analyze_tenant_spend_delegates_to_cost_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """``analyze_tenant_spend`` delegates to ``cost_dashboard.get_tenant_cost`` — the read flows
    through unchanged (paise + counts only, no parallel aggregation here)."""
    from types import SimpleNamespace
    from uuid import uuid4

    import orchestrator.observability.cost_dashboard as cd
    from orchestrator.agent.cost_opt_lane import analyze_tenant_spend

    captured: dict[str, Any] = {}

    def _fake_get_tenant_cost(tid: Any, since: Any, until: Any) -> Any:
        captured["tid"] = tid
        return SimpleNamespace(
            total_paise=12_345,
            by_category={"llm": 10_000, "twilio": 2_345},
            event_count=7,
        )

    monkeypatch.setattr(cd, "get_tenant_cost", _fake_get_tenant_cost)
    tid = str(uuid4())
    out = analyze_tenant_spend.func(tid, 30)  # type: ignore[attr-defined]
    assert out["total_paise"] == 12_345
    assert out["by_category"] == {"llm": 10_000, "twilio": 2_345}
    assert out["event_count"] == 7
    assert out["window_days"] == 30
    assert str(captured["tid"]) == tid


def test_analyze_marketing_roi_delegates_to_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    """``analyze_marketing_roi`` delegates to ``get_attribution_data`` (window mode) — campaign ARRR
    vs send is the marketing-ROI substrate; a low-ARRR campaign surfaces for a cut suggestion."""
    from uuid import uuid4

    import orchestrator.agent.tools.get_attribution_data as attr_mod
    from orchestrator.agent.cost_opt_lane import analyze_marketing_roi
    from orchestrator.agent.tools.get_attribution_data import (
        CampaignAttributionSummary,
        GetAttributionDataOutput,
        WindowAttributionSnapshot,
    )

    def _fake_get_attribution_data(payload: Any) -> Any:
        from datetime import datetime, timezone

        return GetAttributionDataOutput(
            mode="window",
            window=WindowAttributionSnapshot(
                window_start=datetime.now(timezone.utc),
                window_end=datetime.now(timezone.utc),
                campaign_count=1,
                total_transacting_count=0,
                total_arrr_paise=0,
                per_campaign_summary=[
                    CampaignAttributionSummary(
                        campaign_id="c1",
                        attribution_status="closed",
                        transacting_count=0,
                        arrr_paise=0,
                    )
                ],
            ),
            complete=False,
        )

    monkeypatch.setattr(attr_mod, "get_attribution_data", _fake_get_attribution_data)
    out = analyze_marketing_roi.func(str(uuid4()), 30)  # type: ignore[attr-defined]
    assert out["campaign_count"] == 1
    assert out["total_arrr_paise"] == 0
    assert out["per_campaign"][0]["campaign_id"] == "c1"  # a closed, zero-ARRR (low-ROI) campaign


def test_identify_spend_anomaly_filters_to_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """``identify_spend_anomaly`` filters the WORKSPACE scans to THIS tenant (the manager reads one
    business); a runaway for a different tenant is excluded."""
    from types import SimpleNamespace
    from uuid import uuid4

    import orchestrator.observability.cost_dashboard as cd
    from orchestrator.agent.cost_opt_lane import identify_spend_anomaly

    tid = uuid4()
    other = uuid4()
    monkeypatch.setattr(cd, "detect_cost_anomalies", lambda: [])
    monkeypatch.setattr(
        cd,
        "runaway_alert_candidates",
        lambda: [
            SimpleNamespace(tenant_id=other, window_cost_paise=1, plan_monthly_paise=2, pct_observed=0.5),
            SimpleNamespace(tenant_id=tid, window_cost_paise=99, plan_monthly_paise=100, pct_observed=0.99),
        ],
    )
    out = identify_spend_anomaly.func(str(tid))  # type: ignore[attr-defined]
    assert out["anomaly"] is None
    assert out["runaway"] is not None
    assert out["runaway"]["window_cost_paise"] == 99  # the THIS-tenant row, not the other


# --- (3) ADVISE-only output: acted=False, owner_gated; FUTURE-act seam documented -------------------


def test_advice_output_is_advise_only() -> None:
    """``CostOptAdvice`` carries advice with no act handle: ``acted`` is False; suggestions are
    owner_gated by default (acting is owner-gated business-impact, v1 NEVER acts)."""
    from orchestrator.agent.cost_opt_lane import CostOptAdvice, CostOptSuggestion

    advice = CostOptAdvice(
        tenant_id="t1",
        suggestions=[
            CostOptSuggestion(
                category="low_roi_marketing",
                finding="campaign c1 sent volume with 0 attributed revenue",
                suggestion="pause c1 and re-allocate budget",
                recalibration_lever="full_utilization",
            )
        ],
        summary="one low-ROI campaign",
    )
    assert advice.acted is False  # v1 NEVER acts
    assert advice.suggestions[0].owner_gated is True  # acting is owner-gated


def test_future_act_seam_is_documented_not_built() -> None:
    """The FUTURE-act seam is a documented marker carrying NO capability — it names the owner-gated
    path (VT-467) the act tool will plug into, and confirms v1 does NOT build it."""
    from orchestrator.agent import cost_opt_lane

    seam = cost_opt_lane.FUTURE_ACT_SEAM
    assert isinstance(seam, str)
    assert "owner-gated" in seam.lower()
    assert "advise" in seam.lower()
    # the seam is a string constant, NOT a callable/tool — no act capability on this surface.
    assert not callable(seam)


# --- (4) SPECIALIST_SPEC is registration-ready (coordinator appends centrally) ----------------------


def test_specialist_spec_is_registration_ready() -> None:
    """The exported ``SPECIALIST_SPEC`` is a valid ``SpecialistSpec`` the coordinator appends to
    ROSTER: a CompiledStateGraph sub-graph (wrap_node=False, -> END), advise-only (update_builder
    None — the lane self-fetches), with a spawn tool + route key the supervisor graph can wire."""
    from orchestrator.agent.cost_opt_lane import SPECIALIST_SPEC
    from orchestrator.agent.roster import SpecialistSpec

    assert isinstance(SPECIALIST_SPEC, SpecialistSpec)
    assert SPECIALIST_SPEC.name == "cost_opt"
    assert SPECIALIST_SPEC.agent_name == "cost_opt_lane"
    assert SPECIALIST_SPEC.spawn_tool_name == "spawn_cost_opt"
    assert SPECIALIST_SPEC.route_key == "spawn_cost_opt"
    assert SPECIALIST_SPEC.wrap_node is False  # CompiledStateGraph — never function-wrapped
    assert SPECIALIST_SPEC.edge_to is None  # -> END (emits advice, not a campaign plan)
    assert SPECIALIST_SPEC.prereq is None


def test_specialist_spec_node_builder_passes_the_guard() -> None:
    """The spec's node_builder builds a sub-graph that passes the VT-268 fail-closed guard (the lane
    is advise-only). A FakeModel stands in for ChatAnthropic — no live call."""
    from orchestrator.agent.cost_opt_lane import SPECIALIST_SPEC

    class _FakeModel:
        def bind_tools(self, tools: Any, **kwargs: Any) -> "_FakeModel":
            return self

    # Builds without raising — proves the registered node holds no forbidden capability.
    node = SPECIALIST_SPEC.node_builder(_FakeModel())
    assert node is not None
