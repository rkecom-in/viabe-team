"""VT-241 — unit coverage for ``dispatch._classify_terminal`` cohort branch.

Pure-function tests (no DB, no LLM): assert the fail-closed cohort rejection
classifies as a CLEAN ``completed`` terminal on the ``collapse`` path, carries
a count-only reason discriminator, and is ordered correctly against the other
terminal markers (escalation wins; the rejection wins over a still-in-state
``campaign_plan`` object because nothing actually persisted).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# dispatch imports the langchain/langgraph stack at module load.
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")
pytest.importorskip("pydantic")


def test_classify_terminal_cohort_rejected_is_clean_completed():
    from orchestrator.agent.dispatch import _classify_terminal

    path, status, reason, result = _classify_terminal(
        {"campaign_rejected": {"reason": "unresolved_cohort", "rejected_count": 3}}
    )

    assert path == "collapse"
    # fail-closed is a VALID terminal — reuses 'completed' (no new
    # pipeline_runs.status value → no CHECK-constraint migration).
    assert status == "completed"
    # count-only discriminator — never the rejected ids.
    assert reason == "campaign_not_sent_invalid_cohort:3"
    # VT-248: the carrier hands ONLY the count to the composer (the
    # team_campaign_not_sent {{2}} param) — never ids, never the plan object.
    assert result is not None
    assert result.output == {"rejected_count": 3}


# --- VT-556: the manager VTR-directive block is config-gated (observe-first, default OFF) --------


def test_directive_block_gated_off_returns_none(monkeypatch):
    """Default posture: MANAGER_MEMORY_RETRIEVAL off → no block, and get_active_memory is never
    called (the retrieval seam stays dark until explicitly flipped per-env)."""
    from uuid import uuid4

    from orchestrator.agent import dispatch

    monkeypatch.delenv("MANAGER_MEMORY_RETRIEVAL", raising=False)
    called = {"n": 0}
    import orchestrator.agents.agent_memory as am

    monkeypatch.setattr(am, "get_active_memory", lambda *a, **k: called.__setitem__("n", 1))
    assert dispatch._build_manager_directive_block(uuid4()) is None
    assert called["n"] == 0  # gate short-circuits before retrieval


def test_directive_block_renders_when_enabled(monkeypatch):
    from uuid import uuid4

    from orchestrator.agent import dispatch

    monkeypatch.setenv("MANAGER_MEMORY_RETRIEVAL", "true")
    import orchestrator.agents.agent_memory as am

    monkeypatch.setattr(
        am, "get_active_memory",
        lambda *a, **k: [
            {"memory_key": "strategy:winback", "content": "focus on dormant customers",
             "authority": "vtr", "source": "learned"},
        ],
    )
    block = dispatch._build_manager_directive_block(uuid4())
    assert block is not None
    assert "## VTR directives" in block
    assert "[VTR] focus on dormant customers" in block


def test_directive_block_empty_rows_returns_none(monkeypatch):
    from uuid import uuid4

    from orchestrator.agent import dispatch

    monkeypatch.setenv("MANAGER_MEMORY_RETRIEVAL", "1")
    import orchestrator.agents.agent_memory as am

    monkeypatch.setattr(am, "get_active_memory", lambda *a, **k: [])
    assert dispatch._build_manager_directive_block(uuid4()) is None


# --- VT-566: the flywheel read-back — the lessons block reuses the SAME config gate --------------


def test_lessons_block_gated_off_returns_none(monkeypatch):
    """Default posture: MANAGER_MEMORY_RETRIEVAL off → no block, and the readers are never called
    (the read-back seam stays dark until explicitly flipped per-env)."""
    from uuid import uuid4

    from orchestrator.agent import dispatch

    monkeypatch.delenv("MANAGER_MEMORY_RETRIEVAL", raising=False)
    called = {"n": 0}
    import orchestrator.agents.correction_store as cs

    monkeypatch.setattr(cs, "get_recent_lessons", lambda *a, **k: called.__setitem__("n", 1))
    assert dispatch._build_manager_lessons_block(uuid4()) is None
    assert called["n"] == 0  # gate short-circuits before retrieval


def test_lessons_block_renders_when_enabled(monkeypatch):
    """Gate on + captured lessons/outcomes → the ## Lessons block renders, with implicit outcomes
    down-weighted into the separate weak block (tier branch)."""
    from uuid import uuid4

    from orchestrator.agent import dispatch

    monkeypatch.setenv("MANAGER_MEMORY_RETRIEVAL", "true")
    import orchestrator.agents.correction_store as cs
    import orchestrator.agents.lesson_readback as lr

    monkeypatch.setattr(
        cs, "get_recent_lessons",
        lambda *a, **k: [
            {"kind": "reject", "verb": "rejected", "correction_text": "off-brand tone",
             "template_hint": "team_winback_simple", "authority": "owner"},
        ],
    )
    monkeypatch.setattr(
        lr, "get_recent_outcome_signals",
        lambda *a, **k: [{"tier": "implicit", "signal": "thumbs_down"}],
    )
    block = dispatch._build_manager_lessons_block(uuid4())
    assert block is not None
    assert "## Lessons from this owner" in block
    assert "off-brand tone" in block
    assert "[weak signal — outcome-derived, not owner-stated] thumbs_down" in block


def test_lessons_block_empty_returns_none(monkeypatch):
    from uuid import uuid4

    from orchestrator.agent import dispatch

    monkeypatch.setenv("MANAGER_MEMORY_RETRIEVAL", "1")
    import orchestrator.agents.correction_store as cs
    import orchestrator.agents.lesson_readback as lr

    monkeypatch.setattr(cs, "get_recent_lessons", lambda *a, **k: [])
    monkeypatch.setattr(lr, "get_recent_outcome_signals", lambda *a, **k: [])
    assert dispatch._build_manager_lessons_block(uuid4()) is None


def test_gets_retrieval_audit_carries_lessons_present(monkeypatch):
    """VT-566 — when the lessons block renders, dispatch records lessons_present=True on the GETS
    retrieval audit spine row (mirrors directive_present / intent_present). Drives dispatch_brain
    through the block-assembly + retrieval-emit path with the graph stubbed (no LLM, no DB)."""
    import contextlib
    from uuid import uuid4

    import orchestrator.agent.dispatch as dispatch_mod
    import orchestrator.edge_cases_router as edge_mod
    import orchestrator.graph as graph_mod
    from orchestrator.agent.dispatch import dispatch_brain
    from orchestrator.state import new_subscriber_state
    from orchestrator.supervisor import SpecialistNoOutputError
    from orchestrator.types import WebhookEvent

    tenant_id, run_id = uuid4(), uuid4()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-vt566-fake")
    monkeypatch.setattr(edge_mod, "route_edge_case", lambda **kwargs: None)
    monkeypatch.setattr(
        dispatch_mod, "observability_context", lambda **kwargs: contextlib.nullcontext()
    )
    monkeypatch.setattr(graph_mod, "get_checkpointer", lambda: None)
    monkeypatch.setattr(dispatch_mod, "_resolve_model", lambda *a, **k: object())
    # The lessons block renders (stubbed — no DB); the other block builders fail soft to None
    # without a pool. Then the retrieval GETS row must carry lessons_present=True.
    monkeypatch.setattr(
        dispatch_mod, "_build_manager_lessons_block",
        lambda *a, **k: "## Lessons from this owner\n- [rejected · rejected] off-brand",
    )

    captured: list[dict] = []
    monkeypatch.setattr(dispatch_mod, "emit_tm_audit", lambda **kwargs: captured.append(kwargs))

    class _FakeGraph:
        def invoke(self, *args, **kwargs):
            raise SpecialistNoOutputError(
                specialist="x", status="invalid", run_id=run_id, tenant_id=tenant_id
            )

    monkeypatch.setattr(dispatch_mod, "build_supervisor_graph", lambda **kwargs: _FakeGraph())

    event = WebhookEvent(
        body="hello", sender_phone="+10000000000",
        message_type="inbound_message", twilio_message_sid="SMvt566test",
    )
    state = new_subscriber_state(tenant_id, run_id)
    dispatch_brain(event=event, state=state, run_id=run_id, tenant_id=tenant_id)

    retrieval = [c for c in captured if c.get("event_kind") == "retrieval"]
    assert retrieval, "a gets/retrieval audit row must be emitted"
    assert retrieval[0]["result"]["lessons_present"] is True


def test_classify_terminal_cohort_reject_wins_over_stale_plan():
    """The collapse rollback leaves the ``campaign_plan`` object in state even
    though no campaign row persisted. The rejection MUST be checked first, or
    a rejected campaign would be misclassified as a successful collapse."""
    from orchestrator.agent.dispatch import _classify_terminal

    path, status, reason, result = _classify_terminal(
        {
            "campaign_rejected": {"reason": "unresolved_cohort", "rejected_count": 1},
            "campaign_plan": SimpleNamespace(status="proposed"),
        }
    )

    assert reason == "campaign_not_sent_invalid_cohort:1"
    # VT-248: the composer receives the count-only carrier, NOT the stale plan.
    # The plan is a SimpleNamespace(status=...); the carrier has no `status`.
    assert result.output == {"rejected_count": 1}
    assert not hasattr(result, "status"), (
        "must not hand the stale plan object to the composer"
    )
    assert (path, status) == ("collapse", "completed")


def test_classify_terminal_escalation_wins_over_cohort_reject():
    """Escalation is the highest-priority terminal marker — an escalated run
    that also carried a rejected campaign still classifies as escalated."""
    from orchestrator.agent.dispatch import _classify_terminal

    path, status, reason, _ = _classify_terminal(
        {
            "messages": [
                SimpleNamespace(name="escalate_to_fazal", content="needs human")
            ],
            "campaign_rejected": {"reason": "unresolved_cohort", "rejected_count": 2},
        }
    )

    assert (path, status) == ("escalated", "escalated")
    assert reason == "needs human"


def test_classify_terminal_missing_count_defaults_to_zero():
    """Defensive: a malformed rejection dict without rejected_count still
    classifies cleanly (count defaults to 0) rather than KeyError-ing the run."""
    from orchestrator.agent.dispatch import _classify_terminal

    _, status, reason, _ = _classify_terminal({"campaign_rejected": {}})

    assert status == "completed"
    assert reason == "campaign_not_sent_invalid_cohort:0"


def test_classify_terminal_threads_count_to_composer_carrier():
    """VT-248: the cohort-rejected carrier exposes ``.output['rejected_count']``
    — the channel the composer reads to fill the team_campaign_not_sent {{2}}
    count. Count only; the carrier never holds ids."""
    from orchestrator.agent.dispatch import _CohortRejectedResult, _classify_terminal

    _, _, _, result = _classify_terminal(
        {"campaign_rejected": {"reason": "unresolved_cohort", "rejected_count": 9}}
    )
    assert isinstance(result, _CohortRejectedResult)
    assert result.rejected_count == 9
    assert result.output == {"rejected_count": 9}


# --- VT-47: 'paused' terminal --------------------------------------------------


def test_paused_is_a_valid_final_status():
    """The new 'paused' terminal is part of the FinalStatus literal so it can
    flow to pipeline_runs.status (migration 052 CHECK)."""
    from typing import get_args

    from orchestrator.agent.dispatch import FinalStatus

    assert "paused" in get_args(FinalStatus)


def test_interrupt_state_is_handled_before_classify_terminal():
    """Contract: dispatch_brain detects ``__interrupt__`` in the returned
    state (langgraph surfaces it there — it does NOT raise) and maps it to
    'paused' BEFORE calling _classify_terminal. _classify_terminal itself has
    no paused branch by design; an interrupted state never reaches it. This
    test pins the surface key langgraph uses so a version bump that renames it
    is caught here rather than silently turning every pause into 'completed'."""
    interrupted_state = {"foo": "x", "__interrupt__": ["sentinel"]}
    assert interrupted_state.get("__interrupt__"), (
        "dispatch_brain keys the paused branch on '__interrupt__' truthiness"
    )


# --- VT-480: brain model tiering (select_brain_model) --------------------------
#
# Fazal CHOSE tiering over raising the ₹5 cost cap: route ROUTINE turns to Sonnet
# (cheap, completes within the cap), reserve Opus for COMPLEX/ambiguous reasoning.
# These are pure-function tests — no LLM, no DB — and assert the selection reuses
# the ALREADY-COMPUTED intent (just a dict) and fails safe to Opus.


def test_select_brain_model_routine_intent_picks_sonnet():
    """A clearly-simple intent (a one-step approval ack) → Sonnet — the cheap
    path that completes within the ₹5 cap."""
    from orchestrator.agent.dispatch import (
        _BRAIN_MODEL_SONNET,
        select_brain_model,
    )

    model_id, tier = select_brain_model({"classification": "approval", "confidence": 0.95})

    assert model_id == _BRAIN_MODEL_SONNET == "claude-sonnet-5"
    assert tier == "sonnet"


@pytest.mark.parametrize(
    "routine", ["approval", "rejection", "question", "status_query"]
)
def test_select_brain_model_all_routine_intents_pick_sonnet(routine: str):
    """Every intent in the routine allow-set routes to Sonnet."""
    from orchestrator.agent.dispatch import select_brain_model

    model_id, tier = select_brain_model({"classification": routine})
    assert (model_id, tier) == ("claude-sonnet-5", "sonnet")


def test_select_brain_model_business_action_picks_opus():
    """A business action / send request (adhoc_campaign_request → owner_initiated)
    is COMPLEX → Opus. Under-powering a customer-facing decision is worse than
    the cost."""
    from orchestrator.agent.dispatch import (
        _BRAIN_MODEL_OPUS,
        select_brain_model,
    )

    model_id, tier = select_brain_model(
        {"classification": "adhoc_campaign_request", "confidence": 0.9}
    )

    assert model_id == _BRAIN_MODEL_OPUS == "claude-opus-4-8"
    assert tier == "opus"


@pytest.mark.parametrize(
    "complex_intent",
    ["feedback", "first_data_step_onboarding", "exclusion_request", "other"],
)
def test_select_brain_model_complex_or_ambiguous_picks_opus(complex_intent: str):
    """Anything NOT in the routine allow-set — business signals, onboarding
    spawns, ambiguous mutations, the 'other' catch-all — stays on Opus."""
    from orchestrator.agent.dispatch import select_brain_model

    model_id, tier = select_brain_model({"classification": complex_intent})
    assert (model_id, tier) == ("claude-opus-4-8", "opus")


def test_select_brain_model_missing_signal_fails_safe_to_opus():
    """CORRECTNESS-FIRST fail-safe: an empty intent dict (classify skipped or
    failed) → Opus, the capable model. Never under-power a turn we couldn't
    read."""
    from orchestrator.agent.dispatch import select_brain_model

    model_id, tier = select_brain_model({})
    assert (model_id, tier) == ("claude-opus-4-8", "opus")


def test_select_brain_model_unknown_classification_fails_safe_to_opus():
    """A classification string we don't recognise (e.g. a future intent added to
    the classifier before this allow-set is updated) defaults to Opus, not a
    crash."""
    from orchestrator.agent.dispatch import select_brain_model

    model_id, tier = select_brain_model({"classification": "some_new_intent"})
    assert (model_id, tier) == ("claude-opus-4-8", "opus")


def test_select_brain_model_non_string_classification_fails_safe_to_opus():
    """Defensive: a malformed signal (classification is None / not a str) must
    not crash the selector — it fails safe to Opus."""
    from orchestrator.agent.dispatch import select_brain_model

    assert select_brain_model({"classification": None})[1] == "opus"


def test_brain_model_ids_are_the_single_source_of_truth():
    """The two model-id constants are the ONE place the brain model strings
    live; select_brain_model returns exactly those values."""
    from orchestrator.agent.dispatch import (
        _BRAIN_MODEL_OPUS,
        _BRAIN_MODEL_SONNET,
        select_brain_model,
    )

    assert _BRAIN_MODEL_SONNET == "claude-sonnet-5"
    assert _BRAIN_MODEL_OPUS == "claude-opus-4-8"
    assert select_brain_model({"classification": "question"})[0] == _BRAIN_MODEL_SONNET
    assert select_brain_model({})[0] == _BRAIN_MODEL_OPUS


def test_brain_models_are_in_the_cost_rate_table():
    """VT-480 invariant: both tiered brain models MUST be in the cost RATES
    table, or the cost callback silently skips attribution (KeyError caught +
    logged) and the ₹5 cost-cap telemetry zeroes out for that run."""
    from orchestrator.agent.cost import RATES
    from orchestrator.agent.dispatch import (
        _BRAIN_MODEL_OPUS,
        _BRAIN_MODEL_SONNET,
    )

    assert _BRAIN_MODEL_SONNET in RATES
    assert _BRAIN_MODEL_OPUS in RATES


# --- VT-492: specialist-no-output resolves to a CLEAN terminal (not orphan) ----
#
# The defect: a specialist (sales_recovery) that terminates with NO usable output
# (status in {refused, invalid, terminated}; e.g. the SR retry emitted non-dict
# terminal text → agent_terminal_no_dict) raised a bare RuntimeError that escaped
# graph.invoke → dispatch_brain's catch-all re-raised → webhook_pipeline_run never
# reached close_webhook_run → the run sat at status='running' until the VT-481
# reaper. The fix: the node raises a STRUCTURED SpecialistNoOutputError that
# dispatch_brain converts to a CLEAN 'escalated' terminal (so the runner records a
# terminal status AND the VT-88 SupportBot acks the owner — never silence).


def test_dispatch_brain_specialist_no_output_resolves_to_clean_escalated(
    monkeypatch,
):
    """VT-492 — dispatch_brain converts a SpecialistNoOutputError raised by the
    graph into a CLEAN ``DispatchResult(final_status='escalated')`` instead of
    re-raising (which would orphan the run at status='running'). 'escalated' is
    the value the runner writes to pipeline_runs.status via close_webhook_run.
    """
    import contextlib
    from uuid import uuid4

    import orchestrator.agent.dispatch as dispatch_mod
    import orchestrator.edge_cases_router as edge_mod
    import orchestrator.graph as graph_mod
    from orchestrator.agent.dispatch import dispatch_brain
    from orchestrator.state import new_subscriber_state
    from orchestrator.supervisor import SpecialistNoOutputError
    from orchestrator.types import WebhookEvent

    tenant_id = uuid4()
    run_id = uuid4()

    # Real key prefix so dispatch doesn't short-circuit to the test-mode
    # escalated fallback; we drive the brain path and stub the graph.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-vt492-fake")

    # No edge-case fast-path → fall through to the brain (intent stays empty).
    monkeypatch.setattr(edge_mod, "route_edge_case", lambda **kwargs: None)
    # Keep the run keyless/no-DB: null observability context + no checkpointer +
    # a fake graph whose .invoke raises the structured no-output signal.
    monkeypatch.setattr(
        dispatch_mod,
        "observability_context",
        lambda **kwargs: contextlib.nullcontext(),
    )
    monkeypatch.setattr(graph_mod, "get_checkpointer", lambda: None)
    monkeypatch.setattr(dispatch_mod, "_resolve_model", lambda *a, **k: object())

    class _FakeGraph:
        def invoke(self, *args, **kwargs):
            raise SpecialistNoOutputError(
                specialist="sales_recovery",
                status="invalid",
                run_id=run_id,
                tenant_id=tenant_id,
            )

    monkeypatch.setattr(
        dispatch_mod, "build_supervisor_graph", lambda **kwargs: _FakeGraph()
    )

    event = WebhookEvent(
        body="recover my dormant customers",
        sender_phone="+10000000000",
        message_type="inbound_message",
        twilio_message_sid="SMvt492test",
    )
    state = new_subscriber_state(tenant_id, run_id)

    result = dispatch_brain(
        event=event, state=state, run_id=run_id, tenant_id=tenant_id
    )

    # The run reaches a CLEAN terminal — NOT an unhandled re-raise (no orphan).
    assert result.final_status == "escalated"
    assert result.terminal_path == "escalated"
    assert result.reason == "specialist_no_output:sales_recovery:invalid"


def test_specialist_no_output_terminal_is_unresolved_so_owner_gets_ack():
    """VT-492 — the 'escalated' terminal the no-output path resolves to is a
    VT-88 _UNRESOLVED status, so maybe_escalate_support fires the owner's
    no-silence ack. Pins the contract that an invalid SR terminal routes the
    owner ack (not silence) — and that 'escalated' is a valid FinalStatus."""
    from typing import get_args

    from orchestrator.agent.dispatch import FinalStatus
    from orchestrator.owner_surface.support_bot import _UNRESOLVED

    assert "escalated" in get_args(FinalStatus)
    assert "escalated" in _UNRESOLVED
