"""VT-50 — self_evaluate MCP tool tests.

Imports ``run_tool_test`` per the VT-39 harness contract (the
``gate-vt39-tools-harness-import`` CI gate scans this file for the
import). All non-canary tests mock the Anthropic client; CI burns ZERO
API quota.
"""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("yaml")

from orchestrator.agent.self_evaluate import (  # noqa: E402
    SelfEvaluateOutcome,
)
from orchestrator.agent.tools.self_evaluate import (  # noqa: E402
    SelfEvaluateAdapter,
    SelfEvaluateInput,
    SelfEvaluateTool,
)
from team_shared.mcp import ErrorCode, ToolContext, ToolStatus, run_tool_test  # noqa: E402
from team_shared.mcp.test_harness import (  # noqa: E402
    ToolTestFixture,
    no_op_db_factory,
)


# ---------- helpers -----------------------------------------------------------


def _ctx() -> ToolContext:
    return ToolContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        agent_id="sales_recovery",
        parent_tool_call_id=None,
        cost_budget_remaining_paise=10_000,
        wallclock_remaining_ms=60_000,
        db_handle=no_op_db_factory,
    )


def _fake_response(json_text: str) -> Any:
    class _TextBlock(SimpleNamespace):
        pass

    return SimpleNamespace(
        usage=SimpleNamespace(input_tokens=200, output_tokens=80),
        content=[_TextBlock(type="text", text=json_text)],
        stop_reason="end_turn",
    )


def _patch_client_to_return(monkeypatch, json_text: str) -> MagicMock:
    fake = MagicMock()
    fake.messages.create.return_value = _fake_response(json_text)
    monkeypatch.setattr(
        SelfEvaluateTool, "_make_client", classmethod(lambda cls: fake)
    )
    return fake


def _draft() -> dict[str, Any]:
    """A minimal-shape draft dict (the tool doesn't re-validate it as
    CampaignPlan; the evaluator does that semantically). Used for
    transport tests where the prompt's verdict is what matters."""
    return {
        "version": "1.0",
        "status": "proposed",
        "target_cohort": {
            "customer_ids": [str(uuid4())],
            "cohort_label": "60-90 day dormants",
            "cohort_size": 1,
            "selection_reason": "stub [E1].",
        },
    }


# ---------- 1. Pass: clean draft -> outcome=pass, all feedback null ----------


def test_pass_first_try_yields_outcome_pass_and_no_feedback(monkeypatch):
    monkeypatch.setenv("VIABE_ENV", "test")
    payload = {
        "outcome": "pass",
        "feedback": {
            "schema": None,
            "pillar": None,
            "consistency": None,
            "legal": None,
        },
    }
    _patch_client_to_return(monkeypatch, json.dumps(payload))

    adapter = SelfEvaluateAdapter(ctx=_ctx())
    verdict = adapter.evaluate(_draft(), criteria=["schema", "pillar", "consistency", "legal"])

    assert verdict.outcome is SelfEvaluateOutcome.PASS
    assert verdict.feedback is None


# ---------- 2-5. Single-category revises -------------------------------------


@pytest.mark.parametrize(
    "category, critiques",
    [
        ("schema", ["target_cohort.cohort_size=200 but customer_ids has 87 entries — mismatch."]),
        ("pillar", ["selection_reason cites 'cafés typically have 30% return rate' — invented per-vertical heuristic."]),
        ("consistency", ["target_cohort.cohort_label='90-180 day dormants' but context_summary.attribution_snapshot shows 0 customers in that bucket."]),
        ("legal", ["message_plan.template_params.body contains 'last chance' — high-pressure language prohibited under WhatsApp content policy."]),
    ],
)
def test_single_category_revise_populates_only_that_field(
    monkeypatch, category, critiques
):
    """v1.1: each category is a LIST of distinct critique strings. A
    single-violation category is a one-entry list; the others stay None."""
    monkeypatch.setenv("VIABE_ENV", "test")
    feedback: dict[str, Any] = {
        "schema": None,
        "pillar": None,
        "consistency": None,
        "legal": None,
    }
    feedback[category] = critiques
    payload = {"outcome": "revise", "feedback": feedback}
    _patch_client_to_return(monkeypatch, json.dumps(payload))

    adapter = SelfEvaluateAdapter(ctx=_ctx())
    verdict = adapter.evaluate(_draft(), criteria=[])

    assert verdict.outcome is SelfEvaluateOutcome.REVISE
    assert verdict.feedback is not None
    # The named category carries the list; the others stay None.
    assert getattr(verdict.feedback, category) == critiques
    for other in ("schema", "pillar", "consistency", "legal"):
        if other != category:
            assert getattr(verdict.feedback, other) is None


