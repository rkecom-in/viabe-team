"""VT-32 — sales_recovery agent skeleton tests.

Three surfaces:

1. ``AgentResult`` contract: shape, defaults, ``terminated_by`` accepts
   every ``HardLimitAxis`` member.
2. ``run_sales_recovery_agent`` with a MOCKED ``anthropic.Anthropic``
   client — zero real API calls in CI. Exercises the placeholder happy
   path, raw_messages capture, cost attribution, status mapping.
3. ``sales_recovery_node`` translates an ``AgentResult`` into a
   reducer-friendly LangGraph state update.

A real-API canary test against ``claude-haiku-4-5`` lives at the bottom,
env-gated by ``VIABE_RUN_AGENT_CANARY=1`` and ``ANTHROPIC_API_KEY`` so it
DOES NOT run in CI (CI must not burn API quota — hard rule, VT-32).
Fazal triggers it manually once before merge.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("yaml")

import orchestrator.context_builder as _cb_mod  # noqa: E402

from orchestrator.agent.cost import RATES, compute_cost_paise  # noqa: E402
from orchestrator.agent.sales_recovery import (  # noqa: E402
    SalesRecoveryContext,
    run_sales_recovery_agent,
)
from orchestrator.agent.sales_recovery_node import sales_recovery_node  # noqa: E402
from orchestrator.agent.types import AgentResult  # noqa: E402
from orchestrator.failures import HardLimitAxis  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_db_backed_campaigns_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    """VT-138 / VT-146 / VT-67: ``_build_recent_campaigns``,
    ``_build_pending_owner_inputs`` and ``_build_ledger_summary`` (L2) are now
    live DB reads via ``tenant_connection``. The unit tests in this file never
    spin up a DB substrate — they exercise the agent loop and node wrappers with
    mocked Anthropic clients. Stub the DB-backed builders to safe-empty so
    bundle construction stays pure-Python.
    """
    monkeypatch.setattr(_cb_mod, "_build_recent_campaigns", lambda tid: ([], False))
    monkeypatch.setattr(
        _cb_mod, "_build_pending_owner_inputs", lambda tid: ([], False)
    )
    monkeypatch.setattr(
        _cb_mod, "_build_ledger_summary", lambda tid: (_cb_mod.LedgerSummary(), True)
    )
    monkeypatch.setattr(
        _cb_mod, "_build_l3_priors", lambda tid, rid: (_cb_mod.L3Priors(), False)
    )
    monkeypatch.setattr(
        _cb_mod, "_build_l4_skills", lambda tid, req: (_cb_mod.L4Skills(), False)
    )


# --- 1. AgentResult contract -------------------------------------------------


def test_agent_result_defaults_are_safe():
    """A freshly constructed AgentResult has zero-spend, empty trace, no
    terminated state. Required so callers can build it incrementally
    without leaking junk numbers into telemetry."""
    result = AgentResult(status="completed")
    assert result.terminated_by is None
    assert result.output is None
    assert result.tokens_used == 0
    assert result.tool_calls_made == 0
    assert result.wallclock_ms == 0
    assert result.cost_paise == 0
    assert result.raw_messages == []
    assert result.terminated_reason is None


@pytest.mark.parametrize("axis", list(HardLimitAxis))
def test_agent_result_accepts_every_hard_limit_axis(axis: HardLimitAxis):
    """terminated_by reuses the failures.HardLimitAxis enum (CL-242).
    VT-35's enforcers will populate this field; the dataclass must
    accept every value the enum defines without translation."""
    result = AgentResult(
        status="terminated",
        terminated_by=axis,
        terminated_reason=f"{axis.value} budget exceeded",
    )
    assert result.terminated_by is axis


# --- 2. run_sales_recovery_agent with mocked Anthropic ----------------------


def _fake_response(
    *,
    text: str,
    input_tokens: int = 10,
    output_tokens: int = 5,
    stop_reason: str = "end_turn",
) -> Any:
    """Build a SimpleNamespace shaped like an Anthropic Message response."""

    class _TextBlock(SimpleNamespace):
        def model_dump(self) -> dict[str, Any]:
            return {"type": "text", "text": self.text}

    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens
        ),
        content=[_TextBlock(type="text", text=text)],
        stop_reason=stop_reason,
    )


def _patched_client(response: Any) -> Any:
    """Make Anthropic() return a client whose messages.create returns ``response``."""
    fake = MagicMock()
    fake.messages.create.return_value = response
    return fake


def test_run_sales_recovery_agent_placeholder_happy_path(monkeypatch):
    """Placeholder prompt → model returns the placeholder JSON →
    status='placeholder', output is the parsed dict, raw_messages
    captures the assistant turn, cost is non-zero, no terminated_by.

    Token counts are deliberately not tiny: the paise-per-token table is
    coarse, so a 10/5 split would round to 0 paise; use realistic
    placeholder-turn counts so the cost-accumulation path is asserted."""
    response = _fake_response(
        text='{"status": "placeholder"}', input_tokens=2000, output_tokens=200
    )
    fake_client = _patched_client(response)

    monkeypatch.setenv("VIABE_ENV", "test")  # → Haiku
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )

    result = run_sales_recovery_agent(
        SalesRecoveryContext(tenant_id="t1", run_id="r1", user_request="test request")
    )

    assert result.status == "placeholder"
    assert result.output == {"status": "placeholder"}
    assert result.terminated_by is None
    assert result.tokens_used == 2200  # 2000 input + 200 output
    assert result.tool_calls_made == 0
    assert result.wallclock_ms >= 0
    assert result.cost_paise > 0  # Phase-1 rates are positive for Haiku
    # raw_messages has the seeded "begin" user turn + one assistant turn.
    assert any(
        m["role"] == "assistant"
        and any(
            block.get("text") == '{"status": "placeholder"}'
            for block in m["content"]
        )
        for m in result.raw_messages
    )


def test_run_sales_recovery_agent_uses_resolved_model_from_env(monkeypatch):
    """VIABE_ENV='production' → Opus; default/dev/test → the CAPABLE tier (Sonnet,
    VT-501 — NOT Haiku: SR-draft is complex grounded reasoning per VT-480). The
    model id is read from config/models.yaml, never hardcoded in the runner."""
    response = _fake_response(text='{"status": "placeholder"}')
    fake_client = _patched_client(response)
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )

    monkeypatch.setenv("VIABE_ENV", "production")
    run_sales_recovery_agent(SalesRecoveryContext(tenant_id="t1", run_id="r1", user_request="test request"))
    assert fake_client.messages.create.call_args.kwargs["model"] == "claude-opus-4-7"

    fake_client.messages.create.reset_mock()
    monkeypatch.setenv("VIABE_ENV", "test")
    run_sales_recovery_agent(SalesRecoveryContext(tenant_id="t1", run_id="r1", user_request="test request"))
    # VT-501: dev/test SR-draft now resolves to the capable model, never Haiku —
    # complex grounded reasoning must not fall to the cheap slot (misclassification fix).
    resolved = fake_client.messages.create.call_args.kwargs["model"]
    assert resolved == "claude-sonnet-4-6"
    assert resolved != "claude-haiku-4-5"


def test_run_sales_recovery_agent_passes_brief_required_params(monkeypatch):
    """Per-response output cap (NOT the VT-35 run-level hard limit),
    extended thinking on, empty tools. ``max_tokens`` here is the
    per-call response cap; the 80K run-level token ceiling is a
    documented constant the VT-35 token meter enforces, never passed
    to ``messages.create``. Pin the call shape so a regression is loud."""
    from orchestrator.agent.sales_recovery import (
        _MAX_OUTPUT_TOKENS_PER_TURN,
        _RUN_LEVEL_TOKEN_HARD_LIMIT,
    )

    response = _fake_response(text='{"status": "placeholder"}')
    fake_client = _patched_client(response)
    monkeypatch.setenv("VIABE_ENV", "test")
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )

    run_sales_recovery_agent(SalesRecoveryContext(tenant_id="t1", run_id="r1", user_request="test request"))
    call = fake_client.messages.create.call_args
    assert call.kwargs["max_tokens"] == _MAX_OUTPUT_TOKENS_PER_TURN
    assert call.kwargs["max_tokens"] != _RUN_LEVEL_TOKEN_HARD_LIMIT, (
        "messages.create max_tokens must NOT be the run-level 80K ceiling"
    )
    # Extended thinking is intentionally NOT wired for the v1.0 prompt
    # path (VT-32). The real agent's thinking policy is a VT-4.2-era
    # decision; VT-33 (system prompt) does not pre-empt it.
    assert "thinking" not in call.kwargs
    assert call.kwargs["tools"] == []
    # System prompt is the v1.0 sales_recovery file (VT-33). Spot-check
    # identity, output-contract reference, and a Pillar 4 marker so a
    # silent edit that drops a load-bearing section is caught.
    prompt = call.kwargs["system"]
    assert "Sales Recovery Agent" in prompt
    assert "CampaignPlan" in prompt
    assert "out_of_scope" in prompt
    assert "insufficient_data" in prompt
    assert "Pillar 4" in prompt  # retrieve-don't-calculate enforcement


def test_run_sales_recovery_agent_status_invalid_when_output_unparseable(monkeypatch):
    """Non-JSON model output → status='invalid', output=None.

    Locks against a regression in which the fence-stripper grows into a
    loose "first { to last }" extractor: garbage like "hello {world}"
    must STILL classify as invalid, not silently parse into a partial
    dict.

    CL-287: Path A now emits a FailureRecord(AGENT_INVALID_OUTPUT)
    before breaking — patch route_failure to a MagicMock so the
    FailureRecord UUID construction has valid input and the emit is
    captured rather than crashing on this test's placeholder tenant
    string."""
    from uuid import uuid4

    response = _fake_response(text="hello {world}")
    fake_client = _patched_client(response)
    monkeypatch.setenv("VIABE_ENV", "test")
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.route_failure", MagicMock()
    )

    result = run_sales_recovery_agent(
        SalesRecoveryContext(
            tenant_id=str(uuid4()),
            run_id=str(uuid4()),
            user_request="test request",
        )
    )
    assert result.status == "invalid"
    assert result.output is None


def test_run_sales_recovery_agent_tolerates_markdown_json_fence(monkeypatch):
    """Haiku/Opus intermittently wrap JSON in a ```json ... ``` fence.
    The placeholder canary failure (#3) was caused by the parser
    rejecting fenced output. Production CampaignPlan output will hit
    the same wrapper — fence tolerance is a correctness fix, not a
    placeholder-specific one."""
    fenced = '```json\n{"status": "placeholder"}\n```'
    response = _fake_response(text=fenced)
    fake_client = _patched_client(response)
    monkeypatch.setenv("VIABE_ENV", "test")
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )

    result = run_sales_recovery_agent(
        SalesRecoveryContext(tenant_id="t1", run_id="r1", user_request="test request")
    )
    assert result.status == "placeholder"
    assert result.output == {"status": "placeholder"}


def test_run_sales_recovery_agent_tolerates_bare_code_fence(monkeypatch):
    """Same regression, fence without the ``json`` tag — bare triple
    backticks. Some models emit this shape."""
    fenced = '```\n{"status": "placeholder"}\n```'
    response = _fake_response(text=fenced)
    fake_client = _patched_client(response)
    monkeypatch.setenv("VIABE_ENV", "test")
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )

    result = run_sales_recovery_agent(
        SalesRecoveryContext(tenant_id="t1", run_id="r1", user_request="test request")
    )
    assert result.status == "placeholder"
    assert result.output == {"status": "placeholder"}


def test_run_sales_recovery_agent_does_not_loose_extract_from_prose(monkeypatch):
    """Defensive: garbage prose containing a JSON-shaped substring must
    still classify as invalid. The fence stripper is NARROW — it only
    matches a recognised fence pattern at the message boundaries. A
    loose ``"first { to last }"`` extractor would silently parse the
    embedded substring and corrupt the status classification.

    CL-287: Path A emit now needs a valid UUID tenant_id; patch
    route_failure so the new FailureRecord construction is captured."""
    from uuid import uuid4

    response = _fake_response(
        text='I think the answer is {"status": "placeholder"} or so'
    )
    fake_client = _patched_client(response)
    monkeypatch.setenv("VIABE_ENV", "test")
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.route_failure", MagicMock()
    )

    result = run_sales_recovery_agent(
        SalesRecoveryContext(
            tenant_id=str(uuid4()),
            run_id=str(uuid4()),
            user_request="test request",
        )
    )
    assert result.status == "invalid"
    assert result.output is None


def test_run_sales_recovery_agent_path_a_emits_failure_record(monkeypatch):
    """CL-287: Path A (terminal output not a JSON dict → status='invalid'
    with output=None) MUST emit a FailureRecord(AGENT_INVALID_OUTPUT)
    before breaking. Closes the CL-238 silent-failure hole where the
    agent could exit invalid without observability."""
    from uuid import uuid4

    from orchestrator.failures import FailureRecord, FailureType

    response = _fake_response(text="I cannot proceed without a cohort spec.")
    fake_client = _patched_client(response)
    monkeypatch.setenv("VIABE_ENV", "test")
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )
    router = MagicMock()
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.route_failure", router
    )

    tenant_id = uuid4()
    run_id = uuid4()
    result = run_sales_recovery_agent(
        SalesRecoveryContext(
            tenant_id=tenant_id,
            run_id=run_id,
            user_request="Recover dormant customers from the last 60 days",
        )
    )
    assert result.status == "invalid"
    assert result.output is None

    # Exactly one FailureRecord(AGENT_INVALID_OUTPUT) routed with source
    # 'agent_terminal_no_dict' — the new CL-287 emit, not the gate's.
    assert router.call_count == 1
    failure = router.call_args.args[0]
    assert isinstance(failure, FailureRecord)
    assert failure.failure_type is FailureType.AGENT_INVALID_OUTPUT
    assert failure.tenant_id == tenant_id
    assert failure.run_id == run_id
    assert failure.metadata["source"] == "agent_terminal_no_dict"
    assert "did not parse as a single JSON dict" in failure.message


def test_run_sales_recovery_agent_variant_discriminator_invalid_emits_failure(
    monkeypatch,
):
    """VT-4: model emits a JSON dict whose ``status`` is not one of the
    three legal CampaignPlan v1.0 variants → ``_construct_variant_payload``
    raises ValueError → the loop MUST emit a FailureRecord
    (AGENT_INVALID_OUTPUT) before breaking with status='invalid'.

    Closes the second CL-238 silent-failure hole — the loop previously
    exited invalid here without any observability."""
    import json
    from uuid import uuid4

    from orchestrator.failures import FailureRecord, FailureType

    response = _fake_response(text=json.dumps({"status": "not_a_legal_variant"}))
    fake_client = _patched_client(response)
    monkeypatch.setenv("VIABE_ENV", "test")
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )
    router = MagicMock()
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.route_failure", router
    )

    tenant_id = uuid4()
    run_id = uuid4()
    result = run_sales_recovery_agent(
        SalesRecoveryContext(
            tenant_id=tenant_id,
            run_id=run_id,
            user_request="Recover dormant customers",
        )
    )
    assert result.status == "invalid"
    # output is preserved (unlike Path A) — the model DID emit a parseable
    # dict; the rejection is at the discriminator. Keeping the raw payload
    # in the AgentResult helps post-hoc debugging without leaking through
    # the orchestrator's variant typing.
    assert result.output == {"status": "not_a_legal_variant"}
    assert router.call_count == 1
    failure = router.call_args.args[0]
    assert isinstance(failure, FailureRecord)
    assert failure.failure_type is FailureType.AGENT_INVALID_OUTPUT
    assert failure.tenant_id == tenant_id
    assert failure.run_id == run_id
    assert failure.metadata["source"] == "agent_variant_discriminator_invalid"
    assert "variant discriminator" in failure.message


def test_run_sales_recovery_agent_schema_rejection_emits_failure(monkeypatch):
    """VT-4: model emits a JSON dict with a legal ``status`` discriminator
    but the post-coerce payload is rejected by ``parse_campaign_plan``
    (e.g. required variant field absent) → the loop MUST emit a
    FailureRecord(AGENT_INVALID_OUTPUT) before breaking with
    status='invalid'.

    Closes the third CL-238 silent-failure hole. The parse_campaign_plan
    branch only runs when an evaluator is configured (VT-36 gate path);
    a stub FakeSelfEvaluator is supplied so we reach the rejection."""
    import json
    from uuid import uuid4

    from orchestrator.agent.self_evaluate import FakeSelfEvaluator
    from orchestrator.failures import FailureRecord, FailureType

    # Legal status; missing every required variant field. Coercer keeps
    # only declared fields → payload reaches parse_campaign_plan with
    # status+identity only → Pydantic rejects.
    response = _fake_response(text=json.dumps({"status": "proposed"}))
    fake_client = _patched_client(response)
    monkeypatch.setenv("VIABE_ENV", "test")
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )
    router = MagicMock()
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.route_failure", router
    )

    tenant_id = uuid4()
    run_id = uuid4()
    # FakeSelfEvaluator with empty verdicts list — parse_campaign_plan
    # raises before .run() is called, so the verdict queue is never
    # consumed. The evaluator's only role here is to make ``gate`` truthy
    # so the parse branch executes.
    evaluator = FakeSelfEvaluator(verdicts=[])
    result = run_sales_recovery_agent(
        SalesRecoveryContext(
            tenant_id=tenant_id,
            run_id=run_id,
            user_request="Recover dormant customers",
        ),
        evaluator=evaluator,
    )
    assert result.status == "invalid"
    # output reaches the coerce step (legal status discriminator) and is
    # preserved on the AgentResult — useful for post-hoc inspection of
    # what the model emitted before the schema rejection.
    assert isinstance(result.output, dict)
    assert result.output.get("status") == "proposed"
    assert router.call_count == 1
    failure = router.call_args.args[0]
    assert isinstance(failure, FailureRecord)
    assert failure.failure_type is FailureType.AGENT_INVALID_OUTPUT
    assert failure.tenant_id == tenant_id
    assert failure.run_id == run_id
    assert failure.metadata["source"] == "agent_schema_rejection"
    assert "schema rejection" in failure.message
    # VT-496: structured field paths (loc + pydantic type) on the
    # FailureRecord metadata — names the failing CampaignPlanProposed
    # fields so the win-back parse failure is diagnosable on dev.
    field_paths = failure.metadata["schema_field_paths"]
    assert isinstance(field_paths, list)
    # {"status": "proposed"} → every OTHER required variant field is absent.
    assert "proposed.message_plan: missing" in field_paths
    assert "proposed.target_cohort: missing" in field_paths
    assert "proposed.expected_arrr: missing" in field_paths
    assert "proposed.evidence_refs: missing" in field_paths
    # VT-499: campaign_window is NO LONGER a missing field — the coercer
    # server-injects an always-valid now->now+7d window on the proposed
    # variant, so it is supplied even when the model emits nothing for it.
    assert "proposed.campaign_window: missing" not in field_paths
    # (b) NO PII / value leakage — only "<loc>: <type>" pairs. No pydantic
    # ``input_value=`` / ``msg`` echo in either the paths or the message.
    for path in field_paths:
        assert path.startswith("proposed.")
        assert path.endswith(": missing")
        assert "input_value" not in path
    assert "input_value" not in failure.message
    # The reason is rebuilt from the same NON-PII paths (no str(exc) echo).
    # campaign_window is VT-499 server-supplied, so message names a STILL-missing
    # field rather than the (now-filled) window.
    assert "proposed.message_plan: missing" in failure.message


def test_run_sales_recovery_agent_cost_uses_compute_cost_paise(monkeypatch):
    """The agent's cost_paise matches the cost.py table for the resolved model.

    Relative to ``_resolve_model`` (not a hardcoded model id) so it survives a
    config/models.yaml tier change — e.g. VT-501 moved the dev/test slot off Haiku
    onto the capable Sonnet."""
    from orchestrator.agent.sales_recovery import _resolve_model

    response = _fake_response(text='{"status": "placeholder"}', input_tokens=1000, output_tokens=200)
    fake_client = _patched_client(response)
    monkeypatch.setenv("VIABE_ENV", "test")  # dev/test slot (Sonnet 4.6, VT-501)
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )

    result = run_sales_recovery_agent(
        SalesRecoveryContext(tenant_id="t1", run_id="r1", user_request="test request")
    )

    expected = compute_cost_paise(
        model=_resolve_model("sales_recovery"), input_tokens=1000, output_tokens=200
    )
    assert result.cost_paise == expected
    assert result.cost_paise > 0


# --- compute_cost_paise table sanity -----------------------------------------


def test_cost_table_covers_both_haiku_and_opus():
    """Cost table MUST carry both production (Opus) and test (Haiku) rates
    (CL-242 — cost attribution can't go dark for either model)."""
    assert "claude-opus-4-7" in RATES
    assert "claude-haiku-4-5" in RATES


def test_cost_haiku_input_one_million_tokens_is_8500_paise():
    """₹1 = $85 conv; Haiku input = $1/M. 1M Haiku-input tokens = ₹85 = 8500 paise."""
    assert (
        compute_cost_paise(
            model="claude-haiku-4-5",
            input_tokens=1_000_000,
            output_tokens=0,
        )
        == 8500
    )


def test_cost_opus_output_one_million_tokens_is_637500_paise():
    """Opus output = $75/M; 1M output × ₹85/USD × 100 paise/INR = 637,500 paise."""
    assert (
        compute_cost_paise(
            model="claude-opus-4-7",
            input_tokens=0,
            output_tokens=1_000_000,
        )
        == 637_500
    )


def test_cost_unknown_model_raises():
    with pytest.raises(KeyError):
        compute_cost_paise(model="claude-sonnet-9-9", input_tokens=1, output_tokens=1)


def test_cost_rejects_negative_token_counts():
    with pytest.raises(ValueError):
        compute_cost_paise(model="claude-opus-4-7", input_tokens=-1, output_tokens=0)


# --- 3. LangGraph node wrapper -----------------------------------------------


def test_sales_recovery_node_returns_agent_result_under_agent_result_key(monkeypatch):
    """The node translates AgentResult → state update under 'agent_result'.

    The node now (VT-SalesRecovery-Agent gate-wiring) constructs a
    ``SelfEvaluateAdapter`` per invocation and routes through the gate.
    For the placeholder JSON response the loop's placeholder-branch
    fires before the gate runs (gate only sees CampaignPlan-shaped
    drafts), so the gate doesn't need a real seam here.

    Exec-6.85: the node consumes the Context Composer bundle from
    ``state['sales_recovery_context']`` — supply one via
    ``build_sales_recovery_context``.
    """
    from uuid import uuid4

    from orchestrator.context_builder import build_sales_recovery_context

    response = _fake_response(text='{"status": "placeholder"}')
    fake_client = _patched_client(response)
    monkeypatch.setenv("VIABE_ENV", "test")
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake_client
    )

    bundle = build_sales_recovery_context(
        uuid4(), uuid4(), "weekly_cadence", "test request"
    )
    update = sales_recovery_node({"sales_recovery_context": bundle})
    assert "agent_result" in update
    assert update["agent_result"]["status"] == "placeholder"
    assert update["agent_result"]["output"] == {"status": "placeholder"}


def test_sales_recovery_node_fail_loud_on_missing_bundle():
    """Exec-6.85: state with no Context Composer bundle is a contract
    breach at the seam — fail loud with TenantIsolationError rather than
    running the specialist against no task context."""
    from orchestrator._tenant_guard import TenantIsolationError

    with pytest.raises(TenantIsolationError, match="sales_recovery_context"):
        sales_recovery_node({})
    with pytest.raises(TenantIsolationError, match="sales_recovery_context"):
        sales_recovery_node({"sales_recovery_context": None})


# --- CL-288: emit-shape coercion / per-variant payload ----------------------


def _future_window_pair() -> tuple[str, str]:
    """ISO timestamps for a 7-day campaign window starting tomorrow.

    CampaignWindow validator rejects backdated starts and requires
    end > start; pin both to safe future-tz-aware values.
    """
    from datetime import UTC, datetime, timedelta

    start = datetime.now(UTC) + timedelta(days=1)
    end = start + timedelta(days=7)
    return start.isoformat(), end.isoformat()


def _proposed_raw_minimal() -> dict[str, Any]:
    """The minimal valid raw dict an obedient model would emit for the
    PROPOSED variant. Mimics what `_parse_placeholder_output` returns
    once coerced; pre-coercion the model also emits forbidden fields
    that this fixture deliberately includes to exercise the dropper."""
    from uuid import uuid4

    start, end = _future_window_pair()
    return {
        "status": "proposed",
        "campaign_window": {"start": start, "end": end},
        "target_cohort": {
            "customer_ids": [str(uuid4())],
            "cohort_label": "dormant-60d",
            "cohort_size": 1,
            "selection_reason": "Inactive customers last 60d [E1].",
        },
        "expected_arrr": {
            "low_paise": 100_000,
            "high_paise": 500_000,
            "confidence": "low",
            "basis": "Historical recovery rate 20-40% per [E1].",
        },
        "evidence_refs": [
            {
                "claim_id": "E1",
                "source_kind": "l4_skill_corpus",
                "source_id": "dormant-recovery-benchmark",
                "note": None,
            }
        ],
        "message_plan": {
            "template_id": "dormant_recovery_v1",
            "template_params": {"discount": "10"},
            "language": "en",
            "personalization": "Hi {name}, we miss you.",
        },
        # The model would set the identity fields too — coercion overwrites.
        "tenant_id": "00000000-0000-0000-0000-aaaaaaaaaaaa",
        "run_id": "00000000-0000-0000-0000-bbbbbbbbbbbb",
        "generated_at": "2020-01-01T00:00:00+00:00",
        # Forbidden-on-proposed (empty) — should be dropped silently.
        "out_of_scope_reason": None,
        "missing_data": [],
    }


def _out_of_scope_raw_minimal() -> dict[str, Any]:
    return {
        "status": "out_of_scope",
        "out_of_scope_reason": (
            "Request is about review reputation; that's the reputation "
            "specialist, not sales recovery."
        ),
        "suggested_specialist": "reputation",
        # Forbidden empty — should be dropped silently.
        "campaign_window": None,
        "target_cohort": None,
        "expected_arrr": None,
        "evidence_refs": [],
        "message_plan": None,
        "missing_data": [],
        "tenant_id": None,
        "run_id": None,
        "generated_at": None,
    }


def _insufficient_data_raw_minimal() -> dict[str, Any]:
    return {
        "status": "insufficient_data",
        "missing_data": [
            {
                "category": "cohort",
                "description": "No customer rows surfaced for this tenant.",
                "suggested_remediation": "Seed the customer ledger.",
            }
        ],
        # Forbidden empty — should be dropped silently.
        "out_of_scope_reason": None,
        "suggested_specialist": None,
        "campaign_window": None,
        "target_cohort": None,
        "expected_arrr": None,
        "evidence_refs": [],
        "message_plan": None,
        "tenant_id": None,
        "run_id": None,
        "generated_at": None,
    }


def _ctx_with_real_uuids() -> "SalesRecoveryContext":
    from uuid import uuid4

    return SalesRecoveryContext(
        tenant_id=str(uuid4()),
        run_id=str(uuid4()),
        user_request="test request",
    )


def test_construct_variant_payload_proposed_roundtrips_through_parse():
    """CL-288: proposed variant — coerce model raw → parse_campaign_plan
    returns CampaignPlanProposed with identity fields injected from
    context, populated forbidden fields dropped silently (none here),
    and the campaign-side fields preserved."""
    from datetime import UTC, datetime

    from orchestrator.agent.sales_recovery import _construct_variant_payload
    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanProposed,
        CampaignStatus,
        parse_campaign_plan,
    )

    ctx = _ctx_with_real_uuids()
    now = datetime.now(UTC)
    payload, dropped_empty, dropped_populated = _construct_variant_payload(
        _proposed_raw_minimal(), context=ctx, generated_at=now
    )

    plan = parse_campaign_plan(payload)
    assert isinstance(plan, CampaignPlanProposed)
    assert plan.status is CampaignStatus.PROPOSED
    assert str(plan.tenant_id) == ctx.tenant_id  # overwritten from context
    assert str(plan.run_id) == ctx.run_id
    assert plan.generated_at == now
    assert plan.target_cohort.cohort_label == "dormant-60d"
    # `out_of_scope_reason` and `missing_data` (empty on the raw dict)
    # were silently dropped — not present on the payload.
    assert "out_of_scope_reason" not in payload
    assert "missing_data" not in payload
    assert dropped_populated == {}
    assert sorted(dropped_empty) == ["missing_data", "out_of_scope_reason"]


