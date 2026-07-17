"""VT-101 Stage 3(c) — Integration dissolution behind ``TEAM_INTEGRATION_VIA_FRAMEWORK``.

Two properties under test, gated on the flag:

  1. FLAG OFF (default) — BYTE-IDENTICAL to pre-VT-101: the ``integration`` spec is a spawnable
     roster member (``spawn_integration`` offered, ``integration_agent`` graph node + edge wired),
     and the Manager's tool set carries NO connector @tools.

  2. FLAG ON — the ``integration`` spec is DISSOLVED from the spawnable roster (no
     ``spawn_integration`` tool, no ``integration_agent`` node/edge/route — no dangling route_key),
     and the Manager holds the eleven VT-608 connector @tools DIRECTLY (the advisory-tool demotion),
     which pass ``assert_agent_tools_safe``.

Module-level importorskip guards mirror ``test_roster_registry.py`` / ``test_supervisor.py`` so
collection in the CI ``orchestrator`` job import-checks the roster + supervisor chain.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")

from langchain_core.messages import AIMessage  # noqa: E402 — after importorskip

from orchestrator import routing  # noqa: E402
from orchestrator.agent.roster import (  # noqa: E402
    ROSTER,
    roster_spawn_tools,
    spawn_tool_route_keys,
    spawnable_roster,
)
from orchestrator.agent_framework.modules.integration_tools_module import (  # noqa: E402
    FRAMEWORK_ROUTING_FLAG,
    integration_via_framework,
)

# The eleven VT-608 connector tool names (the surface dissolved onto the Manager under the flag).
_EXPECTED_CONNECTOR_TOOL_NAMES = {
    "list_supported_connectors",
    "read_integration_state",
    "start_oauth",
    "check_oauth_status",
    "pull_sample",
    "propose_mapping",
    "confirm_mapping",
    "commit_ingestion",
    "schedule_recurring_pull",
    "verify_connector",
    "integration_escalate_to_fazal",
}


class _FakeModel:
    """Stand-in for the ChatAnthropic the node_builder + create_agent receive. Never invoked — the
    graph build only passes it to node_builder + create_agent's bind_tools (mirrors
    test_roster_registry._FakeModel)."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_FakeModel":
        return self


def _flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FRAMEWORK_ROUTING_FLAG, raising=False)


def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FRAMEWORK_ROUTING_FLAG, "1")


# --- The flag helper itself (mirrors sr_via_framework's contract) ------------------------------


def test_flag_defaults_off_and_parses_truthy(monkeypatch: pytest.MonkeyPatch) -> None:
    _flag_off(monkeypatch)
    assert integration_via_framework() is False
    for truthy in ("1", "true", "TRUE", "yes", "on", " On "):
        monkeypatch.setenv(FRAMEWORK_ROUTING_FLAG, truthy)
        assert integration_via_framework() is True, truthy
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv(FRAMEWORK_ROUTING_FLAG, falsy)
        assert integration_via_framework() is False, falsy


# --- FLAG OFF — the integration specialist is spawnable, byte-identical -------------------------


def test_flag_off_integration_is_spawnable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (flag OFF): spawnable_roster == full ROSTER, spawn_integration offered."""
    _flag_off(monkeypatch)
    assert spawnable_roster() == ROSTER
    assert {s.name for s in spawnable_roster()} == {
        "sales_recovery",
        "integration",
        "onboarding_conductor",
    }
    assert "spawn_integration" in spawn_tool_route_keys()
    assert "spawn_integration" in {t.name for t in roster_spawn_tools()}


# --- FLAG ON — the integration specialist is dissolved from the spawnable roster ----------------


def test_flag_on_excludes_integration_from_spawnable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag ON: integration is not a spawnable spec; the other two remain."""
    _flag_on(monkeypatch)
    names = {s.name for s in spawnable_roster()}
    assert "integration" not in names
    assert names == {"sales_recovery", "onboarding_conductor"}
    # ROSTER (the identity list) is UNCHANGED — dissolution is a spawn-view exclusion, not a mutation.
    assert {s.name for s in ROSTER} == {"sales_recovery", "integration", "onboarding_conductor"}


