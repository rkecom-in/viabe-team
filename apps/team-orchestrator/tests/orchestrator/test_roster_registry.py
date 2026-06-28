"""VT-465 — roster registry + standard handoff protocol tests.

Two properties under test:

  1. THE CHEAP-ADD PROPERTY — appending ONE ``SpecialistSpec`` to ``ROSTER``
     wires a spawn tool + a route + a graph node WITHOUT editing
     ``build_supervisor_graph`` or ``route_after_orchestrator``. We prove it by
     monkeypatching a third spec into ``ROSTER`` and observing the rebuilt graph
     gains the node + the route map gains the branch + the manager gains the
     spawn tool — all from the registry, with no graph-surgery edit.

  2. THE EXISTING SPECIALISTS ARE UNCHANGED — sales_recovery + integration keep
     their exact agent_name / spawn_tool_name / route_key / edge target, and the
     handoff payload still carries the legacy per-lane bundle key the specialist
     node reads (sales_recovery_context), so their behavior + tests are
     byte-for-byte identical.

Module-level importorskip guards mirror test_supervisor.py so collection in the
CI ``orchestrator`` job import-checks the roster chain.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")

from langchain_core.messages import AIMessage  # noqa: E402 — after importorskip

from orchestrator import routing  # noqa: E402
from orchestrator.agent import roster as roster_mod  # noqa: E402
from orchestrator.agent.roster import (  # noqa: E402
    ROSTER,
    SpecialistHandoff,
    SpecialistReturn,
    SpecialistSpec,
    build_handoff_update,
    roster_spawn_tools,
    spawn_tool_route_keys,
)


# --- The two existing specialists are registered UNCHANGED -------------------


def test_existing_specialists_registered_with_pre_vt465_wiring() -> None:
    """sales_recovery + integration keep their exact node/tool/route/edge keys.

    These are the strings build_supervisor_graph hard-wired before VT-465; the
    roster must reproduce them so the existing graph shape + tests are identical.
    """
    by_name = {s.name: s for s in ROSTER}
    # VT-462 — the onboarding-conductor lane joined the two pre-existing specialists.
    # VT-465 central integration — the six business specialist lanes (VT-468..473)
    # are now registered too. The three onboarding/recovery lanes below keep their
    # exact pre-VT-465 wiring; this asserts they are PRESENT (not the only members).
    assert {"sales_recovery", "integration", "onboarding_conductor"} <= set(by_name)

    sr = by_name["sales_recovery"]
    assert sr.agent_name == "sales_recovery_agent"
    assert sr.spawn_tool_name == "spawn_sales_recovery"
    assert sr.route_key == "spawn"
    assert sr.edge_to == "collapse"  # campaign plan -> collapse -> approval rail
    assert sr.wrap_node is True  # plain function -> state-transition hook
    assert sr.prereq == "sales_recovery"  # links to activation_registry

    integ = by_name["integration"]
    assert integ.agent_name == "integration_agent"
    assert integ.spawn_tool_name == "spawn_integration"
    assert integ.route_key == "spawn_integration"
    assert integ.edge_to is None  # -> END (no campaign plan to persist)
    assert integ.wrap_node is False  # CompiledStateGraph — never wrapped

    # VT-462 — the onboarding-conductor: dynamic profile-setup specialist, mirrors integration's
    # CompiledStateGraph wiring (wrap_node=False, edge_to=None -> END).
    cond = by_name["onboarding_conductor"]
    assert cond.agent_name == "onboarding_conductor"
    assert cond.spawn_tool_name == "spawn_onboarding_conductor"
    assert cond.route_key == "spawn_onboarding_conductor"
    assert cond.edge_to is None  # -> END (no campaign plan to persist)
    assert cond.wrap_node is False  # CompiledStateGraph — never wrapped


def test_spawn_tool_route_keys_maps_both_lanes() -> None:
    """The routing function reads this map; it must cover every roster member.

    VT-465 central integration — the map now covers all NINE lanes (3 onboarding/
    recovery + the 6 business specialists VT-468..473). Each spawn tool maps to its
    route key; pinned EXACT so a new lane (or a route-key drift) is caught."""
    assert spawn_tool_route_keys() == {
        "spawn_sales_recovery": "spawn",
        "spawn_integration": "spawn_integration",
        "spawn_onboarding_conductor": "spawn_onboarding_conductor",  # VT-462
        # VT-468..473 — the six business specialist lanes.
        "spawn_sales_lane": "spawn_sales_lane",
        "spawn_marketing": "spawn_marketing",
        "spawn_finance_lane": "spawn_finance_lane",
        "spawn_accounting": "spawn_accounting",
        "spawn_tech": "spawn_tech",
        "spawn_cost_opt": "spawn_cost_opt",
    }


def test_roster_spawn_tools_have_expected_names() -> None:
    """The manager's extra_tools come from the roster; names are pinned (the
    test_no_write_tool_surface HANDOFF_EXPECTED contract). VT-465 — all NINE lanes."""
    names = {t.name for t in roster_spawn_tools()}
    assert names == {
        "spawn_sales_recovery",
        "spawn_integration",
        "spawn_onboarding_conductor",  # VT-462
        # VT-468..473 — the six business specialist lanes.
        "spawn_sales_lane",
        "spawn_marketing",
        "spawn_finance_lane",
        "spawn_accounting",
        "spawn_tech",
        "spawn_cost_opt",
    }


# --- The standard handoff PAYLOAD (design §7) --------------------------------


def test_build_handoff_update_carries_standard_envelope_and_legacy_bundle() -> None:
    """The handoff update carries BOTH the standard {situation, desired_outcome,
    context_slice, data} envelope AND the legacy per-lane bundle key the
    specialist node still reads — backward-compat is structural, not optional.
    """
    sentinel = object()

    spec = SpecialistSpec(
        name="probe",
        agent_name="probe_agent",
        spawn_tool_name="spawn_probe",
        route_key="spawn_probe",
        node_builder=lambda model: (lambda state: {}),
        description="probe",
        update_builder=lambda state: {"probe_bundle": sentinel},
        default_outcome="probe outcome",
    )

    update = build_handoff_update(spec=spec, state={"messages": []})

    # Legacy per-lane key preserved verbatim — the specialist reads exactly what
    # it read before VT-465.
    assert update["probe_bundle"] is sentinel

    # Standard envelope present + well-formed.
    envelope = update["specialist_handoff"]
    assert isinstance(envelope, SpecialistHandoff)
    assert envelope.desired_outcome == "probe outcome"
    # The per-lane bundle is ALSO exposed as the envelope's `data` slice so a
    # generic consumer reads it uniformly.
    assert envelope.data == {"probe_bundle": sentinel}


def test_handoff_update_tolerates_specialist_without_update_builder() -> None:
    """A lane with no per-lane bundle (update_builder=None) still produces a
    valid standard envelope — the data slice is just empty."""
    spec = SpecialistSpec(
        name="bare",
        agent_name="bare_agent",
        spawn_tool_name="spawn_bare",
        route_key="spawn_bare",
        node_builder=lambda model: (lambda state: {}),
        description="bare",
        update_builder=None,
        default_outcome="bare outcome",
    )
    update = build_handoff_update(spec=spec, state={})
    assert set(update) == {"specialist_handoff"}
    assert update["specialist_handoff"].data == {}


def test_specialist_return_two_way_seam_shape() -> None:
    """The specialist->manager return seam supports BOTH action-taken AND the
    two-way pushback (a proposed alternative outcome). The shape must not
    preclude pushback (design §7)."""
    acted = SpecialistReturn(action_taken="sent winback", outcome="3 re-engaged")
    assert acted.pushback is False
    assert acted.proposed_outcome == ""

    pushed = SpecialistReturn(
        pushback=True,
        proposed_outcome="offer a discount instead of a plain reminder",
        reason="cohort already received 2 reminders this month",
    )
    assert pushed.pushback is True
    assert pushed.action_taken == ""  # did NOT act
    assert pushed.proposed_outcome


# --- THE CHEAP-ADD PROPERTY --------------------------------------------------


class _FakeModel:
    """Stand-in for the ChatAnthropic the node_builder receives. Never invoked —
    build_supervisor_graph only passes it to node_builder + create_agent's
    bind_tools, and this test stops before any model call."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_FakeModel":
        return self