def test_construct_variant_payload_out_of_scope_roundtrips_through_parse():
    """CL-288: out_of_scope variant — coerce + parse → CampaignPlanOutOfScope.
    All forbidden proposed-only / insufficient_data-only fields dropped."""
    from datetime import UTC, datetime

    from orchestrator.agent.sales_recovery import _construct_variant_payload
    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanOutOfScope,
        CampaignStatus,
        SuggestedSpecialist,
        parse_campaign_plan,
    )

    ctx = _ctx_with_real_uuids()
    payload, dropped_empty, dropped_populated = _construct_variant_payload(
        _out_of_scope_raw_minimal(),
        context=ctx,
        generated_at=datetime.now(UTC),
    )

    plan = parse_campaign_plan(payload)
    assert isinstance(plan, CampaignPlanOutOfScope)
    assert plan.status is CampaignStatus.OUT_OF_SCOPE
    assert plan.out_of_scope_reason.startswith("Request is about review")
    assert plan.suggested_specialist is SuggestedSpecialist.REPUTATION
    # Forbidden proposed-side fields are not on the payload.
    for forbidden in (
        "campaign_window",
        "target_cohort",
        "expected_arrr",
        "evidence_refs",
        "message_plan",
        "missing_data",
    ):
        assert forbidden not in payload
    assert dropped_populated == {}
    # All forbidden keys were empty in the fixture — landed in dropped_empty.
    assert sorted(dropped_empty) == [
        "campaign_window",
        "evidence_refs",
        "expected_arrr",
        "message_plan",
        "missing_data",
        "target_cohort",
    ]


