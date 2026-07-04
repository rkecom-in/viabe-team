"""VT-599 — marketing_lane tools derive tenant from the run context, never the model.

THE LIVE DEFECT (deployed dev 3c98a78, VT-598 pack): ``list_recent_campaigns`` declares
``tenant_id: str`` as a MODEL-FILLABLE parameter. Sonnet-5 filled it with the tenant's business
NAME ("Sundaram Stores") instead of a UUID — ``UUID()`` raised inside the DB wrapper, the
exception escaped ``graph.invoke`` (marketing_lane's ``create_agent`` build holds no VT-484
tool-error middleware of its own), and the run hung at ``status='running'`` for the DBOS reaper.
Independent of the crash: trusting a model-supplied scope id is the VT-293/294 IDOR class.

Full coverage on marketing_lane (the live-failure module) — every one of its 5
``tenant_id``-taking tools, across the three required scenarios:

  1. name-instead-of-uuid from the model + a run context present -> the tool executes against
     the CONTEXT tenant, a mismatch warning is logged, NO exception.
  2. a foreign (syntactically valid) UUID from the model + a run context present -> the CONTEXT
     tenant wins + a mismatch warning is logged (the IDOR-class case).
  3. no run context + a garbage model value -> a structured tool_error dict is returned, NEVER a
     raise (the VT-484 tool-error invariant these ungated lane sub-graphs still owe the graph).

``draft_campaign_plan`` / ``draft_content`` touch no DB (pure intent construction) so they need no
monkeypatching; ``list_recent_campaigns`` / ``check_send_intent`` / ``check_ad_spend_intent``
delegate to DB-backed / rail-backed helpers that are stubbed so no live DB/pool is required.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")

from orchestrator.observability.decorators import observability_context  # noqa: E402

_LOGGER_NAME = "orchestrator.agent.lane_tenant"


# --- scenario helpers ------------------------------------------------------------------------


def _assert_context_wins_no_raise(
    caplog: pytest.LogCaptureFixture,
    *,
    call: Any,
    tool_name: str,
    context_tenant: Any,
) -> Any:
    """Runs ``call`` (a zero-arg closure invoking the tool) inside a caplog scope; returns the
    tool's result. Asserts exactly one mismatch warning naming ``tool_name`` was logged."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        result = call()
    mismatches = [r for r in caplog.records if tool_name in r.getMessage()]
    assert len(mismatches) == 1, caplog.text
    assert "mismatch" in mismatches[0].getMessage().lower()
    return result


# --- (1) list_recent_campaigns — the exact live-failure tool ---------------------------------


def test_list_recent_campaigns_business_name_from_model_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Reproduces the exact live defect: the model supplies the business NAME as tenant_id."""
    import orchestrator.agent.tools.get_recent_campaigns as grc_mod
    from orchestrator.agent.marketing_lane import list_recent_campaigns

    seen: dict[str, Any] = {}

    def _fake_get_recent_campaigns(payload: Any) -> Any:
        seen["tenant_id"] = payload.tenant_id
        from types import SimpleNamespace

        return SimpleNamespace(campaigns=[])

    monkeypatch.setattr(grc_mod, "get_recent_campaigns", _fake_get_recent_campaigns)

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: list_recent_campaigns.func(  # type: ignore[attr-defined]
                tenant_id="Sundaram Stores", days_back=90, limit=20
            ),
            tool_name="list_recent_campaigns",
            context_tenant=tenant_id,
        )
    assert out["count"] == 0
    assert seen["tenant_id"] == str(tenant_id)  # the CONTEXT tenant reached the DB call, not the name


def test_list_recent_campaigns_foreign_uuid_from_model_overridden(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A syntactically-valid but WRONG tenant UUID from the model is still overridden (IDOR-class)."""
    import orchestrator.agent.tools.get_recent_campaigns as grc_mod
    from orchestrator.agent.marketing_lane import list_recent_campaigns

    seen: dict[str, Any] = {}

    def _fake_get_recent_campaigns(payload: Any) -> Any:
        seen["tenant_id"] = payload.tenant_id
        from types import SimpleNamespace

        return SimpleNamespace(campaigns=[])

    monkeypatch.setattr(grc_mod, "get_recent_campaigns", _fake_get_recent_campaigns)

    run_id, tenant_id, foreign = uuid4(), uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        _assert_context_wins_no_raise(
            caplog,
            call=lambda: list_recent_campaigns.func(  # type: ignore[attr-defined]
                tenant_id=str(foreign), days_back=90, limit=20
            ),
            tool_name="list_recent_campaigns",
            context_tenant=tenant_id,
        )
    assert seen["tenant_id"] == str(tenant_id)
    assert seen["tenant_id"] != str(foreign)


def test_list_recent_campaigns_no_context_garbage_value_returns_tool_error() -> None:
    """No run context + a non-UUID model value -> a structured tool_error, never a raise."""
    from orchestrator.agent.marketing_lane import list_recent_campaigns

    out = list_recent_campaigns.func(  # type: ignore[attr-defined]
        tenant_id="Sundaram Stores", days_back=90, limit=20
    )
    assert out == {
        "status": "error",
        "error": "list_recent_campaigns: no resolvable tenant context",
    }


# --- (2) draft_campaign_plan / draft_content — intent-only, no DB ----------------------------