# ---------- 6. Multi-category revise -----------------------------------------


def test_multi_category_revise_populates_all_flagged(monkeypatch):
    """v1.1 widened model: each flagged category carries a LIST."""
    monkeypatch.setenv("VIABE_ENV", "test")
    payload = {
        "outcome": "revise",
        "feedback": {
            "schema": ["target_cohort.cohort_size mismatch."],
            "pillar": ["invented number in selection_reason."],
            "consistency": None,
            "legal": ["high-pressure language in template_params."],
        },
    }
    _patch_client_to_return(monkeypatch, json.dumps(payload))

    adapter = SelfEvaluateAdapter(ctx=_ctx())
    verdict = adapter.evaluate(_draft(), criteria=[])

    assert verdict.outcome is SelfEvaluateOutcome.REVISE
    assert verdict.feedback is not None
    assert verdict.feedback.schema is not None
    assert verdict.feedback.pillar is not None
    assert verdict.feedback.consistency is None
    assert verdict.feedback.legal is not None


# ---------- 7. Attempt-2 leniency: tool passes attempt_number through --------


def test_attempt_number_is_forwarded_to_the_model(monkeypatch):
    """The tool's job is to FORWARD attempt_number; leniency itself is
    a prompt-level rule the model implements. The unit test asserts
    the value reaches the API call so the model sees it."""
    monkeypatch.setenv("VIABE_ENV", "test")
    payload = {
        "outcome": "pass",
        "feedback": {"schema": None, "pillar": None, "consistency": None, "legal": None},
    }
    fake = _patch_client_to_return(monkeypatch, json.dumps(payload))

    adapter = SelfEvaluateAdapter(ctx=_ctx(), attempt_number=2)
    adapter.evaluate(_draft(), criteria=[])

    # Inspect the user-message JSON the tool sent.
    user_msg_content = fake.messages.create.call_args.kwargs["messages"][0]["content"]
    user_payload = json.loads(user_msg_content)
    assert user_payload["attempt_number"] == 2


# ---------- 8. Independence: input schema rejects reasoning_chain ------------


def test_input_schema_rejects_reasoning_chain():
    """Pillar 7 — evaluator MUST NOT see the agent's reasoning chain.
    SelfEvaluateInput is ``extra='forbid'`` so any agent-supplied
    reasoning_chain field fails validation immediately. The framework's
    INVALID_INPUT path runs; ``execute`` is never reached."""
    with pytest.raises(Exception):
        SelfEvaluateInput.model_validate(
            {
                "draft_campaign_plan": {"foo": "bar"},
                "context_summary": {},
                "attempt_number": 1,
                "reasoning_chain": "the agent's internal deliberation",
            }
        )


# ---------- 9. Framework conformance via run_tool_test -----------------------


def test_framework_conformance_positive_and_negative_via_run_tool_test(
    monkeypatch,
):
    """Brief acceptance + VT-39 harness gate: this test exercises
    ``run_tool_test`` against a synthetic positive + negative fixture
    so the harness import is meaningful, not ceremonial."""
    monkeypatch.setenv("VIABE_ENV", "test")
    payload = {
        "outcome": "pass",
        "feedback": {
            "schema": None,
            "pillar": None,
            "consistency": None,
            "legal": None,
        },
    }
    _patch_client_to_return(monkeypatch, json.dumps(payload))

    positive = ToolTestFixture(
        name="positive: pass verdict round-trips through the framework",
        raw_inputs={
            "draft_campaign_plan": _draft(),
            "context_summary": {},
            "attempt_number": 1,
        },
        ctx=_ctx(),
        expect_status=ToolStatus.OK,
        expect_data_predicate=lambda d: d["outcome"] == "pass",
    )
    negative = ToolTestFixture(
        name="negative: extra field reasoning_chain → INVALID_INPUT",
        raw_inputs={
            "draft_campaign_plan": _draft(),
            "context_summary": {},
            "attempt_number": 1,
            "reasoning_chain": "forbidden",
        },
        ctx=_ctx(),
        expect_status=ToolStatus.ERROR,
        expect_error_code=ErrorCode.INVALID_INPUT,
    )

    reports = run_tool_test(SelfEvaluateTool, [positive, negative])
    assert all(r.passed for r in reports), [
        (r.fixture_name, r.failure_reason) for r in reports if not r.passed
    ]