def test_construct_variant_payload_insufficient_data_roundtrips_through_parse():
    """CL-288: insufficient_data variant — coerce + parse →
    CampaignPlanInsufficientData. Required identity fields injected;
    missing_data preserved; all variant-forbidden fields dropped."""
    from datetime import UTC, datetime

    from orchestrator.agent.sales_recovery import _construct_variant_payload
    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanInsufficientData,
        CampaignStatus,
        parse_campaign_plan,
    )

    ctx = _ctx_with_real_uuids()
    now = datetime.now(UTC)
    payload, dropped_empty, dropped_populated = _construct_variant_payload(
        _insufficient_data_raw_minimal(), context=ctx, generated_at=now
    )

    plan = parse_campaign_plan(payload)
    assert isinstance(plan, CampaignPlanInsufficientData)
    assert plan.status is CampaignStatus.INSUFFICIENT_DATA
    assert len(plan.missing_data) == 1
    assert plan.missing_data[0].category == "cohort"
    assert str(plan.tenant_id) == ctx.tenant_id
    assert str(plan.run_id) == ctx.run_id
    assert plan.generated_at == now
    # Forbidden proposed-side + out_of_scope-side fields all absent.
    for forbidden in (
        "campaign_window",
        "target_cohort",
        "expected_arrr",
        "evidence_refs",
        "message_plan",
        "out_of_scope_reason",
        "suggested_specialist",
    ):
        assert forbidden not in payload
    assert dropped_populated == {}
    assert "campaign_window" in dropped_empty