def test_flag_on_no_dangling_spawn_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag ON: spawn_integration is neither a route key nor a manager spawn tool (no dangling)."""
    _flag_on(monkeypatch)
    route_keys = spawn_tool_route_keys()
    assert "spawn_integration" not in route_keys
    assert set(route_keys) == {"spawn_sales_recovery", "spawn_onboarding_conductor"}

    spawn_names = {t.name for t in roster_spawn_tools()}
    assert "spawn_integration" not in spawn_names
    assert spawn_names == {"spawn_sales_recovery", "spawn_onboarding_conductor"}

    # A stray spawn_integration tool-call now routes to the safe terminal sink (no dangling edge).
    state = {
        "messages": [
            AIMessage(content="", tool_calls=[{"name": "spawn_integration", "args": {}, "id": "1"}])
        ]
    }
    assert routing.route_after_orchestrator(state) == "terminal"


# --- FLAG ON — the graph loses the integration node/edge cleanly --------------------------------


def test_flag_on_graph_has_no_integration_node_or_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag ON: the compiled supervisor graph has no integration_agent node and no
    orchestrator_agent -> integration_agent edge; the OTHER two specialists stay wired."""
    _flag_on(monkeypatch)
    from orchestrator.supervisor import build_supervisor_graph

    graph = build_supervisor_graph(model=_FakeModel())  # type: ignore[arg-type]
    g = graph.get_graph()
    nodes = set(g.nodes)
    assert "integration_agent" not in nodes, sorted(nodes)
    # The other roster specialists are still wired (no collateral removal).
    assert {"sales_recovery_agent", "onboarding_conductor"} <= nodes

    targets_from_orchestrator = {e.target for e in g.edges if e.source == "orchestrator_agent"}
    assert "integration_agent" not in targets_from_orchestrator, targets_from_orchestrator


def test_flag_off_graph_keeps_integration_node_and_end_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag OFF: the integration_agent node + its -> END edge are present (byte-identical shape)."""
    _flag_off(monkeypatch)
    from orchestrator.supervisor import build_supervisor_graph

    graph = build_supervisor_graph(model=_FakeModel())  # type: ignore[arg-type]
    g = graph.get_graph()
    assert "integration_agent" in set(g.nodes)
    integ_targets = {e.target for e in g.edges if e.source == "integration_agent"}
    assert integ_targets == {"__end__"}


# --- FLAG ON — the Manager holds the eleven connector tools; guardrail passes -------------------


def _capture_manager_extra_tools(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Build the supervisor graph and capture the tool NAMES handed to build_orchestrator_agent as
    ``extra_tools`` (the Manager's added tool surface). Delegates to the REAL builder so the graph
    stays valid AND the real assert_agent_tools_safe runs over the assembled set."""
    import orchestrator.supervisor as supervisor_mod

    real_build = supervisor_mod.build_orchestrator_agent
    captured: dict[str, Any] = {}

    def _capture(*, model: Any, extra_tools: Any) -> Any:
        captured["extra_tools"] = list(extra_tools)
        return real_build(model=model, extra_tools=extra_tools)

    monkeypatch.setattr(supervisor_mod, "build_orchestrator_agent", _capture)
    supervisor_mod.build_supervisor_graph(model=_FakeModel())  # type: ignore[arg-type]
    return {getattr(t, "name", repr(t)) for t in captured["extra_tools"]}


def test_flag_on_manager_holds_connector_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag ON: all eleven connector tools are added to the Manager's tool set; spawn_integration
    is not (it is dissolved)."""
    _flag_on(monkeypatch)
    names = _capture_manager_extra_tools(monkeypatch)
    assert _EXPECTED_CONNECTOR_TOOL_NAMES <= names, sorted(names)
    assert "spawn_integration" not in names


def test_flag_off_manager_has_no_connector_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag OFF: the Manager's tool set carries NONE of the connector tools, and DOES carry
    spawn_integration (byte-identical to pre-VT-101)."""
    _flag_off(monkeypatch)
    names = _capture_manager_extra_tools(monkeypatch)
    assert _EXPECTED_CONNECTOR_TOOL_NAMES.isdisjoint(names), sorted(
        _EXPECTED_CONNECTOR_TOOL_NAMES & names
    )
    assert "spawn_integration" in names


def test_connector_tools_pass_tool_guardrail() -> None:
    """The eleven connector tools are VT-268-safe on the orchestrator surface — adding them to the
    Manager cannot open the send/write boundary (assert_agent_tools_safe does not raise)."""
    from orchestrator.agent.integration_agent import INTEGRATION_AGENT_TOOLS
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

    assert {t.name for t in INTEGRATION_AGENT_TOOLS} == _EXPECTED_CONNECTOR_TOOL_NAMES
    # Raises ToolGuardrailViolation on any forbidden capability; a clean return is the assertion.
    assert_agent_tools_safe(list(INTEGRATION_AGENT_TOOLS), surface="orchestrator_agent")
