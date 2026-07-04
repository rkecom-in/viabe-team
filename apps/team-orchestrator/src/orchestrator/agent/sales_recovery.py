"""Sales Recovery specialist — Agent SDK skeleton (VT-32).

This module is the real specialist that the orchestrator's specialist
dispatch calls directly (``run_sales_recovery_agent``, VT-32).

Tier 2 plumbing only (CL-242)
-----------------------------
This module MUST NOT touch the database, send WhatsApp messages, or
mutate LangGraph state directly. It receives a typed context, runs an
agent loop on the Anthropic Messages API, and returns a typed
``AgentResult``. The orchestrator owns persistence + side effects.

VT-35 hook seams
----------------
The two well-named functions below are the seams VT-35's four hard-limit
enforcers attach to. Do NOT collapse them into a single opaque call:

  - ``_run_one_turn`` — the *per-turn boundary*. Each call is one
    Messages.create round-trip. The depth tracker and token meter
    instrument here.
  - ``_dispatch_tool`` — the *tool-dispatch seam*. Each call is one tool
    invocation (success OR failure). The tool counter instruments here.

The wall-clock timer attaches at ``run_sales_recovery_agent`` entry/exit
(it watches the whole run); the cancel coordinator orchestrates a clean
break across all four enforcers.

The placeholder prompt
----------------------
This PR ships with a placeholder system prompt that asks the model to
emit ``{"status": "placeholder"}`` and stop. The real prompt is a later
subtask. The placeholder text is intentionally short and free of
instruction-tuning: it is for plumbing validation, not behaviour
validation.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, cast

import yaml
from anthropic import Anthropic, APITimeoutError

from orchestrator.agent.cost import compute_cost_paise
from orchestrator.agent.limits import (
    PER_TURN_HTTP_TIMEOUT_S,
    CancellationContext,
    DepthTracker,
    TokenMeter,
    ToolCounter,
    WallclockTimer,
)
from pydantic import BaseModel, ValidationError

from orchestrator.agent.schemas.campaign_plan import (
    _MARKER_RE,
    CampaignPlanInsufficientData,
    CampaignPlanOutOfScope,
    CampaignPlanProposed,
    CampaignStatus,
    EvidenceSourceKind,
    parse_campaign_plan,
    schema_rejection_field_paths,
)
from orchestrator.context_builder import serialize_bundle_for_prompt
from orchestrator.agent.self_evaluate import (
    GateAction,
    SelfEvaluateGate,
    SelfEvaluator,
)
from orchestrator.agent.types import AgentResult
from orchestrator.error_router import route_failure
from orchestrator.failures import FailureRecord, FailureType, HardLimitAxis
from orchestrator.observability.agent_callback import reasoning_step_input, with_reasoning_capture
from orchestrator.observability.tm_audit import emit_tm_audit
from orchestrator.privacy.pii_redactor import redact

_logger = logging.getLogger(__name__)

# CL-288: variant-model registry for emit-shape coercion. Maps the
# discriminator value to the variant model so the agent can project the
# model's raw output onto a per-variant allowed-field set BEFORE schema
# validation. The schema's strict ``extra='forbid'`` makes a flat-superset
# emit invalid; this map lets the agent drop forbidden fields cleanly.
_VARIANT_MODELS: dict[CampaignStatus, type[BaseModel]] = {
    CampaignStatus.PROPOSED: CampaignPlanProposed,
    CampaignStatus.OUT_OF_SCOPE: CampaignPlanOutOfScope,
    CampaignStatus.INSUFFICIENT_DATA: CampaignPlanInsufficientData,
}

# Sales Recovery system prompt v1.0 (VT-33 / VT-4.2). Loaded from the
# markdown file under prompts/; the file is the source of truth and is
# CI-gated at 4000 tokens (gate-sr-agent-prompt-token-cap). Prompt edits
# go through versioned files (sales_recovery_v1.md -> _v2.md ...); major
# revisions are Type 2 governance.
_SR_AGENT_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "sales_recovery_v1.md"
)
# VT-493 A1: the markdown is a TEMPLATE, not the final prompt. The proposed-
# variant example's ``campaign_window`` and the ``{{TODAY}}`` instruction are
# date-anchored and MUST reflect the server's CURRENT date at dispatch. The
# original prompt hardcoded an absolute window (``2026-05-22…`` / ``…05-29``);
# 5+ weeks later the model echoed it verbatim, the ``CampaignWindow`` validator
# rejected the backdated ``start`` (campaign_plan.py:156), ``parse_campaign_plan``
# raised, and no plan was emitted. A hardcoded FUTURE date would simply re-stale.
# Fix: keep the dates as tokens in the file and render them per dispatch.
_SR_AGENT_PROMPT_TEMPLATE = _SR_AGENT_PROMPT_PATH.read_text(encoding="utf-8")


def _render_sr_system_prompt(now: datetime | None = None) -> str:
    """Render the SR system-prompt template with the current server date (VT-493).

    Substitutes three tokens so the proposed example + the campaign_window
    instruction always carry a CURRENT, schema-valid date:

      - ``{{TODAY}}`` → today's UTC date (``YYYY-MM-DD``), used in the
        campaign_window instruction ("start must be today or later").
      - ``{{CAMPAIGN_WINDOW_START}}`` / ``{{CAMPAIGN_WINDOW_END}}`` → the
        proposed example's window: a 7-day window starting TOMORROW 09:00 UTC.
        A future-dated start (not "today") is deliberate — it keeps a verbatim
        echo of the example safe against the ``CampaignWindow`` ``start >= now``
        validator regardless of the dispatch time-of-day (a today-09:00 example
        would be in the past for any run after 09:00 UTC).

    Deterministic in ``now`` for testability; defaults to ``datetime.now(UTC)``.
    """
    now = now or datetime.now(UTC)
    today = now.date()
    start = datetime(
        today.year, today.month, today.day, 9, 0, 0, tzinfo=UTC
    ) + timedelta(days=1)
    end = start + timedelta(days=7)
    return (
        _SR_AGENT_PROMPT_TEMPLATE.replace("{{TODAY}}", today.isoformat())
        .replace("{{CAMPAIGN_WINDOW_START}}", start.isoformat())
        .replace("{{CAMPAIGN_WINDOW_END}}", end.isoformat())
    )

# Markdown code-fence stripper. Matches a recognised fence shape and
# captures the inner content. NARROW by design: it does not extract a
# JSON object from arbitrary surrounding prose — that would mask
# genuinely malformed output. Recognised: ``` or ```json (case-
# insensitive) on its own line, optional whitespace, closing ``` on its
# own line.
_CODE_FENCE_RE = re.compile(
    r"^\s*```(?:json)?[ \t]*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)

# Per-response output cap passed to ``messages.create``. Distinct from the
# run-level hard-limit ceiling below — ``max_tokens`` here is "max length
# of ONE response", which is what the Messages API expects (passing the
# 80K run-level budget here also trips the SDK's non-streaming 10-minute
# timeout guard). The placeholder canary response is ~10 tokens; 1024 is
# generous headroom. Real-prompt tuning lands with the real prompt.
# VT-596 follow-up (live pack finding, 2026-07-04): 1024 truncated Sonnet-5's
# grounded CampaignPlanProposed mid-JSON (stop_reason=max_tokens at exactly 1024
# out-tokens → agent_terminal_no_dict → SpecialistNoOutputError → the owner got
# the VT-88 escalation ack instead of a plan). Sonnet 4.6 squeezed under; Sonnet 5
# writes fuller plans. 4096 = parity with the manager brain's own per-response cap;
# the cumulative VT-35 80K run ceiling still binds, and a plan that ever exceeds
# THIS cap still lands on the honest VT-492 escalation net, never silence.
_MAX_OUTPUT_TOKENS_PER_TURN = 4096

# VT-596 follow-up #2 (same pack, second escalation): Sonnet-5's draft length is
# VARIABLE — one run fit in ~3k out-tokens, the next hit 4096 and truncated again.
# Chasing the cap loses; the correct handling for stop_reason=max_tokens is a
# bounded CONTINUATION: append the partial assistant turn + ask the model to
# resume exactly where it stopped, stitching the text parts for the terminal
# parse. Bounded by _MAX_CONTINUATION_TURNS per run (and, as always, the VT-35
# cumulative token meter + wallclock + _MAX_TURNS_PER_RUN).
_MAX_CONTINUATION_TURNS = 3
_CONTINUE_PROMPT = (
    "Your previous message was cut off by the output limit. Continue EXACTLY "
    "where you stopped — no repetition, no preamble, no commentary; just the "
    "remaining characters of the same output."
)

# Run-level hard-limit ceiling. VT-35's token meter enforces a CUMULATIVE
# 80K cap across every turn in one run. This constant lives here only as
# a documented reference for AgentResult semantics (CL-242); it is NOT
# wired into any SDK call. VT-35 will read this when wiring the token
# meter. Renaming this constant requires updating VT-35's enforcer.
_RUN_LEVEL_TOKEN_HARD_LIMIT = 80_000

# Extended thinking is intentionally NOT wired for VT-32's placeholder
# canary path — a placeholder that emits ``{"status": "placeholder"}``
# does zero reasoning, so a thinking budget on the call is meaningless
# AND tripped a 400 from the API when budget_tokens > max_tokens.
# The real agent's thinking policy (whether to enable, with what
# budget) is a VT-4.2-era per-turn reasoning decision, intertwined
# with VT-35's depth tracker. VT-32 must not pre-empt it — when that
# work lands, re-introduce ``thinking={"type": "enabled",
# "budget_tokens": N}`` where N < _MAX_OUTPUT_TOKENS_PER_TURN (the API
# enforces that relationship). Do NOT smuggle a thinking budget in
# here today.

# Loop safety upper bound (NOT a budget). VT-35's depth (≤8), tool-call
# (≤25), wallclock (≤300s) and token (≤80K) enforcers are the real
# budgets — this cap exists only as the final guard against a runaway
# loop if every enforcer somehow failed to fire. Sized comfortably above
# the tool-call cap so tests that exercise the 25/26 boundary can run
# without bumping into it.
_MAX_TURNS_PER_RUN = 50


_MODELS_YAML = (
    Path(__file__).resolve().parents[3] / "config" / "models.yaml"
)


# Exec-6.85 reconciliation: the agent's context IS the orchestrator-side
# Context Composer bundle. The minimal three-field wedge (tenant_id /
# run_id / user_request) is gone — the agent receives the full bundle
# (business_profile, customer_ledger_summary, recent_campaigns,
# attribution_snapshot, pending_owner_inputs, meta, data_completeness, +
# user_request, trigger_reason) so the v1.0 prompt has real task context
# to reason about. The re-export below preserves the historical import
# path ``from orchestrator.agent.sales_recovery import SalesRecoveryContext``.
from orchestrator.context_builder import SalesRecoveryContext  # noqa: E402


def _resolve_model(agent_name: str = "sales_recovery") -> str:
    """Return the model id for ``agent_name`` per ``VIABE_ENV``.

    ``VIABE_ENV in {'production'}`` → ``production`` slot; everything else
    (test/dev/canary or unset) → ``test`` slot. The unset default is
    test/Haiku — never silently fall through to Opus in a development
    environment.
    """
    env = os.environ.get("VIABE_ENV", "test").lower()
    slot = "production" if env == "production" else "test"
    with open(_MODELS_YAML) as f:
        config = yaml.safe_load(f)
    return cast(str, config[agent_name][slot])


def _dispatch_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    tools: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a tool call. VT-35 tool-counter seam.

    For VT-32 ``tools`` is always ``{}`` (no real tools yet). Calling
    this with an empty registry returns a structured ``tool_error``
    result so the agent loop can append it as a ``tool_result`` and
    finish cleanly — instead of raising and unwinding the loop.

    VT-35's tool counter wraps this function: every call increments the
    counter regardless of whether the dispatch succeeded.
    """
    if tool_name not in tools:
        return {
            "tool_name": tool_name,
            "is_error": True,
            "content": f"unknown tool: {tool_name}",
        }
    handler = tools[tool_name]
    try:
        return cast(dict[str, Any], handler(tool_input))
    except Exception as exc:  # noqa: BLE001 — surface as tool_error result
        return {"tool_name": tool_name, "is_error": True, "content": str(exc)}