def test_construct_variant_payload_drops_populated_forbidden_and_emits(
    monkeypatch,
):
    """CL-288 item 2 — populated forbidden field on a non-proposed verdict:
    must be DROPPED from the payload AND surface a FailureRecord
    (MODEL_OUTPUT_CONFLICT) plus a WARN-level log so model self-
    contradiction is observable.

    Fixture: insufficient_data verdict but the model also emitted a
    populated ``message_plan`` (proposed-only) and a populated
    ``out_of_scope_reason`` (out_of_scope-only). Coercion must drop both,
    and the run-loop branch must route exactly one MODEL_OUTPUT_CONFLICT
    failure carrying both keys."""
    from datetime import UTC, datetime

    from orchestrator.agent.sales_recovery import (
        _construct_variant_payload,
        _emit_model_output_conflict,
    )
    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanInsufficientData,
        parse_campaign_plan,
    )
    from orchestrator.failures import FailureRecord, FailureType

    raw = _insufficient_data_raw_minimal()
    raw["message_plan"] = {
        "template_id": "leftover_v1",
        "template_params": {},
        "language": "en",
        "personalization": "hi",
    }
    raw["out_of_scope_reason"] = "leftover prose from a previous reasoning step"

    ctx = _ctx_with_real_uuids()
    payload, dropped_empty, dropped_populated = _construct_variant_payload(
        raw, context=ctx, generated_at=datetime.now(UTC)
    )

    # Both populated forbidden fields surfaced in the dropped_populated
    # dict, with their original raw values preserved for observability.
    assert set(dropped_populated.keys()) == {"message_plan", "out_of_scope_reason"}
    assert dropped_populated["out_of_scope_reason"].startswith("leftover prose")

    # Payload itself does not carry the forbidden keys.
    assert "message_plan" not in payload
    assert "out_of_scope_reason" not in payload

    # The payload still validates as the picked variant.
    plan = parse_campaign_plan(payload)
    assert isinstance(plan, CampaignPlanInsufficientData)

    # Routing the conflict produces exactly one FailureRecord with the
    # expected type + metadata. Patch route_failure so we capture it
    # without touching the DB.
    router = MagicMock()
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.route_failure", router
    )
    _emit_model_output_conflict(
        context=ctx,
        status_value="insufficient_data",
        dropped_keys=list(dropped_populated.keys()),
        raw_values=dropped_populated,
    )
    assert router.call_count == 1
    failure = router.call_args.args[0]
    assert isinstance(failure, FailureRecord)
    assert failure.failure_type is FailureType.MODEL_OUTPUT_CONFLICT
    assert failure.metadata["variant"] == "insufficient_data"
    assert set(failure.metadata["dropped_keys"]) == {
        "message_plan",
        "out_of_scope_reason",
    }
    assert (
        "leftover prose"
        in failure.metadata["dropped_values"]["out_of_scope_reason"]
    )


