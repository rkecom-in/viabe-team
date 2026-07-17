"""VT-607 (Loop Package 6) — ``handoffs._build_sales_recovery_update`` threads the manager loop's
own step framing (``manager_step_desired_outcome`` / ``manager_step_acceptance_criteria``, both
populated in graph state by ``manager.workflow._dispatch_specialist_step``) into the
``SalesRecoveryContext`` bundle it builds at spawn time.

Pure-Python: monkeypatches every DB-backed ``_build_*`` section builder to safe-empty (mirrors
test_context_builder.py's own autouse fixture) so this test needs no DB.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")
pytest.importorskip("pydantic")

from uuid import uuid4  # noqa: E402

from langchain_core.messages import HumanMessage  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_db_backed_builders(monkeypatch: pytest.MonkeyPatch) -> None:
    import orchestrator.context_builder as cb

    monkeypatch.setattr(cb, "_build_recent_campaigns", lambda tid: ([], False))
    monkeypatch.setattr(cb, "_build_pending_owner_inputs", lambda tid: ([], False))
    monkeypatch.setattr(cb, "_build_ledger_summary", lambda tid: (cb.LedgerSummary(), True))
    monkeypatch.setattr(cb, "_build_dormant_cohort", lambda tid: ([], False))
    monkeypatch.setattr(cb, "_build_l3_priors", lambda tid, rid: (cb.L3Priors(), False))
    monkeypatch.setattr(cb, "_build_l4_skills", lambda tid, req: (cb.L4Skills(), False))
    monkeypatch.setattr(cb, "_build_business_profile", lambda tid: (cb.BusinessProfile(), False))
    monkeypatch.setattr(
        cb, "_build_attribution_snapshot", lambda tid: (cb.AttributionSnapshot(), False)
    )
    monkeypatch.setattr(cb, "_build_recovery_target_config", lambda tid: (1.1, 50_000_00))


def test_build_sales_recovery_update_threads_manager_framing() -> None:
    from orchestrator.handoffs import _build_sales_recovery_update

    state = {
        "messages": [HumanMessage(content="recover my dormant customers")],
        "tenant_id": uuid4(),
        "run_id": uuid4(),
        "manager_step_desired_outcome": "win back the dormant cohort within budget",
        "manager_step_acceptance_criteria": ["cohort grounded", "expected recovery cited"],
    }
    update = _build_sales_recovery_update(state)
    bundle = update["sales_recovery_context"]
    assert bundle.manager_desired_outcome == "win back the dormant cohort within budget"
    assert bundle.manager_acceptance_criteria == ["cohort grounded", "expected recovery cited"]


def test_build_sales_recovery_update_defaults_safe_empty_outside_the_loop() -> None:
    """A non-loop dispatch (legacy/shadow mode never sets manager_step_desired_outcome /
    manager_step_acceptance_criteria) gets the CL-190 safe-empty default — never a KeyError,
    never a stray None reaching the bundle."""
    from orchestrator.handoffs import _build_sales_recovery_update

    state = {
        "messages": [HumanMessage(content="recover my dormant customers")],
        "tenant_id": uuid4(),
        "run_id": uuid4(),
    }
    update = _build_sales_recovery_update(state)
    bundle = update["sales_recovery_context"]
    assert bundle.manager_desired_outcome == ""
    assert bundle.manager_acceptance_criteria == []


def test_build_sales_recovery_update_threads_creative_brief_from_situation() -> None:
    """VT-667 — the owner's redacted verbatim ask (``manager_step_situation``, set by
    triage_seam._build_campaign_recovery_plan on the D3/VT-657 campaign path) is threaded
    VERBATIM into ``SalesRecoveryContext.creative_brief`` so the draft path can reflect the
    owner's actual campaign brief (a Diwali OFFER, not the canonical win-back)."""
    from orchestrator.handoffs import _build_sales_recovery_update

    brief = "whip up a Diwali festive offer — 20% off or free dessert, dine-in + online"
    state = {
        "messages": [HumanMessage(content="recover my dormant customers")],
        "tenant_id": uuid4(),
        "run_id": uuid4(),
        "manager_step_situation": brief,
    }
    update = _build_sales_recovery_update(state)
    bundle = update["sales_recovery_context"]
    assert bundle.creative_brief == brief


def test_build_sales_recovery_update_creative_brief_defaults_empty() -> None:
    """VT-667 — an autonomous / non-loop dispatch never sets ``manager_step_situation``; the
    bundle gets the CL-190 safe-empty default so the prompt render section is omitted (brief-less
    flows stay byte-unchanged)."""
    from orchestrator.handoffs import _build_sales_recovery_update

    state = {
        "messages": [HumanMessage(content="recover my dormant customers")],
        "tenant_id": uuid4(),
        "run_id": uuid4(),
    }
    update = _build_sales_recovery_update(state)
    bundle = update["sales_recovery_context"]
    assert bundle.creative_brief == ""
