"""VT-594 — unit coverage for the collapse-path owner-reply seam.

The collapse path (spawn -> specialist -> collapse_node) completes SIX
distinct ways without ever transmitting an owner-facing message (VT-594
finding): the owner sees only runner.py's generic D1 "I'm on it" fallback.
``_maybe_send_collapse_reply`` (mirroring VT-589's ``_maybe_send_manager_
reply``) closes that seam with a deterministic, substance-railed reply built
ONLY from the plan's own typed fields.

Pure-function tests for ``_collapse_reply_body`` (no DB, no LLM, no network)
+ mocked-send tests for ``_maybe_send_collapse_reply`` (patches
``orchestrator.owner_surface.freeform_acks`` — never a real Twilio call).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

# dispatch imports the langchain/langgraph stack at module load.
pytest.importorskip("langchain_anthropic")
pytest.importorskip("langgraph")
pytest.importorskip("pydantic")

from orchestrator.agent.schemas.campaign_plan import (  # noqa: E402
    CampaignPlanInsufficientData,
    CampaignPlanOutOfScope,
    CampaignPlanProposed,
    CampaignWindow,
    ConfidenceLevel,
    EvidenceRef,
    EvidenceSourceKind,
    ExpectedARRR,
    Language,
    MessagePlan,
    MissingDataItem,
    SuggestedSpecialist,
    TargetCohort,
)


def _proposed_plan(cohort_size: int = 6) -> CampaignPlanProposed:
    now = datetime.now(UTC)
    return CampaignPlanProposed(
        tenant_id=uuid4(),
        run_id=uuid4(),
        generated_at=now,
        campaign_window=CampaignWindow(
            start=now + timedelta(hours=1), end=now + timedelta(days=7)
        ),
        target_cohort=TargetCohort(
            customer_ids=[uuid4() for _ in range(cohort_size)],
            cohort_label="60-90 day dormants",
            cohort_size=cohort_size,
            selection_reason="dormant cohort [E1].",
        ),
        expected_arrr=ExpectedARRR(
            low_paise=10_000_00,
            high_paise=30_000_00,
            confidence=ConfidenceLevel.MEDIUM,
            basis="prior winback yields [E1].",
        ),
        evidence_refs=[
            EvidenceRef(
                claim_id="E1",
                source_kind=EvidenceSourceKind.TOOL_CALL,
                source_id="test-evidence",
            )
        ],
        message_plan=MessagePlan(
            template_id="team_winback_v1",
            template_params={"first_name": "Owner"},
            language=Language.EN,
            personalization="owner-first-name.",
        ),
    )


def _out_of_scope_plan() -> CampaignPlanOutOfScope:
    return CampaignPlanOutOfScope(
        tenant_id=uuid4(),
        run_id=uuid4(),
        generated_at=datetime.now(UTC),
        out_of_scope_reason="Request concerns review-reputation handling.",
        suggested_specialist=SuggestedSpecialist.REPUTATION,
    )


def _insufficient_data_plan() -> CampaignPlanInsufficientData:
    return CampaignPlanInsufficientData(
        tenant_id=uuid4(),
        run_id=uuid4(),
        generated_at=datetime.now(UTC),
        missing_data=[
            MissingDataItem(
                category="cohort",
                description="No dormant-customer rows surfaced.",
                suggested_remediation="Seed the customer ledger.",
            )
        ],
    )


# ---------------------------------------------------------------------------
# _collapse_reply_body — pure function, the six cases
# ---------------------------------------------------------------------------


def test_cohort_rejected_body_is_count_only_no_ids():
    from orchestrator.agent.dispatch import _CohortRejectedResult, _collapse_reply_body

    body = _collapse_reply_body({}, _CohortRejectedResult(rejected_count=4))

    assert body is not None
    assert "4" in body["en"]
    assert "4" in body["hi"]
    # VT-241/CL-390: count only — never a customer id/name.
    for text in body.values():
        assert "customer_id" not in text.lower()


def test_out_of_scope_body_carries_the_reason():
    from orchestrator.agent.dispatch import _collapse_reply_body

    plan = _out_of_scope_plan()
    body = _collapse_reply_body({}, plan)

    assert body is not None
    assert plan.out_of_scope_reason in body["en"]
    assert plan.out_of_scope_reason in body["hi"]


def test_insufficient_data_body_carries_missing_data_and_remediation():
    from orchestrator.agent.dispatch import _collapse_reply_body

    plan = _insufficient_data_plan()
    body = _collapse_reply_body({}, plan)

    assert body is not None
    assert plan.missing_data[0].description in body["en"]
    assert plan.missing_data[0].suggested_remediation in body["en"]


def test_proposed_queue_busy_body_says_plan_saved_and_another_open():
    from orchestrator.agent.dispatch import _collapse_reply_body

    plan = _proposed_plan(cohort_size=6)
    body = _collapse_reply_body({"owner_decision": "queue_busy"}, plan)

    assert body is not None
    assert "6" in body["en"]
    assert "another" in body["en"].lower() or "waiting" in body["en"].lower()


def test_proposed_send_failed_body_says_plan_saved_and_will_arrive_next_sync():
    from orchestrator.agent.dispatch import _collapse_reply_body

    plan = _proposed_plan(cohort_size=6)
    body = _collapse_reply_body({"owner_decision": "send_failed"}, plan)

    assert body is not None
    assert "6" in body["en"]
    assert "sync" in body["en"].lower()


def test_proposed_no_decision_budget_skip_body_says_holding_the_ask():
    """Case 6 — the VT-334 weekly-budget skip returned {}: no owner_decision was
    ever set (request_owner_approval_node never ran)."""
    from orchestrator.agent.dispatch import _collapse_reply_body

    plan = _proposed_plan(cohort_size=6)
    body = _collapse_reply_body({}, plan)

    assert body is not None
    assert "6" in body["en"]
    assert "week" in body["en"].lower()


def test_proposed_body_never_claims_persisted_when_it_was_not():
    """Truthfulness rail (plan): out_of_scope / insufficient_data / cohort-
    rejected bodies must NOT say the plan was saved — nothing was persisted."""
    from orchestrator.agent.dispatch import _CohortRejectedResult, _collapse_reply_body

    for result in (
        _CohortRejectedResult(rejected_count=2),
        _out_of_scope_plan(),
        _insufficient_data_plan(),
    ):
        body = _collapse_reply_body({}, result)
        assert body is not None
        for text in body.values():
            assert "saved" not in text.lower()
            assert "drafted" not in text.lower()


def test_proposed_other_decision_returns_none_no_case():
    """approved / rejected / needs_changes / timeout / defer never reach
    dispatch_brain on the initial run (those resolve on the SEPARATE resume
    path — approval_resume.resume_run) — defensive: no invented case, no send."""
    from orchestrator.agent.dispatch import _collapse_reply_body

    plan = _proposed_plan()
    for decision in ("approved", "rejected", "needs_changes", "timeout", "defer"):
        assert _collapse_reply_body({"owner_decision": decision}, plan) is None


def test_unrecognised_specialist_result_returns_none():
    from orchestrator.agent.dispatch import _collapse_reply_body

    assert _collapse_reply_body({}, None) is None
    assert _collapse_reply_body({}, object()) is None


# ---------------------------------------------------------------------------
# _maybe_send_collapse_reply — exactly one send, fail-soft, locale-aware
# ---------------------------------------------------------------------------


def _event(message_type: str = "inbound_message", sender_phone: str = "+10000000000"):
    from orchestrator.types import WebhookEvent

    return WebhookEvent(
        body="make me a plan to win back my lapsed customers",
        sender_phone=sender_phone,
        message_type=message_type,
        twilio_message_sid="SMvt594test",
    )


def test_sends_exactly_once_for_cohort_rejected(monkeypatch):
    import orchestrator.owner_surface.freeform_acks as freeform_acks
    from orchestrator.agent.dispatch import (
        _CohortRejectedResult,
        _maybe_send_collapse_reply,
    )

    sent: list[tuple] = []
    monkeypatch.setattr(freeform_acks, "resolve_owner_locale", lambda tenant_id: "en")
    monkeypatch.setattr(
        freeform_acks,
        "send_freeform_ack",
        lambda tenant_id, recipient, body: sent.append((tenant_id, recipient, body)) or True,
    )

    tenant_id = uuid4()
    _maybe_send_collapse_reply(
        tenant_id, _event(), {}, _CohortRejectedResult(rejected_count=3)
    )

    assert len(sent) == 1
    assert sent[0][0] == tenant_id
    assert sent[0][1] == "+10000000000"
    assert "3" in sent[0][2]


def test_hindi_locale_sends_hindi_variant(monkeypatch):
    import orchestrator.owner_surface.freeform_acks as freeform_acks
    from orchestrator.agent.dispatch import _maybe_send_collapse_reply

    sent: list[str] = []
    monkeypatch.setattr(freeform_acks, "resolve_owner_locale", lambda tenant_id: "hi")
    monkeypatch.setattr(
        freeform_acks,
        "send_freeform_ack",
        lambda tenant_id, recipient, body: sent.append(body) or True,
    )

    _maybe_send_collapse_reply(
        uuid4(), _event(), {}, _out_of_scope_plan()
    )

    assert len(sent) == 1
    assert any("ऀ" <= ch <= "ॿ" for ch in sent[0]), "expected Devanagari text"


def test_non_inbound_event_sends_nothing(monkeypatch):
    import orchestrator.owner_surface.freeform_acks as freeform_acks
    from orchestrator.agent.dispatch import (
        _CohortRejectedResult,
        _maybe_send_collapse_reply,
    )

    called = {"n": 0}
    monkeypatch.setattr(
        freeform_acks,
        "send_freeform_ack",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True,
    )

    _maybe_send_collapse_reply(
        uuid4(),
        _event(message_type="status_callback"),
        {},
        _CohortRejectedResult(rejected_count=1),
    )

    assert called["n"] == 0


def test_unrecognised_result_sends_nothing(monkeypatch):
    import orchestrator.owner_surface.freeform_acks as freeform_acks
    from orchestrator.agent.dispatch import _maybe_send_collapse_reply

    called = {"n": 0}
    monkeypatch.setattr(
        freeform_acks,
        "send_freeform_ack",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True,
    )

    _maybe_send_collapse_reply(uuid4(), _event(), {"owner_decision": "approved"}, _proposed_plan())

    assert called["n"] == 0


def test_exception_inside_send_never_propagates(monkeypatch):
    """Fail-soft: an error resolving locale or sending must never raise into
    dispatch_brain — the D1 fallback remains the net."""
    import orchestrator.owner_surface.freeform_acks as freeform_acks
    from orchestrator.agent.dispatch import (
        _CohortRejectedResult,
        _maybe_send_collapse_reply,
    )

    def _boom(*a, **k):
        raise RuntimeError("send exploded")

    monkeypatch.setattr(freeform_acks, "resolve_owner_locale", lambda tenant_id: "en")
    monkeypatch.setattr(freeform_acks, "send_freeform_ack", _boom)

    # Must not raise.
    _maybe_send_collapse_reply(
        uuid4(), _event(), {}, _CohortRejectedResult(rejected_count=1)
    )


def test_no_recipient_sends_nothing(monkeypatch):
    import orchestrator.owner_surface.freeform_acks as freeform_acks
    from orchestrator.agent.dispatch import (
        _CohortRejectedResult,
        _maybe_send_collapse_reply,
    )

    called = {"n": 0}
    monkeypatch.setattr(
        freeform_acks,
        "send_freeform_ack",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True,
    )

    _maybe_send_collapse_reply(
        uuid4(),
        _event(sender_phone=""),
        {},
        _CohortRejectedResult(rejected_count=1),
    )

    assert called["n"] == 0
