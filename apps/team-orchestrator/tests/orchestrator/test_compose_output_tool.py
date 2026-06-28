"""Tests for the composer tool wrapper (VT-30).

The composer itself is heavily covered in tests/orchestrator/test_output_composer.py
(29 unit tests). These tests verify the langchain ``@tool``-decorated
wrapper that the orchestrator-agent dispatches: argument-shape unmarshal,
return-shape marshal, signature stability.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("yaml")

from orchestrator.agent.tools.compose_output import (  # noqa: E402
    compose_owner_output_tool,
)


def test_tool_returns_dict_with_expected_keys() -> None:
    out = compose_owner_output_tool.invoke({
        "intent_or_trigger": "welcome",
        "tenant_id": str(uuid4()),
        "phase": "onboarding",
        "last_owner_message_at_iso": None,
        "escalation_pending": False,
        "specialist_result_json": None,
    })
    assert set(out.keys()) >= {
        "message_body",
        "message_type",
        "template_name",
        "template_params",
        "urgency",
        "follow_up_required",
        "follow_up_intent",
        "preferred_language",
        "signature",
        "honesty_notes",
    }


def test_tool_dispatches_template_for_outside_window() -> None:
    far_past = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    out = compose_owner_output_tool.invoke({
        "intent_or_trigger": "welcome",
        "tenant_id": str(uuid4()),
        "phase": "onboarding",
        "last_owner_message_at_iso": far_past,
    })
    assert out["message_type"] == "template"
    assert out["template_name"] == "team_welcome2"  # VT-404: reply-inviting copy


def test_tool_signature_deterministic_across_invocations() -> None:
    tenant = str(uuid4())
    args = {
        "intent_or_trigger": "welcome",
        "tenant_id": tenant,
        "phase": "onboarding",
        "last_owner_message_at_iso": None,
    }
    out1 = compose_owner_output_tool.invoke(args)
    out2 = compose_owner_output_tool.invoke(args)
    assert out1["signature"] == out2["signature"]


def test_tool_handles_specialist_result_json() -> None:
    out = compose_owner_output_tool.invoke({
        "intent_or_trigger": "free_form_chat",
        "tenant_id": str(uuid4()),
        "phase": "paid_active",
        "last_owner_message_at_iso": (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat(),
        "specialist_result_json": {
            "status": "terminated",
            "terminated_by": "cost_paise",
            "output": {"message": "Partial result."},
        },
    })
    assert out["message_type"] == "free_form_24h"
    assert "₹50 cost budget" in out["message_body"]


# --- VT-464 D5: dispatch-tenant injection over an LLM-supplied junk tenant ----


def test_tool_uses_dispatch_tenant_over_llm_business_name(monkeypatch) -> None:
    """THE LAUNCH-BLOCKER regression guard.

    Live root cause: the LLM passes the business NAME as the ``tenant_id``
    tool arg; the VT-73 in-flight guard (decorators._assert_tool_tenant) sees
    ``tenant <name> != dispatch <uuid>`` and fail-closes → the brain turn
    crashes → the run is stuck ``running`` forever. The fix injects the
    authoritative dispatch tenant from the ambient ObservabilityContext BEFORE
    the guard runs. This test invokes the tool with a junk business-name
    tenant_id under a real dispatch context and asserts:
      1. no ContextIsolationViolation is raised (guard passes legitimately),
      2. compose succeeds (returns a valid envelope),
      3. the DISPATCH tenant — NOT the LLM's junk value — reached the composer.
    """
    from orchestrator.observability.decorators import observability_context

    dispatch_tenant = uuid4()
    captured: dict[str, object] = {}

    # Capture the tenant_id the underlying composer actually receives via state.
    import orchestrator.agent.tools.compose_output as compose_mod

    orig = compose_mod.compose_owner_output

    def _spy(specialist_result, state, intent_or_trigger, **kw):
        captured["tenant_id"] = state["tenant_id"]
        return orig(specialist_result, state, intent_or_trigger, **kw)

    monkeypatch.setattr(compose_mod, "compose_owner_output", _spy)

    # The LLM supplies a business-name slug as tenant_id (the live failure mode).
    junk_tenant = "rajus-chai-corner"

    with observability_context(run_id=uuid4(), tenant_id=dispatch_tenant):
        out = compose_owner_output_tool.invoke({
            "intent_or_trigger": "welcome",
            "tenant_id": junk_tenant,
            "phase": "onboarding",
            "last_owner_message_at_iso": None,
        })

    # (1)+(2): no raise, valid envelope returned.
    assert "message_body" in out
    assert out["message_type"] in {"template", "free_form_24h"}
    # (3): the authoritative DISPATCH tenant reached the composer, NOT the junk.
    assert captured["tenant_id"] == dispatch_tenant
    assert str(captured["tenant_id"]) != junk_tenant


def test_tool_guard_still_blocks_when_not_opted_in() -> None:
    """The VT-73 guard is NOT globally weakened: a tool that does NOT opt into
    ``tenant_from_context`` still fail-closes on a cross-tenant tenant_id. This
    guards against a future regression that turns the override on everywhere.
    """
    import pytest as _pytest

    from orchestrator.context_validator import ContextIsolationViolation
    from orchestrator.observability.decorators import (
        ObservabilityContext,
        _assert_tool_tenant,
    )

    dispatch = uuid4()
    ctx = ObservabilityContext(run_id=uuid4(), tenant_id=dispatch)
    with _pytest.raises(ContextIsolationViolation):
        _assert_tool_tenant(ctx, {"tenant_id": "some-other-business"}, "some_tool")
