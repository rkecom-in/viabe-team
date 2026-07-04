"""VT-462 — the Onboarding-Conductor specialist (dynamic, brain-conducted onboarding).

exec-3 of the Team-Manager rebuild. The SCRIPTED onboarding question-QUEUE is replaced by a DYNAMIC
specialist that REASONS what to ask next — bounded by the declarative prereq registry (WHAT must be
collected) and kept honest by the DETERMINISTIC completion check (which OWNS "complete"). The
manager (VT-461) routes an onboarding-incomplete owner to THIS specialist for profile-setup; the
connect/integration specialist remains the SUBSEQUENT step after the profile is collected.

SHAPE — mirrors ``integration_agent.build_integration_agent`` byte-for-byte (langchain
``create_agent`` sub-graph + Opus + ``cache_control`` per VT-194), registered as a ``SpecialistSpec``
in ``agent/roster.py`` (VT-465). REUSE, no duplication:

  - ``onboarding.conductor.decide_next_question`` / ``next_question_for_tenant`` is the dynamic
    next-question DECISION (grounded by ``question_brain.compose_onboarding_questions`` — the
    candidate source — and ``onboarding_journey`` state — resumability). The conductor agent exposes
    it as a TOOL; it does NOT build a parallel composer/state-machine.
  - ``onboarding.conductor.profile_collection_complete`` is the DETERMINISTIC completion check — the
    agent calls it to KNOW whether to keep asking; it NEVER self-marks complete.

The agent holds NO send tool and NO write tool (VT-268 ``assert_agent_tools_safe`` at build):
recording answers + sending the next question run on the deterministic journey reply path
(``onboarding/journey.py``). The conductor reasons about WHAT to ask; the rails own the side-effects.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain.agents import AgentState, create_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool, tool

from orchestrator.agent.lane_tenant import lane_tenant_error, resolve_lane_tenant
from orchestrator.types.trigger_reason import TriggerReason

logger = logging.getLogger("orchestrator.agent.onboarding_conductor")

_PROMPT_PATH = (
    Path(__file__).parent.parent / "prompts" / "onboarding_conductor_system.md"
)
ONBOARDING_CONDUCTOR_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# VT-194 prompt caching — the cached prefix amortises the system prompt + tool inventory across
# dispatches (parity with orchestrator_agent / integration_agent).
ONBOARDING_CONDUCTOR_SYSTEM_MESSAGE = SystemMessage(
    content=[
        {
            "type": "text",
            "text": ONBOARDING_CONDUCTOR_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
)

# mypy --strict needs the call-arg ignore for ChatAnthropic's pydantic kwargs (parity with the
# orchestrator/integration agents).
_MODEL = ChatAnthropic(model="claude-opus-4-7", max_tokens=4096)  # type: ignore[call-arg]


# -----------------------------------------------------------------
# Tools — the conductor's GROUNDING (registry-bounded next question) + the DETERMINISTIC complete
# check. Both delegate to onboarding.conductor (no parallel logic here). NO send/write tool.
# -----------------------------------------------------------------


@tool
def onboarding_next_question(tenant_id: str) -> dict[str, Any]:
    """The registry-grounded NEXT onboarding question to ask, decided DYNAMICALLY from current state.

    REUSE: delegates to ``onboarding.conductor.next_question_for_tenant`` — which reads the tenant's
    discovered draft + already-answered (incl. volunteered/out-of-order) + skipped (deferred) fields
    from the ``onboarding_journey`` row and re-derives the next question from
    ``compose_onboarding_questions`` (the candidate source). Phrase the returned ``prompt_en`` /
    ``prompt_hi`` naturally — it is grounding, not a verbatim script.

    Returns ``{field, kind, prompt_en, prompt_hi, draft_value}`` for the next question, or
    ``{"done": true}`` when the registry-bounded set is satisfied (then call
    ``onboarding_profile_complete`` — the deterministic check OWNS "complete", not you).
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="onboarding_next_question")
    if resolved is None:
        return lane_tenant_error("onboarding_next_question")
    tenant_id = str(resolved)

    from orchestrator.onboarding.conductor import next_question_for_tenant

    decision = next_question_for_tenant(resolved)
    q = decision.next_question
    if q is None:
        logger.info("onboarding_conductor: no registry-bounded question remains tenant=%s", tenant_id)
        return {"done": True}
    return {
        "field": q.field,
        "kind": q.kind,
        "prompt_en": q.prompt_en,
        "prompt_hi": q.prompt_hi,
        "draft_value": q.draft_value,
    }