# --- VT-493: SR system-prompt schema conformance (date + source_kind enum) ---
#
# The VT-490 re-drive surfaced two parse_campaign_plan failures on the grounded
# proposed plan: (A1) the prompt hardcoded a stale 2026-05-22 campaign_window
# the model echoed verbatim → CampaignWindow start>=now rejection; (A2) the
# prompt never enumerated EvidenceSourceKind so the model invented off-enum
# source_kind values. Both are fixed by rendering the template with the current
# date + enumerating the 3 legal source_kind values.


def test_sr_prompt_injects_current_date_not_stale_literal_and_enum_source_kinds():
    """VT-493 A1+A2 — the ASSEMBLED prompt carries today's date (not the stale
    2026-05-22 literal) with a >=today campaign_window instruction, fully renders
    every template token, and enumerates the 3 legal EvidenceSourceKind values.
    Deterministic in a fixed ``now``."""
    from datetime import UTC, datetime

    from orchestrator.agent.sales_recovery import _render_sr_system_prompt

    fixed_now = datetime(2026, 7, 1, 14, 30, tzinfo=UTC)
    rendered = _render_sr_system_prompt(now=fixed_now)

    # A1 — the stale absolute window is gone and the template fully rendered.
    assert "2026-05-22" not in rendered
    assert "2026-05-29" not in rendered
    assert "{{" not in rendered  # no unrendered tokens leaked into the prompt
    # today's date is injected into the campaign_window instruction.
    assert "2026-07-01" in rendered
    # the proposed example's window starts TOMORROW 09:00 UTC (future-dated so a
    # verbatim echo still satisfies CampaignWindow start>=now) + ends 7d later.
    assert "2026-07-02T09:00:00+00:00" in rendered
    assert "2026-07-09T09:00:00+00:00" in rendered

    # A2 — all three legal EvidenceSourceKind values enumerated for the model.
    for kind in ("tool_call", "l4_skill_corpus", "l2_episodic_memory"):
        assert kind in rendered


def test_sr_prompt_example_window_passes_campaign_window_validator():
    """VT-493 A1 — the rendered proposed example's campaign_window must ITSELF
    pass the CampaignWindow validator. A verbatim echo (the original failure
    mode) must not re-trigger the backdated-start rejection."""
    import re

    from orchestrator.agent.sales_recovery import _render_sr_system_prompt
    from orchestrator.agent.schemas.campaign_plan import CampaignWindow

    rendered = _render_sr_system_prompt()
    m = re.search(
        r'"start":\s*"([^"]+)",\s*"end":\s*"([^"]+)"', rendered
    )
    assert m is not None, "campaign_window example not found in rendered prompt"
    # Constructs without raising → start>=now and end>start hold.
    window = CampaignWindow(start=m.group(1), end=m.group(2))  # type: ignore[arg-type]
    assert window.end > window.start


def test_sr_prompt_default_render_uses_real_now():
    """VT-493 — the no-arg render uses the live server date (today appears,
    the stale literal does not)."""
    from datetime import UTC, datetime

    from orchestrator.agent.sales_recovery import _render_sr_system_prompt

    rendered = _render_sr_system_prompt()
    assert datetime.now(UTC).date().isoformat() in rendered
    assert "2026-05-22" not in rendered


# --- VT-499: campaign_window is system-owned (server-injected, not LLM) -------
#
# VT-493 maximally prompted the campaign_window (injected today's date, valid
# example, named the validator) and the SR model (Haiku on dev) STILL emitted a
# backdated / invalid / missing window 3/3 — VT-496 named it deterministically
# (``proposed.campaign_window: value_error``). The window is a MECHANICAL value,
# not business judgment, so _construct_variant_payload now OVERRIDES it server-
# side on the PROPOSED variant with an always-valid now->now+7d span. The model
# keeps owning the business fields; the validator is NOT weakened.


def test_server_campaign_window_is_valid_now_to_now_plus_7d():
    """VT-499 — the server-computed window is tz-aware, ~now, spans exactly 7d,
    and constructs through the REAL CampaignWindow validator without raising."""
    from datetime import UTC, datetime, timedelta

    from orchestrator.agent.sales_recovery import _server_campaign_window
    from orchestrator.agent.schemas.campaign_plan import CampaignWindow

    now = datetime.now(UTC)
    w = _server_campaign_window(now)
    start = datetime.fromisoformat(w["start"])
    end = datetime.fromisoformat(w["end"])

    assert start.tzinfo is not None and end.tzinfo is not None
    assert start >= now - timedelta(seconds=1)  # ~now (small forward buffer)
    assert end - start == timedelta(days=7)
    # Constructs without raising → _window_validity passes (start>=now, end>start).
    window = CampaignWindow(start=w["start"], end=w["end"])  # type: ignore[arg-type]
    assert window.end > window.start


@pytest.mark.parametrize("bad_window", ["backdated", "missing", "naive_end_before_start"])
def test_vt499_proposed_window_overridden_so_bad_model_window_now_parses(bad_window):
    """VT-499 — a proposed raw dict whose model-emitted campaign_window is the
    EXACT Haiku failure (backdated / missing / invalid) now PARSES, because
    _construct_variant_payload replaces it with a server now->now+7d window.
    The business fields the model owns are preserved unchanged."""
    from datetime import UTC, datetime, timedelta

    from orchestrator.agent.sales_recovery import _construct_variant_payload
    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanProposed,
        CampaignStatus,
        CampaignWindow,
        parse_campaign_plan,
    )

    raw = _proposed_raw_minimal()
    if bad_window == "backdated":
        # The VT-493/VT-496 failure: the stale 2026-05-22 literal echoed back.
        bad = {"start": "2026-05-22T09:00:00+00:00", "end": "2026-05-29T09:00:00+00:00"}
        raw["campaign_window"] = bad
        # Sanity: this window is genuinely rejected by the validator on its own,
        # so a green parse below can ONLY come from the server override.
        with pytest.raises(ValueError):
            CampaignWindow(start=bad["start"], end=bad["end"])  # type: ignore[arg-type]
    elif bad_window == "missing":
        raw.pop("campaign_window")
    elif bad_window == "naive_end_before_start":
        raw["campaign_window"] = {
            "start": "2026-01-01T00:00:00",  # naive (no tz) AND end < start
            "end": "2025-01-01T00:00:00",
        }

    ctx = _ctx_with_real_uuids()
    now = datetime.now(UTC)
    payload, _dropped_empty, _dropped_populated = _construct_variant_payload(
        raw, context=ctx, generated_at=now
    )

    # Pre-VT-499 this raised proposed.campaign_window: value_error. Now it parses.
    plan = parse_campaign_plan(payload)
    assert isinstance(plan, CampaignPlanProposed)
    assert plan.status is CampaignStatus.PROPOSED

    start = plan.campaign_window.start
    end = plan.campaign_window.end
    assert start.tzinfo is not None and end.tzinfo is not None
    assert start >= now - timedelta(seconds=1)  # ~now — NOT the backdated start
    assert end - start == timedelta(days=7)

    # Business fields stay exactly what the model emitted — only the window moved.
    assert plan.target_cohort.cohort_label == "dormant-60d"
    assert plan.expected_arrr.low_paise == 100_000
    assert plan.expected_arrr.high_paise == 500_000
    assert plan.message_plan.template_id == "dormant_recovery_v1"
    assert plan.evidence_refs[0].claim_id == "E1"