# ---------- 10. Tenant-id rejection at framework registration ----------------


def test_tool_class_does_not_declare_tenant_id_in_input_schema():
    """Pillar 3 (CL-122 / CL-202) — the framework's __init_subclass__
    refuses any tool whose input_schema declares tenant_id. Lock the
    contract by asserting our schema has NO such field; the framework
    enforces the same thing at import time (already tested in
    team-shared's test_framework.py)."""
    assert "tenant_id" not in SelfEvaluateInput.model_fields


# ---------- 11. Registration in the central registry -------------------------


def test_tool_is_registered_under_its_name():
    """``orchestrator.agent.tools.self_evaluate._register()`` runs at
    import time and adds the tool to the central registry. Re-register
    defensively here — test_tool_registry's autouse fixture clears the
    registry across runs, and module-import-time registration cannot
    re-fire (Python caches modules). The tool's ``_register`` is
    idempotent against re-registration of the same class."""
    from orchestrator.agent import tool_registry

    tool_registry.register(SelfEvaluateTool)
    assert tool_registry.get("self_evaluate") is SelfEvaluateTool
    assert SelfEvaluateTool.is_llm_backed() is True
    assert tool_registry.llm_backed_in_subset(["self_evaluate"]) == [
        "self_evaluate"
    ]


# ---------- 12. Model pin resolution -----------------------------------------


def test_model_resolves_per_viabe_env(monkeypatch):
    from orchestrator.agent.tools.self_evaluate import (
        _resolve_self_evaluate_model,
    )

    monkeypatch.setenv("VIABE_ENV", "production")
    assert _resolve_self_evaluate_model() == "claude-opus-4-7"
    monkeypatch.setenv("VIABE_ENV", "test")
    assert _resolve_self_evaluate_model() == "claude-haiku-4-5"


# ---------- 13. Fence-wrapped JSON tolerated --------------------------------


def test_tolerates_markdown_fence_around_json(monkeypatch):
    """Opus occasionally wraps JSON in ```json ... ```. Borrowed
    leniency from the VT-32 canary-failure-#3 fix."""
    monkeypatch.setenv("VIABE_ENV", "test")
    payload = {
        "outcome": "pass",
        "feedback": {
            "schema": None,
            "pillar": None,
            "consistency": None,
            "legal": None,
        },
    }
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    _patch_client_to_return(monkeypatch, fenced)

    adapter = SelfEvaluateAdapter(ctx=_ctx())
    verdict = adapter.evaluate(_draft(), criteria=[])
    assert verdict.outcome is SelfEvaluateOutcome.PASS


# ---------- Canary: real-API, env-gated, NEVER runs in CI --------------------


# ---------------------------------------------------------------------------
# Canary — two-mode design (haiku plumbing / opus fidelity)
# ---------------------------------------------------------------------------
#
# Both canary tests are env-gated on ``VIABE_RUN_SELF_EVALUATE_CANARY=1``
# + ``ANTHROPIC_API_KEY`` (no key in CI → both SKIP). A second env-var
# ``VIABE_CANARY_MODEL`` (haiku | opus, default ``haiku``) selects the
# mode:
#
#   VIABE_CANARY_MODEL=haiku (default) — cheap plumbing check for iteration
#       Real Claude Haiku call. Asserts ONLY: live round-trip happened,
#       the return conforms to the SelfEvaluateVerdict Protocol shape.
#       Does NOT assert judgment correctness — Haiku may judge
#       differently than Opus on borderline drafts, asserting semantic
#       correctness here would be flaky.
#
#   VIABE_CANARY_MODEL=opus — production-fidelity check, pre-merge
#       Real Claude Opus 4.7 call (production pin). Asserts plumbing
#       PLUS judgment: a deliberately-flawed draft (cohort_size mismatch)
#       MUST yield REVISE with the schema OR consistency feedback
#       populated. This is the real gate verification.
#
# A Haiku-mode pass is NOT production verification. The Opus canary is
# the gate — run it before merge.


def _canary_skip_reason(*, mode: str) -> str:
    return (
        f"{mode} canary skipped — set VIABE_RUN_SELF_EVALUATE_CANARY=1 + "
        f"ANTHROPIC_API_KEY + VIABE_CANARY_MODEL={mode}"
    )


