"""VT-461 — Team-Manager supervisor/brain reframe (exec-2 of the rebuild).

Pins the three load-bearing VT-461 invariants WITHOUT a live Anthropic call:

1. The orchestrator-agent's system prompt is the new **Team-Manager** persona
   (NOT the CL-24 router): it frames the brain as the owner's business manager,
   never customer-service; it carries the manager-vs-specialist division
   (situation+outcome+which-specialist for the manager; the ACTION for the
   specialist) and the bias-to-ACT autonomy framing.
2. ``classify_owner_message`` is wired as the brain's intent prior — the edge
   router surfaces its (already-computed) classification via ``intent_sink``,
   and dispatch renders it into the ``## Manager intent signal`` block. NO
   parallel classifier is built (the same Haiku call feeds both seams).
3. The brain still holds NO send/write tool (VT-268 guard — unchanged shape).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")


# --- (1) the new Team-Manager system prompt ---------------------------------


def _prompt() -> str:
    from orchestrator.agent.orchestrator_agent import ORCHESTRATOR_AGENT_SYSTEM_PROMPT

    return ORCHESTRATOR_AGENT_SYSTEM_PROMPT


def test_prompt_is_team_manager_not_router() -> None:
    p = _prompt().lower()
    assert "team-manager" in p, "prompt must frame the brain as the Team-Manager"
    # The brain is explicitly NOT the CL-24 router and NOT customer-service.
    assert "not a router" in p
    assert "not a customer-service" in p or "not\na customer-service" in p
    # The exact live bug: a greeting must not get a customer-service reply.
    assert "order number" in p, "must call out the 'share your order number' anti-pattern"


def test_prompt_carries_versioned_supersession_header() -> None:
    p = _prompt()
    # Type-1 governance: versioned header that supersedes CL-24.
    assert "version=2.0" in p
    assert "supersedes=CL-24" in p or "supersedes the CL-24" in p.replace("\n", " ")


def test_prompt_carries_manager_vs_specialist_division() -> None:
    p = _prompt().lower()
    # Manager = situation + outcome + which-specialist; specialist = the action.
    assert "situation" in p and "outcome" in p
    assert "which specialist" in p or "which-specialist" in p or "which\nspecialist" in p
    # The manager never prescribes the action / never needs domain expertise.
    assert "action" in p
    assert "domain expertise" in p


def test_prompt_carries_bias_to_act_autonomy() -> None:
    p = _prompt().lower()
    assert "bias to act" in p or "biased to act" in p or "bias to\nact" in p
    # "Ask the owner" is a last-resort escalation, not the default.
    assert "last-resort" in p or "last resort" in p
    assert "escalat" in p


def test_prompt_keeps_safety_framing_brain_not_sender() -> None:
    p = _prompt().lower()
    # The brain is NOT the writer/sender; rails are deterministic and not its job.
    assert "not the writer or sender" in p or "not the writer/sender" in p
    assert "deterministic" in p
    # Onboarding-complete stays a deterministic check, not the brain's vibe.
    assert "deterministic check" in p


def test_prompt_routes_greeting_and_onboarding_correctly() -> None:
    p = _prompt().lower()
    # VT-462 — greeting mid-onboarding -> profile-setup (spawn_onboarding_conductor) FIRST, with
    # connect (spawn_integration) as the subsequent step. Not customer-service.
    assert "spawn_onboarding_conductor" in p
    assert "spawn_integration" in p
    assert "spawn_sales_recovery" in p
    assert "greeting" in p


# --- (2) classify wired as the brain's intent prior -------------------------


def test_intent_block_renders_classification_as_prior() -> None:
    from orchestrator.agent.dispatch import _build_manager_intent_block

    block = _build_manager_intent_block(
        {
            "classification": "first_data_step_onboarding",
            "confidence": 0.91,
            "suggested_action": "begin first-data-step floor",
        }
    )
    assert block is not None
    assert "## Manager intent signal" in block
    assert "first_data_step_onboarding" in block
    assert "0.91" in block
    assert "begin first-data-step floor" in block
    # It is a PRIOR, not a verdict (so the brain still reasons).
    assert "prior" in block.lower()


def test_intent_block_absent_when_no_classification() -> None:
    from orchestrator.agent.dispatch import _build_manager_intent_block

    # Empty sink (classify skipped/failed) -> no block; brain reasons from the message.
    assert _build_manager_intent_block({}) is None
    assert _build_manager_intent_block({"classification": None}) is None


def test_edge_router_surfaces_classification_to_intent_sink() -> None:
    """The SAME classification the edge router runs is surfaced to dispatch via the
    intent_sink — no second Haiku call. A fall-through intent (e.g. 'other') populates
    the sink while still returning None (falls through to the agent)."""
    import orchestrator.edge_cases_router as r

    sink: dict[str, object] = {}
    ev = SimpleNamespace(body="hi", sender_phone=None)
    out = r.route_edge_case(
        tenant_id="t",
        event=ev,
        classify_fn=lambda b: SimpleNamespace(
            classification="other", confidence=0.85, suggested_action="acknowledge greeting"
        ),
        intent_sink=sink,
    )
    # 'other' falls through to the agent...
    assert out is None
    # ...but the classification is now available to the brain.
    assert sink["classification"] == "other"
    assert sink["confidence"] == pytest.approx(0.85)
    assert sink["suggested_action"] == "acknowledge greeting"


def test_edge_router_intent_sink_untouched_on_classify_failure() -> None:
    """A classify failure must not crash dispatch and must leave the sink empty (the
    brain then reasons from the message alone)."""
    import orchestrator.edge_cases_router as r

    def boom(_b: str) -> object:
        raise RuntimeError("model JSON broke")

    sink: dict[str, object] = {}
    ev = SimpleNamespace(body="hi", sender_phone=None)
    out = r.route_edge_case(
        tenant_id="t", event=ev, classify_fn=boom, intent_sink=sink
    )
    assert out is None
    assert sink == {}


def test_no_parallel_classifier_dispatch_reuses_classify_owner_message() -> None:
    """Guard the 'no duplicate classifier' standing: dispatch must route its intent prior
    through the existing classify_owner_message seam (the edge router), not a new one."""
    import orchestrator.agent.dispatch as d

    src = d.__file__
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    # dispatch wires classify via the edge router's intent_sink — it does NOT import a
    # second classifier of its own.
    assert "intent_sink" in text
    assert "_build_manager_intent_block" in text
    assert "classify_owner_message_v" not in text, "must not reference a parallel classifier"


# --- (3) the brain still holds NO send/write tool (VT-268, unchanged) -------


def test_brain_holds_no_send_or_write_tool() -> None:
    from orchestrator.agent.orchestrator_agent import ORCHESTRATOR_AGENT_TOOLS
    from orchestrator.agent.tool_guardrail import find_forbidden_tools

    # The reframe swapped the PROMPT, not the tool surface — the VT-268 capability rail
    # still holds: no send / accounts-book-write / ledger-write tool on the brain.
    assert find_forbidden_tools(ORCHESTRATOR_AGENT_TOOLS) == []
