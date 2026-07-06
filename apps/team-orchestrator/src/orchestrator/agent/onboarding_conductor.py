"""VT-462 — the Onboarding-Conductor specialist (dynamic, brain-conducted onboarding).

VT-609 (Loop Package 4, amendment A2) — the REAL tool surface. The conductor used to hold NO write
tool at all (recording answers + sending the next question ran on the deterministic journey
INTERCEPT, ``onboarding/journey.py``'s ``maybe_handle_journey_reply``, called BEFORE the Manager
ever saw the message — runner.py:873). That interceptor is now mode-gated: legacy/shadow keep
routing there byte-identically; in ``enforce`` mode it stops consuming ordinary owner messages, and
THIS specialist — spawned by the Manager like any other roster member — conducts the conversation
for real, through the tool surface below. The journey ROW (``onboarding_journey``) stays the
resumable state substrate for BOTH paths; the tools below are thin, tenant-scoped wrappers around
the EXACT SAME deterministic functions the interceptor used (``onboarding/journey.py``'s new
``record_extracted_answer`` / ``record_field_skip`` / ``confirm_field_answer``, and the existing
``onboarding/conductor.py`` decision + completion functions) — no parallel logic, no drift.

Tenancy (VT-603, binding on every tool here): the AMBIENT dispatch context always wins —
``resolve_lane_tenant`` resolves it; a model-supplied ``tenant_id`` that disagrees is logged and
ignored, never trusted. Every tool returns a structured ``{"status": "error", ...}`` dict on an
unresolvable tenant (VT-484 invariant) — NEVER raises.

The agent now HOLDS write tools (``record_answer`` / ``record_skip`` / ``apply_correction`` /
``confirm_business_policy``) — this is the point of the conversion, not a guardrail regression: none
of these touch a customer send, the owner's accounts-book Sheet, or the customer ledger (the ONLY
capabilities ``tool_guardrail.assert_agent_tools_safe`` forbids an agent from holding directly); they
write the tenant's OWN onboarding-journey/business-profile/policy state, the same state the
deterministic interceptor wrote before this row.

Completion and full activation stay DETERMINISTIC and un-assertable by the model:
``profile_completion_check`` delegates to ``conductor.profile_collection_complete`` (a pure function
of state); ``activation_check`` delegates to ``onboarding_gate.is_agent_eligible`` (the full,
multi-prerequisite activation bar). The conductor reasons about WHAT to ask / record; these two
checks OWN "done".

The deterministic LLM-down floor (amendment A2, VT-597 shape) RELOCATES here from journey.py's old
``turn_brain None -> handle_reply`` fallback: ``build_onboarding_conductor_node`` wraps the compiled
sub-graph's own invocation so a hard failure of the specialist's OWN reasoning/tool-calling loop
(an LLM call error/timeout/unparseable output) never silences the owner or falls through ungated —
it deterministically composes the next scripted question via ``conductor.next_question_for_tenant``
(LLM-free, pure) instead. See ``_deterministic_floor_reply`` below.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain.agents import AgentState, create_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tools import BaseTool, tool
from langgraph.errors import GraphBubbleUp

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
# Tools — the conductor's read/write surface over onboarding_journey + the deterministic gates.
# Every tool delegates to onboarding.journey / onboarding.conductor / onboarding.draft_profile /
# agents.onboarding_gate / agents.business_policy (no parallel logic here).
# -----------------------------------------------------------------


@tool
def read_onboarding_state(tenant_id: str) -> dict[str, Any]:
    """Read this tenant's onboarding-journey state. Call this FIRST every turn (each inbound is a
    fresh thread — this is how you resume where you left off and see what the owner already told
    you, including anything volunteered out of order or in a prior turn).

    Returns ``{"status": "active"|"complete"|"abandoned"|None, "answers": {field: value, ...},
    "skipped": [field, ...], "flow": <post-profile paced-flow marker, or None>}``. ``status`` is
    ``None`` when no journey row exists yet. NEVER ask for a field already present in ``answers`` —
    use ``record_answer`` / ``apply_correction`` instead.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="read_onboarding_state")
    if resolved is None:
        return lane_tenant_error("read_onboarding_state")

    from orchestrator.onboarding.journey import get_journey
    from orchestrator.onboarding.turn_brain import _visible_answers

    g = get_journey(resolved)
    if g is None:
        return {"status": None, "answers": {}, "skipped": [], "flow": None}
    raw_answers = dict(g.get("answers") or {})
    return {
        "status": g.get("status"),
        "answers": _visible_answers(raw_answers),
        "skipped": list(g.get("skipped") or []),
        "flow": raw_answers.get("__flow__"),
    }


