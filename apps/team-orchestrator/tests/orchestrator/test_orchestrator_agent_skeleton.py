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