def test_vt499_non_proposed_variants_get_no_server_window():
    """VT-499 — out_of_scope / insufficient_data carry no window; the override
    is scoped to PROPOSED only and must NOT inject one onto the others."""
    from datetime import UTC, datetime

    from orchestrator.agent.sales_recovery import _construct_variant_payload
    from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan

    ctx = _ctx_with_real_uuids()
    now = datetime.now(UTC)
    for raw in (_out_of_scope_raw_minimal(), _insufficient_data_raw_minimal()):
        payload, _de, _dp = _construct_variant_payload(
            raw, context=ctx, generated_at=now
        )
        assert "campaign_window" not in payload
        # Still a valid plan of its variant (extra='forbid' would reject a window).
        parse_campaign_plan(payload)


def test_vt499_window_override_does_not_mask_other_field_errors():
    """VT-499 — the override fills ONLY campaign_window. A genuinely-bad OTHER
    field (expected_arrr.low > high) must STILL fail parse, and the surfaced
    error is that field — NOT a campaign_window error. Proves the fix supplies a
    valid window without weakening validation of anything else."""
    from datetime import UTC, datetime

    from pydantic import ValidationError

    from orchestrator.agent.sales_recovery import _construct_variant_payload
    from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan

    raw = _proposed_raw_minimal()
    # Backdate the window too — so the ONLY thing keeping the window valid is the
    # server override — AND corrupt expected_arrr (low > high → _ordered fails).
    raw["campaign_window"] = {
        "start": "2026-05-22T09:00:00+00:00",
        "end": "2026-05-29T09:00:00+00:00",
    }
    raw["expected_arrr"]["low_paise"] = 999_999
    raw["expected_arrr"]["high_paise"] = 1

    ctx = _ctx_with_real_uuids()
    payload, _de, _dp = _construct_variant_payload(
        raw, context=ctx, generated_at=datetime.now(UTC)
    )
    with pytest.raises(ValidationError) as exc_info:
        parse_campaign_plan(payload)

    paths = {".".join(str(p) for p in e["loc"]) for e in exc_info.value.errors()}
    assert any("expected_arrr" in p for p in paths), paths
    assert not any("campaign_window" in p for p in paths), paths


# --- VT-501: evidence_refs structure heal (the dominant remaining parse miss) --
#
# The SR model writes grounded prose-markers ([E\d+]) in selection_reason + basis
# but mechanically fails the evidence_refs STRUCTURE — empty/short list
# (proposed.evidence_refs: too_short) or claim_ids that don't match the markers
# (proposed: value_error, the marker⇄ref consistency rule). _repair_evidence_refs
# (called from _construct_variant_payload on the PROPOSED variant) heals the
# structure FROM the model's own citations, without inventing grounding.


def test_vt501_empty_evidence_refs_with_prose_markers_healed_and_parses():
    """The dominant failure: prose cites [E1] in BOTH blocks but evidence_refs is
    EMPTY (too_short). The repair synthesizes a backing ref FROM the cited marker,
    so the plan parses — the validator passes legitimately, not bypassed."""
    from datetime import UTC, datetime

    from orchestrator.agent.sales_recovery import _construct_variant_payload
    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanProposed,
        EvidenceSourceKind,
        parse_campaign_plan,
    )

    raw = _proposed_raw_minimal()
    raw["evidence_refs"] = []  # the Haiku miss: grounded prose, empty refs

    ctx = _ctx_with_real_uuids()
    payload, _de, _dp = _construct_variant_payload(
        raw, context=ctx, generated_at=datetime.now(UTC)
    )

    plan = parse_campaign_plan(payload)
    assert isinstance(plan, CampaignPlanProposed)
    # Exactly the cited marker (E1) is backed; honest bundle-sourced ref.
    assert [r.claim_id for r in plan.evidence_refs] == ["E1"]
    ref = plan.evidence_refs[0]
    assert ref.source_kind is EvidenceSourceKind.L2_EPISODIC_MEMORY
    assert ref.source_id == "context_bundle"
    assert ref.note and "VT-501" in ref.note


def test_vt501_mismatched_claim_id_healed_orphan_dropped():
    """prose cites [E1] but evidence_refs declares a DIFFERENT claim_id (E2) —
    both unbacked (E1) AND uncited (E2). The repair rebuilds the list to exactly
    the cited markers: E1 synthesized, the orphan E2 dropped → parses."""
    from datetime import UTC, datetime

    from orchestrator.agent.sales_recovery import _construct_variant_payload
    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanProposed,
        parse_campaign_plan,
    )

    raw = _proposed_raw_minimal()
    raw["evidence_refs"] = [
        {"claim_id": "E2", "source_kind": "l4_skill_corpus", "source_id": "orphan-ref"}
    ]

    ctx = _ctx_with_real_uuids()
    payload, _de, _dp = _construct_variant_payload(
        raw, context=ctx, generated_at=datetime.now(UTC)
    )

    plan = parse_campaign_plan(payload)
    assert isinstance(plan, CampaignPlanProposed)
    assert [r.claim_id for r in plan.evidence_refs] == ["E1"]  # E2 orphan dropped


def test_vt501_wellformed_model_refs_preserved_idempotent():
    """A model that ALREADY emitted a well-formed, cited ref keeps it verbatim —
    the repair supplies structure only where it is missing, never overwrites the
    model's real grounding."""
    from datetime import UTC, datetime

    from orchestrator.agent.sales_recovery import _construct_variant_payload
    from orchestrator.agent.schemas.campaign_plan import (
        EvidenceSourceKind,
        parse_campaign_plan,
    )

    raw = _proposed_raw_minimal()  # prose [E1] + a well-formed E1 l4 ref

    ctx = _ctx_with_real_uuids()
    payload, _de, _dp = _construct_variant_payload(
        raw, context=ctx, generated_at=datetime.now(UTC)
    )

    plan = parse_campaign_plan(payload)
    assert [r.claim_id for r in plan.evidence_refs] == ["E1"]
    ref = plan.evidence_refs[0]
    # The MODEL's ref is preserved — NOT replaced by the synthesized bundle ref.
    assert ref.source_kind is EvidenceSourceKind.L4_SKILL_CORPUS
    assert ref.source_id == "dormant-recovery-benchmark"


def test_vt501_multiple_markers_each_backed():
    """Markers spread across the two prose blocks ([E1] in selection_reason, [E2]
    in basis) with empty refs → each cited marker gets a backing ref."""
    from datetime import UTC, datetime

    from orchestrator.agent.sales_recovery import _construct_variant_payload
    from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan

    raw = _proposed_raw_minimal()
    raw["target_cohort"]["selection_reason"] = "Inactive customers last 60d [E1]."
    raw["expected_arrr"]["basis"] = "Historical recovery rate 20-40% per [E2]."
    raw["evidence_refs"] = []

    ctx = _ctx_with_real_uuids()
    payload, _de, _dp = _construct_variant_payload(
        raw, context=ctx, generated_at=datetime.now(UTC)
    )

    plan = parse_campaign_plan(payload)
    assert sorted(r.claim_id for r in plan.evidence_refs) == ["E1", "E2"]


def test_vt501_no_grounding_at_all_still_fails():
    """The hard boundary: a proposed plan whose prose cites NO markers AND has
    empty evidence_refs has NO grounding to heal from — the repair is a no-op and
    parse_campaign_plan STILL REJECTS it (too_short). The repair supplies structure
    from existing grounding; it never fabricates grounding to pass the validator."""
    from datetime import UTC, datetime

    from pydantic import ValidationError

    from orchestrator.agent.sales_recovery import _construct_variant_payload
    from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan

    raw = _proposed_raw_minimal()
    raw["target_cohort"]["selection_reason"] = "Inactive customers last 60 days."
    raw["expected_arrr"]["basis"] = "Recovery rate estimated 20-40 percent."
    raw["evidence_refs"] = []

    ctx = _ctx_with_real_uuids()
    payload, _de, _dp = _construct_variant_payload(
        raw, context=ctx, generated_at=datetime.now(UTC)
    )
    # No markers cited → evidence_refs left empty → validator rejects (not bypassed).
    assert payload["evidence_refs"] == []
    with pytest.raises(ValidationError) as exc_info:
        parse_campaign_plan(payload)
    paths = {".".join(str(p) for p in e["loc"]) for e in exc_info.value.errors()}
    assert any("evidence_refs" in p for p in paths), paths


# --- Canary: real API, env-gated, NEVER runs in CI ---------------------------