@with_reasoning_capture
def _run_one_turn(
    client: Anthropic,
    *,
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    timeout: float = PER_TURN_HTTP_TIMEOUT_S,
) -> Any:
    """One Messages.create round-trip. VT-35 per-turn / token-meter seam.

    Isolated so VT-35's enforcers can instrument exactly one turn at a
    time and so tests can mock at this boundary (zero real API calls in
    CI by patching this function).

    ``timeout`` (VT-35): per-turn HTTP ceiling passed to httpx. Caps the
    wall-clock cost of any single round-trip even if the model hangs;
    the run-level wall-clock budget is enforced separately by
    WallclockTimer at the turn boundary.

    VT-182 retrofit: ``@with_reasoning_capture`` (above) wraps each call
    so the response writes one ``agent_reasoning_step`` pipeline_steps
    row via VT-180 write_step. Caller wraps the call in
    ``observability_context(...)`` (VT-181 ContextVar) +
    ``reasoning_step_input(...)`` (VT-182 ContextVar) so the callback has
    run_id/tenant_id + input envelope fields. Without those, the callback
    logs a warning and skips the write (observability is best-effort per
    CL-122).
    """
    # mypy: anthropic.Messages.create's overloads are TypedDict-heavy
    # (MessageParam, ThinkingConfigEnabledParam) — typing the plain-dict
    # messages list to match would add noise without value for a Phase 1
    # placeholder loop. The shape is asserted at runtime by the SDK.
    return client.messages.create(
        model=model,
        max_tokens=_MAX_OUTPUT_TOKENS_PER_TURN,
        system=system_prompt,
        messages=messages,  # type: ignore[arg-type]
        tools=[],
        timeout=timeout,
    )