def test_appending_one_spec_wires_tool_route_and_node_without_graph_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VT-465 cheap-add: register a THIRD specialist by appending ONE
    SpecialistSpec to ROSTER. Without touching build_supervisor_graph or
    route_after_orchestrator, the new lane gains:
      - a spawn tool on the manager,
      - a route_key -> agent_name branch in the conditional-edge map,
      - a node in the compiled graph,
      - an outgoing edge.
    Proven by inspecting the rebuilt graph + routing — the SOLE edit is the
    registry append (monkeypatched here = an append in the real file).
    """

    # A FICTIONAL Phase-2+ lane (not one of the registered nine — VT-465 made
    # 'marketing' real, so the synthetic probe uses 'reputation' to avoid any
    # collision with a registered route_key / agent_name).
    def _reputation_node(state: dict[str, Any]) -> dict[str, Any]:
        return {"active_agent": "reputation_agent"}

    new_spec = SpecialistSpec(
        name="reputation",
        agent_name="reputation_agent",
        spawn_tool_name="spawn_reputation",
        route_key="spawn_reputation",
        node_builder=lambda model: _reputation_node,
        description="Hand off to the (fictional) Reputation Agent.",
        update_builder=None,
        prereq=None,
        edge_to=None,  # -> END
        wrap_node=True,
        default_outcome="manage the business's reputation",
    )

    # The ONLY change a new lane requires: one more registry entry. We append
    # in-place on the live ROSTER list object so both supervisor.py and
    # routing.py (which read it by reference) see the new member with NO edit.
    monkeypatch.setattr(roster_mod, "ROSTER", [*ROSTER, new_spec])
    # supervisor.py imports ROSTER into its own namespace at call-time via the
    # `from orchestrator.agent.roster import ROSTER` inside build_supervisor_graph,
    # so patching the source module is sufficient. routing.py calls
    # spawn_tool_route_keys() which reads roster_mod.ROSTER too.

    # (1) routing: the new spawn tool now maps to its route_key — no edit to
    # route_after_orchestrator.
    assert spawn_tool_route_keys()["spawn_reputation"] == "spawn_reputation"
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{"name": "spawn_reputation", "args": {}, "id": "1"}],
            )
        ]
    }
    assert routing.route_after_orchestrator(state) == "spawn_reputation"

    # (2) the manager's spawn-tool set now includes the new handoff tool.
    assert "spawn_reputation" in {t.name for t in roster_spawn_tools()}

    # (3) + (4): the rebuilt graph gains the node + an edge, derived purely from
    # the registry. Build the real supervisor graph with a fake model — no edit
    # to build_supervisor_graph was made.
    from orchestrator.supervisor import build_supervisor_graph

    graph = build_supervisor_graph(model=_FakeModel())  # type: ignore[arg-type]
    nodes = set(graph.get_graph().nodes)
    assert "reputation_agent" in nodes, sorted(nodes)
    # The existing lanes are still wired (no regression from the append).
    assert {"sales_recovery_agent", "integration_agent"} <= nodes

    # The conditional-edge path map after the orchestrator now routes the new
    # route_key to the new node — assert via the compiled graph's edges that the
    # reputation node is reachable from orchestrator_agent.
    edges = graph.get_graph().edges
    targets_from_orchestrator = {
        e.target for e in edges if e.source == "orchestrator_agent"
    }
    assert "reputation_agent" in targets_from_orchestrator, targets_from_orchestrator


def test_existing_graph_shape_unchanged_by_roster_refactor() -> None:
    """The roster refactor must NOT alter the existing graph's node set or the
    sales_recovery -> collapse / integration -> END edges (the send rail +
    terminal sink stay intact)."""
    from orchestrator.supervisor import build_supervisor_graph

    graph = build_supervisor_graph(model=_FakeModel())  # type: ignore[arg-type]
    g = graph.get_graph()
    nodes = set(g.nodes)

    # All pre-VT-465 nodes present (+ the VT-462 onboarding-conductor lane).
    for expected in (
        "orchestrator_agent",
        "sales_recovery_agent",
        "integration_agent",
        "onboarding_conductor",  # VT-462
        "collapse",
        "orchestrator_terminal",
        "request_owner_approval",
        "campaign_execute",
    ):
        assert expected in nodes, sorted(nodes)

    # sales_recovery -> collapse (the campaign-plan persistence + approval rail).
    sr_targets = {e.target for e in g.edges if e.source == "sales_recovery_agent"}
    assert sr_targets == {"collapse"}

    # integration -> END (no collapse — sub-graph emits no campaign plan).
    integ_targets = {e.target for e in g.edges if e.source == "integration_agent"}
    assert integ_targets == {"__end__"}

    # VT-462 — onboarding_conductor -> END (no campaign plan; same shape as integration).
    cond_targets = {e.target for e in g.edges if e.source == "onboarding_conductor"}
    assert cond_targets == {"__end__"}


def test_uuid_run_identity_still_required_for_existing_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The standard payload wrapper must NOT swallow the fail-loud contract:
    sales_recovery's update_builder still raises when tenant_id/run_id are
    missing (CL-195 / Pillar 3) — composing the standard envelope around it does
    not change that."""
    import orchestrator.context_builder as context_builder_mod
    from orchestrator._tenant_guard import TenantIsolationError

    # Keep bundle construction pure-Python in case it ever gets far enough.
    monkeypatch.setattr(
        context_builder_mod, "_build_recent_campaigns", lambda tid: ([], False)
    )

    sr = next(s for s in ROSTER if s.name == "sales_recovery")
    # tenant_id/run_id absent -> the legacy update_builder fails loud, and
    # build_handoff_update propagates it (no silent envelope).
    with pytest.raises(TenantIsolationError):
        build_handoff_update(spec=sr, state={"messages": [], "trigger_reason": None})


