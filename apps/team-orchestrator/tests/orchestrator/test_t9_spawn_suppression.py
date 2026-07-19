"""T9 — answerable turns answer in-turn; async specialist spawns are suppressed.

The dominant Tier-1 breaker (loop_stall + ignored_speech_act, 43/50 instances in the re-measure):
on an answerable turn (triage direct_reply / task_status) the sync brain spawned an async specialist
(spawn_sales_recovery / spawn_integration) instead of answering, so VT-583 D1 "I'm on it" fired and
the real answer landed late/never. T9 drops those spawn tools on answerable turns so the brain must
answer in-turn from its read-tools. onboarding_conductor stays available (increment-2 owns the
onboarding-status case).

Pure-logic: the roster mechanism + build_supervisor_graph's tool binding, with a capturing fake
model — no DB / no Anthropic call.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_anthropic")

from orchestrator.agent.roster import (  # noqa: E402 — after importorskip
    ANSWERABLE_SUPPRESSED_ROUTE_KEYS,
    roster_spawn_tools,
)


# ── roster_spawn_tools exclusion mechanism ──────────────────────────────────────────────────


def test_roster_spawn_tools_full_set_by_default():
    names = {t.name for t in roster_spawn_tools()}
    assert {"spawn_sales_recovery", "spawn_integration", "spawn_onboarding_conductor"} <= names


def test_roster_spawn_tools_excludes_by_route_key():
    kept = {t.name for t in roster_spawn_tools(exclude_route_keys={"spawn", "spawn_integration"})}
    assert "spawn_sales_recovery" not in kept
    assert "spawn_integration" not in kept
    # onboarding_conductor is NOT excluded — it conducts the onboarding answer (increment-2).
    assert "spawn_onboarding_conductor" in kept


def test_answerable_suppressed_route_keys_is_non_onboarding_only():
    # The constant must drop SR + integration and KEEP the onboarding conductor, else an answerable
    # onboarding turn would lose its only way to be conducted.
    assert ANSWERABLE_SUPPRESSED_ROUTE_KEYS == frozenset({"spawn", "spawn_integration"})
    kept = {t.name for t in roster_spawn_tools(exclude_route_keys=ANSWERABLE_SUPPRESSED_ROUTE_KEYS)}
    assert kept == {"spawn_onboarding_conductor"} | (kept - {"spawn_onboarding_conductor"})
    assert "spawn_onboarding_conductor" in kept
    assert "spawn_sales_recovery" not in kept


# ── build_supervisor_graph honors suppress_answerable_spawns ─────────────────────────────────


class _CapturingModel:
    """Fake ChatAnthropic that records every tool name bound anywhere in the graph build. The
    spawn tools appear ONLY in the orchestrator's tool set, so collecting across all bind_tools
    calls is enough to assert whether a spawn was offered."""

    def __init__(self) -> None:
        self.all_tool_names: set[str] = set()

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_CapturingModel":
        self.all_tool_names |= {getattr(t, "name", None) for t in tools}
        return self


def _capture_roster_exclusions(monkeypatch) -> dict[str, Any]:
    """Patch supervisor.roster_spawn_tools to record the exclude_route_keys it is called with,
    while still returning the REAL tool list (the orchestrator binds tools lazily, so this is the
    reliable seam to assert the build->roster wiring)."""
    import orchestrator.supervisor as sup

    captured: dict[str, Any] = {}
    real = sup.roster_spawn_tools

    def _capturing(exclude_route_keys=()):
        captured["exclude"] = frozenset(exclude_route_keys)
        return real(exclude_route_keys=exclude_route_keys)

    monkeypatch.setattr(sup, "roster_spawn_tools", _capturing)
    return captured


def test_build_supervisor_graph_no_exclusions_by_default(monkeypatch):
    import orchestrator.supervisor as sup

    captured = _capture_roster_exclusions(monkeypatch)
    sup.build_supervisor_graph(model=_CapturingModel())  # type: ignore[arg-type]
    assert captured["exclude"] == frozenset()


def test_build_supervisor_graph_suppresses_non_onboarding_spawns_on_answerable_turn(monkeypatch):
    import orchestrator.supervisor as sup

    captured = _capture_roster_exclusions(monkeypatch)
    sup.build_supervisor_graph(model=_CapturingModel(), suppress_answerable_spawns=True)  # type: ignore[arg-type]
    # suppress -> the non-onboarding spawns (SR + integration) are excluded; conductor kept.
    assert captured["exclude"] == ANSWERABLE_SUPPRESSED_ROUTE_KEYS
    kept = {t.name for t in roster_spawn_tools(exclude_route_keys=captured["exclude"])}
    assert "spawn_sales_recovery" not in kept and "spawn_integration" not in kept
    assert "spawn_onboarding_conductor" in kept


def test_suppression_does_not_alter_graph_nodes():
    # Excluding a spawn TOOL must not remove the specialist NODE — the node stays (unreachable
    # this turn), so no graph-shape regression / no dangling-edge compile error.
    from orchestrator.supervisor import build_supervisor_graph

    g_full = build_supervisor_graph(model=_CapturingModel())  # type: ignore[arg-type]
    g_supp = build_supervisor_graph(  # type: ignore[arg-type]
        model=_CapturingModel(), suppress_answerable_spawns=True
    )
    assert set(g_full.get_graph().nodes) == set(g_supp.get_graph().nodes)