def _extract_text(content_blocks: list[Any]) -> str:
    """Concatenate every TextBlock's text from a response's content."""
    out: list[str] = []
    for block in content_blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            out.append(text)
    return "".join(out)


def _parse_placeholder_output(text: str) -> dict[str, Any] | None:
    """Best-effort parse of the placeholder JSON. Returns None on failure.

    Tolerates ONE level of markdown code-fence wrapping (``` or ```json)
    — models intermittently wrap JSON in a fence even when the prompt
    forbids it. The strip is narrow: a recognised fence shape only,
    NOT a loose "first { to last }" extraction. Genuinely malformed or
    truncated output must still return None so the caller classifies
    ``status='invalid'`` rather than silently inventing a parse.
    """
    import json

    text = text.strip()
    if not text:
        return None
    fence_match = _CODE_FENCE_RE.match(text)
    if fence_match is not None:
        text = fence_match.group("body").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


_EMPTY_SENTINELS: tuple[Any, ...] = (None, "", [], {})


# VT-499: the campaign_window is the SYSTEM's to set, not the LLM's. It is a
# MECHANICAL value (the campaign runs from ~now for a fixed span), NOT business
# judgment — yet it was the dominant win-back parse failure. VT-493 maximally
# prompted the field (injected today's date, gave a valid example, named the
# validator) and the SR model (Haiku on dev) STILL emitted a backdated / invalid
# / missing window 3/3 (VT-496 diagnostic: ``proposed.campaign_window:
# value_error`` — a ``CampaignWindow._window_validity`` rejection). So the system
# OWNS the window now: ``_construct_variant_payload`` OVERRIDES it server-side on
# the PROPOSED variant with an always-valid ``now → now+7d`` span. The model
# keeps owning every business field (target_cohort, message_plan, expected_arrr,
# objective, evidence_refs). The validator is NOT weakened — we SUPPLY a valid
# window so it passes legitimately, and genuinely-bad OTHER fields still fail.
#
# START_BUFFER rationale: ``_window_validity`` rejects a past start with a STRICT
# ``start < now`` where ``now`` is recomputed at VALIDATION time — strictly AFTER
# this coercion (the agent-gate parse at the parse_campaign_plan seam, then again
# the supervisor re-parse, the latter across one self-evaluate gate call). A
# literal ``start == now`` would be backdated by the time the validator runs and
# be rejected. A small forward buffer lands ``start`` comfortably ahead of every
# downstream re-validation while staying "approximately now" for a 7-day window.
# There is NO re-validation later than the supervisor parse — the persisted plan
# is a validated model object, never re-parsed — so the buffer only has to clear
# the in-request gap (one Opus self-evaluate call at most).
_CAMPAIGN_WINDOW_START_BUFFER = timedelta(minutes=5)
_CAMPAIGN_WINDOW_DURATION = timedelta(days=7)


def _server_campaign_window(now: datetime) -> dict[str, str]:
    """VT-499: a server-computed, always-schema-valid ``campaign_window`` dict.

    ``start = now + START_BUFFER`` (clears the validator's later ``start >= now``
    recompute), ``end = start + 7 days``. ISO-8601 strings (tz-aware, since
    ``now`` is server UTC) so the value drops straight into the raw payload that
    ``parse_campaign_plan`` validates. Deterministic in ``now`` for testability.
    """
    start = now + _CAMPAIGN_WINDOW_START_BUFFER
    end = start + _CAMPAIGN_WINDOW_DURATION
    return {"start": start.isoformat(), "end": end.isoformat()}


# VT-498: the message body is PLACEHOLDER-only — never literal customer PII.
# serialize_bundle_for_prompt shows the model each dormant-cohort customer's real
# ``display_name`` (so it can pick + name the target subset). The SR model (Haiku on
# dev) was observed copying that name straight into ``message_plan.personalization``
# ("Hi Anita, …") — customer PII baked into the persisted plan (collapse_campaign_plan
# stores plan_json raw). The real personalization is hydrated PER-RECIPIENT at SEND
# from the customer record (the team_winback_simple ``customer_name`` param —
# sales_recovery_executor._allowed_param_values fills it from bundle.display_name), so
# the PLAN must carry a placeholder, never a literal name. Mirroring the VT-499
# server-owned-field discipline, this scrub strips any literal cohort name the model
# emitted (in personalization AND every template_params value) back to the
# ``<customer_name>`` token — the SAME token target_cohort.selection_reason already
# carries — regardless of whether the model obeyed the prompt. The placeholder the
# prompt asks for (``<customer_name>``) is itself a redactor token, so it passes
# through untouched (idempotent); only a real cohort name is rewritten.


def _cohort_name_registry(
    context: SalesRecoveryContext,
) -> Callable[[str], bool] | None:
    """A redactor name-predicate over the dormant-cohort display names the model saw.

    Mirrors ``customer_registry.make_name_registry`` (exact case-folded match) but
    sources the names from the IN-CONTEXT cohort — NO DB read (this module is Tier-2,
    CL-242). Returns ``None`` when the cohort carries no names (nothing to scrub).
    """
    names = frozenset(
        m.display_name.casefold()
        for m in context.dormant_cohort
        if getattr(m, "display_name", None)
    )
    if not names:
        return None
    return lambda text: text.casefold() in names


def _scrub_message_plan_pii(
    payload: dict[str, Any], context: SalesRecoveryContext
) -> None:
    """VT-498 — strip literal cohort-customer PII from a PROPOSED plan's message_plan.

    In-place: rewrites ``message_plan.personalization`` and every
    ``message_plan.template_params`` value through the redactor with the cohort
    name-registry, so any literal customer name (or pattern PII the model leaked into
    the body) becomes a ``<customer_name>`` / pattern token before the plan is parsed
    + persisted. A no-op when the cohort has no names or message_plan is absent.
    """
    registry = _cohort_name_registry(context)
    if registry is None:
        return
    message_plan = payload.get("message_plan")
    if not isinstance(message_plan, dict):
        return
    personalization = message_plan.get("personalization")
    if isinstance(personalization, str):
        message_plan["personalization"] = redact(
            personalization, name_registry=registry
        )
    params = message_plan.get("template_params")
    if isinstance(params, dict):
        message_plan["template_params"] = {
            key: (redact(value, name_registry=registry) if isinstance(value, str) else value)
            for key, value in params.items()
        }


