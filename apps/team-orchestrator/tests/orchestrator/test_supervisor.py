"""Supervisor-graph tests (VT-3.4 PR 1/3 + PR 2/3).

PR 1/3 (CL-129): ONE happy-path integration test —
``test_orchestrator_spawns_sales_recovery_returns_campaign_plan`` — two real
Anthropic calls, ``@pytest.mark.integration``, additionally guarded on
ANTHROPIC_API_KEY.

PR 2/3 (CL-202 / CL-203): the landmine-1 precedence test —
``test_supervisor_graph_spawn_vs_no_spawn_precedence``. Keyless: it runs the
REAL ``build_supervisor_graph`` / ``create_agent`` / ``Command.PARENT`` /
conditional edge, substituting only the model layer with a fake. It exercises
the undocumented precedence between the spawn tool's
``Command(goto=..., graph=Command.PARENT)`` and the ``add_conditional_edges``
after the orchestrator node, and captures the observed behaviour.

Module-level imports run after the importorskip guards, so collecting this
file in the CI ``orchestrator`` job import-checks the whole supervisor chain.
"""

from __future__ import annotations

import os
import warnings
from typing import Any

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")

from langchain_anthropic import ChatAnthropic  # noqa: E402 — after importorskip
from langchain_core.language_models import LanguageModelInput  # noqa: E402
from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.runnables import Runnable  # noqa: E402

from orchestrator import routing  # noqa: E402
from orchestrator.supervisor import build_supervisor_graph  # noqa: E402
from orchestrator.types.campaign_plan import CampaignPlan  # noqa: E402


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
def test_orchestrator_spawns_sales_recovery_returns_campaign_plan() -> None:
    """Orchestrator routes to the stub specialist; the specialist returns a
    CampaignPlan.

    Asserts: the graph runs end-to-end; active_agent == 'sales_recovery_agent';
    campaign_plan is a valid CampaignPlan with proposed_by/status as expected.
    """
    model = ChatAnthropic(model="claude-opus-4-7")  # type: ignore[call-arg]
    graph = build_supervisor_graph(model=model)

    result = graph.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Recover dormant customers from the last 60 days",
                }
            ]
        }
    )

    assert result.get("active_agent") == "sales_recovery_agent"
    plan = result.get("campaign_plan")
    assert isinstance(plan, CampaignPlan)
    assert plan.proposed_by == "sales_recovery_agent"
    assert plan.status == "proposed"


# --- VT-3.4 PR 2/3: landmine-1 keyless precedence test (CL-202 / CL-203) ------