@tool
def extract_owner_answer(tenant_id: str, field: str, value: str) -> dict[str, Any]:
    """Record a business-context field the owner JUST told you, as a plain (unconfirmed) answer —
    a gap-fill field like hours / typical customer / price range, not a confirm-the-draft field.
    Does NOT promote to the canonical business profile (use ``record_answer`` for that). Absorb a
    multi-field reply by calling this once per field in the SAME turn — never ask again for a field
    you already have. Returns ``{"recorded": bool, "field": ...}``.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="extract_owner_answer")
    if resolved is None:
        return lane_tenant_error("extract_owner_answer")

    from orchestrator.onboarding.journey import record_extracted_answer

    return record_extracted_answer(resolved, field, value)


@tool
def record_answer(tenant_id: str, field: str, value: str) -> dict[str, Any]:
    """Promote a CONFIRMED field to the canonical business profile — the owner said yes to a
    confirm-the-draft question, or stated the value as settled fact. Routes through the CL-390
    never-assert promotion gate: an off-taxonomy ``business_type`` is recorded but NOT asserted as
    fact (``promoted`` comes back False — treat that field as still unresolved, do not claim it is
    set). Returns ``{"recorded": bool, "promoted": bool, "field": ...}``.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="record_answer")
    if resolved is None:
        return lane_tenant_error("record_answer")

    from orchestrator.onboarding.journey import confirm_field_answer

    return confirm_field_answer(resolved, field, value)


@tool
def record_skip(tenant_id: str, field: str) -> dict[str, Any]:
    """The owner wants to skip/defer ``field`` ("later" / "skip" / "pass"). Defers it — it will not
    be re-asked every turn; only a later full pass revisits it. Returns
    ``{"recorded": bool, "field": ...}``.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="record_skip")
    if resolved is None:
        return lane_tenant_error("record_skip")

    from orchestrator.onboarding.journey import record_field_skip

    return record_field_skip(resolved, field)


@tool
def apply_correction(tenant_id: str, field: str, value: str) -> dict[str, Any]:
    """The owner is CORRECTING a value you already have (confirmed or auto-populated) — e.g. "no,
    actually we're in Pune, not Mumbai". Records the corrected value through the SAME promotion
    gate ``record_answer`` uses (an owner correction always wins over a prior discovery/populate
    value). Do NOT call this with a bare rejection ("no" / "wrong") as the value — ask for the
    correct value FIRST, then call this with what the owner actually gives you. Returns
    ``{"recorded": bool, "promoted": bool, "field": ...}``.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="apply_correction")
    if resolved is None:
        return lane_tenant_error("apply_correction")

    from orchestrator.onboarding.journey import confirm_field_answer

    return confirm_field_answer(resolved, field, value)