def test_spawn_tool_handoff_attaches_standard_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invoking a roster spawn tool returns a Command whose update carries the
    standard envelope + the legacy bundle key — the END-TO-END payload shape on
    the real handoff path (not just build_handoff_update in isolation)."""
    import orchestrator.context_builder as context_builder_mod

    # Stub the DB-backed bundle builders (keyless, no pool).
    monkeypatch.setattr(
        context_builder_mod, "_build_recent_campaigns", lambda tid: ([], False)
    )
    monkeypatch.setattr(
        context_builder_mod, "_build_pending_owner_inputs", lambda tid: ([], False)
    )
    monkeypatch.setattr(
        context_builder_mod,
        "_build_ledger_summary",
        lambda tid: (context_builder_mod.LedgerSummary(), True),
    )
    monkeypatch.setattr(
        context_builder_mod,
        "_build_l3_priors",
        lambda tid, rid: (context_builder_mod.L3Priors(), False),
    )
    monkeypatch.setattr(
        context_builder_mod,
        "_build_l4_skills",
        lambda tid, req: (context_builder_mod.L4Skills(), False),
    )

    sr = next(s for s in ROSTER if s.name == "sales_recovery")
    tool = sr.make_spawn()

    tenant_id = uuid4()
    run_id = uuid4()
    state = {
        "messages": [{"role": "user", "content": "Recover dormant customers"}],
        "tenant_id": tenant_id,
        "run_id": run_id,
        "trigger_reason": "owner_initiated",
    }
    # The @tool wraps the handoff; call the underlying function with injected args.
    command = tool.func(state=state, tool_call_id="tc-1")  # type: ignore[attr-defined]

    assert command.goto == "sales_recovery_agent"
    update = command.update
    # Legacy bundle key the specialist node reads is present + correctly built.
    bundle = update["sales_recovery_context"]
    assert bundle.tenant_id == tenant_id
    assert bundle.run_id == run_id
    assert bundle.user_request == "Recover dormant customers"
    # Standard envelope rides alongside it.
    envelope = update["specialist_handoff"]
    assert isinstance(envelope, SpecialistHandoff)
    assert envelope.desired_outcome == "recover sales from dormant customers"
