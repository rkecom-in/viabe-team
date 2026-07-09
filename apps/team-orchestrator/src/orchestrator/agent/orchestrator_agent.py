"""Orchestrator-Agent — the supervisor of the multi-agent graph (VT-3.9 / VT-3.4 / VT-125).

An Opus 4.7 agent built with langchain ``create_agent`` and the reviewed system
prompt. ``build_orchestrator_agent`` is the factory: callers pass ``extra_tools``
to add context-specific tools (VT-3.4's supervisor passes the ``spawn_sales_recovery``
handoff tool — a specialist handoff is only meaningful inside the parent graph,
so it is NOT in the base tool set).

The module-level ``orchestrator_agent`` is the default-built instance (base tools
only); it is the importable handle used by tests and any standalone caller.

VT-125 (this row) registers the broader tool inventory: in-scope tools that
exist on main (``compose_owner_output_tool``, ``self_evaluate``) plus
explicit STUB tools for L0 memory / send-whatsapp / subscriber-state /
pipeline-history. The stubs log intent + return placeholder outputs;
their real wiring lands in successor VT-N rows (tagged in each stub's
docstring).

Hard-limit enforcement (5 tool calls / 10K tokens / depth 3 / 2-min /
₹5) lives in the companion `orchestrator_agent_driver.py` (VT-125).
The agent itself is a langchain `create_agent` runnable — the driver
wraps invocation with usage tracking + `HardLimitExceeded` raising.

Observability: orchestrator-agent uses langchain's ``ChatAnthropic`` (NOT
direct ``client.messages.create``), so VT-182's ``@with_reasoning_capture``
decorator does not apply. VT-125 adds ``OrchestratorReasoningCallback`` —
a ``langchain_core.callbacks.BaseCallbackHandler`` that fires on
``on_llm_end`` and calls ``write_step('agent_reasoning_step', ...)``
under the active ``ObservabilityContext`` (VT-181 ContextVar). Callers
attach the callback per invocation via the driver.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import wrap_tool_call
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.errors import GraphBubbleUp

from orchestrator.observability.decorators import tool_step
from orchestrator.observability.envelopes.l0_query import (
    L0QueryInput,
    L0QueryOutput,
)
from orchestrator.observability.envelopes.l0_write import (
    L0WriteInput,
    L0WriteOutput,
)
from orchestrator.observability.l0_memory import (
    FragmentType,
    query_l0 as _query_l0_impl,
    write_l0_fragment as _write_l0_fragment_impl,
)
from orchestrator.types.trigger_reason import TriggerReason

logger = logging.getLogger("orchestrator.agent")

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "orchestrator_agent_system.md"
ORCHESTRATOR_AGENT_SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# VT-194 prompt caching: wrap the system prompt in a SystemMessage whose
# content is a structured block list carrying ``cache_control:
# {"type": "ephemeral"}``. Anthropic caches the marked prefix for 5 min;
# subsequent dispatches within the TTL read the cached tokens at ~10%
# of the input-token cost (90% discount on the cached prefix). Per
# Anthropic docs, when a system block is cached and tool schemas follow
# in the same request, the tool schemas are typically cached as part of
# the same prefix (validated empirically in VT-194 canary A1 — see
# ``vt194_prompt_caching.py``). Q1 Option A locked per Cowork plan-review.
ORCHESTRATOR_AGENT_SYSTEM_MESSAGE = SystemMessage(
    content=[
        {
            "type": "text",
            "text": ORCHESTRATOR_AGENT_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
)

# VT-632 (Step 1) — the owner-reply-tool directive, appended to the system prompt ONLY when the
# reply_to_owner tool is bound (flag-gated). Kept as a build-time append (not in the static .md) so
# the instruction and the tool are turned on together: telling the model to call a tool it doesn't
# hold would just confuse it. See build_orchestrator_agent + tools/reply_to_owner.py.
_REPLY_TOOL_DIRECTIVE = (
    "\n\n## Replying to the owner (REQUIRED)\n"
    "You have a tool, `reply_to_owner`, and it is the ONLY channel that reaches the owner. END "
    "EVERY handle-directly turn by calling `reply_to_owner` with the COMPLETE message you want the "
    "owner to read, in their language. Text you write but do NOT pass to the tool is never "
    "delivered. You never pass a phone number — the runtime sends it to the owner.\n"
    "- After you delegate and read the result, call `reply_to_owner` to tell the owner what "
    "happened — do not stop at an intention.\n"
    "- NEVER claim you did something (\"done\", \"sent\", \"I've connected it\") unless a tool "
    "actually did it this turn. If you only intend to act, take the action, THEN report it.\n"
    "- Do NOT repeat a message you already sent. If you have nothing new, give the next concrete "
    "step, delegate the work, or ask ONE specific question."
)


def _reply_to_owner_enabled() -> bool:
    """VT-632 flag gate. reply_to_owner (+ its directive + the dispatch scrape-skip) is active ONLY
    when ``MANAGER_REPLY_TOOL`` is truthy. Dev-first rollout; the production cutover is Fazal-only.
    Default OFF ⇒ the emission path is byte-identical to pre-VT-632."""
    return os.getenv("MANAGER_REPLY_TOOL", "").strip().lower() in {"1", "true", "on", "yes"}


def _system_message_with_reply_directive() -> SystemMessage:
    """The base system prompt plus the reply_to_owner directive (VT-632). The base text keeps its
    ephemeral cache_control; the short directive rides as a second, uncached block."""
    return SystemMessage(
        content=[
            {
                "type": "text",
                "text": ORCHESTRATOR_AGENT_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": _REPLY_TOOL_DIRECTIVE},
        ]
    )

# Pinned exactly in pyproject (langgraph / langchain-* == ): the agent's model
# behaviour is version-sensitive, so model + library bumps are Type 2 changes.
# The call-arg ignore below is needed because mypy --strict does not expand
# ChatAnthropic's pydantic fields into __init__ kwargs without the pydantic
# plugin; the call is valid at runtime (smoke-tested) and a repo-wide mypy
# plugin change is out of scope for this PR.
_MODEL = ChatAnthropic(model="claude-opus-4-7", max_tokens=4096)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Base tools — VT-125 inventory (existing real tools + STUBs for not-yet-shipped).
#
# Stub tools log intent + return placeholder. Marked in docstrings with the
# successor VT-N row that ships the real wiring. The orchestrator-agent's
# system prompt enumerates these so the model knows the surface area.
# ---------------------------------------------------------------------------


@tool
def escalate_to_fazal(run_id: str, reason: str, context: str) -> str:
    """Escalate to Fazal. Log-only in this skeleton; real wiring is VT-3.6."""
    logger.warning(
        "ESCALATE_TO_FAZAL run_id=%s reason=%s context=%s",
        run_id,
        reason,
        context,
    )
    return f"[skeleton] escalation logged for run_id={run_id}"


# VT-126: real L0 memory tools (replaced VT-125 stubs).
#
# The langchain @tool decorator wraps an observability-decorated impl: the
# inner ``_write_l0_fragment_impl`` / ``_query_l0_impl`` are wrapped at
# import time via @tool_step, so each tool call writes one
# pipeline_steps row with step_kind='l0_write' / 'l0_query' (CL-220) and
# the TOOL_STEP_REGISTRY entry is created exactly once per process.

_write_l0_fragment_observed = tool_step(
    step_kind="l0_write",
    envelope_in=L0WriteInput,
    envelope_out=L0WriteOutput,
    step_name="write_l0_fragment",
)(_write_l0_fragment_impl)

_query_l0_observed = tool_step(
    step_kind="l0_query",
    envelope_in=L0QueryInput,
    envelope_out=L0QueryOutput,
    step_name="query_l0",
)(_query_l0_impl)


@tool
def write_l0_fragment(
    fragment_type: str,
    cohort_key: str,
    content: dict[str, Any],
) -> dict[str, Any]:
    """Append a cohort-keyed L0 memory fragment.

    Use for routing decisions, specialist outcomes, or trigger patterns
    you'd want a future invocation to see — fragments are aggregated
    across tenants under k-anonymity (CL-28, k=10). NEVER embed tenant-
    identifying content; the runtime PII gate rejects writes that
    redact under ``redact_for_log``.
    """
    # langchain @tool exposes a JSON-schema with `str` (the Literal narrowing
    # happens inside the impl + DB CHECK constraint). cast keeps mypy --strict
    # happy at the impl boundary.
    return _write_l0_fragment_observed(
        fragment_type=cast("FragmentType", fragment_type),
        cohort_key=cohort_key,
        content=content,
    )


@tool
def query_l0(
    fragment_type: str,
    cohort_key: str,
    k: int = 5,
) -> dict[str, Any]:
    """Recall up to ``k`` L0 fragments for a cohort_key + fragment_type.

    Returns an empty list when no fragment has reached the k-anonymity
    threshold (observation_count >= 10). Use the recalled fragments as
    PRIORS for the current routing decision; don't treat them as
    authoritative.
    """
    return _query_l0_observed(
        fragment_type=cast("FragmentType", fragment_type),
        cohort_key=cohort_key,
        k=k,
    )


# VT-466: the manager's WRITE seam — record/update the per-tenant business
# OBJECTIVE / will / decisions / learnings the manager holds across turns. This
# is TENANT-SCOPED context (vs. write_l0_fragment, which is the cross-tenant
# k-anonymous cohort path). Composes over the EXISTING L1 business_profile entity
# (MERGE-not-clobber, RLS-scoped) — NOT a new store, NOT a send/ledger/accounts
# write (passes the VT-268 guardrail: the name carries no forbidden capability).
@tool
def record_business_objective(
    tenant_id: str,
    objective: str | None = None,
    will: str | None = None,
    policy: str | None = None,
    decisions: str | None = None,
    learnings: str | None = None,
) -> dict[str, Any]:
    """Record/update what you (the manager) hold for THIS business — the standing
    OBJECTIVE / owner WILL / action POLICY / cross-turn DECISIONS / LEARNINGS.

    Use this to persist the "what's good for this business" you reason about, so a
    LATER turn (and the scoped slice you hand a specialist) sees it. TENANT-scoped
    — for THIS owner only (cohort-generalizable learnings that should reach OTHER
    businesses go to ``write_l0_fragment`` instead).

    MERGE-not-clobber: supply ONLY the fields you are setting; omitted fields keep
    their prior value (a single learning never wipes the standing objective). The
    objective is owner/business context, NEVER customer PII.

    Returns the merged objective record (the full current state after your patch).
    """
    from orchestrator.knowledge import write_business_objective
    from orchestrator.observability.decorators import _observability_context

    # Pillar 3 — the AUTHORITATIVE tenant is the ambient dispatch context, NOT the
    # model-supplied arg. The brain occasionally hallucinates a malformed/placeholder
    # ``tenant_id`` string ("the tenant's id", a truncated uuid, …); trusting it raised
    # ``ValueError: badly formed hexadecimal UUID string`` inside ``write_business_objective``
    # and crashed the whole brain run (langgraph re-raised the tool error → the run hung at
    # 'running', never reaching the next tool / a specialist spawn). Resolve from the
    # ObservabilityContext when present; fall back to the arg only if the context is absent.
    ctx = _observability_context.get()
    resolved_tenant: UUID | str | None = ctx.tenant_id if ctx is not None else None
    if resolved_tenant is None:
        try:
            resolved_tenant = UUID(str(tenant_id))
        except (ValueError, TypeError):
            # No ambient context AND an unusable arg — surface a tool error the agent can
            # read and route around, NOT an exception that aborts the whole run.
            return {
                "status": "error",
                "error": "record_business_objective: no resolvable tenant context",
            }

    patch = {
        k: v
        for k, v in (
            ("objective", objective),
            ("will", will),
            ("policy", policy),
            ("decisions", decisions),
            ("learnings", learnings),
        )
        if v is not None
    }
    merged = write_business_objective(resolved_tenant, patch)
    return {"status": "recorded", "objective": merged}


# VT-579: the manager's brain-commanded RETRIEVAL over the LIFETIME conversation log. The always-on
# window (agent/dispatch.py) carries the last ≤20 turns within 24h; THIS tool lets the manager reach
# FURTHER BACK ("referred to whenever required", CL-2026-07-03) — a lexical search over the whole tenant
# conversation. Read-only, k-capped, tenant-scoped: no forbidden capability (passes the VT-268 guardrail).
# Plain @tool (no tool_step) ON PURPOSE — the results carry verbatim conversation text; NOT writing an
# observability envelope keeps that text out of pipeline_steps (the "never app-log message text" rule).
@tool
def search_conversation_history(query: str, limit: int = 10) -> dict[str, Any]:
    """Search this owner's ENTIRE past conversation — further back than the recent window already in your
    context.

    Use it when you need something said EARLIER than the last-24h window shows: a past decision, a number
    the owner gave a while ago, an earlier stated preference. ``query`` is matched case-insensitively
    against the message text; up to ``limit`` (max 50) matches return NEWEST-first. THIS owner only.

    Returns ``{"status": "ok"|"error", "matches": [{"role", "text", "at"}]}``.
    """
    from orchestrator.observability.decorators import _observability_context

    # Pillar 3 — the AUTHORITATIVE tenant is the ambient dispatch context, NOT a model-supplied arg (the
    # brain hallucinates ids); mirrors record_business_objective. No context ⇒ honest error, never a raise.
    ctx = _observability_context.get()
    resolved_tenant: UUID | str | None = ctx.tenant_id if ctx is not None else None
    if resolved_tenant is None:
        return {
            "status": "error",
            "error": "search_conversation_history: no resolvable tenant context",
            "matches": [],
        }
    from orchestrator.conversation_log import search_history

    rows = search_history(resolved_tenant, query, limit=limit)
    matches = [
        {
            "role": r.get("role"),
            "text": r.get("text"),
            "at": (
                r["created_at"].isoformat()
                if hasattr(r.get("created_at"), "isoformat")
                else str(r.get("created_at"))
            ),
        }
        for r in rows
    ]
    return {"status": "ok", "matches": matches}


# VT-194 dropped 3 STUBs (send_whatsapp_template_stub /
# get_subscriber_state_stub / query_pipeline_history_stub). Each carried
# ~300 tokens of schema text in the agent's prompt (~900 tokens total)
# with no real implementation. Restore when VT-5.7 / VT-5.2 / VT-5.3
# ship the real surfaces.


# Base tools every orchestrator-agent has, regardless of context. Specialist
# handoff tools (spawn_*) are NOT here — they are passed as extra_tools by the
# supervisor graph, since a handoff is only valid inside the parent graph.
#
# VT-125 inventory: real tools (compose_owner_output_tool) + 5 STUBs marked
# in each tool's docstring with the successor VT-N row.
# self_evaluate is OMITTED from the base inventory — its MCPTool subclass
# signature mismatches langchain's @tool surface (VT-181 retrofit deferred);
# specialist agents invoke it directly via the MCPTool framework.
ORCHESTRATOR_AGENT_TOOLS: list[BaseTool] = [
    escalate_to_fazal,
    # VT-590: compose_owner_output_tool REMOVED from the manager inventory. Its
    # output (canned text keyed by a routing intent) is discarded — nothing sends
    # it — so on a handle-directly turn the manager would write an opener + defer
    # the body to this tool, and only the opener (its trailing message text) got
    # transmitted (VT-589). The manager now writes the WHOLE reply as its final
    # message text; that text IS what the owner receives. The function still exists
    # for the deterministic composer paths — it is just no longer a manager tool.
    write_l0_fragment,
    query_l0,
    record_business_objective,  # VT-466 manager WRITE seam (tenant-scoped objective)
    search_conversation_history,  # VT-579 manager RETRIEVAL over the lifetime conversation log
]


class OrchestratorAgentState(AgentState, total=False):
    """State schema for the orchestrator ``create_agent`` subgraph (VT-3.4 PR 2/3).

    create_agent's default ``AgentState`` is messages-centric — its subgraph
    filters parent state down to that schema, so the supervisor's run-identity
    fields would never reach a handoff tool's ``InjectedState`` (verified seam,
    CL-209). This subclass adds them back, narrowly: extending ``AgentState``
    (rather than swapping the whole schema to ``AgentGraphState``) keeps
    create_agent's own state fields intact and keeps sales-recovery bundle
    fields OUT of the orchestrator subgraph.

    total=False: the three fields are populated by upstream producers
    (VT-3.3 / VT-3.5 / VT-3.8) and may be absent at orchestrator entry —
    matching ``AgentGraphState``'s totality for the same keys (CL-195).
    """

    run_id: UUID | None
    tenant_id: UUID | None
    trigger_reason: TriggerReason | None


# ---------------------------------------------------------------------------
# VT-484 — tool-error recovery middleware (LAUNCH-BLOCKER robustness fix).
#
# THE DEFECT it fixes: a tool/spawn that RAISES orphans its ``tool_use`` block.
# langchain's ``create_agent`` ToolNode defaults to ``_default_handle_tool_errors``,
# which RE-RAISES every non-``ToolInvocationError`` exception (it only swallows
# arg/validation errors). A re-raised tool error escapes the ToolNode WITHOUT
# emitting a ``tool_result`` for that ``tool_use`` id → the conversation is now
# Anthropic-invalid → the NEXT model call returns 400 ``tool_use ids were found
# without tool_result blocks`` → the brain run HANGS at ``status='running'`` and
# never reaches the next tool / a specialist spawn (e.g. spawn_sales_recovery).
#
# This is GENERAL: ANY tool that raises hangs the run, not just integration. The
# live win-back drive hit it via a spawn whose handoff builder raised; VT-483
# patched ONE tool (``record_business_objective``) to return an error dict, but
# that does not generalise — this middleware does.
#
# THE FIX: wrap every tool call so a raised exception is turned into an ERROR
# ``ToolMessage`` carrying the SAME ``tool_call_id`` — a valid ``tool_result``.
# The conversation stays Anthropic-valid; the brain reads the error and either
# recovers (re-routes / picks another tool) or terminates cleanly. No orphan.
#
# ``GraphBubbleUp`` (the base of ``GraphInterrupt`` raised by the owner-approval
# ``interrupt()`` and of subgraph-control signals) is RE-RAISED unchanged —
# catching it would break the Pillar-7 approval pause. This mirrors the ToolNode's
# own ``except GraphBubbleUp: raise`` carve-out. Spawn handoffs that RETURN a
# ``Command(goto=...)`` are unaffected: they return normally, never raise here.
@wrap_tool_call
def _tool_error_to_tool_result(request: Any, handler: Any) -> Any:
    """Turn a raised tool error into an error ``ToolMessage`` (VT-484).

    Keeps the ``tool_use``/``tool_result`` pairing intact so a raising tool can
    never orphan its ``tool_use`` and 400 the next Anthropic call. ``GraphBubbleUp``
    (interrupts / subgraph control) propagates unchanged.
    """
    try:
        return handler(request)
    except GraphBubbleUp:
        # The owner-approval interrupt() + subgraph-control signals MUST bubble.
        raise
    except Exception as exc:  # noqa: BLE001 — convert ANY tool error to a tool_result
        tool_call = request.tool_call
        logger.warning(
            "orchestrator_agent: tool %r raised; emitting error tool_result "
            "(VT-484 — preventing orphaned tool_use / 400 hang): %s",
            tool_call.get("name"),
            type(exc).__name__,
        )
        # VT-530 (C2a): make the manager's self-handling VISIBLE in the audit spine.
        _emit_recovery_attempted(tool_call.get("name"), exc)
        return ToolMessage(
            content=f"Error executing tool {tool_call.get('name')!r}: {exc!r}",
            name=tool_call.get("name"),
            tool_call_id=tool_call["id"],
            status="error",
        )


def _emit_recovery_attempted(tool_name: str | None, exc: Exception) -> None:
    """VT-530 (C2a) — record that a tool error was surfaced to the manager as a RECOVERY
    opportunity.

    Today the VT-484 seam turns a raised tool error into an error ``tool_result`` and the brain
    "either recovers (re-routes / picks another tool) or terminates cleanly" — but the audit spine
    only shows two disconnected ``tool_invoked``/``tool_result`` rows, so the self-handling is
    invisible. This emits one ``recovery_attempted`` row (``event_layer='decides'``,
    ``status='pending'`` — the outcome is decided on the brain's next turn) correlated by
    ``run_id`` + the failed tool, so "tool X failed → manager handed the error to recover" is a
    greppable event.

    Deterministic + fully FAIL-SOFT: an audit failure (or an absent observability context) must
    NEVER affect the VT-484 error-to-``tool_result`` conversion the brain depends on."""
    try:
        from orchestrator.observability.decorators import _observability_context
        from orchestrator.observability.tm_audit import emit_tm_audit

        ctx = _observability_context.get()
        if ctx is None:
            return  # best-effort — no run context to scope the row
        emit_tm_audit(
            event_layer="decides",
            event_kind="recovery_attempted",
            actor="team_manager",
            tenant_id=ctx.tenant_id,
            run_id=ctx.run_id,
            summary=(
                f"tool {tool_name!r} raised {type(exc).__name__}; error surfaced to the manager "
                "to recover or terminate (VT-484 seam)"
            ),
            decision={"failed_tool": tool_name, "error_type": type(exc).__name__},
            severity="warning",
            status="pending",
        )
    except Exception:  # noqa: BLE001 — fail-soft: observability must never break the recovery path
        logger.debug("VT-530 recovery_attempted audit emit failed (fail-soft)", exc_info=True)


def build_orchestrator_agent(
    model: ChatAnthropic,
    *,
    extra_tools: Sequence[BaseTool] = (),
) -> Any:
    """Build the orchestrator-agent with the base tools plus ``extra_tools``.

    name="orchestrator_agent" is load-bearing — VT-3.4's supervisor graph
    references this exact string as the node name.

    ``state_schema=OrchestratorAgentState`` (VT-3.4 PR 2/3): propagates
    tenant_id / run_id / trigger_reason into the subgraph so the
    ``spawn_sales_recovery`` handoff can read them from ``InjectedState``.

    ``create_agent`` (langchain 1.x) is the supported successor to the
    deprecated ``langgraph.prebuilt.create_react_agent`` (CL-134).

    Per VT-125: caller wraps invocation with ``OrchestratorAgentDriver``
    for hard-limit enforcement + ``OrchestratorReasoningCallback`` for
    observability. The agent itself is a plain runnable.
    """
    tools = [*ORCHESTRATOR_AGENT_TOOLS, *extra_tools]
    # VT-632 (Step 1) — flag-gated: bind the owner-reply-authoring tool + append its directive.
    # reply_to_owner SENDS to the owner, but the recipient is resolved SERVER-SIDE (the model never
    # supplies a number) so the VT-268 boundary holds — its name carries no forbidden capability, so
    # the guardrail below passes it by construction (the carve-out is documented in tool_guardrail).
    system_message = ORCHESTRATOR_AGENT_SYSTEM_MESSAGE
    if _reply_to_owner_enabled():
        from orchestrator.agent.tools.reply_to_owner import reply_to_owner

        tools.append(reply_to_owner)
        system_message = _system_message_with_reply_directive()
    # VT-268: fail-CLOSED guardrail — the agent must never hold a direct
    # send-to-customer / accounts-book-write / ledger-write tool (raises at build if it does).
    from orchestrator.agent.tool_guardrail import assert_agent_tools_safe

    assert_agent_tools_safe(tools, surface="orchestrator_agent")
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=system_message,
        name="orchestrator_agent",
        state_schema=OrchestratorAgentState,
        # VT-484: a raised tool/spawn must STILL emit a tool_result (error) so the
        # tool_use is never orphaned (no 400 "tool_use without tool_result" → no
        # hang at status='running'). This is the launch-blocker robustness fix.
        middleware=[_tool_error_to_tool_result],
    )


# Default module-level instance — base tools only. The VT-3.4 supervisor builds
# its own instance with the spawn_sales_recovery handoff tool added.
orchestrator_agent = build_orchestrator_agent(_MODEL)

# VT-632: once-per-boot visibility of the reply-tool flag state (low-noise; aids deploy confirmation).
logger.info("VT-632: reply_to_owner flag enabled=%s", _reply_to_owner_enabled())