@tool
def next_required_question(tenant_id: str) -> dict[str, Any]:
    """The registry-grounded NEXT onboarding question to ask, decided DYNAMICALLY from current
    state.

    REUSE: delegates to ``onboarding.conductor.next_question_for_tenant`` — which reads the
    tenant's discovered draft + already-answered (incl. volunteered/out-of-order) + skipped
    (deferred) fields from the ``onboarding_journey`` row and re-derives the next question from
    ``compose_onboarding_questions`` (the candidate source). Phrase the returned ``prompt_en`` /
    ``prompt_hi`` naturally — it is grounding, not a verbatim script.

    Returns ``{field, kind, prompt_en, prompt_hi, draft_value}`` for the next question, or
    ``{"done": true}`` when the registry-bounded set is satisfied (then call
    ``profile_completion_check`` — the deterministic check OWNS "complete", not you).
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="next_required_question")
    if resolved is None:
        return lane_tenant_error("next_required_question")
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
def profile_completion_check(tenant_id: str) -> dict[str, Any]:
    """The DETERMINISTIC profile-collection completion check — the conductor NEVER self-declares
    this.

    REUSE: delegates to ``onboarding.conductor.profile_collection_complete`` — true IFF NO
    registry-bounded question remains that the owner has neither answered nor skipped. A pure
    function of state; the brain conducts the conversation, this owns "done". When true, the
    system hands the owner to the SUBSEQUENT connect/integration step.

    Returns ``{"complete": <bool>}``.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="profile_completion_check")
    if resolved is None:
        return lane_tenant_error("profile_completion_check")
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
def activation_check(tenant_id: str, agent: str = "sales_recovery") -> dict[str, Any]:
    """The FULL deterministic agent-activation check (journey-complete + GST verification + a
    connected data source + ingested customers + ownership-verified, per ``agent``'s declared bar
    in ``activation_registry``) — the conductor NEVER self-asserts this either. This is the gate
    the NEXT specialist (``sales_recovery`` by default; also ``integration_agent``) needs crossed.
    Call it to know whether the owner is ready to move on — never claim activation yourself.

    Returns ``{"agent": ..., "eligible": bool}``.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="activation_check")
    if resolved is None:
        return lane_tenant_error("activation_check")

    from orchestrator.agents.onboarding_gate import is_agent_eligible
    from orchestrator.db import tenant_connection

    with tenant_connection(resolved) as conn:
        eligible = is_agent_eligible(resolved, agent, conn=conn)
    return {"agent": agent, "eligible": eligible}


@tool
def confirm_business_policy(
    tenant_id: str,
    allowed_action_types: list[str],
    allowed_segments: list[str],
    frequency_caps: dict[str, int],
    spend_ceiling_minor: int = 0,
) -> dict[str, Any]:
    """Record the owner's CONFIRMED machine-enforceable action bounds
    (``business_policy.grant_business_policy``) — call this ONLY after the owner has explicitly
    agreed to SPECIFIC bounds in conversation (e.g. "yes, message lapsed customers up to twice a
    month, nothing over 500 rupees"). This is an explicit OWNER act, never your own choice.

    ``allowed_action_types`` — a subset of ``customer_send`` / ``spend`` / ``commitment`` /
    ``config``. ``allowed_segments`` — which customer segments may be targeted (``"all"`` is a
    wildcard the owner can grant). ``frequency_caps`` — ``{cap_key: max_in_period}`` (e.g.
    ``{"customer_send_per_month": 2}``). ``spend_ceiling_minor`` — the max single-action spend, in
    paise.

    Missing/unconfirmed policy stays DENY-ALL (every autonomous business action is blocked) — this
    tool is the ONLY way that changes. Never claim a policy is in place without actually calling
    this.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="confirm_business_policy")
    if resolved is None:
        return lane_tenant_error("confirm_business_policy")

    from orchestrator.agents.business_policy import grant_business_policy
    from orchestrator.db import tenant_connection

    with tenant_connection(resolved) as conn:
        policy = grant_business_policy(
            resolved,
            allowed_action_types=allowed_action_types,
            allowed_segments=allowed_segments,
            frequency_caps=frequency_caps,
            spend_ceiling_minor=spend_ceiling_minor,
            conn=conn,
        )
    return {
        "granted": True,
        "allowed_action_types": sorted(policy.allowed_action_types),
        "allowed_segments": sorted(policy.allowed_segments),
        "frequency_caps": dict(policy.frequency_caps),
        "spend_ceiling_minor": policy.spend_ceiling_minor,
    }


@tool
def conductor_escalate_to_fazal(run_id: str, reason: str, owner_stuck_at: str) -> str:
    """Escalate to Fazal when the owner is stuck in profile setup. Log + return ack (last-resort)."""
    logger.warning(
        "ONBOARDING_CONDUCTOR_ESCALATE run_id=%s reason=%s stuck_at=%s",
        run_id, reason, owner_stuck_at,
    )
    return f"[escalated] reason={reason}"