@pytest.mark.skipif(
    os.environ.get("VIABE_RUN_SELF_EVALUATE_CANARY") != "1"
    or not os.environ.get("ANTHROPIC_API_KEY")
    or os.environ.get("VIABE_CANARY_MODEL", "haiku") != "haiku",
    reason=_canary_skip_reason(mode="haiku"),
)
def test_canary_plumbing_haiku(monkeypatch):
    """Plumbing-only canary against Claude Haiku (the ``test`` slot in
    models.yaml; model id read from config — never hardcoded).

    Cheap. Run across iteration without burning Opus budget. Asserts:
      - elapsed > 0.5s — distinguishes real network call from mock
      - outcome ∈ {PASS, REVISE} — Protocol shape conformance
      - feedback is None OR a SelfEvaluateFeedback with each category
        either None or a string (never any other type)

    DOES NOT assert judgment correctness. Haiku may judge a borderline
    draft differently than Opus. A Haiku PASS is not production
    verification — the Opus canary (``test_canary_fidelity_opus``) is."""
    monkeypatch.setenv("VIABE_ENV", "test")  # → Haiku per models.yaml

    adapter = SelfEvaluateAdapter(ctx=_ctx())
    start = time.monotonic()
    verdict = adapter.evaluate(_draft(), criteria=[])
    elapsed = time.monotonic() - start

    assert elapsed > 0.5, (
        f"canary completed in {elapsed:.2f}s — likely mocked, not a real "
        "Haiku call. Check that _make_client was not patched and that "
        "ANTHROPIC_API_KEY reached the SDK."
    )
    assert verdict.outcome in {
        SelfEvaluateOutcome.PASS,
        SelfEvaluateOutcome.REVISE,
    }
    # Protocol-shape conformance on feedback.
    if verdict.feedback is not None:
        for cat in ("schema", "pillar", "consistency", "legal"):
            val = getattr(verdict.feedback, cat)
            assert val is None or isinstance(val, str), (
                f"feedback.{cat} must be str | None; got {type(val).__name__}"
            )


@pytest.mark.skipif(
    os.environ.get("VIABE_RUN_SELF_EVALUATE_CANARY") != "1"
    or not os.environ.get("ANTHROPIC_API_KEY")
    or os.environ.get("VIABE_CANARY_MODEL") != "opus",
    reason=_canary_skip_reason(mode="opus"),
)
def test_canary_fidelity_opus(monkeypatch):
    """Production-fidelity canary against Claude Opus 4.7 (the
    ``production`` slot in models.yaml — the actual self_evaluate
    production pin; model id read from config, never hardcoded).

    Run pre-merge to verify the production gate behaviour. Plumbing
    assertions (real round-trip, Protocol shape) AND a judgment
    assertion: a deliberately-flawed draft (cohort_size doesn't match
    len(customer_ids)) MUST be flagged as REVISE with either the
    ``schema`` or ``consistency`` feedback category populated.

    The cohort_size mismatch is a cross-field semantic error Opus
    catches reliably; this is the load-bearing judgment assertion. If
    it fails, the production gate is not catching what it must —
    BLOCK the merge."""
    monkeypatch.setenv("VIABE_ENV", "production")  # → Opus 4.7 per models.yaml

    # Deliberately-flawed: cohort_size says 200 but customer_ids has 1.
    flawed_draft = _draft()
    flawed_draft["target_cohort"]["cohort_size"] = 200

    adapter = SelfEvaluateAdapter(ctx=_ctx())
    start = time.monotonic()
    verdict = adapter.evaluate(flawed_draft, criteria=[])
    elapsed = time.monotonic() - start

    assert elapsed > 0.5, (
        f"canary completed in {elapsed:.2f}s — likely mocked, not a real "
        "Opus call. Check that _make_client was not patched and that "
        "ANTHROPIC_API_KEY reached the SDK."
    )
    assert verdict.outcome is SelfEvaluateOutcome.REVISE, (
        f"Opus must flag a cohort_size mismatch as REVISE — the "
        f"production gate is failing its job if it returns "
        f"{verdict.outcome.value!r}."
    )
    assert verdict.feedback is not None
    # Either category is a defensible flag site for this mismatch;
    # accept both so we lock judgment-presence, not phrasing.
    flagged = any(
        getattr(verdict.feedback, cat) is not None
        for cat in ("schema", "consistency")
    )
    assert flagged, (
        "Opus must populate either schema or consistency feedback for "
        "a cohort_size mismatch; got "
        f"{verdict.feedback}."
    )
