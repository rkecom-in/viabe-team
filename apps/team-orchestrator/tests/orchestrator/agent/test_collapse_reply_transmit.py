"""VT-594 — unit coverage for the collapse-path owner-reply seam.

The collapse path (spawn -> specialist -> collapse_node) completes SIX
distinct ways without ever transmitting an owner-facing message (VT-594
finding): the owner sees only runner.py's generic D1 "I'm on it" fallback.
``_maybe_send_collapse_reply`` (mirroring VT-589's ``_maybe_send_manager_
reply``) closes that seam with a deterministic, substance-railed reply built
ONLY from the plan's own typed fields.

Post-review restructure (adversarial review, 2026-07-04): the proposed-variant
bodies (queue_busy / send_failed / budget-skip) are now SELF-CONTAINED single
messages with no automatic-resurfacing promise, and agent-authored free text
(out_of_scope_reason / missing_data) is redacted before it reaches the body.

Pure-function tests for ``_collapse_reply_body`` (no DB, no LLM, no network —
the customer-name registry build is monkeypatched to None so every call takes
the fast pattern-only-redaction path) + mocked-send tests for
``_maybe_send_collapse_reply`` (patches ``orchestrator.owner_surface.
freeform_acks`` — never a real Twilio call).
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


@pytest.fixture(autouse=True)
def _no_registry_build(monkeypatch):
    """Every test here exercises the redaction call path (out_of_scope /
    insufficient_data cases) — short-circuit the customer-name registry build
    to None (fail-soft pattern-only redaction) so tests run fast + DB-free
    instead of eating a real connection-attempt per call."""
    from orchestrator.agent import dispatch as dispatch_mod

    monkeypatch.setattr(dispatch_mod, "_registry_for_tenant", lambda tenant_id: None)


def _proposed_plan(
    cohort_size: int = 6, cohort_label: str = "60-90 day dormants"
) -> CampaignPlanProposed:
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
            cohort_label=cohort_label,
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


def _out_of_scope_plan(reason: str = "Request concerns review-reputation handling.") -> CampaignPlanOutOfScope:
    return CampaignPlanOutOfScope(
        tenant_id=uuid4(),
        run_id=uuid4(),
        generated_at=datetime.now(UTC),
        out_of_scope_reason=reason,
        suggested_specialist=SuggestedSpecialist.REPUTATION,
    )


def _insufficient_data_plan(
    *,
    description: str = "No dormant-customer rows surfaced.",
    remediation: str = "Seed the customer ledger.",
    extra_items: list[tuple[str, str, str]] | None = None,
) -> CampaignPlanInsufficientData:
    items = [
        MissingDataItem(
            category="cohort", description=description, suggested_remediation=remediation
        )
    ]
    for category, desc, rem in extra_items or []:
        items.append(
            MissingDataItem(category=category, description=desc, suggested_remediation=rem)
        )
    return CampaignPlanInsufficientData(
        tenant_id=uuid4(),
        run_id=uuid4(),
        generated_at=datetime.now(UTC),
        missing_data=items,
    )


# ---------------------------------------------------------------------------
# _collapse_reply_body — pure function, the six cases
# ---------------------------------------------------------------------------


def test_cohort_rejected_body_is_count_only_no_ids():
    from orchestrator.agent.dispatch import _CohortRejectedResult, _collapse_reply_body

    body = _collapse_reply_body(uuid4(), {}, _CohortRejectedResult(rejected_count=4))

    assert body is not None
    assert "4" in body["en"]
    assert "4" in body["hi"]
    # VT-241/CL-390: count only — never a customer id/name.
    for text in body.values():
        assert "customer_id" not in text.lower()


def test_out_of_scope_body_carries_the_reason():
    from orchestrator.agent.dispatch import _collapse_reply_body

    plan = _out_of_scope_plan()
    body = _collapse_reply_body(uuid4(), {}, plan)

    assert body is not None
    assert plan.out_of_scope_reason in body["en"]
    assert plan.out_of_scope_reason in body["hi"]


def test_insufficient_data_body_is_owner_register_never_agent_prose():
    """VT-600 (VT-598 opus-judge finding): the agent's missing_data descriptions
    are ENGINEER prose — they must NOT reach the owner body. The owner gets the
    deterministic owner-register line (honest, actionable, jargon-free); the
    per-item detail persists in the VT-379 observability rows instead."""
    from orchestrator.agent.dispatch import _collapse_reply_body

    plan = _insufficient_data_plan(
        description="Dormant-cohort substrate not populated (target_cohort.customer_ids empty).",
        remediation="Run customer-ledger ingest / dormant-cohort extraction.",
        extra_items=[
            ("purchase_history", "No L4 skill-corpus rows for expected_arrr basis.", "Seed the ledger."),
        ],
    )
    body = _collapse_reply_body(uuid4(), {}, plan)

    assert body is not None
    for text in body.values():
        for item in plan.missing_data:
            assert item.description not in text
            assert item.suggested_remediation not in text
        for jargon in ("substrate", "cohort", "expected_arrr", "customer_ids", "ingest", "L4"):
            assert jargon not in text
    assert "connect your store" in body["en"].lower()
    assert "स्टोर" in body["hi"]


def test_proposed_queue_busy_body_recaps_cohort_and_says_another_open():
    from orchestrator.agent.dispatch import _collapse_reply_body

    plan = _proposed_plan(cohort_size=6)
    body = _collapse_reply_body(uuid4(), {"owner_decision": "queue_busy"}, plan)

    assert body is not None
    assert "6" in body["en"]
    assert plan.target_cohort.cohort_label in body["en"]
    assert "another" in body["en"].lower()
    assert "waiting" in body["en"].lower()
    # Conditioned on the owner asking again — not an automatic promise.
    assert "ask me" in body["en"].lower()


def test_proposed_send_failed_body_is_status_only_no_recap():
    """Review Blocker 3 restructure: send_failed means the chat summary (sent
    BEFORE the template inside arm_pause_request) already reached the owner —
    this body must be status-only, no cohort-size/label recap."""
    from orchestrator.agent.dispatch import _collapse_reply_body

    plan = _proposed_plan(cohort_size=6)
    body = _collapse_reply_body(uuid4(), {"owner_decision": "send_failed"}, plan)

    assert body is not None
    assert "6" not in body["en"], "send_failed must not recap cohort size"
    assert plan.target_cohort.cohort_label not in body["en"]
    assert "ask me" in body["en"].lower()


def test_proposed_no_decision_budget_skip_body_recaps_full_plan():
    """Case 6 — the VT-334 weekly-budget skip returned {}: no owner_decision was
    ever set (request_owner_approval_node never ran, so no chat summary went
    out either) — this is the owner's ONLY chance to see the plan, so the
    recap is full (cohort size + label + expected recovery range)."""
    from orchestrator.agent.dispatch import _collapse_reply_body

    plan = _proposed_plan(cohort_size=6)
    body = _collapse_reply_body(uuid4(), {}, plan)

    assert body is not None
    assert "6" in body["en"]
    assert plan.target_cohort.cohort_label in body["en"]
    assert "week" in body["en"].lower()
    assert "10,000" in body["en"] or "₹10,000" in body["en"]  # low_paise=10_000_00 -> ₹10,000
    assert "ask me" in body["en"].lower()


def test_no_case_promises_automatic_resurfacing():
    """MAJOR finding: no automatic-delivery promise anywhere — no live
    re-surfacing path exists (run_weekly_cadence_body is a VT-176 stub). The
    only true affordance is asking again."""
    from orchestrator.agent.dispatch import _collapse_reply_body

    plan = _proposed_plan(cohort_size=6)
    for state in ({"owner_decision": "queue_busy"}, {"owner_decision": "send_failed"}, {}):
        body = _collapse_reply_body(uuid4(), state, plan)
        assert body is not None
        for text in body.values():
            assert "next sync" not in text.lower()
            assert "weekly sync" not in text.lower()


def test_proposed_body_never_claims_persisted_when_it_was_not():
    """Truthfulness rail (plan): out_of_scope / insufficient_data / cohort-
    rejected bodies must NOT say the plan was saved — nothing was persisted."""
    from orchestrator.agent.dispatch import _CohortRejectedResult, _collapse_reply_body

    for result in (
        _CohortRejectedResult(rejected_count=2),
        _out_of_scope_plan(),
        _insufficient_data_plan(),
    ):
        body = _collapse_reply_body(uuid4(), {}, result)
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
        assert _collapse_reply_body(uuid4(), {"owner_decision": decision}, plan) is None


def test_unrecognised_specialist_result_returns_none():
    from orchestrator.agent.dispatch import _collapse_reply_body

    assert _collapse_reply_body(uuid4(), {}, None) is None
    assert _collapse_reply_body(uuid4(), {}, object()) is None


# ---------------------------------------------------------------------------
# Redaction — VT-594 review Blocker 2
# ---------------------------------------------------------------------------


def test_out_of_scope_reason_poison_phone_is_redacted():
    from orchestrator.agent.dispatch import _collapse_reply_body

    plan = _out_of_scope_plan(reason="Call Anita Sharma at 9876543210 about her order.")
    body = _collapse_reply_body(uuid4(), {}, plan)

    assert body is not None
    for text in body.values():
        assert "9876543210" not in text


def test_insufficient_data_description_and_remediation_poison_phone_redacted():
    from orchestrator.agent.dispatch import _collapse_reply_body

    plan = _insufficient_data_plan(
        description="Call Anita Sharma at 9876543210 for the missing cohort data.",
        remediation="Ping 9123456780 to confirm the ledger export.",
    )
    body = _collapse_reply_body(uuid4(), {}, plan)

    assert body is not None
    for text in body.values():
        assert "9876543210" not in text
        assert "9123456780" not in text


def test_registered_customer_name_is_redacted_from_owner_body(monkeypatch):
    """Delta-review Defect 3 — the autouse fixture stubs the registry to None,
    so only PATTERN redaction was ever exercised; a registered customer NAME in
    agent prose (the primary VT-498 concern) was never asserted-scrubbed. Wire a
    real exact-match predicate and assert the name never reaches the owner."""
    from orchestrator.agent import dispatch as dispatch_mod
    from orchestrator.agent.dispatch import _collapse_reply_body

    registered = {"anita", "sharma"}
    monkeypatch.setattr(
        dispatch_mod,
        "_registry_for_tenant",
        lambda tenant_id: (lambda tok: tok.lower() in registered),
    )
    plan = _out_of_scope_plan(reason="Anita Sharma asked us to stop campaigns.")
    body = _collapse_reply_body(uuid4(), {}, plan)

    assert body is not None
    for text in body.values():
        assert "Anita" not in text
        assert "Sharma" not in text


def test_queue_busy_and_budget_skip_poison_cohort_label_is_redacted():
    """Delta-review Defect 1 — cohort_label is the SAME unconstrained
    agent-authored free-text class as selection_reason; it must pass the
    redactor before reaching the queue_busy / budget-skip owner bodies."""
    from orchestrator.agent.dispatch import _collapse_reply_body

    plan = _proposed_plan(cohort_label="buyers who complained to 9876543210")

    busy = _collapse_reply_body(uuid4(), {"owner_decision": "queue_busy"}, plan)
    skip = _collapse_reply_body(uuid4(), {}, plan)

    for body in (busy, skip):
        assert body is not None
        for text in body.values():
            assert "9876543210" not in text


def test_redaction_uses_registry_for_tenant_and_redact_for_log(monkeypatch):
    """Wire-up check: the redaction call actually flows through
    ``_registry_for_tenant`` (per-tenant, fail-soft) + the canonical
    ``redact_for_log`` primitive — not a bespoke ad-hoc scrub."""
    from orchestrator.agent import dispatch as dispatch_mod

    calls: list[str] = []
    monkeypatch.setattr(
        dispatch_mod, "_registry_for_tenant", lambda tenant_id: calls.append("registry") or None
    )
    tenant_id = uuid4()
    dispatch_mod._redact_agent_text(tenant_id, "Call 9876543210 please.")

    assert calls == ["registry"]


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