ONBOARDING_CONDUCTOR_TOOLS: list[BaseTool] = [
    read_onboarding_state,
    extract_owner_answer,
    record_answer,
    record_skip,
    apply_correction,
    next_required_question,
    profile_completion_check,
    activation_check,
    confirm_business_policy,
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

    VT-268 fail-CLOSED guardrail: the conductor must never hold a direct customer-send /
    accounts-book-write / ledger-write tool (raises at build if it does) — see the module
    docstring for why the tools above (which DO write onboarding/business-profile/policy state)
    correctly pass this guard.
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


# -----------------------------------------------------------------
# VT-609 amendment A2 — the deterministic LLM-down floor + the node-level wrapper roster.py uses.
# -----------------------------------------------------------------

_FLOOR_FALLBACK_EN = (
    "Sorry — I'm having a little trouble on my end right now. Could you say that again in a moment?"
)
_FLOOR_FALLBACK_HI = (
    "माफ़ कीजिए — अभी मुझे थोड़ी दिक्कत हो रही है। क्या आप एक पल में फिर से बता सकते हैं?"
)
_FLOOR_ALL_SET_EN = "Thanks — I've got everything I need for your profile for now."
_FLOOR_ALL_SET_HI = "धन्यवाद — फ़िलहाल आपकी प्रोफ़ाइल के लिए जो चाहिए था मिल गया।"


def _floor_language(tenant_id: UUID) -> str:
    """Best-effort tenant-language read for the floor's reply choice. Fail-soft -> 'en' (mirrors the
    fail-soft posture of every other best-effort enrichment read in this codebase)."""
    try:
        from orchestrator.db import tenant_connection

        with tenant_connection(tenant_id) as conn:
            row = conn.execute(
                "SELECT preferred_language, language_preference FROM tenants WHERE id = %s",
                (str(tenant_id),),
            ).fetchone()
        if row is None:
            return "en"
        preferred = row["preferred_language"] if isinstance(row, dict) else row[0]
        fallback = row["language_preference"] if isinstance(row, dict) else row[1]
        lang = preferred or fallback or "en"
        return "hi" if str(lang).lower().startswith("hi") else "en"
    except Exception:  # noqa: BLE001 — a language-read hiccup never blocks the floor itself
        return "en"


def _deterministic_floor_reply(state: dict[str, Any]) -> dict[str, Any]:
    """VT-609 amendment A2 — the deterministic LLM-down floor (VT-597 shape). Composes a scripted,
    honest, on-topic reply via ``conductor.next_question_for_tenant`` (LLM-free, pure) instead of a
    canned apology, so the owner keeps making progress even when the specialist's own model call
    failed/timed out/unparsed. Falls back to a generic honest-trouble line only when even the
    floor's OWN read fails (defense in depth — NEVER silence, NEVER an unclassified message falling
    through ungated)."""
    tenant_id = state.get("tenant_id")
    text: str | None = None
    lang = "en"
    if tenant_id is not None:
        lang = _floor_language(tenant_id)
        try:
            from orchestrator.onboarding.conductor import next_question_for_tenant

            decision = next_question_for_tenant(tenant_id)
            q = decision.next_question
            if q is not None:
                text = q.prompt_hi if lang == "hi" else q.prompt_en
            else:
                text = _FLOOR_ALL_SET_HI if lang == "hi" else _FLOOR_ALL_SET_EN
        except Exception:  # noqa: BLE001 — the floor's own floor; fall through to the generic line
            logger.warning(
                "onboarding_conductor: deterministic floor's own next-question read failed",
                exc_info=True,
            )
    if not text:
        text = _FLOOR_FALLBACK_HI if lang == "hi" else _FLOOR_FALLBACK_EN
    return {"messages": [AIMessage(content=text)]}


def build_onboarding_conductor_node(model: ChatAnthropic = _MODEL) -> Any:
    """VT-609 — the onboarding_conductor GRAPH NODE, wrapping the compiled specialist sub-graph
    with the deterministic LLM-down floor. Builds the sub-graph ONCE (matching the prior
    per-graph-build cost of a raw ``build_onboarding_conductor_agent`` node) and returns a PLAIN
    function: on ANY failure of the specialist's own reasoning/tool-calling loop it falls back to
    ``_deterministic_floor_reply`` instead of letting the exception propagate — never silence,
    never a generic escalation ack when an honest, on-topic scripted question is possible.

    ``GraphBubbleUp`` (interrupt/subgraph-control signals) re-raises unchanged — the conductor
    calls no ``interrupt()`` today, but this is the same defense-in-depth carve-out
    ``supervisor._wrap_lane_node_exceptions`` uses. Returning a plain function (rather than the raw
    ``CompiledStateGraph``) is also what lets the roster wrap this node with the VT-183
    state-transition observability hook (``spec.wrap_node=True``) — a compiled sub-graph cannot be
    function-wrapped, but this plain closure can.
    """
    sub_graph = build_onboarding_conductor_agent(model=model)

    def _node(state: dict[str, Any]) -> Any:
        try:
            return sub_graph.invoke(state)
        except GraphBubbleUp:
            raise
        except Exception:  # noqa: BLE001 — the deterministic floor: the whole point is to catch ANY
            # specialist reasoning failure here, before it ever reaches the generic LaneNodeError net.
            logger.warning(
                "onboarding_conductor: specialist invocation failed — deterministic floor engaged",
                exc_info=True,
            )
            return _deterministic_floor_reply(state)

    return _node


__all__ = [
    "ONBOARDING_CONDUCTOR_SYSTEM_MESSAGE",
    "ONBOARDING_CONDUCTOR_SYSTEM_PROMPT",
    "ONBOARDING_CONDUCTOR_TOOLS",
    "OnboardingConductorState",
    "build_onboarding_conductor_agent",
    "build_onboarding_conductor_node",
    "onboarding_conductor",
]
