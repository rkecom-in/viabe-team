"""VT-35 — hard-limit enforcement tests.

All four enforcers + the cancel coordinator wired into the
sales_recovery loop. Every test mocks the anthropic client; ZERO real
API calls in CI.

Boundary semantics
------------------
The four limits use strict ``> LIMIT`` semantics:
  - token total > 80_000   → cancel(tokens)
  - tool calls > 25        → cancel(tool_calls)     (26th trips)
  - depth > 8              → cancel(depth)          (9th trips)
  - wallclock > 300s       → cancel(wall_clock)

Boundary tests assert BOTH sides: exactly the limit completes; one
above cancels.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("yaml")

from orchestrator.agent.limits import (  # noqa: E402
    CancellationContext,
    DEPTH_HARD_LIMIT,
    DepthTracker,
    TOOL_CALL_HARD_LIMIT,
    TokenMeter,
    ToolCounter,
    WALL_CLOCK_HARD_LIMIT_S,
    WallclockTimer,
)
from orchestrator.agent.sales_recovery import (  # noqa: E402
    SalesRecoveryContext,
    _RUN_LEVEL_TOKEN_HARD_LIMIT,
    run_sales_recovery_agent,
)
from orchestrator.failures import HardLimitAxis  # noqa: E402


# ---------- helpers -----------------------------------------------------------


class _ToolUseBlock(SimpleNamespace):
    """Mock anthropic ToolUseBlock — type='tool_use', has .name/.id/.input."""

    def model_dump(self) -> dict[str, Any]:
        return {
            "type": "tool_use",
            "name": self.name,
            "id": self.id,
            "input": dict(self.input),
        }


class _TextBlock(SimpleNamespace):
    def model_dump(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


def _tool_use_response(
    *,
    input_tokens: int = 5,
    output_tokens: int = 5,
    tool_name: str = "noop",
    tool_id: str | None = None,
    n_tool_uses: int = 1,
) -> Any:
    """One turn with ``n_tool_uses`` tool_use blocks (lets tests exercise
    the tool-call cap independently of the depth cap — depth increments
    once per turn, tool count increments once per dispatch)."""
    blocks = []
    for _ in range(n_tool_uses):
        this_id = tool_id or f"toolu_{uuid4().hex[:12]}"
        blocks.append(
            _ToolUseBlock(type="tool_use", name=tool_name, id=this_id, input={})
        )
    return SimpleNamespace(
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        content=blocks,
        stop_reason="tool_use",
    )


def _end_turn_response(*, input_tokens: int = 5, output_tokens: int = 5, text: str = '{"status":"placeholder"}') -> Any:
    return SimpleNamespace(
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        content=[_TextBlock(type="text", text=text)],
        stop_reason="end_turn",
    )


def _patch_client(monkeypatch, responses: list[Any]) -> MagicMock:
    fake = MagicMock()
    fake.messages.create.side_effect = list(responses)
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake
    )
    return fake


def _patch_router(monkeypatch) -> MagicMock:
    """Capture route_failure calls so tests don't actually hit the DB."""
    router = MagicMock()
    monkeypatch.setattr("orchestrator.agent.sales_recovery.route_failure", router)
    return router


def _ctx() -> SalesRecoveryContext:
    return SalesRecoveryContext(tenant_id=str(uuid4()), run_id=str(uuid4()))


# ---------- coordinator: first-wins -------------------------------------------


def test_coordinator_first_wins_ignores_later_signals():
    """The first signal becomes terminated_by; any later signal is a no-op."""
    ctx = CancellationContext()
    ctx.signal(HardLimitAxis.TOKENS, "first")
    ctx.signal(HardLimitAxis.WALL_CLOCK, "second (should be ignored)")
    ctx.signal(HardLimitAxis.DEPTH, "third (ignored)")
    assert ctx.is_cancelled
    assert ctx.cancelled_by is HardLimitAxis.TOKENS
    assert ctx.reason == "first"


# ---------- token meter -------------------------------------------------------


def test_token_meter_does_not_cancel_at_exactly_the_limit(monkeypatch):
    """Strict > semantics: tokens == 80_000 is OK; >80_000 cancels."""
    _patch_router(monkeypatch)
    ctx = CancellationContext()
    meter = TokenMeter(ctx)
    meter.record_turn(input_tokens=40_000, output_tokens=40_000)  # total = 80_000
    assert not ctx.is_cancelled
    meter.record_turn(input_tokens=0, output_tokens=1)  # total = 80_001
    assert ctx.is_cancelled
    assert ctx.cancelled_by is HardLimitAxis.TOKENS


