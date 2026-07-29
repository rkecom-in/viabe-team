"""VT-721 S2 — the daily revision pass (mode flag, collect substrate, parse tolerance,
LLM-failure keeps yesterday's plan, gate integration via injected llm)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest

pytest.importorskip("dbos")

from orchestrator.business_plan import week_plan_revision as wpr  # noqa: E402


def test_mode_off_by_default(monkeypatch):
    monkeypatch.delenv("TEAM_WEEK_PLAN", raising=False)
    assert wpr.week_plan_mode() == "off"
    assert wpr.revise_week_plan(uuid4()) is None  # no DB, no LLM touched


@pytest.mark.parametrize("v", ["shadow", "active"])
def test_mode_values(monkeypatch, v):
    monkeypatch.setenv("TEAM_WEEK_PLAN", v)
    assert wpr.week_plan_mode() == v


def test_parse_strict_and_prose_wrapped():
    doc = {"actions": [{"key": "a"}], "notes": []}
    assert wpr._parse(json.dumps(doc))[0] == [{"key": "a"}]
    acts, _ = wpr._parse("Here is the plan:\n" + json.dumps(doc) + "\nDone.")
    assert acts == [{"key": "a"}]
    with pytest.raises(ValueError):
        wpr._parse("no json here")


def _wire(monkeypatch, *, prior=None, llm_reply=None, llm_raises=False):
    monkeypatch.setenv("TEAM_WEEK_PLAN", "shadow")
    monkeypatch.setattr(
        "orchestrator.business_plan.week_plan.latest_plan", lambda t: prior
    )
    monkeypatch.setattr(wpr, "_collect", lambda t: {
        "prior_actions": [], "roadmap_items": [], "outcomes_24h": [], "commitments": [],
    })
    monkeypatch.setattr(
        "orchestrator.business_plan.generator._resolve_plan_model", lambda: "claude-test"
    )
    written = {}

    def _write(tenant_id, actions, notes, **kw):
        written.update({"actions": actions, "notes": notes, **kw})
        return uuid4()

    monkeypatch.setattr("orchestrator.business_plan.week_plan.write_revision", _write)

    def _llm(prompt, model):
        if llm_raises:
            raise RuntimeError("llm down")
        return llm_reply

    return written, _llm


def test_revision_writes_via_gate_path(monkeypatch):
    reply = json.dumps({
        "actions": [{"key": "a1", "objective": "o", "directive": "d", "assigned_to": "sr",
                     "source": "reactive"}],
        "notes": [{"action_key": "a1", "change": "add", "reason": "roadmap item 1"}],
    })
    written, llm = _wire(monkeypatch, llm_reply=reply)
    out = wpr.revise_week_plan(uuid4(), now=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc), llm=llm)
    assert out is not None
    assert written["actions"][0]["key"] == "a1"
    assert written["model_id"] == "claude-test"


def test_llm_failure_keeps_yesterdays_plan(monkeypatch):
    written, llm = _wire(monkeypatch, llm_raises=True)
    out = wpr.revise_week_plan(uuid4(), now=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc), llm=llm)
    assert out is None and written == {}


def test_already_revised_today_is_noop(monkeypatch):
    from types import SimpleNamespace

    prior = SimpleNamespace(plan_date=date(2026, 7, 30))
    written, llm = _wire(monkeypatch, prior=prior, llm_reply="{}")
    out = wpr.revise_week_plan(uuid4(), now=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc), llm=llm)
    assert out is None and written == {}