# VT-501: evidence_refs is the STRUCTURED backing for the prose claim-markers
# (``[E\d+]``) the model writes in ``target_cohort.selection_reason`` +
# ``expected_arrr.basis``. The dominant remaining win-back parse failure (VT-496
# diagnostic on dev): the SR model writes grounded prose-markers but emits an
# EMPTY/SHORT ``evidence_refs`` (``proposed.evidence_refs: too_short``) OR a list
# whose ``claim_id``s don't match the cited markers (``proposed: value_error`` —
# the ``_evidence_marker_consistency`` rule, campaign_plan.py:312-336). Both are
# MECHANICAL structure misses ON TOP OF grounding the model ALREADY supplied — the
# prose marker IS the model's citation.
#
# Mirroring VT-499 (server-owned campaign_window) + VT-498 (PII scrub), this HEALS
# the evidence_refs STRUCTURE from the model's OWN prose citations: for every marker
# the model cited, ensure a structurally-valid backing ``EvidenceRef`` exists — keep
# the model's real ref when its ``claim_id`` matches and it is well-formed, else
# synthesize one pointing at the supplied context bundle. It does NOT invent
# grounding: when the model cited NO markers at all (no ``[E\d+]`` in the prose)
# there is nothing to heal from, the list is left exactly as the model emitted, and
# the plan FAILS the validator legitimately (min_length>=1 / consistency). The
# validator stays authoritative — we SUPPLY structure for grounding the model
# asserted, never bypass the check.
_LEGAL_SOURCE_KINDS: frozenset[str] = frozenset(k.value for k in EvidenceSourceKind)
_CLAIM_ID_RE = re.compile(r"^E\d+$")


def _is_wellformed_evidence_ref(ref: Any) -> bool:
    """True iff ``ref`` is a dict that would satisfy the ``EvidenceRef`` schema
    (claim_id ``^E\\d+$``, legal source_kind enum, non-empty source_id). Used to
    decide whether a model-emitted ref can be KEPT as-is vs needs healing."""
    if not isinstance(ref, dict):
        return False
    claim_id = ref.get("claim_id")
    source_kind = ref.get("source_kind")
    source_id = ref.get("source_id")
    return (
        isinstance(claim_id, str)
        and _CLAIM_ID_RE.match(claim_id) is not None
        and source_kind in _LEGAL_SOURCE_KINDS
        and isinstance(source_id, str)
        and bool(source_id.strip())
    )


def _synthesize_evidence_ref(claim_id: str) -> dict[str, Any]:
    """A structurally-valid EvidenceRef for a marker the model cited in prose but
    failed to back with a well-formed ref. Sourced from the supplied context bundle
    (the tenant's own dormancy / ledger / campaign history the claim is grounded in)
    — an HONEST source label, not a fabricated benchmark id. The ``note`` records the
    server heal for auditability."""
    return {
        "claim_id": claim_id,
        "source_kind": EvidenceSourceKind.L2_EPISODIC_MEMORY.value,
        "source_id": "context_bundle",
        "note": "evidence_refs structure healed server-side from the model's prose citation (VT-501)",
    }


def _repair_evidence_refs(payload: dict[str, Any]) -> None:
    """VT-501 — heal the PROPOSED variant's ``evidence_refs`` STRUCTURE from the
    model's own prose citations, in-place. See the module note above.

    Reads the SAME two prose blocks (+ the SAME marker regex) the
    ``_evidence_marker_consistency`` validator reads, so the healed list satisfies
    both ``evidence_refs`` min_length>=1 AND the two-way marker⇄ref consistency
    rule. No-op when the model cited no markers (no grounding to heal → the
    validator legitimately rejects)."""
    cohort = payload.get("target_cohort")
    arrr = payload.get("expected_arrr")
    prose_blocks = [
        cohort.get("selection_reason") if isinstance(cohort, dict) else None,
        arrr.get("basis") if isinstance(arrr, dict) else None,
    ]
    cited: list[str] = []
    seen: set[str] = set()
    for prose in prose_blocks:
        if not isinstance(prose, str):
            continue
        for match in _MARKER_RE.finditer(prose):
            marker = match.group(1)
            if marker not in seen:
                seen.add(marker)
                cited.append(marker)
    if not cited:
        # No prose grounding markers → nothing the model cited to heal from. Leave
        # evidence_refs untouched; parse_campaign_plan rejects it (too_short /
        # consistency) — a plan with no grounding at all still fails.
        return

    raw_refs = payload.get("evidence_refs")
    existing: dict[str, dict[str, Any]] = {}
    if isinstance(raw_refs, list):
        for ref in raw_refs:
            if _is_wellformed_evidence_ref(ref):
                existing.setdefault(ref["claim_id"], ref)

    # Final list == exactly the cited markers (in first-seen order): keep the
    # model's well-formed ref where the claim_id matches, synthesize otherwise.
    # Orphan refs (declared but NOT cited by any prose marker) are dropped — the
    # consistency validator already treats them as invalid, and a ref nothing
    # points to backs nothing.
    payload["evidence_refs"] = [
        existing[marker] if marker in existing else _synthesize_evidence_ref(marker)
        for marker in cited
    ]


