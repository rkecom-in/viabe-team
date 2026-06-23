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