@pytest.mark.skipif(
    os.environ.get("VIABE_RUN_AGENT_CANARY") != "1"
    or not os.environ.get("ANTHROPIC_API_KEY"),
    reason="canary skipped — needs VIABE_RUN_AGENT_CANARY=1 + ANTHROPIC_API_KEY",
)
def test_canary_real_haiku_run_completes_with_parseable_json(monkeypatch):
    """One real Messages-API call against the dev/test SR model (Sonnet 4.6 since
    VT-501; was Haiku) to prove the SDK plumbing + v1.0 system prompt work
    end-to-end. Fazal runs this manually once before merge. CI must NEVER reach
    here (VIABE_RUN_AGENT_CANARY unset).

    VT-33 updated this canary's success criteria. The v1.0 prompt
    instructs the agent to emit a CampaignPlan JSON — no longer the
    VT-32 ``{"status": "placeholder"}`` sentinel. Success now means the
    model produced parseable JSON the loop classified as 'completed'
    (or, validly, 'refused' if Haiku declined). Tokens accrued + the
    raw message trace landed."""
    monkeypatch.setenv("VIABE_ENV", "test")  # dev/test slot (Sonnet 4.6, VT-501)
    result = run_sales_recovery_agent(
        SalesRecoveryContext(
            tenant_id="canary",
            run_id="canary",
            user_request="Recover dormant customers from the last 60 days",
        )
    )
    assert result.status in {"completed", "refused"}, asdict(result)
    assert result.tokens_used > 0
    assert result.cost_paise > 0
    # The model's final message landed in raw_messages.
    assert any(m.get("role") == "assistant" for m in result.raw_messages)


# --- CL-288 real-model integration canary ---------------------------------
#
# Marked @pytest.mark.integration, skipif on ANTHROPIC_API_KEY + DATABASE_URL
# (same gating pattern as the supervisor canary in test_supervisor.py). Runs
# the REAL Opus model against the REAL revised sales_recovery_v1.md prompt;
# verifies (a) the model's emitted `status` string is one
# _construct_variant_payload recognises and (b) the three per-variant prompt
# examples actually drive shape-conformant model output.
#
# Env requirement is THREE-WAY:
#   - RUN_INTEGRATION_TESTS=1 (conftest hook strips @pytest.mark.integration
#     skip; without it, the marker collects a skip regardless of the keys)
#   - ANTHROPIC_API_KEY (this test's skipif)
#   - DATABASE_URL (this test's skipif)
# RUN_INTEGRATION_TESTS=1 ALONE cannot satisfy — the skipif still fires
# when the API key or DB url is missing. All three are independent gates.
#
# Asserts valid-any-variant. DOES NOT assert status='proposed' — empty
# uuid4() tenant has no dormant customers, so insufficient_data is the
# correct verdict and is a PASS. Plan-quality / seeded-fixture proposed-path
# verification is CL-289's job (separate subtask, requires seed data).
# CL-288 verifies schema CONFORMANCE, not plan QUALITY.


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY")
    or not os.environ.get("DATABASE_URL"),
    reason=(
        "CL-288 real-model integration test needs ANTHROPIC_API_KEY"
        " (one real Opus messages.create) AND DATABASE_URL (route_failure"
        " best-effort writes pipeline_steps if model emits a forbidden"
        " field — see _emit_model_output_conflict). RUN_INTEGRATION_TESTS=1"
        " alone is insufficient — all three env gates are required and"
        " independent."
    ),
)
def test_cl288_real_opus_emit_shape_round_trips_through_parse(monkeypatch):
    """Real Opus + real v1.0 prompt: model output flows through
    _construct_variant_payload -> parse_campaign_plan and yields a
    valid v1.0 CampaignPlan, whatever variant the model picks.

    Load-bearing assertions PROVE a real Opus round-trip happened — a
    green pass here cannot be reached by a mock-leak / silent
    substitution path. Specifically:
      (1) ``_SubstitutingClient.calls_to_real_anthropic`` records every
          call that reaches ``self._real.messages.create(...)`` AFTER
          it returns. >=1 entry is required.
      (2) The first such entry carries the canary user input — not the
          'begin' placeholder. Proves the substitution fired AND that
          the substituted call actually reached the SDK.
      (3) ``model='claude-opus-4-7'``. Proves Opus (not Haiku) was the
          target.
      (4) The response carries an ``id`` starting with ``'msg_'`` —
          the format only a real Anthropic API response produces.
      (5) ``result.tokens_used > 0`` and ``result.cost_paise > 0`` —
          proxy proof through the agent's own usage accounting, which
          pulls from the real response's ``usage`` attribute.
    """
    from uuid import uuid4

    from anthropic import Anthropic as _RealAnthropic

    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanInsufficientData,
        CampaignPlanOutOfScope,
        CampaignPlanProposed,
        parse_campaign_plan,
    )

    # Sanity: confirm `anthropic.Anthropic` is the genuine SDK class at
    # test runtime. If a session-scoped conftest had replaced it, this
    # test would silently route through whatever fake was installed.
    assert _RealAnthropic.__module__.startswith("anthropic"), (
        f"anthropic.Anthropic appears non-genuine: module={_RealAnthropic.__module__!r}"
    )

    USER_INPUT = "Recover dormant customers from the last 60 days"

    class _SubstitutingClient:
        """Real Anthropic SDK, but rewrites the agent loop's hardcoded
        'begin' cue (VT-32 placeholder) to the canary's real user request.

        PR #39's CL-287 fix threads the orchestrator's user request
        through SalesRecoveryContext; that lands separately. This branch
        (CL-288, off main) still has the 'begin' hardcode. Patching at
        the SDK boundary is the integration-test seam — production
        behaviour and run_sales_recovery_agent's signature both remain
        unchanged.

        ``calls_to_real_anthropic`` is the proof-of-call ledger. Each
        entry is appended ONLY AFTER ``self._real.messages.create(...)``
        returns. If the real network call never happens (mock leak,
        SDK-patch swallowing, etc.) the list stays empty and the
        assertion below fails the test loud.
        """

        # Class-level (not instance) so the assertions below can reach
        # it without holding a reference to the constructed client.
        calls_to_real_anthropic: list[dict[str, Any]] = []

        def __init__(self) -> None:
            self._real = _RealAnthropic()

        @property
        def messages(self):  # type: ignore[no-untyped-def]
            return self

        def create(self, **kwargs):  # type: ignore[no-untyped-def]
            msgs = list(kwargs.get("messages", []))
            if (
                msgs
                and isinstance(msgs[0], dict)
                and msgs[0].get("role") == "user"
                and msgs[0].get("content") == "begin"
            ):
                msgs = [{"role": "user", "content": USER_INPUT}] + msgs[1:]
            kwargs["messages"] = msgs
            # Real network round-trip — the ONLY place this test wants
            # to touch the SDK boundary. If anything in this expression
            # is patched/mocked silently, no record gets appended and
            # the proof-of-call assertion fails the test.
            response = self._real.messages.create(**kwargs)
            _SubstitutingClient.calls_to_real_anthropic.append(
                {
                    "model": kwargs.get("model"),
                    "first_user_message": (
                        msgs[0].get("content")
                        if msgs and isinstance(msgs[0], dict)
                        else None
                    ),
                    "response_id": getattr(response, "id", None),
                    "response_usage_input": getattr(
                        getattr(response, "usage", None),
                        "input_tokens",
                        None,
                    ),
                    "response_usage_output": getattr(
                        getattr(response, "usage", None),
                        "output_tokens",
                        None,
                    ),
                }
            )
            return response

    # Reset the class-level ledger so a previous run's state cannot
    # satisfy this test's proof-of-call assertion.
    _SubstitutingClient.calls_to_real_anthropic = []

    monkeypatch.setenv("VIABE_ENV", "production")  # _resolve_model -> Opus
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", _SubstitutingClient
    )

    wallclock_start = time.monotonic()
    context = SalesRecoveryContext(
        tenant_id=str(uuid4()), run_id=str(uuid4())
    )
    result = run_sales_recovery_agent(context, evaluator=None)
    wallclock_s = time.monotonic() - wallclock_start

    # Diagnostic surface for debug if the test fails.
    diag = {
        "status": result.status,
        "terminated_by": (
            result.terminated_by.value if result.terminated_by else None
        ),
        "terminated_reason": result.terminated_reason,
        "tokens_used": result.tokens_used,
        "cost_paise": result.cost_paise,
        "wallclock_s": wallclock_s,
        "real_call_ledger": _SubstitutingClient.calls_to_real_anthropic,
        "output_keys": (
            sorted(result.output.keys()) if isinstance(result.output, dict) else None
        ),
        "output_status_field": (
            result.output.get("status") if isinstance(result.output, dict) else None
        ),
    }

    # --- PROOF-OF-CALL: load-bearing. ---------------------------------
    # (1) Real Anthropic.messages.create returned at least once.
    assert len(_SubstitutingClient.calls_to_real_anthropic) >= 1, diag
    first_call = _SubstitutingClient.calls_to_real_anthropic[0]

    # (2) Canary user input reached the SDK (not 'begin').
    assert first_call["first_user_message"] == USER_INPUT, diag

    # (3) Opus was the target.
    assert first_call["model"] == "claude-opus-4-7", diag

    # (4) Response id has the 'msg_' prefix only real Anthropic emits.
    assert isinstance(first_call["response_id"], str), diag
    assert first_call["response_id"].startswith("msg_"), diag

    # (5) Usage attributes are present + populated (proxy proof through
    # agent accounting — pulled from response.usage which is set by
    # the real API server).
    assert result.tokens_used > 0, diag
    assert result.cost_paise > 0, diag

    # Wall-clock floor — WEAK backup signal per brief. Opus single-turn
    # is typically >= 1.5s end-to-end; <0.5s strongly suggests no real
    # call. Not the primary gate; the four assertions above are.
    assert wallclock_s > 0.5, diag

    # --- Shape conformance (the actual CL-288 claim). -----------------
    # Output must exist — Path A / refusal / hard-limit-terminated paths
    # do not exercise CL-288's coercion seam.
    assert result.output is not None, diag
    assert result.status == "completed", diag

    # The coerced payload round-trips through the strict v1.0 union.
    plan = parse_campaign_plan(result.output)

    # ANY variant is a PASS. Do NOT assert 'proposed' — empty tenant
    # legitimately yields insufficient_data. See CL-289 for the seeded-
    # fixture proposed-path test.
    assert isinstance(
        plan,
        (
            CampaignPlanProposed,
            CampaignPlanOutOfScope,
            CampaignPlanInsufficientData,
        ),
    ), diag

    # Identity-injection invariant — agent overwrites, not the model.
    assert str(plan.tenant_id) == context.tenant_id, diag
    assert str(plan.run_id) == context.run_id, diag