def test_draft_campaign_plan_business_name_from_model_uses_context_tenant(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from orchestrator.agent.marketing_lane import draft_campaign_plan

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: draft_campaign_plan.func(  # type: ignore[attr-defined]
                tenant_id="Sundaram Stores",
                objective="re-engage festival crowd",
                segment_label="diwali_buyers",
                offer_summary="15% off",
                message_draft="Happy Diwali!",
            ),
            tool_name="draft_campaign_plan",
            context_tenant=tenant_id,
        )
    assert out["tenant_id"] == str(tenant_id)


def test_draft_campaign_plan_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.marketing_lane import draft_campaign_plan

    out = draft_campaign_plan.func(  # type: ignore[attr-defined]
        tenant_id="Sundaram Stores",
        objective="x",
        segment_label="x",
        offer_summary="x",
        message_draft="x",
    )
    assert out == {
        "status": "error",
        "error": "draft_campaign_plan: no resolvable tenant context",
    }


def test_draft_content_foreign_uuid_from_model_overridden(caplog: pytest.LogCaptureFixture) -> None:
    from orchestrator.agent.marketing_lane import draft_content

    run_id, tenant_id, foreign = uuid4(), uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: draft_content.func(  # type: ignore[attr-defined]
                tenant_id=str(foreign),
                content_type="whatsapp_offer",
                brief="x",
                draft="x",
            ),
            tool_name="draft_content",
            context_tenant=tenant_id,
        )
    assert out["tenant_id"] == str(tenant_id)


def test_draft_content_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.marketing_lane import draft_content

    out = draft_content.func(  # type: ignore[attr-defined]
        tenant_id="not-a-uuid", content_type="x", brief="x", draft="x"
    )
    assert out == {"status": "error", "error": "draft_content: no resolvable tenant context"}


# --- (3) check_send_intent / check_ad_spend_intent — rail-facing, delegate to the rails ------


def test_check_send_intent_business_name_from_model_uses_context_tenant(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import orchestrator.agents.business_policy as policy_mod
    from orchestrator.agent.marketing_lane import check_send_intent
    from orchestrator.agents.business_policy import PolicyActionClass, PolicyCheck, PolicyDecision

    seen: dict[str, Any] = {}

    def _fake_assert(tenant_id: Any, action_class: Any, action_attrs: Any = None, *, conn: Any = None) -> PolicyCheck:
        seen["tenant_id"] = tenant_id
        return PolicyCheck(
            decision=PolicyDecision.IN_POLICY, reason="ok",
            action_class=PolicyActionClass.CUSTOMER_SEND.value,
        )

    monkeypatch.setattr(policy_mod, "assert_within_policy", _fake_assert)

    run_id, tenant_id = uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        out = _assert_context_wins_no_raise(
            caplog,
            call=lambda: check_send_intent.func(  # type: ignore[attr-defined]
                tenant_id="Sundaram Stores", segment_label="vip"
            ),
            tool_name="check_send_intent",
            context_tenant=tenant_id,
        )
    assert out["in_policy"] is True
    assert seen["tenant_id"] == tenant_id


def test_check_send_intent_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.marketing_lane import check_send_intent

    out = check_send_intent.func(  # type: ignore[attr-defined]
        tenant_id="Sundaram Stores", segment_label="vip"
    )
    assert out == {"status": "error", "error": "check_send_intent: no resolvable tenant context"}


def test_check_ad_spend_intent_foreign_uuid_from_model_overridden(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import orchestrator.agents.business_impact_choke as choke_mod
    from orchestrator.agent.marketing_lane import check_ad_spend_intent
    from orchestrator.agents.business_impact_choke import (
        BusinessActionDecision,
        BusinessActionGate,
        BusinessImpactClass,
    )

    seen: dict[str, Any] = {}

    def _fake_gate(tenant_id: Any, action_class: Any, magnitude_minor: int, *, action_attrs: Any = None, conn: Any = None) -> BusinessActionGate:
        seen["tenant_id"] = tenant_id
        return BusinessActionGate(
            decision=BusinessActionDecision.AUTONOMOUS, reason="within_tier",
            action_class=BusinessImpactClass.SPEND.value, magnitude_minor=magnitude_minor, tier="autonomous",
        )

    monkeypatch.setattr(choke_mod, "assert_or_gate_business_action", _fake_gate)

    run_id, tenant_id, foreign = uuid4(), uuid4(), uuid4()
    with observability_context(run_id=run_id, tenant_id=tenant_id):
        _assert_context_wins_no_raise(
            caplog,
            call=lambda: check_ad_spend_intent.func(  # type: ignore[attr-defined]
                tenant_id=str(foreign), magnitude_minor=5000, purpose="boost"
            ),
            tool_name="check_ad_spend_intent",
            context_tenant=tenant_id,
        )
    assert str(seen["tenant_id"]) == str(tenant_id)
    assert str(seen["tenant_id"]) != str(foreign)


def test_check_ad_spend_intent_no_context_garbage_value_returns_tool_error() -> None:
    from orchestrator.agent.marketing_lane import check_ad_spend_intent

    out = check_ad_spend_intent.func(  # type: ignore[attr-defined]
        tenant_id="Sundaram Stores", magnitude_minor=5000, purpose="x"
    )
    assert out == {
        "status": "error",
        "error": "check_ad_spend_intent: no resolvable tenant context",
    }