def _construct_variant_payload(
    raw: dict[str, Any],
    *,
    context: SalesRecoveryContext,
    generated_at: datetime,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Reshape the model's raw output into a per-variant CampaignPlan dict.

    Returns ``(payload, dropped_empty, dropped_populated)``.

    - ``payload``: a dict containing ONLY fields declared on the picked
      variant model. Identity / provenance fields (``tenant_id``,
      ``run_id``, ``generated_at``) are OVERWRITTEN from ``context`` and
      the server-time ``generated_at`` — the model has no authority to
      set these (CL-288 item 1).
    - ``dropped_empty``: forbidden keys whose value was None / [] / {} / "".
      Routine emit-shape mismatch; caller logs at debug level.
    - ``dropped_populated``: a dict ``{key: original_value}`` for forbidden
      keys whose value was non-empty — i.e. model self-contradiction.
      Caller emits FailureRecord(MODEL_OUTPUT_CONFLICT) and warn-logs.
      Empty when no populated forbidden fields were seen.

    Raises ``ValueError`` if ``raw['status']`` is missing or doesn't
    match any of the three discriminated-union variants.
    """
    status_value = raw.get("status")
    if status_value is None:
        raise ValueError("missing 'status' discriminator in raw model output")
    try:
        variant_enum = CampaignStatus(status_value)
    except ValueError as exc:
        raise ValueError(
            f"unknown CampaignStatus discriminator: {status_value!r}"
        ) from exc
    variant_cls = _VARIANT_MODELS[variant_enum]
    allowed = set(variant_cls.model_fields.keys())

    payload: dict[str, Any] = {}
    dropped_empty: list[str] = []
    dropped_populated: dict[str, Any] = {}
    for key, value in raw.items():
        if key in allowed:
            payload[key] = value
            continue
        if value in _EMPTY_SENTINELS:
            dropped_empty.append(key)
        else:
            dropped_populated[key] = value

    # Identity injection — agent authority. Overwrite any model-emitted
    # values for these three (the model has no reliable source of truth
    # for tenant identity / run identity / server time).
    payload["tenant_id"] = context.tenant_id
    payload["run_id"] = context.run_id
    payload["generated_at"] = generated_at.isoformat()

    # VT-499: campaign_window is system-owned, not LLM-owned (see the module
    # constant above). On the PROPOSED variant ONLY, OVERRIDE whatever the model
    # emitted — backdated / invalid / missing, the VT-496 failure — with a
    # server-computed always-valid now→now+7d window, so parse_campaign_plan
    # clears the CampaignWindow validator LEGITIMATELY (the validator is NOT
    # weakened; we supply a valid window). out_of_scope / insufficient_data carry
    # no window — untouched.
    if variant_enum is CampaignStatus.PROPOSED:
        payload["campaign_window"] = _server_campaign_window(generated_at)
        # VT-498: scrub any literal cohort-customer name the model copied into the
        # message body (personalization / template_params) → <customer_name> token,
        # so the persisted plan is PII-free. The real name is hydrated per-recipient
        # at send (see _scrub_message_plan_pii docstring + the module note above).
        _scrub_message_plan_pii(payload, context)
        # VT-501: heal the evidence_refs STRUCTURE from the model's own prose
        # citations (the dominant remaining parse failure: grounded prose-markers
        # but empty/short/mismatched evidence_refs). Supplies structure for
        # grounding the model asserted — does NOT invent grounding; a plan with no
        # cited markers at all is left to fail the validator (see _repair_evidence_refs).
        _repair_evidence_refs(payload)

    return payload, dropped_empty, dropped_populated


def _emit_model_output_conflict(
    *,
    context: SalesRecoveryContext,
    status_value: str,
    dropped_keys: list[str],
    raw_values: dict[str, Any],
) -> None:
    """Route a FailureRecord(MODEL_OUTPUT_CONFLICT) for populated
    wrong-variant fields the agent dropped during coercion (CL-288).

    Best-effort — observability only, run continues. Strategy is
    ACCEPT_AND_LOG; nothing retries on this.

    ``raw_values`` is the {key: value} snapshot for the dropped keys
    only (not the full raw dict), capped at 200 chars per value to
    bound payload size.
    """
    snapshot: dict[str, str] = {
        k: (repr(v)[:200] if not isinstance(v, str) else v[:200])
        for k, v in raw_values.items()
    }
    failure = FailureRecord(
        failure_type=FailureType.MODEL_OUTPUT_CONFLICT,
        message=(
            f"model emitted populated forbidden fields on variant"
            f" {status_value!r}: {sorted(dropped_keys)}"
        ),
        occurred_at=datetime.now(UTC),
        tenant_id=context.tenant_id,
        run_id=context.run_id,
        metadata={
            "variant": status_value,
            "dropped_keys": sorted(dropped_keys),
            "dropped_values": snapshot,
        },
    )
    route_failure(failure)


def run_sales_recovery_agent(
    context: SalesRecoveryContext,
    *,
    evaluator: SelfEvaluator | None = None,
) -> AgentResult:
    """Run the sales_recovery specialist; return a structured ``AgentResult``.

    Hand-written agent loop on the Anthropic Messages API (CL-242).
    Tier-2 plumbing: no DB, no side effects — the orchestrator owns
    those. The orchestrator measures (VT-35 hard limits attach here),
    the agent does not see its own usage.

    VT-35: four hard-limit enforcers — TokenMeter, ToolCounter,
    DepthTracker, WallclockTimer — instantiate per invocation (budgets
    do not carry across dispatches) and report into the shared
    CancellationContext. First signal wins. On cancel: status becomes
    'terminated', terminated_by is the winning axis, terminated_reason
    is the enforcer's message, and a FailureRecord(AGENT_HARD_LIMIT_BREACH)
    is emitted to the error router. cost_paise STILL accrues — the API
    spend already happened.

    VT-36 self-evaluate gate (``evaluator`` parameter): when provided,
    a draft CampaignPlan that the model produces at terminal goes
    through the gate before being returned. Two-revise-then-fail policy
    per ``config/self_evaluate.yaml``. ``evaluator=None`` skips the
    gate — that is the current production default because VT-50 (the
    real Opus-backed evaluator) is backlog. When VT-50 lands, every
    caller starts passing it.
    """
    start = time.monotonic()
    client = Anthropic()
    model = _resolve_model("sales_recovery")
    # VT-493 A1: render the date-anchored template ONCE per dispatch so the
    # proposed example + campaign_window instruction carry today's date (not the
    # stale 2026-05-22 literal the model used to echo into a backdated window).
    system_prompt = _render_sr_system_prompt()
    # CL-287: the orchestrator-supplied user request is the initial user
    # message — NOT a hardcoded "begin" cue. Empty / whitespace-only is
    # a structural error (the orchestrator never spawns a specialist
    # without a request); fail loud rather than feeding "" to the model.
    if not context.user_request or not context.user_request.strip():
        raise ValueError(
            "run_sales_recovery_agent: context.user_request must be a"
            " non-empty string (orchestrator must supply the user request"
            " before dispatch)"
        )
    # VT-4 ship-thin: render the Composer bundle into the first user
    # message so the model can actually use the context. Before this
    # wiring landed, the bundle was a Python dataclass the agent loop
    # ignored — the LLM only saw ``user_request``. The serializer lives
    # in ``orchestrator.context_builder`` (the upstream Composer
    # module); the agent does NOT build its own context — it just
    # consumes the rendered block. ``templates_available`` +
    # ``target_recovered_paise`` defaults are ship-thin scaffolding
    # until the approved-templates registry + per-tenant attribution
    # targets land as separate VT rows.
    initial_user_content = serialize_bundle_for_prompt(context)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": initial_user_content},
    ]
    tools: dict[str, Any] = {}  # VT-32: no real tools yet.
    raw_messages: list[dict[str, Any]] = list(messages)

    # VT-35 enforcers — fresh per invocation.
    ctx = CancellationContext()
    token_meter = TokenMeter(ctx)
    tool_counter = ToolCounter(ctx)
    depth_tracker = DepthTracker(ctx)
    wallclock_timer = WallclockTimer(ctx)

    # VT-36 self-evaluate gate — fresh per invocation when an evaluator
    # is provided. Shares the same ToolCounter so the gate's calls land
    # on the 25-call cap (Pillar 1 / VT-35 precedence).
    gate: SelfEvaluateGate | None = (
        SelfEvaluateGate(evaluator=evaluator, ctx=ctx, tool_counter=tool_counter)
        if evaluator is not None
        else None
    )

    input_tokens_used = 0
    output_tokens_used = 0
    tool_calls_made = 0
    status: str = "completed"
    output: dict[str, Any] | None = None
    # VT-596 #2 — max_tokens continuation state: partial terminal text parts
    # stitched (in order) with the final turn's text before the JSON parse.
    continuation_turns = 0
    terminal_text_parts: list[str] = []

    # VT-182/VT-514 — stage the per-turn reasoning input envelope so the
    # @with_reasoning_capture callback writes agent_reasoning_step rows (the
    # DECIDES substrate tm_audit.reasoning_ref points at). Hash is best-effort.
    import hashlib

    try:
        _bundle_hash = hashlib.sha256(
            initial_user_content.encode("utf-8")
        ).hexdigest()
    except Exception:  # noqa: BLE001 — observability input is best-effort
        _bundle_hash = "<unhashable-bundle>"

    for _ in range(_MAX_TURNS_PER_RUN):
        # Pre-turn checks: wallclock (the only enforcer that can fire
        # without a per-turn event source — accumulated time).
        wallclock_timer.check()
        if ctx.is_cancelled:
            break

        try:
            with reasoning_step_input(
                context_bundle_hash=_bundle_hash,
                context_bundle_components=["sales_recovery_bundle"],
                context_bundle_token_count=0,
                prior_tool_calls_count=tool_calls_made,
                prior_tool_calls_summary=[],
            ):
                response = _run_one_turn(
                    client,
                    model=model,
                    system_prompt=system_prompt,
                    messages=messages,
                )
        except APITimeoutError:
            # Per-turn HTTP ceiling tripped — one round-trip exceeded
            # PER_TURN_HTTP_TIMEOUT_S. The underlying condition is "this
            # run is taking too long"; convert to a wall-clock hard
            # limit so the cancel path runs uniformly (terminated_by =
            # wall_clock, FailureRecord routed). Distinguished from the
            # turn-boundary check by the reason string.
            wallclock_timer.ctx.signal(
                HardLimitAxis.WALL_CLOCK,
                f"per-turn HTTP timeout exceeded {PER_TURN_HTTP_TIMEOUT_S}s",
            )
            break

        usage = getattr(response, "usage", None)
        if usage is not None:
            in_t = int(getattr(usage, "input_tokens", 0) or 0)
            out_t = int(getattr(usage, "output_tokens", 0) or 0)
            input_tokens_used += in_t
            output_tokens_used += out_t
            token_meter.record_turn(input_tokens=in_t, output_tokens=out_t)

        # Depth: if the previous beat was a tool dispatch, THIS turn is
        # the post-tool reasoning step — increment depth.
        depth_tracker.record_reasoning_turn()

        # Post-turn cancellation check (token/depth may have signalled).
        if ctx.is_cancelled:
            break

        content_blocks = list(getattr(response, "content", []) or [])
        raw_messages.append(
            {"role": "assistant", "content": [_block_to_dict(b) for b in content_blocks]}
        )

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "tool_use":
            tool_results: list[dict[str, Any]] = []
            for block in content_blocks:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool_calls_made += 1
                tool_counter.record_dispatch()
                depth_tracker.record_tool_dispatch()
                if ctx.is_cancelled:
                    break
                result = _dispatch_tool(
                    block.name, dict(block.input or {}), tools
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result.get("content", ""),
                        "is_error": bool(result.get("is_error", False)),
                    }
                )
            if ctx.is_cancelled:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": [_block_to_dict(b) for b in content_blocks],
                }
            )
            messages.append({"role": "user", "content": tool_results})
            raw_messages.append({"role": "user", "content": tool_results})
            continue

        if stop_reason == "max_tokens" and continuation_turns < _MAX_CONTINUATION_TURNS:
            # VT-596 #2 — the turn was cut mid-output. Bank the partial text and
            # ask the model to resume; the terminal parse below stitches the
            # parts. Exhausting the continuation budget falls through to the
            # normal terminal handling (a still-broken stitch lands on the
            # CL-287 invalid-output FailureRecord — observable, never silent).
            continuation_turns += 1
            terminal_text_parts.append(_extract_text(content_blocks))
            messages.append(
                {
                    "role": "assistant",
                    "content": [_block_to_dict(b) for b in content_blocks],
                }
            )
            messages.append({"role": "user", "content": _CONTINUE_PROMPT})
            raw_messages.append({"role": "user", "content": _CONTINUE_PROMPT})
            continue

        # No tool_use → terminal. Extract output (stitching any banked
        # continuation parts — empty list on the normal single-turn path).
        text = "".join([*terminal_text_parts, _extract_text(content_blocks)])
        output = _parse_placeholder_output(text)
        if output is not None and output.get("status") == "placeholder":
            status = "placeholder"
            break
        if stop_reason == "refusal":
            status = "refused"
            break
        if output is None:
            # CL-287: emit a FailureRecord BEFORE breaking so the run
            # is observable in pipeline_steps / error router. Previous
            # Path A was silent — a CL-238 violation. Best-effort, must
            # not re-raise into the loop. Snapshot the model's terminal
            # text (capped) for diagnosis without leaking unbounded
            # payload into the error router.
            _emit_invalid_output(
                context=context,
                reason=(
                    "agent terminal output did not parse as a single JSON"
                    " dict; first 200 chars: " + text.strip()[:200]
                ),
                tokens_used=input_tokens_used + output_tokens_used,
                tool_calls_made=tool_calls_made,
                wallclock_ms=int((time.monotonic() - start) * 1000),
                source="agent_terminal_no_dict",
            )
            status = "invalid"
            break

        # CL-288: coerce the model's raw flat-superset emit into a
        # per-variant CampaignPlan dict that conforms to the v1.0 strict
        # discriminated union. The schema's ``extra='forbid'`` rejects
        # cross-variant fields; the model has trained-from priors that
        # default to flat-with-optional, so it consistently emits
        # forbidden fields. Coerce BEFORE schema validation (gate-on or
        # gate-off path).
        try:
            output, dropped_empty, dropped_populated = _construct_variant_payload(
                output, context=context, generated_at=datetime.now(UTC)
            )
        except ValueError as exc:
            # VT-4: unknown / missing ``status`` discriminator — the
            # model didn't pick a legal variant. Emit a FailureRecord
            # BEFORE breaking so the run is observable downstream
            # (CL-238 — no silent swallow). Best-effort, must not
            # re-raise into the loop.
            _emit_invalid_output(
                context=context,
                reason=(
                    "variant discriminator missing or unknown; "
                    + str(exc)[:200]
                ),
                tokens_used=input_tokens_used + output_tokens_used,
                tool_calls_made=tool_calls_made,
                wallclock_ms=int((time.monotonic() - start) * 1000),
                source="agent_variant_discriminator_invalid",
            )
            status = "invalid"
            break
        if dropped_empty:
            _logger.debug(
                "sales_recovery: dropped empty wrong-variant fields"
                " %s on variant %r",
                sorted(dropped_empty),
                output["status"],
            )
        if dropped_populated:
            _logger.warning(
                "sales_recovery: dropped POPULATED wrong-variant fields"
                " %s on variant %r — model self-contradiction observed",
                sorted(dropped_populated.keys()),
                output["status"],
            )
            _emit_model_output_conflict(
                context=context,
                status_value=str(output["status"]),
                dropped_keys=list(dropped_populated.keys()),
                raw_values=dropped_populated,
            )

        # VT-36 self-evaluate gate. Without a configured evaluator the
        # gate is skipped (VT-50 deferral); the draft ships with
        # self_evaluate_status='not_yet_evaluated' (schema default).
        if gate is None:
            status = "completed"
            break

        try:
            draft_plan = parse_campaign_plan(output)
        except Exception as exc:
            # VT-4: model emitted something that wasn't a valid
            # CampaignPlan (post-coerce schema rejection) — the gate has
            # no draft to evaluate. Emit a FailureRecord so the run is
            # observable (CL-238 — no silent swallow). Best-effort,
            # must not re-raise into the loop.
            #
            # VT-496: capture the pydantic ValidationError's structured
            # field paths (``loc`` + error ``type``) as NON-PII metadata
            # that SURVIVES redaction. The old ``str(exc)`` message was
            # (a) long enough that the redactor SHA-hashes it whole
            # (``_hash_raw_body``), so the failing field was un-nameable on
            # dev, and (b) carried ``input_value`` — the offending VALUE,
            # potential customer PII. ``schema_rejection_field_paths`` reads
            # ONLY loc+type (schema paths, never content); the reason string
            # is rebuilt from those so it no longer echoes a value.
            field_paths = (
                schema_rejection_field_paths(exc)
                if isinstance(exc, ValidationError)
                else []
            )
            if field_paths:
                reason = (
                    "post-coerce CampaignPlan schema rejection: "
                    + "; ".join(field_paths)
                )
            else:
                # Non-ValidationError (should not happen — parse only raises
                # ValidationError) — name the exception type, NOT str(exc),
                # to keep any value out of the message.
                reason = (
                    "post-coerce CampaignPlan schema rejection ("
                    + type(exc).__name__
                    + ")"
                )
            _emit_invalid_output(
                context=context,
                reason=reason,
                tokens_used=input_tokens_used + output_tokens_used,
                tool_calls_made=tool_calls_made,
                wallclock_ms=int((time.monotonic() - start) * 1000),
                source="agent_schema_rejection",
                schema_field_paths=field_paths,
            )
            status = "invalid"
            break

        gate_outcome = gate.run(draft_plan)

        # Every gate.run() — emit a self_evaluate event so production
        # REVISE-frequency and per-attempt verdicts are observable in
        # pipeline_steps. Best-effort; routing failure does NOT re-raise.
        _emit_self_evaluate_gate(
            context=context,
            attempt_number=gate_outcome.attempt_number,
            outcome=gate_outcome.outcome,
            rejection_feedback=gate_outcome.rejection_feedback,
            feedback_messages=gate_outcome.feedback_messages,
        )

        # VT-514 DECIDES — self_eval / policy_applied spine row (fail-soft,
        # conn=None). Verdict + two-revise-then-fail policy outcome; no PII.
        emit_tm_audit(
            event_layer="decides",
            event_kind="self_eval",
            actor="sales_recovery",
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            summary=(
                f"self-evaluate gate {gate_outcome.action.value} on attempt "
                f"{gate_outcome.attempt_number}"
            ),
            decision={
                "gate_action": gate_outcome.action.value,
                "verdict": (
                    gate_outcome.outcome.value if gate_outcome.outcome else None
                ),
                "self_evaluate_status": (
                    gate_outcome.self_evaluate_status.value
                    if gate_outcome.self_evaluate_status else None
                ),
                "attempt_number": gate_outcome.attempt_number,
            },
        )

        if gate_outcome.action is GateAction.SHIP:
            stamped = draft_plan.model_copy(
                update={"self_evaluate_status": gate_outcome.self_evaluate_status}
            )
            output = stamped.model_dump(mode="json")
            status = "completed"
            break
        if gate_outcome.action is GateAction.ABORTED:
            # Hard-limit cancel during the gate; the post-loop branch
            # handles termination uniformly.
            break
        if gate_outcome.action is GateAction.SEAM_ERROR:
            _emit_invalid_output(
                context=context,
                reason=gate_outcome.error_message or "self_evaluate seam error",
                tokens_used=input_tokens_used + output_tokens_used,
                tool_calls_made=tool_calls_made + gate.evaluator_calls,
                wallclock_ms=int((time.monotonic() - start) * 1000),
            )
            status = "invalid"
            break
        if gate_outcome.action is GateAction.REJECTED:
            # Exhausted the one-retry budget; the draft is known-bad.
            # Do NOT ship. Route SELF_EVAL_REJECTED for escalation; the
            # router's default_strategy is ESCALATE_TO_FAZAL.
            _emit_self_eval_rejected(
                context=context,
                rejection_feedback=gate_outcome.rejection_feedback,
                attempt_number=gate_outcome.attempt_number,
                tokens_used=input_tokens_used + output_tokens_used,
                tool_calls_made=tool_calls_made + gate.evaluator_calls,
                wallclock_ms=int((time.monotonic() - start) * 1000),
            )
            stamped = draft_plan.model_copy(
                update={"self_evaluate_status": gate_outcome.self_evaluate_status}
            )
            output = stamped.model_dump(mode="json")
            status = "rejected"
            break

        # RETRY — append feedback as a user message and let the loop
        # ask the model for a new draft. The next turn re-enters the
        # terminal branch and runs the gate again.
        for fb_msg in gate_outcome.feedback_messages:
            messages.append(fb_msg)
            raw_messages.append(fb_msg)
        continue

    wallclock_ms = int((time.monotonic() - start) * 1000)
    # cost_paise accrues even on terminated runs (hard rule, VT-35 brief):
    # the API spend already happened; refunds are not a thing.
    cost_paise = compute_cost_paise(
        model=model,
        input_tokens=input_tokens_used,
        output_tokens=output_tokens_used,
    )
    tokens_used = input_tokens_used + output_tokens_used
    # VT-36: gate's self_evaluate calls count toward the model's tool-
    # dispatch budget. They also count toward the AgentResult's
    # observability tool_calls_made for parity with the enforcer count.
    if gate is not None:
        tool_calls_made += gate.evaluator_calls

    terminated_by: HardLimitAxis | None = None
    terminated_reason: str | None = None
    if ctx.is_cancelled:
        status = "terminated"
        terminated_by = ctx.cancelled_by
        terminated_reason = ctx.reason
        _emit_hard_limit_breach(
            context=context,
            axis=cast(HardLimitAxis, terminated_by),
            reason=cast(str, terminated_reason),
            tokens_used=tokens_used,
            tool_calls_made=tool_calls_made,
            wallclock_ms=wallclock_ms,
        )

    return AgentResult(
        status=cast(Any, status),
        terminated_by=terminated_by,
        output=output,
        tokens_used=tokens_used,
        tool_calls_made=tool_calls_made,
        wallclock_ms=wallclock_ms,
        cost_paise=cost_paise,
        raw_messages=raw_messages,
        terminated_reason=terminated_reason,
    )


def _emit_self_evaluate_gate(
    *,
    context: SalesRecoveryContext,
    attempt_number: int,
    outcome: Any,
    rejection_feedback: Any,
    feedback_messages: list[dict[str, str]],
) -> None:
    """Write one pipeline_steps row per gate.run() — per-attempt
    self_evaluate telemetry (VT-SalesRecovery-Agent wiring).

    step_kind = 'self_evaluate_gate' (canonical per VT-179 Option A;
    renamed from legacy 'self_evaluate_attempt'). output_envelope carries the
    attempt number + verdict + reasons (list-per-category preserved
    when present). VT-379: written via the shared redacting writer
    (``write_redacted_step_row``) — gate reasons / feedback messages are
    free text and were previously INSERTed raw; redaction (patterns +
    tenant name registry) now runs at write. RLS-scoped via
    tenant_connection inside the helper. Best-effort —
    observability MUST NOT break the run."""
    from orchestrator.observability.pipeline_observability import (
        write_redacted_step_row,
    )

    envelope: dict[str, Any] = {
        "attempt_number": attempt_number,
        "outcome": outcome.value if outcome is not None else None,
    }
    # rejection_feedback is populated only on REJECTED outcomes; for
    # RETRY the feedback lives in feedback_messages (the structured
    # message bag the loop appends).
    if rejection_feedback is not None:
        envelope["reasons"] = {
            "schema": rejection_feedback.schema,
            "pillar": rejection_feedback.pillar,
            "consistency": rejection_feedback.consistency,
            "legal": rejection_feedback.legal,
        }
    elif feedback_messages:
        envelope["feedback_messages"] = feedback_messages

    try:
        write_redacted_step_row(
            run_id=context.run_id,
            tenant_id=context.tenant_id,
            step_kind="self_evaluate_gate",
            output_envelope=envelope,
        )
    except Exception:
        # Observability never breaks recovery (CL-242 — same precedent
        # as orchestrator.error_router._log_decision).
        pass


def _emit_self_eval_rejected(
    *,
    context: SalesRecoveryContext,
    rejection_feedback: Any,
    attempt_number: int,
    tokens_used: int,
    tool_calls_made: int,
    wallclock_ms: int,
) -> None:
    """Route a FailureRecord(SELF_EVAL_REJECTED) — the gate exhausted
    its one-retry budget and the run is rejected. Router escalates
    to Fazal per the spec (severity HIGH, default_strategy
    ESCALATE_TO_FAZAL)."""
    reasons: dict[str, Any] = {}
    if rejection_feedback is not None:
        reasons = {
            "schema": rejection_feedback.schema,
            "pillar": rejection_feedback.pillar,
            "consistency": rejection_feedback.consistency,
            "legal": rejection_feedback.legal,
        }
    failure = FailureRecord(
        failure_type=FailureType.SELF_EVAL_REJECTED,
        message=(
            f"self_evaluate gate rejected after {attempt_number} attempts "
            "(initial draft + one retry)"
        ),
        occurred_at=datetime.now(UTC),
        tenant_id=context.tenant_id,
        run_id=context.run_id,
        metadata={
            "source": "self_evaluate_gate",
            "attempt_number": attempt_number,
            "reasons": reasons,
            "tokens_used": tokens_used,
            "tool_calls_made": tool_calls_made,
            "wallclock_ms": wallclock_ms,
        },
    )
    route_failure(failure)


def _emit_invalid_output(
    *,
    context: SalesRecoveryContext,
    reason: str,
    tokens_used: int,
    tool_calls_made: int,
    wallclock_ms: int,
    source: str = "self_evaluate_gate",
    schema_field_paths: list[str] | None = None,
) -> None:
    """Route a FailureRecord(AGENT_INVALID_OUTPUT). Callers:

    - ``source="self_evaluate_gate"`` — VT-36 gate seam error.
    - ``source="agent_terminal_no_dict"`` — CL-287: agent terminated
      with text that did not parse as a single JSON dict (Path A).
      Closes the CL-238 silent-failure hole where the loop previously
      exited ``status='invalid'`` without observability.
    - ``source="agent_variant_discriminator_invalid"`` — VT-4: the
      model emitted a JSON dict but the ``status`` discriminator was
      missing or not one of the v1.0 legal variants. Closes the second
      CL-238 silent-failure hole.
    - ``source="agent_schema_rejection"`` — VT-4: post-coerce
      CampaignPlan schema rejection (e.g. required field absent on a
      legal variant). Closes the third CL-238 silent-failure hole.

    VT-496: ``schema_field_paths`` (the ``agent_schema_rejection`` path)
    carries the pydantic ValidationError's ``"<loc>: <type>"`` summaries —
    NON-PII schema paths (loc + error code only, never the offending value).
    They land in ``metadata['schema_field_paths']`` as a structured list of
    short, pattern-free strings, so they SURVIVE the write-time redactor
    (which SHA-hashes the long free-text ``message`` whole). A plain SQL read
    of ``pipeline_steps.error->'metadata'->'schema_field_paths'`` then names
    the failing CampaignPlanProposed fields without the LLM key.

    Best-effort — routing failure must NOT re-raise into the run."""
    metadata: dict[str, Any] = {
        "source": source,
        "tokens_used": tokens_used,
        "tool_calls_made": tool_calls_made,
        "wallclock_ms": wallclock_ms,
    }
    if schema_field_paths:
        metadata["schema_field_paths"] = schema_field_paths
    failure = FailureRecord(
        failure_type=FailureType.AGENT_INVALID_OUTPUT,
        message=reason,
        occurred_at=datetime.now(UTC),
        tenant_id=context.tenant_id,
        run_id=context.run_id,
        metadata=metadata,
    )
    route_failure(failure)


def _emit_hard_limit_breach(
    *,
    context: SalesRecoveryContext,
    axis: HardLimitAxis,
    reason: str,
    tokens_used: int,
    tool_calls_made: int,
    wallclock_ms: int,
) -> None:
    """Construct + route a FailureRecord for a hard-limit cancellation
    (VT-35 / VT-29 surface). Best-effort — a routing failure must NOT
    re-raise into the run (observability cannot break recovery; the
    error_router itself swallows + logs internally)."""
    failure = FailureRecord(
        failure_type=FailureType.AGENT_HARD_LIMIT_BREACH,
        message=reason,
        occurred_at=datetime.now(UTC),
        tenant_id=context.tenant_id,
        run_id=context.run_id,
        metadata={
            "axis": axis.value,
            "tokens_used": tokens_used,
            "tool_calls_made": tool_calls_made,
            "wallclock_ms": wallclock_ms,
        },
    )
    route_failure(failure)


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Best-effort serialisation of an Anthropic content block to a dict."""
    if hasattr(block, "model_dump"):
        return cast(dict[str, Any], block.model_dump())
    if isinstance(block, dict):
        return block
    return {
        "type": getattr(block, "type", None),
        "text": getattr(block, "text", None),
    }


__all__ = [
    "SalesRecoveryContext",
    "run_sales_recovery_agent",
]
