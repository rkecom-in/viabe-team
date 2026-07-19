"""VT-3.9 PR 1/N — orchestrator-agent skeleton tests.

test_orchestrator_agent_imports_and_compiles is a keyless unit smoke test — it
runs in the CI ``orchestrator`` job (full deps, no ANTHROPIC_API_KEY).

The two routing tests make real Opus 4.7 calls; they are @pytest.mark.integration
(skipped unless RUN_INTEGRATION_TESTS=1, per conftest.py) and additionally
guarded on ANTHROPIC_API_KEY being set.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")


def test_orchestrator_agent_imports_and_compiles():
    """Keyless smoke: the agent module imports and the agent compiles."""
    from orchestrator.agent import ORCHESTRATOR_AGENT_SYSTEM_PROMPT, orchestrator_agent

    assert orchestrator_agent is not None
    assert hasattr(orchestrator_agent, "invoke")
    assert hasattr(orchestrator_agent, "stream")
    assert ORCHESTRATOR_AGENT_SYSTEM_PROMPT.strip(), "system prompt is empty"


def test_record_business_objective_resolves_tenant_from_context(monkeypatch):
    """REGRESSION (VT-482 win-back blocker): the brain occasionally calls
    ``record_business_objective`` with a malformed/placeholder ``tenant_id`` string. That
    used to reach ``UUID(str(tenant_id))`` inside ``write_business_objective`` and raise
    ``ValueError: badly formed hexadecimal UUID string`` — langgraph re-raised the tool
    error and the whole brain run hung at 'running', never reaching the sales_recovery
    spawn. The tool must resolve the AUTHORITATIVE tenant from the ObservabilityContext and
    ignore a bad arg (Pillar 3)."""
    from uuid import UUID

    import importlib

    oa = importlib.import_module("orchestrator.agent.orchestrator_agent")
    from orchestrator.observability.decorators import (
        ObservabilityContext,
        _observability_context,
    )

    real_tenant = UUID("63211ce5-8074-4960-b409-a57c69fe5356")
    seen: dict[str, object] = {}

    def _fake_write(tenant_id, patch):
        seen["tenant_id"] = tenant_id
        seen["patch"] = patch
        return {"objective": patch.get("objective")}

    # The tool imports write_business_objective from orchestrator.knowledge at call time.
    import orchestrator.knowledge as knowledge

    monkeypatch.setattr(knowledge, "write_business_objective", _fake_write)

    fn = oa.record_business_objective.func  # underlying @tool callable
    token = _observability_context.set(
        ObservabilityContext(run_id=UUID(int=1), tenant_id=real_tenant)
    )
    try:
        # The model supplies GARBAGE for tenant_id — must be ignored in favour of the context.
        out = fn(tenant_id="the tenant", objective="raise weekday AOV")
    finally:
        _observability_context.reset(token)

    assert out["status"] == "recorded"
    assert seen["tenant_id"] == real_tenant  # context won, not the garbage arg
    assert seen["patch"] == {"objective": "raise weekday AOV"}


def test_record_business_objective_no_context_bad_arg_returns_error(monkeypatch):
    """With NO ambient context and an unusable arg, the tool returns a structured error the
    agent can route around — it must NOT raise (which would abort the whole run)."""
    import importlib

    oa = importlib.import_module("orchestrator.agent.orchestrator_agent")
    from orchestrator.observability.decorators import _observability_context

    import orchestrator.knowledge as knowledge

    monkeypatch.setattr(
        knowledge,
        "write_business_objective",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    fn = oa.record_business_objective.func
    token = _observability_context.set(None)
    try:
        out = fn(tenant_id="not-a-uuid", objective="x")
    finally:
        _observability_context.reset(token)
    assert out["status"] == "error"


def _tool_calls(result: dict) -> list[str]:
    """Names of every tool call across the agent's returned message history."""
    names: list[str] = []
    for msg in result["messages"]:
        for call in getattr(msg, "tool_calls", None) or []:
            names.append(call["name"])
    return names


# ---------------------------------------------------------------------------
# VT-484 — tool-error recovery middleware (LAUNCH-BLOCKER robustness).
#
# A tool/spawn that RAISES must STILL emit a tool_result (error) for its
# tool_use id, so the conversation stays Anthropic-valid and the run can
# recover / terminate cleanly — NEVER an orphaned tool_use that 400s the
# next call and hangs the run at status='running'. These are KEYLESS: the
# real build_orchestrator_agent + create_agent + ToolNode + the VT-484
# middleware run with a fake model layer (the same ToolBindableFake seam
# the supervisor landmine test uses).
# ---------------------------------------------------------------------------


def _tool_bindable_fake(canned_messages):
    """A GenericFakeChatModel that survives create_agent's bind_tools.

    Same construction the supervisor landmine test uses: bind_tools must not
    raise (BaseChatModel.bind_tools is NotImplementedError) so create_agent can
    finish wiring; tool_calls are baked into the canned AIMessages.
    """
    from typing import Any

    from langchain_core.language_models import LanguageModelInput
    from langchain_core.language_models.fake_chat_models import (
        GenericFakeChatModel,
    )
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable

    class _ToolBindableFake(GenericFakeChatModel):
        def bind_tools(
            self, tools: Any, *, tool_choice: Any = None, **kwargs: Any
        ) -> Runnable[LanguageModelInput, AIMessage]:
            return self

    return _ToolBindableFake(messages=iter(canned_messages))