class ToolBindableFake(GenericFakeChatModel):
    """GenericFakeChatModel that survives ``create_agent``'s tool binding.

    GenericFakeChatModel inherits BaseChatModel.bind_tools, whose body is
    ``raise NotImplementedError`` — so ``create_agent``, which calls
    ``model.bind_tools(tools)`` to wire the orchestrator's tools, blows up on
    the raw fake (verified probe, CL-203).

    tool_calls are baked into the pre-canned AIMessages, so this fake never
    synthesises a tool call from a schema. ``bind_tools`` only needs to not
    raise, so ``create_agent`` can finish wiring; ``return self`` does that and
    preserves the GenericFakeChatModel invoke-iterator path. The landmine-1
    observation is determined by the langgraph executor, not the model layer —
    this amendment is upstream of the observation surface.
    """

    def bind_tools(
        self,
        tools: Any,
        *,
        tool_choice: Any = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        return self


def _state_summary(state: dict[str, Any]) -> dict[str, Any]:
    """Compact final-state view — scalar fields only, no message dump."""
    return {
        "keys": sorted(state.keys()),
        "active_agent": state.get("active_agent"),
        "terminated_without_spawn": state.get("terminated_without_spawn"),
        "campaign_plan_present": state.get("campaign_plan") is not None,
        "message_count": len(state.get("messages", [])),
    }


def _run_supervisor_path(
    monkeypatch: pytest.MonkeyPatch,
    *,
    canned_messages: list[AIMessage],
    user_text: str,
) -> tuple[list[str], dict[str, Any], list[str], list[str]]:
    """Build + run the real supervisor graph with a ToolBindableFake.

    Wraps ``route_after_orchestrator`` to record whether — and with what key —
    the conditional edge's router actually fires. Returns
    ``(node_visit_trace, final_state, route_keys, captured_warnings)``.
    """
    import orchestrator.supervisor as supervisor_mod

    route_keys: list[str] = []
    real_route = routing.route_after_orchestrator

    def recording_route(state: Any) -> str:
        key: str = real_route(state)
        route_keys.append(key)
        return key

    # build_supervisor_graph reads route_after_orchestrator as a module global;
    # patch the supervisor module's binding before the graph is built.
    monkeypatch.setattr(supervisor_mod, "route_after_orchestrator", recording_route)

    trace: list[str] = []
    final_state: dict[str, Any] = {}
    initial = {"messages": [{"role": "user", "content": user_text}]}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fake = ToolBindableFake(messages=iter(canned_messages))
        # fake is not a ChatAnthropic — intentional: only the model layer is
        # stubbed (CL-203). The graph wiring under test is real.
        graph = build_supervisor_graph(model=fake)
        for mode, chunk in graph.stream(initial, stream_mode=["updates", "values"]):
            if mode == "updates":
                trace.extend(chunk.keys())
            elif mode == "values":
                final_state = chunk

    return trace, final_state, route_keys, [str(w.message) for w in caught]


def test_supervisor_graph_spawn_vs_no_spawn_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Landmine 1 — Command.PARENT vs conditional-edge precedence (CL-202).

    The spawn tool returns ``Command(goto='sales_recovery_agent',
    graph=Command.PARENT)`` while ``add_conditional_edges`` ALSO sits after the
    orchestrator node. Context7 does not document which wins for this
    composition. This test runs both paths with a fake model and records the
    observed node-visit trace, the final state, whether the conditional-edge
    router fired, and any langgraph warnings.

    The assertions hold for BOTH possible precedence outcomes — they pin the
    destination guarantees only. The distinguishing detail (does
    orchestrator_terminal also run on the spawn path, does sales_recovery_agent
    run twice) is printed under LANDMINE1_ prefixes, NOT asserted.
    """
    # Spawn path: 1 spawn tool-call message + cushion content messages so a
    # double-fire (DIVERGENCE) yields a clean trace rather than a StopIteration
    # crash from the fake's exhausted iterator.
    spawn_messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "spawn_sales_recovery", "args": {}, "id": "1"}],
        ),
        AIMessage(content="stub specialist done (1)"),
        AIMessage(content="stub specialist done (2)"),
        AIMessage(content="stub specialist done (3)"),
    ]
    # No-spawn path: orchestrator produces a plain AIMessage, no tool_calls.
    no_spawn_messages = [
        AIMessage(content="Cannot help with that.", tool_calls=[]),
    ]

    s_trace, s_final, s_route, s_warn = _run_supervisor_path(
        monkeypatch,
        canned_messages=spawn_messages,
        user_text="Recover dormant customers from the last 60 days",
    )
    print("LANDMINE1_TRACE: spawn:", s_trace)
    print("LANDMINE1_FINAL_STATE: spawn:", _state_summary(s_final))
    print(
        "LANDMINE1_ROUTE_FN_INVOKED: spawn:",
        bool(s_route),
        "keys=",
        s_route,
    )
    print("LANDMINE1_WARNINGS: spawn:", s_warn)
    print(
        "LANDMINE1_NOTE: spawn orchestrator_terminal_visited=",
        "orchestrator_terminal" in s_trace,
        "sales_recovery_agent_visit_count=",
        s_trace.count("sales_recovery_agent"),
    )

    n_trace, n_final, n_route, n_warn = _run_supervisor_path(
        monkeypatch,
        canned_messages=no_spawn_messages,
        user_text="Just checking in, nothing for you to do today",
    )
    print("LANDMINE1_TRACE: no_spawn:", n_trace)
    print("LANDMINE1_FINAL_STATE: no_spawn:", _state_summary(n_final))
    print(
        "LANDMINE1_ROUTE_FN_INVOKED: no_spawn:",
        bool(n_route),
        "keys=",
        n_route,
    )
    print("LANDMINE1_WARNINGS: no_spawn:", n_warn)

    # Assertions — destination guarantees, true for either precedence outcome.
    assert "sales_recovery_agent" in s_trace, (
        "spawn path must reach sales_recovery_agent"
    )
    assert "orchestrator_terminal" in n_trace, (
        "no-spawn path must reach orchestrator_terminal"
    )
    assert "sales_recovery_agent" not in n_trace, (
        "no-spawn path must NOT reach sales_recovery_agent"
    )