@tool
def onboarding_profile_complete(tenant_id: str) -> dict[str, Any]:
    """The DETERMINISTIC profile-collection completion check — the conductor NEVER self-declares this.

    REUSE: delegates to ``onboarding.conductor.profile_collection_complete`` — true IFF NO
    registry-bounded question remains that the owner has neither answered nor skipped. A pure
    function of state; the brain conducts the conversation, this owns "done". When true, the system
    hands the owner to the SUBSEQUENT connect/integration step.

    Returns ``{"complete": <bool>}``.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="onboarding_profile_complete")
    if resolved is None:
        return lane_tenant_error("onboarding_profile_complete")
    tenant_id = str(resolved)

    from orchestrator.onboarding.conductor import profile_collection_complete
    from orchestrator.onboarding.draft_profile import get_draft
    from orchestrator.onboarding.journey import _tenant_phase_and_type, get_journey

    g = get_journey(resolved) or {}
    answers = dict(g.get("answers") or {})
    skipped = list(g.get("skipped") or [])
    _, business_type = _tenant_phase_and_type(resolved)
    draft = get_draft(resolved)
    complete = profile_collection_complete(
        business_type=business_type,
        draft=draft,
        answered=list(answers.keys()),
        skipped=skipped,
    )
    logger.info("onboarding_conductor: profile_complete tenant=%s -> %s", tenant_id, complete)
    return {"complete": complete}


@tool
def conductor_escalate_to_fazal(run_id: str, reason: str, owner_stuck_at: str) -> str:
    """Escalate to Fazal when the owner is stuck in profile setup. Log + return ack (last-resort)."""
    logger.warning(
        "ONBOARDING_CONDUCTOR_ESCALATE run_id=%s reason=%s stuck_at=%s",
        run_id, reason, owner_stuck_at,
    )
    return f"[escalated] reason={reason}"


ONBOARDING_CONDUCTOR_TOOLS: list[BaseTool] = [
    onboarding_next_question,
    onboarding_profile_complete,
    conductor_escalate_to_fazal,
]


class OnboardingConductorState(AgentState, total=False):
    """State schema for the onboarding_conductor sub-graph (mirrors IntegrationAgentState).

    Carries the run-identity fields into the sub-graph so a future handoff tool's ``InjectedState``
    can read them (parity with the integration agent; not consumed by the current tool set, which
    keys on ``tenant_id`` passed as a tool arg).
    """

    run_id: UUID | None
    tenant_id: UUID | None
    trigger_reason: TriggerReason | None


def build_onboarding_conductor_agent(
    model: ChatAnthropic = _MODEL,
    *,
    extra_tools: Sequence[BaseTool] = (),
) -> Any:
    """Build the Onboarding-Conductor specialist sub-graph (mirrors ``build_integration_agent``).

    VT-268 fail-CLOSED guardrail: the conductor must never hold a direct send / accounts-book-write /
    ledger-write tool (raises at build if it does) — it reasons about WHAT to ask; the deterministic
    journey reply path owns the side-effects.
    """
    tools = [*ONBOARDING_CONDUCTOR_TOOLS, *extra_tools]
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

    assert_agent_tools_safe(tools, surface="onboarding_conductor")
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=ONBOARDING_CONDUCTOR_SYSTEM_MESSAGE,
        name="onboarding_conductor",
        state_schema=OnboardingConductorState,
    )


onboarding_conductor = build_onboarding_conductor_agent(_MODEL)


__all__ = [
    "ONBOARDING_CONDUCTOR_SYSTEM_MESSAGE",
    "ONBOARDING_CONDUCTOR_SYSTEM_PROMPT",
    "ONBOARDING_CONDUCTOR_TOOLS",
    "OnboardingConductorState",
    "build_onboarding_conductor_agent",
    "onboarding_conductor",
]
