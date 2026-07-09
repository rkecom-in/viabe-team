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
``propose_business_policy``) — this is the point of the conversion, not a guardrail regression:
none of these touch a customer send, the owner's accounts-book Sheet, or the customer ledger (the
ONLY capabilities ``tool_guardrail.assert_agent_tools_safe`` forbids an agent from holding
directly); they write the tenant's OWN onboarding-journey/business-profile/policy state, the same
state the deterministic interceptor wrote before this row.

Completion and full activation stay DETERMINISTIC and un-assertable by the model:
``profile_completion_check`` delegates to ``conductor.profile_collection_complete`` (a pure function
of state); ``activation_check`` delegates to ``onboarding_gate.is_agent_eligible`` (the full,
multi-prerequisite activation bar). The conductor reasons about WHAT to ask / record; these two
checks OWN "done".

The business-policy GRANT itself is a SEPARATE Pillar-7 fix-round redesign (a money-bearing write —
the deterministic guard every autonomous customer_send/spend action reads): the model can only
PROPOSE (validated + clamped bounds, arming a durable owner-approval row shown to the owner in the
specialist's own reply). The GRANT is applied by the DETERMINISTIC approval-glue
(``business_policy.apply_business_policy_decision``) when the owner's own explicit yes/no resolves
that row — NOT by a second tool call. (Fix round 2, CRITICAL: a first cut gave the specialist a
resolve tool, but an inbound owner reply is consumed by ``runner.try_resume_pending_approval``
BEFORE the specialist is ever re-dispatched, so that tool was never reliably reached on a clear
yes — see ``business_policy.py``'s module docstring.) The model never supplies bounds at grant
time; it structurally cannot grant anything itself.

Mode-gating (VT-609 fix round, MAJOR): the Manager routes to this specialist UNCONDITIONALLY across
modes, so a legacy/shadow fall-through dispatch (the journey gate returned None; the Manager still
spawned this node) must NOT reach the write/policy surface — ``build_onboarding_conductor_node``
selects ``ONBOARDING_CONDUCTOR_TOOLS`` (enforce) vs ``LEGACY_ONBOARDING_CONDUCTOR_TOOLS`` (legacy/
shadow — the pre-PR 2-read-tool set) via ``is_enforce()``, checked fresh every dispatch.

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
from uuid import UUID, uuid4

from langchain.agents import AgentState, create_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tools import BaseTool, tool
from langgraph.errors import GraphBubbleUp

from orchestrator.agent.lane_tenant import lane_tenant_error, resolve_lane_tenant
from orchestrator.agents.business_policy import MAX_SANE_SPEND_CEILING_MINOR, PolicyActionClass
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
_MODEL = ChatAnthropic(model="claude-opus-4-8", max_tokens=4096)  # type: ignore[call-arg]
# VT-617/cost: opus-4-8 is the declared capable default (dispatch _BRAIN_MODEL_OPUS) and ~1/3 the
# token cost of legacy 4-7. A registry-grounded, scripted-conducted profile conductor does not need
# the pricier legacy model; this cuts the per-turn cost so a multi-field onboarding save fits under
# the ₹5 (500 paise) hard limit instead of aborting mid-loop. Sets no temperature (opus rejects it).


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

    Also runs the DETERMINISTIC populate-first pass (CL-2026-07-03): derivable profile facts from
    an identity-anchored discovery draft (business_type/category/about/city/website) are
    auto-promoted + recorded here EVERY call, before you reason about what to ask — so you never
    interrogate the owner for a fact public discovery already found. ``populated`` names any field
    this exact call just (re-)populated (empty on a normal call — it is idempotent + card-once); if
    non-empty, present those facts to the owner as a quick confirmable card rather than asking for
    them one-by-one.

    Returns ``{"status": "active"|"complete"|"abandoned"|None, "answers": {field: value, ...},
    "skipped": [field, ...], "flow": <post-profile paced-flow marker, or None>, "populated": {...}}``.
    ``status`` is ``None`` when no journey row exists yet. NEVER ask for a field already present in
    ``answers`` — use ``record_answer`` / ``apply_correction`` instead.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="read_onboarding_state")
    if resolved is None:
        return lane_tenant_error("read_onboarding_state")

    from orchestrator.onboarding.journey import get_journey, populate_profile_from_draft
    from orchestrator.onboarding.turn_brain import _visible_answers

    # CL-2026-07-03 populate-first: run BEFORE the state read so the answers below already reflect
    # any derivable fact discovery just found — idempotent + card-once (returns {} when nothing
    # changed), so this is safe to run on every call (mirrors the interceptor's own eager call at
    # its lazy-start seam). No-op when there is no active journey / no identity-anchored draft.
    # Best-effort (VT-484 tool invariant: a tool must never raise) — populate_profile_from_draft
    # itself carries no try/except of its own (unlike the interceptor's blanket fail-open wrapper).
    try:
        populated = populate_profile_from_draft(resolved)
    except Exception:  # noqa: BLE001 — populate-first is enrichment; a read failure must never
        # break the state read itself (the specialist still gets answers/skipped/status below).
        logger.warning(
            "read_onboarding_state: populate-first pass failed tenant=%s (fail-soft)", resolved,
            exc_info=True,
        )
        populated = {}

    if populated:
        # VT-609 gap-close: populate-first can land the LAST remaining necessities with no owner
        # turn following it — re-check the deterministic completion signal now (mirrors the legacy
        # walker's own lazy-start call site, which completes inline when populate leaves nothing
        # else to ask). Best-effort: the just-populated fields are already committed regardless.
        try:
            from orchestrator.onboarding.journey import maybe_complete_from_populate

            maybe_complete_from_populate(resolved)
        except Exception:  # noqa: BLE001
            logger.warning(
                "read_onboarding_state: post-populate completion check failed tenant=%s", resolved,
                exc_info=True,
            )

    g = get_journey(resolved)
    if g is None:
        return {"status": None, "answers": {}, "skipped": [], "flow": None, "populated": {}}
    raw_answers = dict(g.get("answers") or {})
    return {
        "status": g.get("status"),
        "answers": _visible_answers(raw_answers),
        "skipped": list(g.get("skipped") or []),
        "flow": raw_answers.get("__flow__"),
        "populated": populated,
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


def _profile_is_complete(tenant_id: UUID) -> bool:
    """The DETERMINISTIC profile-collection completeness check, factored out of the
    ``profile_completion_check`` tool so ``propose_business_policy`` can enforce the SAME gate IN
    CODE (VT-609 fix round — the ordering MAJOR: the model must not be able to reach the
    policy-proposal tool before the profile is actually done, regardless of what it believes or
    what order it called tools in)."""
    from orchestrator.onboarding.conductor import profile_collection_complete
    from orchestrator.onboarding.draft_profile import get_draft
    from orchestrator.onboarding.journey import _tenant_phase_and_type, get_journey

    g = get_journey(tenant_id) or {}
    answers = dict(g.get("answers") or {})
    skipped = list(g.get("skipped") or [])
    _, business_type = _tenant_phase_and_type(tenant_id)
    draft = get_draft(tenant_id)
    return profile_collection_complete(
        business_type=business_type,
        draft=draft,
        answered=list(answers.keys()),
        skipped=skipped,
    )


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

    complete = _profile_is_complete(resolved)
    logger.info("onboarding_conductor: profile_complete tenant=%s -> %s", resolved, complete)
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


# --- VT-609 fix round (CRITICAL — Pillar-7) -----------------------------------------------------
#
# ``business_policy.grant_business_policy`` is a MONEY-BEARING write: it sets the machine-
# enforceable bounds EVERY autonomous customer_send/spend action is checked against. The tool used
# to call it DIRECTLY from the specialist's own turn, on prompt-instruction alone — no owner-
# approval provenance, no bounds validation, ``granted_by`` NULL. Redesigned: the specialist can
# only PROPOSE (validated, clamped, arms a durable approval row showing the owner the SPECIFIC
# bounds). The GRANT is applied by the deterministic approval-glue when the owner's own explicit
# yes/no resolves that row (fix round 2 — see ``business_policy.apply_business_policy_decision``
# and its module docstring for why a second SPECIALIST TOOL to do this was scrapped: it was never
# reliably reached). The model never supplies bounds at grant time; it cannot turn "sure, message
# my customers" into a broader grant than what was actually shown.

# ``_VALID_POLICY_ACTION_TYPES`` + the spend ceiling clamp are read off ``business_policy`` — the
# SAME constants ``grant_business_policy`` re-applies at the true chokepoint (fix round 2, MINOR-3:
# belt-and-braces means the SAME rule enforced twice, not two magic numbers that happen to agree).
_VALID_POLICY_ACTION_TYPES = frozenset(e.value for e in PolicyActionClass)
_MAX_SEGMENT_LABEL_LEN = 64  # a segment is a free-form label (e.g. "lapsed_45d") — no fixed
# taxonomy exists anywhere in this codebase (marketing_lane.draft_campaign_plan's own docstring
# confirms this) — so validation here is STRUCTURAL sanity, not a whitelist lookup.


def _validate_policy_bounds(
    allowed_action_types: list[str],
    allowed_segments: list[str],
    frequency_caps: dict[str, int],
    spend_ceiling_minor: int,
) -> dict[str, Any] | None:
    """Validate + clamp the owner's confirmed bounds before they ever reach a proposal row.
    Returns the normalized bounds dict, or ``None`` if nothing survives validation (e.g. every
    action type was unrecognized) — the caller refuses the propose in that case rather than arming
    an empty/meaningless proposal."""
    valid_types = sorted({t for t in (allowed_action_types or []) if t in _VALID_POLICY_ACTION_TYPES})
    valid_segments = sorted(
        {
            s.strip() for s in (allowed_segments or [])
            if isinstance(s, str) and s.strip() and len(s.strip()) <= _MAX_SEGMENT_LABEL_LEN
        }
    )
    caps: dict[str, int] = {}
    for k, v in (frequency_caps or {}).items():
        try:
            caps[str(k)] = max(0, int(v))
        except (TypeError, ValueError):
            continue
    try:
        ceiling = max(0, min(int(spend_ceiling_minor), MAX_SANE_SPEND_CEILING_MINOR))
    except (TypeError, ValueError):
        ceiling = 0

    if not valid_types:
        return None
    return {
        "allowed_action_types": valid_types,
        "allowed_segments": valid_segments,
        "frequency_caps": caps,
        "spend_ceiling_minor": ceiling,
    }


@tool
def propose_business_policy(
    tenant_id: str,
    allowed_action_types: list[str],
    allowed_segments: list[str],
    frequency_caps: dict[str, int],
    spend_ceiling_minor: int = 0,
) -> dict[str, Any]:
    """PROPOSE machine-enforceable action bounds — call this ONLY after the owner has stated
    SPECIFIC bounds in conversation (e.g. "yes, message lapsed customers up to twice a month,
    nothing over 500 rupees"). This does NOT grant anything — it VALIDATES + CLAMPS the bounds and
    arms a durable proposal you must show the owner back in your own reply (the SPECIFIC numbers,
    not a vague "ok, set up"), then wait for their real yes/no. Their reply IS the answer — you do
    NOT call another tool to apply it; the owner's own yes/no resolves the proposal automatically
    and that is the ONLY thing that actually changes the policy. Just acknowledge what they said.

    Requires the profile to be deterministically complete first (refuses otherwise —
    ``profile_completion_check`` is the gate, not your own sense of the conversation).

    ``allowed_action_types`` — a subset of ``customer_send`` / ``spend`` / ``commitment`` /
    ``config`` (an unrecognized value is silently dropped). ``allowed_segments`` — which customer
    segments may be targeted (``"all"`` is a wildcard the owner can grant; free-form labels like
    "lapsed_45d" — there is no fixed segment list). ``frequency_caps`` —
    ``{cap_key: max_in_period}``. ``spend_ceiling_minor`` — max single-action spend, in paise
    (clamped to a defensive sanity ceiling).

    Returns ``{"status": "pending_owner_approval", "approval_id": ..., **the CLAMPED bounds}`` —
    phrase your reply from these returned values (they may differ from what you passed in, if
    anything was clamped/dropped). ``{"status": "error", ...}`` if the profile isn't complete yet
    or nothing valid survived validation. ``{"status": "refused", "reason": "approval_queue_busy"}``
    if another approval is already open for this tenant.
    """
    resolved = resolve_lane_tenant(tenant_id, tool_name="propose_business_policy")
    if resolved is None:
        return lane_tenant_error("propose_business_policy")

    if not _profile_is_complete(resolved):
        return {"status": "error", "error": "profile_setup_incomplete"}

    validated = _validate_policy_bounds(
        allowed_action_types, allowed_segments, frequency_caps, spend_ceiling_minor
    )
    if validated is None:
        return {"status": "error", "error": "no_valid_action_types"}

    from orchestrator.agents.business_policy import propose_business_policy_grant
    from orchestrator.db import tenant_connection

    with tenant_connection(resolved) as conn, conn.transaction():
        result = propose_business_policy_grant(resolved, **validated, conn=conn)
    return result


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
    propose_business_policy,
    conductor_escalate_to_fazal,
]

# VT-609 fix round (MAJOR — mode-gating): the Manager routes to onboarding_conductor
# UNCONDITIONALLY across modes (the roster has no mode branch), so a fall-through case in
# LEGACY/SHADOW (post-profile-flow concluded, bare-greeting-mid-onboarding — the journey gate
# returns None and the Manager still spawns this specialist) can still reach this node in
# production-default legacy. Before this row the specialist held only 2 READ tools there —
# harmless. Now it holds write + policy tools too, so a legacy fall-through could perform a REAL
# onboarding write / policy grant outside the byte-identical legacy contract. Fix: legacy/shadow
# get EXACTLY the pre-PR read-only toolset; only ``enforce`` gets the full write+policy surface.
# Selected in ``build_onboarding_conductor_node`` via ``is_enforce()``, checked fresh every turn
# (the whole supervisor graph — and every specialist's own sub-graph — is already rebuilt fresh on
# every dispatch; this adds no new per-turn cost class).
LEGACY_ONBOARDING_CONDUCTOR_TOOLS: list[BaseTool] = [
    next_required_question,
    profile_completion_check,
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
    tools_mode: str = "enforce",
    extra_tools: Sequence[BaseTool] = (),
) -> Any:
    """Build the Onboarding-Conductor specialist sub-graph (mirrors ``build_integration_agent``).

    ``tools_mode`` (VT-609 fix round, MAJOR — mode-gating): ``"enforce"`` (default) gets the full
    read/write/policy surface; ``"legacy"`` gets EXACTLY the pre-PR read-only toolset
    (``LEGACY_ONBOARDING_CONDUCTOR_TOOLS``) so a legacy/shadow fall-through dispatch can never
    perform a real onboarding write or policy grant outside the byte-identical legacy contract.

    VT-268 fail-CLOSED guardrail: the conductor must never hold a direct customer-send /
    accounts-book-write / ledger-write tool (raises at build if it does) — see the module
    docstring for why the tools above (which DO write onboarding/business-profile/policy state)
    correctly pass this guard.
    """
    base_tools = ONBOARDING_CONDUCTOR_TOOLS if tools_mode == "enforce" else LEGACY_ONBOARDING_CONDUCTOR_TOOLS
    tools = [*base_tools, *extra_tools]
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

    assert_agent_tools_safe(tools, surface="onboarding_conductor")
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=ONBOARDING_CONDUCTOR_SYSTEM_MESSAGE,
        name="onboarding_conductor",
        state_schema=OnboardingConductorState,
        # VT-618: do NOT inherit the parent supervisor's PostgresSaver. A hard-limit abort
        # (cost/tool_calls) mid-ReAct-loop would otherwise persist an orphaned tool_use in a
        # NESTED checkpoint under the parent run thread; the DBOS retry of dispatch_brain then
        # resumes that broken checkpoint and re-sends the orphan to Anthropic → 400 (`tool_use`
        # without following `tool_result`) → deterministic floor → the owner saw a fake "snag
        # saving". The conductor calls no interrupt() and rebuilds onboarding state from the DB
        # each turn (read_onboarding_state), so it needs no cross-superstep checkpoint continuity.
        checkpointer=False,
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
# VT-609 fix round (MINOR — the floor must reflect the policy-confirmation stage too, not just
# profile completeness; telling the owner they're "all set" while the deny-all default is still in
# force is misleading).
_FLOOR_POLICY_PENDING_EN = (
    "Thanks — your profile's all set. One more thing before I can act on your behalf: tell me what "
    "I'm allowed to do automatically (which customers, how often, what spend limit) and I'll set "
    "that up."
)
_FLOOR_POLICY_PENDING_HI = (
    "धन्यवाद — आपकी प्रोफ़ाइल तैयार है। एक और बात: बताइए मैं अपने आप क्या कर सकता हूँ (किन ग्राहकों को, "
    "कितनी बार, कितना खर्च) ताकि मैं वह सेट कर सकूँ।"
)


def _policy_confirmed(tenant_id: UUID) -> bool:
    """Has the owner confirmed ANY business-policy bounds yet (vs the fail-closed deny-all
    default)? Read-only, reuses ``business_policy.get_business_policy`` — no parallel notion of
    policy-completeness. Fail-soft -> False: telling the owner policy confirmation is still
    pending is never WRONG even if this read itself failed (deny-all is the active policy either
    way), whereas the reverse (claiming "all set" on a read failure) could be."""
    try:
        from orchestrator.agents.business_policy import get_business_policy

        policy = get_business_policy(tenant_id)
        return bool(policy.allowed_action_types)
    except Exception:  # noqa: BLE001
        return False


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
            elif not _policy_confirmed(tenant_id):
                # VT-609 fix round (MINOR): the profile is done, but the deny-all default is still
                # in force — "all set" would be misleading here.
                text = _FLOOR_POLICY_PENDING_HI if lang == "hi" else _FLOOR_POLICY_PENDING_EN
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


def _emit_floor_engaged_seam(state: dict[str, Any], exc: Exception) -> None:
    """VT-609 fix round (MAJOR — the floor must not swallow failures INVISIBLY). The deterministic
    floor's whole point is that ``_node`` returns a NORMAL dict on a specialist failure (so the
    owner conversation keeps moving) — but that means ``with_state_transition_hook`` (VT-183, which
    wraps this node at the roster level) sees a successful return and records
    ``status='completed'``. Without a separate signal, a genuine outage (Anthropic down, a DB
    error inside the tool-calling loop) reads as a normal success to observability. This emits a
    discrete, queryable event an outage dashboard/alert can key on — separate from (not instead of)
    the honest scripted reply the floor still composes. Best-effort: an observability failure must
    never re-break the floor it's reporting on."""
    try:
        from orchestrator.observability.log import log_event

        raw_tenant = state.get("tenant_id")
        raw_run = state.get("run_id")
        tenant_id = raw_tenant if isinstance(raw_tenant, UUID) else (UUID(str(raw_tenant)) if raw_tenant else None)
        run_id = raw_run if isinstance(raw_run, UUID) else (UUID(str(raw_run)) if raw_run else uuid4())
        log_event(
            event_type="onboarding_conductor_floor_engaged",
            run_id=run_id,
            tenant_id=tenant_id,
            severity="error",
            component="onboarding_conductor",
            payload={"exception_type": type(exc).__name__},
        )
    except Exception:  # noqa: BLE001
        logger.warning("onboarding_conductor: floor-engaged seam emit failed", exc_info=True)


def build_onboarding_conductor_node(model: ChatAnthropic = _MODEL) -> Any:
    """VT-609 — the onboarding_conductor GRAPH NODE, wrapping the compiled specialist sub-graph
    with the deterministic LLM-down floor. Builds the sub-graph ONCE (matching the prior
    per-graph-build cost of a raw ``build_onboarding_conductor_agent`` node) and returns a PLAIN
    function: on ANY failure of the specialist's own reasoning/tool-calling loop it falls back to
    ``_deterministic_floor_reply`` instead of letting the exception propagate — never silence,
    never a generic escalation ack when an honest, on-topic scripted question is possible.

    VT-609 fix round (MAJOR — mode-gating): the tool surface is selected ONCE here via
    ``is_enforce()``, checked fresh EVERY call (the whole supervisor graph — hence every
    specialist's own sub-graph — is already rebuilt fresh on every dispatch; see
    ``dispatch_brain``, so this adds no new per-turn cost class). ``enforce`` gets the full
    read/write/policy surface; legacy/shadow get EXACTLY the pre-PR read-only toolset.

    ``GraphBubbleUp`` (interrupt/subgraph-control signals) re-raises unchanged — the conductor
    calls no ``interrupt()`` today, but this is the same defense-in-depth carve-out
    ``supervisor._wrap_lane_node_exceptions`` uses. Returning a plain function (rather than the raw
    ``CompiledStateGraph``) is also what lets the roster wrap this node with the VT-183
    state-transition observability hook (``spec.wrap_node=True``) — a compiled sub-graph cannot be
    function-wrapped, but this plain closure can.
    """
    from orchestrator.manager.loop_mode import is_enforce

    tools_mode = "enforce" if is_enforce() else "legacy"
    sub_graph = build_onboarding_conductor_agent(model=model, tools_mode=tools_mode)

    def _node(state: dict[str, Any]) -> Any:
        try:
            return sub_graph.invoke(state)
        except GraphBubbleUp:
            raise
        except Exception as exc:  # noqa: BLE001 — the deterministic floor: the whole point is to
            # catch ANY specialist reasoning failure here, before it ever reaches the generic
            # LaneNodeError net.
            logger.warning(
                "onboarding_conductor: specialist invocation failed — deterministic floor engaged",
                exc_info=True,
            )
            _emit_floor_engaged_seam(state, exc)
            return _deterministic_floor_reply(state)

    return _node


__all__ = [
    "ONBOARDING_CONDUCTOR_SYSTEM_MESSAGE",
    "ONBOARDING_CONDUCTOR_SYSTEM_PROMPT",
    "ONBOARDING_CONDUCTOR_TOOLS",
    "LEGACY_ONBOARDING_CONDUCTOR_TOOLS",
    "OnboardingConductorState",
    "build_onboarding_conductor_agent",
    "build_onboarding_conductor_node",
    "onboarding_conductor",
]
