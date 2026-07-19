"""VT-667 — the approval echo must show WHAT the owner is approving.

``collapse._build_chat_summary_body`` (armed BEFORE the approval template, so the owner sees the
plan) previously carried cohort size + window + expected-recovery ₹ only. VT-667: a generic
lapsed-recovery win-back was approved when a Diwali OFFER was asked for, because the owner could
not tell the two apart from the summary. These tests pin the echo now:

  - it names the template identity + shows the actual {{3}} ``offer_description`` copy when the
    plan carries an offer (mirroring the send-path fill — typed param, else the personalization
    the deterministic repair promotes into {{3}});
  - it names the template honestly and shows NO fabricated copy for an offer-less template;
  - it NEVER surfaces ``target_cohort.selection_reason`` (the VT-498 PII-bearing field).

Pure-Python: ``money`` is passed explicitly so no ``_derive_summary_money`` DB read runs, and the
agent-label redaction is fail-soft (pattern-only) without a name registry — so no DB is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("pydantic")

from orchestrator import collapse  # noqa: E402
from orchestrator.agent.schemas.campaign_plan import (  # noqa: E402
    CampaignPlanProposed,
    CampaignWindow,
    ConfidenceLevel,
    EvidenceRef,
    EvidenceSourceKind,
    ExpectedARRR,
    Language,
    MessagePlan,
    TargetCohort,
)

_SELECTION_REASON = "picked the 8 highest-spend lapsed regulars [E1]."


def _plan(
    template_id: str,
    template_params: dict[str, str],
    *,
    personalization: str = "We miss you — come back soon.",
) -> CampaignPlanProposed:
    now = datetime.now(UTC)
    return CampaignPlanProposed(
        tenant_id=uuid4(),
        run_id=uuid4(),
        generated_at=now,
        campaign_window=CampaignWindow(start=now + timedelta(hours=1), end=now + timedelta(days=7)),
        target_cohort=TargetCohort(
            customer_ids=[uuid4()],
            cohort_label="lapsed-45d",
            cohort_size=1,
            selection_reason=_SELECTION_REASON,
        ),
        expected_arrr=ExpectedARRR(
            low_paise=100_000,
            high_paise=500_000,
            confidence=ConfidenceLevel.LOW,
            basis="prior win-back yields [E1].",
        ),
        evidence_refs=[
            EvidenceRef(claim_id="E1", source_kind=EvidenceSourceKind.TOOL_CALL, source_id="t")
        ],
        message_plan=MessagePlan(
            template_id=template_id,
            template_params=template_params,
            language=Language.EN,
            personalization=personalization,
        ),
    )


_MONEY = collapse._SummaryMoney(low_rupees=100, high_rupees=300, arrr_grounded=False, total_lapsed=None)


# --- _summary_offer_copy: the (label, offer copy) resolution --------------------------------


def test_offer_copy_from_typed_template_param() -> None:
    """The typed {{3}} offer_description the model filled is echoed verbatim."""
    plan = _plan(
        "team_winback_offer",
        {"customer_name": "<customer_name>", "business_name": "Cafe", "offer_description": "20% off this Diwali"},
    )
    label, offer = collapse._summary_offer_copy(plan)
    assert offer == "20% off this Diwali"
    assert "offer" in label


def test_offer_copy_promotes_personalization_when_param_underfilled() -> None:
    """The send path fills {{3}} from personalization when the model under-fills template_params
    (VT-633); the echo mirrors that so echo == what actually sends."""
    plan = _plan(
        "team_winback_offer",
        {"customer_name": "<customer_name>"},
        personalization="Diwali special: a free dessert with any dine-in!",
    )
    _, offer = collapse._summary_offer_copy(plan)
    assert offer == "Diwali special: a free dessert with any dine-in!"


def test_offer_copy_treats_placeholder_param_as_unfilled() -> None:
    """A literal ``<offer_description>`` placeholder is unfilled to the send-path repair — the echo
    promotes personalization, matching what sends."""
    plan = _plan(
        "team_winback_offer",
        {"offer_description": "<offer_description>"},
        personalization="Festive 15% off, this week only.",
    )
    _, offer = collapse._summary_offer_copy(plan)
    assert offer == "Festive 15% off, this week only."


def test_offer_copy_none_for_offerless_template() -> None:
    """team_winback_simple has no {{3}} slot — no offer copy, just an honest label; NEVER a
    fabricated offer."""
    plan = _plan("team_winback_simple", {"customer_name": "<customer_name>", "business_name": "Cafe"})
    label, offer = collapse._summary_offer_copy(plan)
    assert offer is None
    assert label == "a simple win-back message"


def test_offer_copy_failsoft_on_unknown_template() -> None:
    """An unknown template id (registry miss) fails soft to param-presence + a neutral label —
    the money-path echo never raises."""
    plan = _plan("team_winback_v1", {"first_name": "Owner", "discount": "10"})
    label, offer = collapse._summary_offer_copy(plan)
    assert offer is None
    assert label == "a win-back message"


# --- _build_chat_summary_body: the owner-facing echo ----------------------------------------


def test_summary_shows_offer_copy_both_languages() -> None:
    plan = _plan(
        "team_winback_offer",
        {"customer_name": "<customer_name>", "business_name": "Cafe", "offer_description": "20% off this Diwali or a free dessert"},
    )
    body = collapse._build_chat_summary_body(plan, plan.tenant_id, money=_MONEY)
    assert "20% off this Diwali or a free dessert" in body["en"]
    assert "20% off this Diwali or a free dessert" in body["hi"]


def test_summary_names_template_when_no_offer() -> None:
    plan = _plan("team_winback_simple", {"customer_name": "<customer_name>", "business_name": "Cafe"})
    body = collapse._build_chat_summary_body(plan, plan.tenant_id, money=_MONEY)
    assert "simple win-back message" in body["en"]
    assert "no special offer" in body["en"]


def test_summary_never_leaks_selection_reason() -> None:
    """VT-498: selection_reason is agent-authored free prose that can bake customer names — it must
    stay OUT of the owner-facing summary. The offer echo does not reintroduce it."""
    plan = _plan(
        "team_winback_offer",
        {"offer_description": "20% off this Diwali"},
    )
    body = collapse._build_chat_summary_body(plan, plan.tenant_id, money=_MONEY)
    assert _SELECTION_REASON not in body["en"]
    assert _SELECTION_REASON not in body["hi"]


def test_summary_offer_echo_is_deterministic() -> None:
    """Same plan → same echo (no per-call variance); the offer clause is pure in the plan."""
    plan = _plan("team_winback_offer", {"offer_description": "20% off this Diwali"})
    a = collapse._build_chat_summary_body(plan, plan.tenant_id, money=_MONEY)
    b = collapse._build_chat_summary_body(plan, plan.tenant_id, money=_MONEY)
    assert a == b


def test_summary_caps_a_very_long_offer() -> None:
    long_offer = "Diwali blowout! " + ("free dessert " * 60)
    plan = _plan("team_winback_offer", {"offer_description": long_offer})
    body = collapse._build_chat_summary_body(plan, plan.tenant_id, money=_MONEY)
    # capped + honestly marked truncated; never dumps an unbounded body into the owner chat
    assert "…" in body["en"]
    assert len(body["en"]) < len(long_offer) + 400