def test_token_cap_terminates_run(monkeypatch):
    """A run whose accumulated usage crosses 80K terminates with terminated_by=tokens."""
    _patch_router(monkeypatch)
    # Turn 1: 60K tokens (still under). Turn 2: 25K → 85K total > 80K cancels.
    # Turn 3 must NOT happen (no orphan messages.create).
    responses = [
        _tool_use_response(input_tokens=30_000, output_tokens=30_000),
        _tool_use_response(input_tokens=10_000, output_tokens=15_000),
        _tool_use_response(input_tokens=1, output_tokens=1),  # MUST NOT BE CALLED
    ]
    fake = _patch_client(monkeypatch, responses)
    monkeypatch.setenv("VIABE_ENV", "test")

    result = run_sales_recovery_agent(_ctx())

    assert result.status == "terminated"
    assert result.terminated_by is HardLimitAxis.TOKENS
    assert "token budget exceeded" in (result.terminated_reason or "")
    # Zero-orphan: exactly 2 messages.create calls — the 3rd staged
    # response was never consumed.
    assert fake.messages.create.call_count == 2


# ---------- tool counter ------------------------------------------------------


def test_tool_counter_boundary_25_ok_26_cancels():
    """Boundary lock: exactly 25 dispatches OK; the 26th cancels."""
    ctx = CancellationContext()
    counter = ToolCounter(ctx)
    for _ in range(TOOL_CALL_HARD_LIMIT):
        counter.record_dispatch()
    assert not ctx.is_cancelled
    counter.record_dispatch()  # 26th
    assert ctx.is_cancelled
    assert ctx.cancelled_by is HardLimitAxis.TOOL_CALLS


def test_tool_cap_terminates_run(monkeypatch):
    """A run that dispatches a 26th tool call terminates with terminated_by=tool_calls.

    Each turn here emits 4 tool_use blocks so 26 dispatches accumulate
    before the depth cap (depth +1 per turn, not per dispatch). 7 turns
    × 4 dispatches = 28 attempts; the 26th cancels mid-turn. Depth at
    that point is 6 (< 8), so DEPTH is not the winning axis."""
    _patch_router(monkeypatch)
    responses: list[Any] = [_tool_use_response(n_tool_uses=4) for _ in range(8)]
    fake = _patch_client(monkeypatch, responses)
    monkeypatch.setenv("VIABE_ENV", "test")

    result = run_sales_recovery_agent(_ctx())

    assert result.status == "terminated"
    assert result.terminated_by is HardLimitAxis.TOOL_CALLS
    assert result.tool_calls_made == TOOL_CALL_HARD_LIMIT + 1  # the 26th dispatch
    # 6 turns of 4 = 24 dispatches; turn 7 dispatches 2 (25th, then
    # 26th which cancels mid-block-loop). Subsequent turns are NOT
    # consumed.
    assert fake.messages.create.call_count == 7


# ---------- depth tracker -----------------------------------------------------


def test_depth_tracker_boundary_8_ok_9_cancels():
    """Boundary lock: 8 think→tool cycles OK; the 9th cancels."""
    ctx = CancellationContext()
    tracker = DepthTracker(ctx)
    # think→tool→think pattern: simulate the loop's record_tool_dispatch
    # followed by record_reasoning_turn on the NEXT iteration.
    for _ in range(DEPTH_HARD_LIMIT):
        tracker.record_tool_dispatch()
        tracker.record_reasoning_turn()  # increments depth
    assert not ctx.is_cancelled
    assert tracker.depth == DEPTH_HARD_LIMIT
    tracker.record_tool_dispatch()
    tracker.record_reasoning_turn()  # depth -> 9
    assert ctx.is_cancelled
    assert ctx.cancelled_by is HardLimitAxis.DEPTH


def test_depth_cap_terminates_run(monkeypatch):
    """A 9-deep think→tool chain terminates with terminated_by=depth."""
    _patch_router(monkeypatch)
    # 10 tool-use turns. Each turn is a "think" followed by a "tool
    # dispatch" — depth fires on the post-tool reasoning turn of the 9th
    # such cycle.
    responses = [_tool_use_response() for _ in range(12)]
    _patch_client(monkeypatch, responses)
    monkeypatch.setenv("VIABE_ENV", "test")

    result = run_sales_recovery_agent(_ctx())

    assert result.status == "terminated"
    assert result.terminated_by is HardLimitAxis.DEPTH
    # Depth increments on the post-tool think; the 9th tool dispatch is
    # what trips it. Turn 1 (think, no prior tool) has depth=0. Turns
    # 2..9 each see a prior tool dispatch and increment by 1 → depth=8
    # at turn 9. Turn 10's reasoning step takes depth → 9, cancel.
    # tool_calls counter still increments on each dispatch up to cancel.


# ---------- wallclock ---------------------------------------------------------


class _FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_wallclock_does_not_cancel_at_exactly_the_limit():
    """Strict > semantics: elapsed == 300s is OK; >300s cancels."""
    clock = _FakeClock()
    ctx = CancellationContext()
    timer = WallclockTimer(ctx, time_fn=clock)
    clock.now += WALL_CLOCK_HARD_LIMIT_S  # exactly 300.0s
    timer.check()
    assert not ctx.is_cancelled
    clock.now += 0.001  # 300.001s
    timer.check()
    assert ctx.is_cancelled
    assert ctx.cancelled_by is HardLimitAxis.WALL_CLOCK