# --- VT-498: message body is PLACEHOLDER-only — no literal customer PII --------
#
# The SR model is shown each dormant-cohort customer's real display_name (so it
# can pick + name the target subset). The Haiku composing model (dev re-drive,
# deterministic 3/3) copied that name straight into message_plan.personalization
# ("Hi Anita, …") — customer PII baked into the persisted plan, while
# target_cohort.selection_reason was correctly placeholdered. The fix: (1) the
# prompt forbids literal PII + asks for a {{customer_name}} placeholder hydrated
# at send; (2) _construct_variant_payload scrubs any literal cohort name the model
# emits in the message body back to <customer_name> (the VT-499 server-owns-the-
# field discipline). The real name is resolved per-recipient at SEND from the
# customer record (sales_recovery_executor._allowed_param_values).


def _ctx_with_cohort(*names: str, business: str = "Bogus Winback Kirana"):
    """A SalesRecoveryContext whose dormant_cohort carries the given customer
    display names — i.e. the names the model is shown in its prompt context."""
    from uuid import uuid4

    from orchestrator.agents.sales_recovery_executor import CustomerFactBundle

    cohort = [
        CustomerFactBundle(
            customer_id=uuid4(),
            display_name=n,
            days_since_last_sale=61,
            last_sale_amount_paise=10_000,
            lifetime_spend_paise=50_000,
            business_name=business,
        )
        for n in names
    ]
    return SalesRecoveryContext(
        tenant_id=str(uuid4()),
        run_id=str(uuid4()),
        user_request="test request",
        dormant_cohort=cohort,
    )


def test_vt498_prompt_instructs_placeholder_personalization_no_literal_pii():
    """VT-498 — the rendered SR prompt forbids literal customer PII in the message
    body and shows a corrected example using the {{customer_name}} placeholder
    token (mirroring the selection_reason <customer_name> discipline), not a
    literal name."""
    from orchestrator.agent.sales_recovery import _render_sr_system_prompt

    rendered = _render_sr_system_prompt()

    # The placeholder token the model must emit is present (the SAME <customer_name>
    # token target_cohort.selection_reason already carries — mirrored discipline)...
    assert "<customer_name>" in rendered
    # ...the corrected proposed example uses it in personalization...
    assert "Hi <customer_name>, we miss you" in rendered
    # ...and the old literal-name-shaped placeholder is gone.
    assert "Hi {name}" not in rendered

    # Explicit no-literal-PII guidance is present (prose + a Pillar-7 "Do not").
    assert "never a literal name" in rendered
    assert "PII-free" in rendered


def test_vt498_construct_payload_scrubs_literal_cohort_name_from_message_plan():
    """VT-498 — a proposed raw dict whose message_plan carries a LITERAL cohort
    customer name (the exact dev-re-drive leak) is scrubbed to <customer_name> in
    BOTH personalization and the template_params value before the plan parses +
    persists. The business name (not a customer name) is left intact."""
    from datetime import UTC, datetime

    from orchestrator.agent.sales_recovery import _construct_variant_payload
    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanProposed,
        parse_campaign_plan,
    )

    raw = _proposed_raw_minimal()
    raw["message_plan"]["personalization"] = (
        "Hi Anita, we'd love to welcome you back to Bogus Winback Kirana."
    )
    raw["message_plan"]["template_params"] = {
        "customer_name": "Anita",
        "business_name": "Bogus Winback Kirana",
    }

    ctx = _ctx_with_cohort("Anita", business="Bogus Winback Kirana")
    payload, _de, _dp = _construct_variant_payload(
        raw, context=ctx, generated_at=datetime.now(UTC)
    )

    plan = parse_campaign_plan(payload)
    assert isinstance(plan, CampaignPlanProposed)
    # The literal customer name is gone from both leak fields → placeholder token.
    assert "Anita" not in plan.message_plan.personalization
    assert "<customer_name>" in plan.message_plan.personalization
    assert plan.message_plan.template_params["customer_name"] == "<customer_name>"
    # The business name is NOT customer PII — it is preserved.
    assert plan.message_plan.template_params["business_name"] == "Bogus Winback Kirana"


def test_vt498_construct_payload_leaves_placeholder_personalization_untouched():
    """VT-498 — when the model obeys the prompt and emits the <customer_name>
    placeholder, the scrub is a no-op (the placeholder is itself a redactor token,
    not a registered cohort name): the message still personalizes at send, the plan
    stays PII-free."""
    from datetime import UTC, datetime

    from orchestrator.agent.sales_recovery import _construct_variant_payload
    from orchestrator.agent.schemas.campaign_plan import parse_campaign_plan

    raw = _proposed_raw_minimal()
    raw["message_plan"]["personalization"] = "Hi <customer_name>, we miss you."
    raw["message_plan"]["template_params"] = {
        "customer_name": "<customer_name>",
        "discount": "10",
    }

    ctx = _ctx_with_cohort("Anita")
    payload, _de, _dp = _construct_variant_payload(
        raw, context=ctx, generated_at=datetime.now(UTC)
    )

    plan = parse_campaign_plan(payload)
    assert plan.message_plan.personalization == "Hi <customer_name>, we miss you."
    assert plan.message_plan.template_params["customer_name"] == "<customer_name>"


def test_vt498_send_time_hydrator_fills_customer_name_from_record():
    """VT-498 — the placeholder the PLAN carries is resolved at SEND from the
    customer record: sales_recovery_executor._allowed_param_values (the per-
    recipient win-back hydrator) maps the customer_name param to the customer's
    own display_name, so the message still personalizes while the plan stays
    PII-free. The hydrator's param KEYS are exactly the template signature."""
    from uuid import uuid4

    from orchestrator.agents import sales_recovery_executor as sre

    bundle = sre.CustomerFactBundle(
        customer_id=uuid4(),
        display_name="Anita",
        days_since_last_sale=61,
        last_sale_amount_paise=10_000,
        lifetime_spend_paise=50_000,
        business_name="Bogus Winback Kirana",
    )
    allowed = sre._allowed_param_values(bundle)
    assert allowed["customer_name"] == "Anita"          # hydrated from the record
    assert allowed["business_name"] == "Bogus Winback Kirana"
    # The plan's placeholder KEYS must be exactly the hydrator/template contract.
    assert set(sre.WINBACK_TEMPLATE_PARAMS) == {"customer_name", "business_name"}
