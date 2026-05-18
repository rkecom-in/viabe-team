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
def test_orchestrator_agent_routes_to_spawn_sales_recovery():
    """A dormant-customer winback trigger routes to spawn_sales_recovery."""
    from orchestrator.agent import orchestrator_agent

    result = orchestrator_agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Weekly trigger: about 40 dormant customers from "
                        "January have not returned. The owner wants a winback "
                        "push for them."
                    ),
                }
            ]
        }
    )
    calls = _tool_calls(result)
    assert "spawn_sales_recovery" in calls, f"expected spawn_sales_recovery, got {calls}"
    assert "escalate_to_fazal" not in calls


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