def test_per_turn_http_timeout_terminates_as_wall_clock(monkeypatch):
    """A hung single turn → anthropic raises APITimeoutError → loop
    converts to wall_clock cancel + emits the FailureRecord. Distinct
    branch from the turn-boundary deadline (mocked-clock) test."""
    from anthropic import APITimeoutError

    router = _patch_router(monkeypatch)
    fake = MagicMock()
    # APITimeoutError needs a request argument in current anthropic SDK.
    fake.messages.create.side_effect = APITimeoutError(request=MagicMock())
    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.Anthropic", lambda: fake
    )
    monkeypatch.setenv("VIABE_ENV", "test")

    result = run_sales_recovery_agent(_ctx())

    assert result.status == "terminated"
    assert result.terminated_by is HardLimitAxis.WALL_CLOCK
    assert "per-turn HTTP timeout" in (result.terminated_reason or "")
    # Cancel emits exactly one FailureRecord, axis=wall_clock.
    assert router.call_count == 1
    assert router.call_args.args[0].metadata["axis"] == "wall_clock"
    # Loop must NOT retry the call after the timeout.
    assert fake.messages.create.call_count == 1


def test_wallclock_cap_terminates_run(monkeypatch):
    """A run whose elapsed wall-clock exceeds 300s terminates with
    terminated_by=wall_clock. Inject a fake clock into WallclockTimer."""
    _patch_router(monkeypatch)

    clock = _FakeClock()

    def fake_wallclock_timer_factory(ctx: CancellationContext) -> WallclockTimer:
        timer = WallclockTimer(ctx, time_fn=clock)
        # Advance the clock past 300s BEFORE the first turn check.
        clock.now += WALL_CLOCK_HARD_LIMIT_S + 1.0
        return timer

    monkeypatch.setattr(
        "orchestrator.agent.sales_recovery.WallclockTimer",
        fake_wallclock_timer_factory,
    )

    # Stage one response that should NOT be consumed (wallclock fires
    # before the first turn).
    fake = _patch_client(monkeypatch, [_end_turn_response()])
    monkeypatch.setenv("VIABE_ENV", "test")

    result = run_sales_recovery_agent(_ctx())

    assert result.status == "terminated"
    assert result.terminated_by is HardLimitAxis.WALL_CLOCK
    assert "wallclock budget exceeded" in (result.terminated_reason or "")
    # Zero-orphan: the wall-clock signal fired before the first turn.
    assert fake.messages.create.call_count == 0


# ---------- simultaneous breach: first wins -----------------------------------


def test_simultaneous_signals_first_wins(monkeypatch):
    """If two enforcers signal in the same instant, the FIRST is recorded.
    Direct enforcer test — order is determined by the loop's call order."""
    ctx = CancellationContext()
    # Pre-arm: token + tool both at one increment from cancel.
    meter = TokenMeter(ctx)
    counter = ToolCounter(ctx)
    meter.total = _RUN_LEVEL_TOKEN_HARD_LIMIT  # one input bumps over.
    counter.count = TOOL_CALL_HARD_LIMIT       # one more dispatch bumps over.
    # Fire token first, then tool. token must win.
    meter.record_turn(input_tokens=1, output_tokens=0)
    counter.record_dispatch()
    assert ctx.cancelled_by is HardLimitAxis.TOKENS


# ---------- clean cancellation: no orphan API calls ---------------------------


def test_completion_emits_no_failure_to_router(monkeypatch):
    """Normal run completion (no cancel) must NOT emit a hard-limit failure.
    Locks the cancel path so an over-eager refactor that always emits is caught."""
    router = _patch_router(monkeypatch)
    _patch_client(monkeypatch, [_end_turn_response()])
    monkeypatch.setenv("VIABE_ENV", "test")

    result = run_sales_recovery_agent(_ctx())

    assert result.status == "placeholder"
    assert result.terminated_by is None
    router.assert_not_called()


def test_cancellation_emits_one_failure_to_router(monkeypatch):
    """A cancelled run MUST emit exactly one FailureRecord to the router."""
    router = _patch_router(monkeypatch)
    # Multi-tool-use per turn → exercise the tool-call cap without
    # tripping depth first (see test_tool_cap_terminates_run).
    responses: list[Any] = [_tool_use_response(n_tool_uses=4) for _ in range(8)]
    _patch_client(monkeypatch, responses)
    monkeypatch.setenv("VIABE_ENV", "test")

    run_sales_recovery_agent(_ctx())

    assert router.call_count == 1
    failure_arg = router.call_args.args[0]
    assert failure_arg.failure_type.value == "agent_hard_limit_breach"
    assert failure_arg.metadata["axis"] == "tool_calls"


# ---------- resource cleanup: no zombie watchdog ------------------------------


def test_no_zombie_watchdog_after_run(monkeypatch):
    """Option-(b) wall-clock resolution: no asyncio task, no spawned thread.
    Active thread count must NOT grow across a normal run."""
    _patch_router(monkeypatch)
    _patch_client(monkeypatch, [_end_turn_response()])
    monkeypatch.setenv("VIABE_ENV", "test")

    before = threading.active_count()
    run_sales_recovery_agent(_ctx())
    after = threading.active_count()
    assert after == before, (
        f"thread count changed across run: before={before} after={after}"
    )