def test_orchestrator_agent_raising_tool_emits_error_tool_result_no_orphan():
    """VT-484 (a): a tool that RAISES produces an error ToolMessage (a valid
    tool_result with the SAME tool_call_id) and the run RECOVERS — it does not
    hang / abort with an orphaned tool_use.

    Without the middleware, create_agent's ToolNode re-raises the tool error
    (its default handler only swallows ToolInvocationError), the tool_use is
    orphaned, and the next model turn would 400. With the VT-484 middleware the
    error becomes a tool_result the brain reads, and the canned follow-up
    AIMessage proves the loop continued past the failed tool.
    """
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.tools import tool

    from orchestrator.agent.orchestrator_agent import build_orchestrator_agent

    @tool
    def boom_tool(reason: str) -> str:
        """A tool that always raises (simulates a spawn/handoff builder raising)."""
        raise RuntimeError(f"kaboom: {reason}")

    canned = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "boom_tool", "args": {"reason": "x"}, "id": "tc-boom"}
            ],
        ),
        AIMessage(content="recovered: the tool failed, replying directly"),
    ]
    fake = _tool_bindable_fake(canned)
    # The fake stands in for the model layer only — the agent wiring (tools +
    # ToolNode + the VT-484 middleware) is real (parity with the supervisor
    # landmine test, which also passes a non-ChatAnthropic fake here).
    agent = build_orchestrator_agent(fake, extra_tools=[boom_tool])

    result = agent.invoke({"messages": [{"role": "user", "content": "do it"}]})
    msgs = result["messages"]

    tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
    # A tool_result exists for the raising tool_use — no orphan.
    boom_results = [m for m in tool_msgs if m.tool_call_id == "tc-boom"]
    assert boom_results, (
        "raising tool produced NO tool_result — tool_use 'tc-boom' is orphaned "
        f"(would 400 the next Anthropic call). messages={[type(m).__name__ for m in msgs]}"
    )
    assert boom_results[0].status == "error"
    assert "RuntimeError" in boom_results[0].content
    # The run recovered: the canned follow-up AIMessage was reached (the loop did
    # not hang/abort on the failed tool).
    assert isinstance(msgs[-1], AIMessage), [type(m).__name__ for m in msgs]
    assert "recovered" in (msgs[-1].content or "")


def test_orchestrator_agent_tool_error_middleware_reraises_graph_interrupt():
    """VT-484 (a): the recovery middleware must RE-RAISE GraphBubbleUp (the base
    of GraphInterrupt raised by the owner-approval interrupt()) — catching it
    would break the Pillar-7 approval pause. Only ordinary tool exceptions are
    converted to a tool_result."""
    from langgraph.errors import GraphInterrupt

    from orchestrator.agent.orchestrator_agent import (
        _tool_error_to_tool_result,
    )

    class _Req:
        tool_call = {"name": "spawn_x", "id": "tc-1"}

    def _handler_raises_interrupt(_req):
        raise GraphInterrupt("owner approval pause")

    # GraphBubbleUp/GraphInterrupt must propagate unchanged (not become a ToolMessage).
    # wrap_tool_call is a bound method on the middleware instance (self already bound).
    with pytest.raises(GraphInterrupt):
        _tool_error_to_tool_result.wrap_tool_call(
            _Req(), _handler_raises_interrupt
        )


# ---------------------------------------------------------------------------
# VT-484 (b) — win-back routing must target Sales-Recovery, NOT integration.
#
# The live drive mis-routed a win-back ("find/win-back my lapsed customers") to
# spawn_integration. The lane catalogue + roster route map are the deterministic
# contract; the real-LLM proof that the brain actually obeys it runs on deployed
# dev. These keyless tests pin the contract that backs the routing.
# ---------------------------------------------------------------------------


def test_winback_routing_contract_in_manager_prompt():
    """The manager prompt routes win-back to Sales-Recovery and EXPLICITLY forbids
    routing it to integration on a 'need their data first' reasoning (the live
    mis-route). Pins the prompt contract that drives the brain's routing."""
    from orchestrator.agent import ORCHESTRATOR_AGENT_SYSTEM_PROMPT as P

    low = P.lower()
    # Win-back is owned by the Sales lane / Sales-Recovery.
    assert "spawn_sales_recovery" in P
    assert "win back my lapsed" in low or "win-back" in low
    # The anti-pattern is named: a win-back must NOT go to integration "for data".
    assert "spawn_integration" in P
    assert "do not divert it to" in low or "never** send a" in low or (
        "never" in low and "win" in low and "integration" in low
    ), "prompt must explicitly forbid routing win-back to spawn_integration"


def test_roster_routes_spawn_sales_recovery_to_sr_node_not_integration():
    """The roster route map sends spawn_sales_recovery → the sales_recovery_agent
    node (NOT the integration node). This is the deterministic edge that carries a
    win-back to SR once the brain fires the right spawn tool."""
    from orchestrator.agent.roster import spawn_tool_route_keys

    route_for_tool = spawn_tool_route_keys()
    # spawn_sales_recovery must map to the SR route key, distinct from integration's.
    assert route_for_tool.get("spawn_sales_recovery") == "spawn"
    assert route_for_tool.get("spawn_integration") == "spawn_integration"
    assert route_for_tool["spawn_sales_recovery"] != route_for_tool["spawn_integration"]


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
def test_orchestrator_agent_routes_to_escalate_on_legal_keyword():
    """A refund + consumer-court message routes to escalate_to_fazal."""
    from orchestrator.agent import orchestrator_agent

    result = orchestrator_agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "An owner messaged: a customer is demanding a full "
                        "refund and says they will take us to consumer court "
                        "if we refuse."
                    ),
                }
            ]
        }
    )
    calls = _tool_calls(result)
    assert "escalate_to_fazal" in calls, f"expected escalate_to_fazal, got {calls}"
