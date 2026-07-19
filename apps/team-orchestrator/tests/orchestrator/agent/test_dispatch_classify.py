"""VT-241 — unit coverage for ``dispatch._classify_terminal`` cohort branch.

Pure-function tests (no DB, no LLM): assert the fail-closed cohort rejection
classifies as a CLEAN ``completed`` terminal on the ``collapse`` path, carries
a count-only reason discriminator, and is ordered correctly against the other
terminal markers (escalation wins; the rejection wins over a still-in-state
``campaign_plan`` object because nothing actually persisted).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Annotated

import pytest

# dispatch imports the langchain/langgraph stack at module load.
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")
pytest.importorskip("pydantic")

# VT-602 Part 2 tests build small ad-hoc StateGraphs with a local TypedDict state
# schema. Because this module uses `from __future__ import annotations`, TypedDict
# field annotations are lazily resolved via THIS module's globals (langgraph's
# `get_type_hints` call) — not the enclosing test function's locals — so
# `Annotated` / `add_messages` / `TypedDict` must be importable at MODULE level
# even though the TypedDict classes themselves are defined inside the test
# functions below.
from langgraph.graph.message import add_messages  # noqa: E402 — after importorskip
from typing_extensions import TypedDict  # noqa: E402 — langgraph dep; after importorskip


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


# --- VT-480 / VT-619: brain model tiering (select_brain_model) -----------------
#
# VT-619 cost policy (Fazal 2026-07-07): route ROUTINE turns to Haiku (the cheap
# default workhorse), reserve Sonnet for COMPLEX/ambiguous reasoning; OPUS DROPPED.
# These are pure-function tests — no LLM, no DB — and assert the selection reuses
# the ALREADY-COMPUTED intent (just a dict) and fails safe to Sonnet.


def test_select_brain_model_routine_intent_picks_haiku():
    """A clearly-simple intent (a one-step approval ack) → Haiku — the cheap
    default workhorse."""
    from orchestrator.agent.dispatch import (
        _BRAIN_MODEL_HAIKU,
        select_brain_model,
    )

    model_id, tier = select_brain_model({"classification": "approval", "confidence": 0.95})

    assert model_id == _BRAIN_MODEL_HAIKU == "claude-haiku-4-5"
    assert tier == "haiku"


@pytest.mark.parametrize(
    "routine", ["approval", "rejection", "question", "status_query"]
)
def test_select_brain_model_all_routine_intents_pick_haiku(routine: str):
    """Every intent in the routine allow-set routes to Haiku."""
    from orchestrator.agent.dispatch import select_brain_model

    model_id, tier = select_brain_model({"classification": routine})
    assert (model_id, tier) == ("claude-haiku-4-5", "haiku")


def test_select_brain_model_business_action_picks_sonnet():
    """A business action / send request (adhoc_campaign_request → owner_initiated)
    is COMPLEX → Sonnet (the capable tier; opus dropped). Under-powering a
    customer-facing decision is worse than the cost."""
    from orchestrator.agent.dispatch import (
        _BRAIN_MODEL_SONNET,
        select_brain_model,
    )

    model_id, tier = select_brain_model(
        {"classification": "adhoc_campaign_request", "confidence": 0.9}
    )

    assert model_id == _BRAIN_MODEL_SONNET == "claude-sonnet-5"
    assert tier == "sonnet"


@pytest.mark.parametrize(
    "complex_intent",
    [
        "feedback",
        "first_data_step_onboarding",
        "exclusion_request",
        "other",
        "business_analysis",
    ],
)
def test_select_brain_model_complex_or_ambiguous_picks_sonnet(complex_intent: str):
    """Anything NOT in the routine allow-set — business signals, onboarding
    spawns, ambiguous mutations, the 'other' catch-all — stays on Sonnet (the
    capable tier; opus dropped).

    VT-595: business_analysis (an owner asking WHICH/WHY over their data) is
    deliberately excluded from _ROUTINE_INTENTS — it needs the capable model,
    not the cheap Haiku path status_query gets."""
    from orchestrator.agent.dispatch import select_brain_model

    model_id, tier = select_brain_model({"classification": complex_intent})
    assert (model_id, tier) == ("claude-sonnet-5", "sonnet")


def test_business_analysis_not_in_routine_intents():
    """VT-595 pin: business_analysis must NEVER be added to _ROUTINE_INTENTS —
    it is a non-routine, brain-owned analysis intent (Sonnet tier), unlike the
    pure-fact status_query it was previously confused with."""
    from orchestrator.agent.dispatch import _ROUTINE_INTENTS

    assert "business_analysis" not in _ROUTINE_INTENTS


def test_select_brain_model_missing_signal_fails_safe_to_sonnet():
    """CORRECTNESS-FIRST fail-safe: an empty intent dict (classify skipped or
    failed) → Sonnet, the capable model. Never under-power a turn we couldn't
    read."""
    from orchestrator.agent.dispatch import select_brain_model

    model_id, tier = select_brain_model({})
    assert (model_id, tier) == ("claude-sonnet-5", "sonnet")


def test_select_brain_model_unknown_classification_fails_safe_to_sonnet():
    """A classification string we don't recognise (e.g. a future intent added to
    the classifier before this allow-set is updated) defaults to Sonnet, not a
    crash."""
    from orchestrator.agent.dispatch import select_brain_model

    model_id, tier = select_brain_model({"classification": "some_new_intent"})
    assert (model_id, tier) == ("claude-sonnet-5", "sonnet")


def test_select_brain_model_non_string_classification_fails_safe_to_sonnet():
    """Defensive: a malformed signal (classification is None / not a str) must
    not crash the selector — it fails safe to Sonnet."""
    from orchestrator.agent.dispatch import select_brain_model

    assert select_brain_model({"classification": None})[1] == "sonnet"


def test_brain_model_ids_are_the_single_source_of_truth():
    """The two model-id constants are the ONE place the brain model strings
    live; select_brain_model returns exactly those values."""
    from orchestrator.agent.dispatch import (
        _BRAIN_MODEL_HAIKU,
        _BRAIN_MODEL_SONNET,
        select_brain_model,
    )

    assert _BRAIN_MODEL_SONNET == "claude-sonnet-5"
    assert _BRAIN_MODEL_HAIKU == "claude-haiku-4-5"
    assert select_brain_model({"classification": "question"})[0] == _BRAIN_MODEL_HAIKU
    assert select_brain_model({})[0] == _BRAIN_MODEL_SONNET


def test_brain_models_are_in_the_cost_rate_table():
    """VT-480 invariant: both tiered brain models MUST be in the cost RATES
    table, or the cost callback silently skips attribution (KeyError caught +
    logged) and the ₹5 cost-cap telemetry zeroes out for that run."""
    from orchestrator.agent.cost import RATES
    from orchestrator.agent.dispatch import (
        _BRAIN_MODEL_HAIKU,
        _BRAIN_MODEL_SONNET,
    )

    assert _BRAIN_MODEL_SONNET in RATES
    assert _BRAIN_MODEL_HAIKU in RATES


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


# ---------------------------------------------------------------------------
# VT-602 Part 1 — a LANE sub-graph node exception (marketing/sales/finance/
# accounting/tech/cost_opt/integration/onboarding_conductor) must resolve to the
# SAME clean 'escalated' terminal the VT-492 SpecialistNoOutputError path uses —
# never a bare re-raise that lets DBOS retry forever (owner silence). The
# structural net (supervisor._wrap_lane_node_exceptions, wrapped around every
# ROSTER node) converts the raw exception into LaneNodeError; this pins
# dispatch_brain's OWN conversion of THAT signal into a clean DispatchResult.
# ---------------------------------------------------------------------------


def test_dispatch_brain_lane_node_error_resolves_to_clean_escalated(monkeypatch):
    """VT-602 — a LaneNodeError escaping graph.invoke() (the structural net's typed
    signal for ANY lane-node exception) converts to a CLEAN 'escalated' terminal —
    mirrors the VT-492 SpecialistNoOutputError test above byte-for-byte, but for the
    lane-exception class the VT-598 live pack found had NO net at all."""
    import contextlib
    from uuid import uuid4

    import orchestrator.agent.dispatch as dispatch_mod
    import orchestrator.edge_cases_router as edge_mod
    import orchestrator.graph as graph_mod
    from orchestrator.agent.dispatch import dispatch_brain
    from orchestrator.state import new_subscriber_state
    from orchestrator.supervisor import LaneNodeError
    from orchestrator.types import WebhookEvent

    tenant_id = uuid4()
    run_id = uuid4()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-vt602-fake")
    monkeypatch.setattr(edge_mod, "route_edge_case", lambda **kwargs: None)
    monkeypatch.setattr(
        dispatch_mod, "observability_context", lambda **kwargs: contextlib.nullcontext()
    )
    monkeypatch.setattr(graph_mod, "get_checkpointer", lambda: None)
    monkeypatch.setattr(dispatch_mod, "_resolve_model", lambda *a, **k: object())

    class _FakeGraph:
        def invoke(self, *args, **kwargs):
            raise LaneNodeError(lane="marketing_lane", exc_type="ValueError")

    monkeypatch.setattr(
        dispatch_mod, "build_supervisor_graph", lambda **kwargs: _FakeGraph()
    )

    event = WebhookEvent(
        body="go plan that Diwali campaign",
        sender_phone="+10000000000",
        message_type="inbound_message",
        twilio_message_sid="SMvt602test",
    )
    state = new_subscriber_state(tenant_id, run_id)

    result = dispatch_brain(
        event=event, state=state, run_id=run_id, tenant_id=tenant_id
    )

    # The run reaches a CLEAN terminal — NOT an unhandled re-raise (no DBOS-retry
    # hang, no orphan at status='running').
    assert result.final_status == "escalated"
    assert result.terminal_path == "escalated"
    # PII-safe reason: lane + exception TYPE only — never str(exc).
    assert result.reason == "lane_exception:marketing_lane:ValueError"


def test_dispatch_brain_generic_exception_still_reraises_for_dbos(monkeypatch):
    """VT-602 — the structural net is SCOPED to lane-node exceptions (LaneNodeError /
    SpecialistNoOutputError / HardLimitExceeded). Anything else escaping graph.invoke()
    (a bug OUTSIDE the ROSTER lane surface — e.g. orchestrator/routing/collapse) must
    still re-raise so DBOS's retry semantics are unchanged for that class — VT-602 does
    NOT swallow every exception, only the lane-node class the live pack identified."""
    import contextlib
    from uuid import uuid4

    import orchestrator.agent.dispatch as dispatch_mod
    import orchestrator.edge_cases_router as edge_mod
    import orchestrator.graph as graph_mod
    from orchestrator.agent.dispatch import dispatch_brain
    from orchestrator.state import new_subscriber_state
    from orchestrator.types import WebhookEvent

    tenant_id, run_id = uuid4(), uuid4()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-vt602b-fake")
    monkeypatch.setattr(edge_mod, "route_edge_case", lambda **kwargs: None)
    monkeypatch.setattr(
        dispatch_mod, "observability_context", lambda **kwargs: contextlib.nullcontext()
    )
    monkeypatch.setattr(graph_mod, "get_checkpointer", lambda: None)
    monkeypatch.setattr(dispatch_mod, "_resolve_model", lambda *a, **k: object())

    class _FakeGraph:
        def invoke(self, *args, **kwargs):
            raise RuntimeError("some unrelated framework bug, not a lane exception")

    monkeypatch.setattr(
        dispatch_mod, "build_supervisor_graph", lambda **kwargs: _FakeGraph()
    )

    event = WebhookEvent(
        body="hello", sender_phone="+10000000000",
        message_type="inbound_message", twilio_message_sid="SMvt602btest",
    )
    state = new_subscriber_state(tenant_id, run_id)

    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="some unrelated framework bug"):
        dispatch_brain(event=event, state=state, run_id=run_id, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# VT-602 Part 2 — the reported crash: `ValueError: Received multiple
# non-consecutive system messages` in the marketing-lane agent invocation.
#
# ROOT CAUSE (confirmed by direct reproduction against a real LangGraph
# checkpointer, NOT the marketing_lane build itself): dispatch_brain's
# `graph.invoke(initial_state, config={"configurable": {"thread_id": str(run_id)}})`
# reuses `thread_id == run_id` across every call for a run (VT-47 needs the same
# thread for a pause/resume). LangGraph's `add_messages` reducer keys purely on
# `BaseMessage.id`; a message built with none gets a FRESH random uuid at merge
# time, so a DBOS retry of the whole `dispatch_brain` function (the module's own
# `except Exception: raise` — ANY unhandled exception after at least one graph
# superstep already checkpointed) rebuilds brand-new SystemMessage/HumanMessage
# objects that do not match the checkpointed ones' ids. The reducer APPENDS them
# after whatever the checkpoint already holds (e.g. the orchestrator's own prior
# AIMessage/ToolMessage from spawning a lane) instead of replacing the initial
# turn in place — producing a SECOND system-message island later in the list,
# exactly what langchain_anthropic's `_format_messages` rejects. The fix
# (`_initial_turn_msg_id`) scopes each initial-turn message's id to
# `(run_id, slot)` so `add_messages` replaces it IN PLACE on every retry.
# ---------------------------------------------------------------------------


def test_initial_turn_msg_id_is_stable_per_run_and_slot():
    """VT-602 — the id helper is deterministic per (run_id, slot): same inputs ->
    same id (so a retry's freshly-built message replaces in place), different
    slot/run_id -> different id (so distinct blocks never collide)."""
    from uuid import uuid4

    from orchestrator.agent.dispatch import _initial_turn_msg_id

    run_a, run_b = uuid4(), uuid4()

    assert _initial_turn_msg_id(run_a, "l1_block") == _initial_turn_msg_id(
        run_a, "l1_block"
    )
    assert _initial_turn_msg_id(run_a, "l1_block") != _initial_turn_msg_id(
        run_a, "business_block"
    )
    assert _initial_turn_msg_id(run_a, "l1_block") != _initial_turn_msg_id(
        run_b, "l1_block"
    )


def _lane_messages_for_anthropic(state_messages: list) -> None:
    """Mirrors create_agent's model_node (`messages = [system_message, *state["messages"]]`,
    langchain/agents/factory.py) using the marketing lane's REAL cached system prompt, then
    runs the messages through a REAL ``ChatAnthropic`` request-payload build — the exact
    langchain_anthropic validation that raised VT-602's reported ValueError. No network call
    (``_get_request_payload`` only assembles the request; it never calls the API), so this
    needs no API key."""
    from langchain_anthropic import ChatAnthropic

    from orchestrator.agent.marketing_lane import MARKETING_LANE_SYSTEM_MESSAGE

    messages = [MARKETING_LANE_SYSTEM_MESSAGE, *state_messages]
    ChatAnthropic(model="claude-opus-4-7", max_tokens=16)._get_request_payload(  # type: ignore[call-arg]
        messages
    )


def test_vt602_retry_without_stable_ids_reproduces_the_reported_crash():
    """Control case — proves the harness actually exercises the real defect: WITHOUT
    stable per-(run_id, slot) ids (the pre-fix shape — brand-new SystemMessage/HumanMessage
    objects on every dispatch_brain call), a DBOS-style retry against the SAME checkpointed
    thread reproduces the EXACT reported ValueError."""
    from uuid import uuid4

    import pytest as _pytest
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    class _State(TypedDict, total=False):
        messages: Annotated[list, add_messages]

    def _orchestrator_stub(state):
        # Mirrors the orchestrator deciding to spawn marketing: an AIMessage with a
        # tool_call + make_spawn_tool's handoff ToolMessage (handoffs.py `handoff()`).
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "spawn_marketing", "args": {}, "id": "tc1"}],
                ),
                ToolMessage(
                    content="Handing off to marketing_lane",
                    name="spawn_marketing",
                    tool_call_id="tc1",
                ),
            ]
        }

    def _marketing_stub(state):
        _lane_messages_for_anthropic(state["messages"])
        return {"messages": [AIMessage(content="marketing ran")]}

    graph = StateGraph(_State)
    graph.add_node("orchestrator", _orchestrator_stub)
    graph.add_node("marketing_lane", _marketing_stub)
    graph.add_edge(START, "orchestrator")
    graph.add_edge("orchestrator", "marketing_lane")
    graph.add_edge("marketing_lane", END)

    run_id = uuid4()
    cfg = {"configurable": {"thread_id": str(run_id)}}

    def _build_initial_no_stable_ids():
        # The PRE-FIX shape: fresh SystemMessage/HumanMessage every call, no id=.
        return {
            "messages": [
                SystemMessage(content="business ctx"),
                HumanMessage(content="go plan that Diwali campaign"),
            ]
        }

    # attempt 1: marketing_lane raises (simulating ANY failure post-orchestrator —
    # the specific cause doesn't matter; only that at least one superstep checkpointed
    # before the failure). The orchestrator's AI/Tool messages are now checkpointed.
    def _marketing_stub_raises(state):
        raise RuntimeError("attempt 1 fails for some unrelated reason")

    compiled_first = StateGraph(_State)
    compiled_first.add_node("orchestrator", _orchestrator_stub)
    compiled_first.add_node("marketing_lane", _marketing_stub_raises)
    compiled_first.add_edge(START, "orchestrator")
    compiled_first.add_edge("orchestrator", "marketing_lane")
    compiled_first.add_edge("marketing_lane", END)
    cp = InMemorySaver()
    g1 = compiled_first.compile(checkpointer=cp)
    with _pytest.raises(RuntimeError, match="attempt 1 fails"):
        g1.invoke(_build_initial_no_stable_ids(), config=cfg)

    # attempt 2 (the DBOS retry): SAME thread_id, a FRESH initial state built the
    # SAME (pre-fix) way. Reuse the SAME checkpointer so the retry sees attempt 1's
    # checkpointed orchestrator messages.
    g2 = graph.compile(checkpointer=cp)
    with _pytest.raises(ValueError, match="non-consecutive system messages"):
        g2.invoke(_build_initial_no_stable_ids(), config=cfg)


def test_vt602_retry_with_stable_ids_does_not_crash():
    """VT-602 fix — the SAME retry-against-a-progressed-checkpoint scenario as above,
    but using the REAL `_initial_turn_msg_id` scheme dispatch.py now applies. The
    marketing lane's REAL cached system prompt + the retried state must NOT trip
    langchain_anthropic's non-consecutive-system-messages check — the lane runs."""
    from uuid import uuid4

    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from orchestrator.agent.dispatch import _initial_turn_msg_id

    class _State(TypedDict, total=False):
        messages: Annotated[list, add_messages]

    def _orchestrator_stub(state):
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "spawn_marketing", "args": {}, "id": "tc1"}],
                ),
                ToolMessage(
                    content="Handing off to marketing_lane",
                    name="spawn_marketing",
                    tool_call_id="tc1",
                ),
            ]
        }

    ran_marketing: list[bool] = []

    def _marketing_stub(state):
        _lane_messages_for_anthropic(state["messages"])
        ran_marketing.append(True)
        return {"messages": [AIMessage(content="marketing ran")]}

    def _marketing_stub_raises(state):
        raise RuntimeError("attempt 1 fails for some unrelated reason")

    run_id = uuid4()
    cfg = {"configurable": {"thread_id": str(run_id)}}

    def _build_initial_with_stable_ids():
        # dispatch.py's REAL fix: each initial-turn message id-scoped to (run_id, slot).
        return {
            "messages": [
                SystemMessage(
                    content="business ctx",
                    id=_initial_turn_msg_id(run_id, "business_block"),
                ),
                HumanMessage(
                    content="go plan that Diwali campaign",
                    id=_initial_turn_msg_id(run_id, "human_input"),
                ),
            ]
        }

    cp = InMemorySaver()

    g1 = StateGraph(_State)
    g1.add_node("orchestrator", _orchestrator_stub)
    g1.add_node("marketing_lane", _marketing_stub_raises)
    g1.add_edge(START, "orchestrator")
    g1.add_edge("orchestrator", "marketing_lane")
    g1.add_edge("marketing_lane", END)
    compiled_1 = g1.compile(checkpointer=cp)

    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="attempt 1 fails"):
        compiled_1.invoke(_build_initial_with_stable_ids(), config=cfg)

    g2 = StateGraph(_State)
    g2.add_node("orchestrator", _orchestrator_stub)
    g2.add_node("marketing_lane", _marketing_stub)
    g2.add_edge(START, "orchestrator")
    g2.add_edge("orchestrator", "marketing_lane")
    g2.add_edge("marketing_lane", END)
    compiled_2 = g2.compile(checkpointer=cp)

    # Must NOT raise — the retry's stable ids replace the initial turn in place.
    compiled_2.invoke(_build_initial_with_stable_ids(), config=cfg)

    assert ran_marketing == [True], "marketing_lane must actually run (no crash swallowed it)"
