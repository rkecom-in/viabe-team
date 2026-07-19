"""Tests for the unified output composer (VT-30).

Pure deterministic Python — no DB, no LLM, no network. Honesty rules,
24h-window logic, template routing, escalation framing, hard-limit
explanation, mixed-language pass-through.

Fazal-priority: honesty-rule tests get personal review at pre-merge per
Pillar 7 (owner-truth).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("yaml")


from orchestrator.output_composer import (  # noqa: E402
    ComposedOutput,
    compose_owner_output,
    load_template_routing,
    load_twilio_templates,
)


# ---------------------------------------------------------------------------
# Yaml shape — routing + templates parse cleanly
# ---------------------------------------------------------------------------

def test_template_routing_yaml_loads_with_all_eight_tier_a_names() -> None:
    routing = load_template_routing()
    templates = load_twilio_templates()
    # Every routing entry must map to a name present in twilio_templates.yaml.
    for intent, phase_map in routing.items():
        assert isinstance(phase_map, dict), f"{intent} not a mapping"
        for phase, name in phase_map.items():
            assert name in templates, (
                f"routing[{intent}][{phase}] -> {name} not in twilio_templates.yaml"
            )
    # All 8 Tier-A templates should be reachable via at least one routing key.
    reachable: set[str] = set()
    for phase_map in routing.values():
        reachable.update(phase_map.values())
    expected = {
        "team_welcome4",  # VT-555: welcome routing repointed to UTILITY quick-reply (team_welcome3 → MARKETING)
        "team_weekly_approval",
        "team_opt_out_confirmation",
        "team_dsr_acknowledgment",
        "team_agent_stuck_escalation",
        "team_status_ping",
        "team_unable_to_complete_request",
        "team_error_handler",
    }
    assert expected <= reachable, f"missing: {expected - reachable}"


# ---------------------------------------------------------------------------
# 24-hour window — Tier-A routing
# ---------------------------------------------------------------------------

def _state(**kw) -> dict:
    base = {
        "phase": "onboarding",
        "escalation_pending": False,
        "last_owner_message_at": None,
    }
    base.update(kw)
    return base


def test_outside_24h_window_forces_template() -> None:
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=25))
    out = compose_owner_output(None, state, "welcome", now=now)
    assert out.message_type == "template"
    assert out.template_name == "team_welcome4"  # VT-555: UTILITY quick-reply welcome (team_welcome3 → MARKETING)


def test_inside_24h_window_with_template_still_uses_template() -> None:
    """When a template applies for the (intent, phase), template path wins.

    Free-form is reserved for cases inside the window AND no template
    applies (caller passes an intent that doesn't route to a template).
    """
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=1))
    out = compose_owner_output(None, state, "welcome", now=now)
    assert out.message_type == "template"


def test_inside_24h_window_no_template_routes_free_form() -> None:
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=1))
    specialist = SimpleNamespace(
        status="completed", terminated_by=None, output={"message": "Hi there"}
    )
    out = compose_owner_output(specialist, state, "free_form_chat", now=now)
    assert out.message_type == "free_form_24h"
    assert out.template_name is None
    assert out.message_body == "Hi there"


def test_unknown_intent_outside_window_falls_back_to_unable_to_complete() -> None:
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=48))
    out = compose_owner_output(None, state, "some_unknown_intent", now=now)
    assert out.message_type == "template"
    assert out.template_name == "team_unable_to_complete_request"


def test_reengage_outside_window_selects_team_reengage() -> None:
    """VT-486: >24h since last inbound + intent 'reengage' → the out-of-window owner
    re-engagement template, with {{1}}=owner_name as the only positional param."""
    now = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=30), owner_name="Sundaram")
    out = compose_owner_output(None, state, "reengage", now=now)
    assert out.message_type == "template"
    assert out.template_name == "team_reengage"
    assert out.template_params == {"owner_name": "Sundaram"}


# ---------------------------------------------------------------------------
# Honesty rule #1 — no ARRR overstatement
# ---------------------------------------------------------------------------

def test_arrr_overstatement_prefixed_when_attribution_uncertain() -> None:
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=1))
    specialist = SimpleNamespace(
        status="completed",
        terminated_by=None,
        output={
            "attribution_uncertain": True,
            "message": "We recovered ₹5000 from your campaign.",
        },
    )
    out = compose_owner_output(specialist, state, "free_form_chat", now=now)
    assert "approximately ₹5000" in out.message_body
    assert "arrr_uncertainty_prefix_applied" in out.honesty_notes


def test_arrr_not_prefixed_when_attribution_certain() -> None:
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=1))
    specialist = SimpleNamespace(
        status="completed",
        terminated_by=None,
        output={
            "attribution_uncertain": False,
            "message": "We recovered ₹5000 from your campaign.",
        },
    )
    out = compose_owner_output(specialist, state, "free_form_chat", now=now)
    assert "approximately" not in out.message_body
    assert "₹5000" in out.message_body


# ---------------------------------------------------------------------------
# Honesty rule #2 — no hidden failures (escalation_pending + terminated_by)
# ---------------------------------------------------------------------------

def test_escalation_pending_prepends_honest_framing() -> None:
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(escalation_pending=True, last_owner_message_at=now - timedelta(hours=1))
    specialist = SimpleNamespace(
        status="completed", terminated_by=None, output={"message": "Result here."}
    )
    out = compose_owner_output(specialist, state, "free_form_chat", now=now)
    assert out.message_body.startswith("The agent encountered an issue")
    assert out.urgency == "high"
    assert "escalation_framing_prepended" in out.honesty_notes


def test_terminated_by_hard_limit_explained_in_plain_language() -> None:
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=1))
    specialist = SimpleNamespace(
        status="terminated",
        terminated_by="cost_paise",
        output={"message": "Partial result."},
    )
    out = compose_owner_output(specialist, state, "free_form_chat", now=now)
    assert "₹50 cost budget" in out.message_body
    assert any(n.startswith("hard_limit_axis_explained") for n in out.honesty_notes)


def test_terminated_by_tokens_axis_uses_response_budget_phrasing() -> None:
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=1))
    specialist = SimpleNamespace(
        status="terminated",
        terminated_by="tokens",
        output={"message": "Partial."},
    )
    out = compose_owner_output(specialist, state, "free_form_chat", now=now)
    assert "response budget" in out.message_body


# ---------------------------------------------------------------------------
# Honesty rule #3 — no retention pressure
# ---------------------------------------------------------------------------

def test_pressure_phrase_detected_and_noted() -> None:
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=1))
    specialist = SimpleNamespace(
        status="completed",
        terminated_by=None,
        output={"message": "Are you sure? Look at all this value you're missing out on!"},
    )
    out = compose_owner_output(specialist, state, "free_form_chat", now=now)
    assert any(n.startswith("pressure_phrase_detected") for n in out.honesty_notes)


def test_clean_cancellation_response_has_no_pressure_notes() -> None:
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=1))
    specialist = SimpleNamespace(
        status="completed",
        terminated_by=None,
        output={"message": "Got it — I'll process the cancellation for you."},
    )
    out = compose_owner_output(specialist, state, "free_form_chat", now=now)
    assert not any(n.startswith("pressure_phrase_detected") for n in out.honesty_notes)


# ---------------------------------------------------------------------------
# Honesty rule #4 — no certainty claims about customer intent
# ---------------------------------------------------------------------------

def test_certainty_claim_soft_landed_when_intent_inferred() -> None:
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=1))
    specialist = SimpleNamespace(
        status="completed",
        terminated_by=None,
        output={
            "intent_inferred": True,
            "message": "Customer wants a refund based on the message tone.",
        },
    )
    out = compose_owner_output(specialist, state, "free_form_chat", now=now)
    assert "pattern suggests" in out.message_body.lower()
    assert "customer wants" not in out.message_body.lower()


def test_explicit_intent_no_softening_applied() -> None:
    """Without the ``intent_inferred`` flag the composer leaves explicit
    claims alone (specialist knows what it's doing)."""
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=1))
    specialist = SimpleNamespace(
        status="completed",
        terminated_by=None,
        output={"message": "Customer wants the refund processed today."},
    )
    out = compose_owner_output(specialist, state, "free_form_chat", now=now)
    assert "customer wants" in out.message_body.lower()


# ---------------------------------------------------------------------------
# Language selection
# ---------------------------------------------------------------------------

def test_preferred_language_env_override(monkeypatch) -> None:
    monkeypatch.setenv("TENANT_DEFAULT_LANGUAGE", "hi")
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=1))
    out = compose_owner_output(None, state, "welcome", now=now)
    assert out.preferred_language == "hi"


def test_invalid_preferred_language_env_falls_back_to_en(monkeypatch) -> None:
    monkeypatch.setenv("TENANT_DEFAULT_LANGUAGE", "fr")
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=1))
    out = compose_owner_output(None, state, "welcome", now=now)
    assert out.preferred_language == "en"


# VT-416 PR-3 — per-tenant preferred_language (state wins over global default)

def test_per_tenant_hindi_preference_yields_hindi_variant(monkeypatch) -> None:
    """A Hindi-preference tenant in state gets 'hi', even when the global default is 'en'."""
    monkeypatch.setenv("TENANT_DEFAULT_LANGUAGE", "en")
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(
        last_owner_message_at=now - timedelta(hours=1),
        preferred_language="hi",
    )
    out = compose_owner_output(None, state, "welcome", now=now)
    assert out.preferred_language == "hi"


def test_per_tenant_state_overrides_global_default(monkeypatch) -> None:
    """State preference wins even when the global default points the other way."""
    monkeypatch.setenv("TENANT_DEFAULT_LANGUAGE", "hi")
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(
        last_owner_message_at=now - timedelta(hours=1),
        preferred_language="en",
    )
    out = compose_owner_output(None, state, "welcome", now=now)
    assert out.preferred_language == "en"


def test_absent_preferred_language_falls_back_to_global_default(monkeypatch) -> None:
    """No per-tenant value → the global default (env) is used (fallback intact)."""
    monkeypatch.setenv("TENANT_DEFAULT_LANGUAGE", "hi")
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=1))  # no preferred_language key
    out = compose_owner_output(None, state, "welcome", now=now)
    assert out.preferred_language == "hi"


@pytest.mark.parametrize("bad", ["", None, "fr", "EN-US"])
def test_invalid_or_empty_state_value_falls_back_to_default(monkeypatch, bad) -> None:
    """Empty / unrecognised state value → fall back to the global default."""
    monkeypatch.setenv("TENANT_DEFAULT_LANGUAGE", "en")
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(
        last_owner_message_at=now - timedelta(hours=1),
        preferred_language=bad,
    )
    out = compose_owner_output(None, state, "welcome", now=now)
    assert out.preferred_language == "en"


def test_state_hindi_value_is_case_insensitive(monkeypatch) -> None:
    """An uppercase 'HI' on state still resolves to the 'hi' variant."""
    monkeypatch.setenv("TENANT_DEFAULT_LANGUAGE", "en")
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(
        last_owner_message_at=now - timedelta(hours=1),
        preferred_language="HI",
    )
    out = compose_owner_output(None, state, "welcome", now=now)
    assert out.preferred_language == "hi"


# ---------------------------------------------------------------------------
# Signature determinism
# ---------------------------------------------------------------------------

def test_signature_deterministic_same_inputs_same_output() -> None:
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=1))
    out1 = compose_owner_output(None, state, "welcome", now=now)
    out2 = compose_owner_output(None, state, "welcome", now=now)
    assert out1.signature == out2.signature
    assert out1.message_body == out2.message_body


def test_signature_differs_when_intent_differs() -> None:
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=1))
    out_w = compose_owner_output(None, state, "welcome", now=now)
    out_s = compose_owner_output(None, state, "status_ping", now=now)
    assert out_w.signature != out_s.signature


# ---------------------------------------------------------------------------
# Tier-A intent → template_name mapping (full sweep)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "intent,phase,expected_template",
    [
        ("welcome", "onboarding", "team_welcome4"),  # VT-555: UTILITY quick-reply welcome (team_welcome3 → MARKETING)
        ("welcome", "trial", "team_welcome4"),
        ("weekly_approval", "paid_active", "team_weekly_approval"),
        ("weekly_approval", "paid_at_risk", "team_weekly_approval"),
        ("opt_out_confirmed", "trial", "team_opt_out_confirmation"),
        ("dsr_acknowledged", "paid_active", "team_dsr_acknowledgment"),
        ("agent_stuck", "paid_active", "team_agent_stuck_escalation"),
        ("status_ping", "paid_active", "team_status_ping"),
    ],
)
def test_tier_a_intent_phase_resolves_correct_template(
    intent: str, phase: str, expected_template: str
) -> None:
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    state = _state(phase=phase, last_owner_message_at=now - timedelta(hours=48))
    out = compose_owner_output(None, state, intent, now=now)
    assert out.template_name == expected_template
    assert out.message_type == "template"


# ---------------------------------------------------------------------------
# ComposedOutput dataclass shape
# ---------------------------------------------------------------------------

def test_composed_output_frozen() -> None:
    out = ComposedOutput(
        message_body="x", message_type="template", template_name="team_welcome"
    )
    with pytest.raises(Exception):
        out.message_body = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# VT-248 — fail-closed campaign rejection → count-bearing owner template
# ---------------------------------------------------------------------------

def test_campaign_not_sent_routes_to_count_template_with_count_param() -> None:
    """The campaign_not_sent_invalid_cohort intent resolves the dedicated
    team_campaign_not_sent template and maps rejected_count → {{2}}."""
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=48))
    # The dispatch carrier hands the composer ONLY the count (no ids).
    result = SimpleNamespace(output={"rejected_count": 3})
    out = compose_owner_output(
        result, state, "campaign_not_sent_invalid_cohort", now=now
    )
    assert out.template_name == "team_campaign_not_sent"
    assert out.message_type == "template"
    # {{1}} owner_name (empty in Phase 1), {{2}} the COUNT — never ids.
    assert out.template_params == {"owner_name": "", "unverified_count": "3"}


def test_campaign_not_sent_params_match_registry_signature() -> None:
    """The composer's params for the rejection intent satisfy the registry
    variable signature exactly, so a downstream send validates cleanly."""
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=48))
    result = SimpleNamespace(output={"rejected_count": 7})
    out = compose_owner_output(
        result, state, "campaign_not_sent_invalid_cohort", now=now
    )
    templates = load_twilio_templates()
    variables = set(templates["team_campaign_not_sent"]["variables"])
    assert set(out.template_params.keys()) == variables
    assert out.template_params["unverified_count"] == "7"


def test_campaign_not_sent_count_defaults_zero_when_absent() -> None:
    """Defensive: a carrier without rejected_count still composes (count 0)
    rather than crashing the deterministic composer."""
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=48))
    out = compose_owner_output(
        SimpleNamespace(output={}), state, "campaign_not_sent_invalid_cohort", now=now
    )
    assert out.template_name == "team_campaign_not_sent"
    assert out.template_params["unverified_count"] == "0"


# ---------------------------------------------------------------------------
# VT-594 — compose_owner_output must not raise on a RAW CampaignPlan variant.
# The collapse path hands the composer the pydantic model itself (no `.output`
# attribute), not an AgentResult-shaped specialist_result. Every bare
# `specialist_result.output` read in this module previously raised
# AttributeError, swallowed at dispatch.py:1127 into an empty envelope.
# ---------------------------------------------------------------------------

pytest.importorskip("pydantic")


def _campaign_plan_proposed():
    from uuid import uuid4

    from orchestrator.agent.schemas.campaign_plan import (
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

    now = datetime.now(timezone.utc)
    return CampaignPlanProposed(
        tenant_id=uuid4(),
        run_id=uuid4(),
        generated_at=now,
        campaign_window=CampaignWindow(
            start=now + timedelta(hours=1), end=now + timedelta(days=7)
        ),
        target_cohort=TargetCohort(
            customer_ids=[uuid4()],
            cohort_label="60-90 day dormants",
            cohort_size=1,
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


def _campaign_plan_out_of_scope():
    from uuid import uuid4

    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanOutOfScope,
        SuggestedSpecialist,
    )

    return CampaignPlanOutOfScope(
        tenant_id=uuid4(),
        run_id=uuid4(),
        generated_at=datetime.now(timezone.utc),
        out_of_scope_reason="Request concerns review-reputation handling.",
        suggested_specialist=SuggestedSpecialist.REPUTATION,
    )


def _campaign_plan_insufficient_data():
    from uuid import uuid4

    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanInsufficientData,
        MissingDataItem,
    )

    return CampaignPlanInsufficientData(
        tenant_id=uuid4(),
        run_id=uuid4(),
        generated_at=datetime.now(timezone.utc),
        missing_data=[
            MissingDataItem(
                category="cohort",
                description="No dormant-customer rows surfaced.",
                suggested_remediation="Seed customer ledger.",
            )
        ],
    )


@pytest.mark.parametrize(
    "plan_factory",
    [_campaign_plan_proposed, _campaign_plan_out_of_scope, _campaign_plan_insufficient_data],
    ids=["proposed", "out_of_scope", "insufficient_data"],
)
def test_compose_owner_output_does_not_raise_on_raw_campaign_plan(plan_factory) -> None:
    """VT-594: every CampaignPlan variant composes without raising when handed
    to compose_owner_output as the raw specialist_result (no AgentResult
    wrapper, no `.output` attribute). Inside the 24h window (the real collapse
    scenario — the owner just messaged) with no template mapped for
    "owner_substantive_message", the free-form path is exercised."""
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=1))
    plan = plan_factory()
    out = compose_owner_output(plan, state, "owner_substantive_message", now=now)
    assert isinstance(out.message_body, str)
    assert out.message_type == "free_form_24h"


def test_compose_owner_output_cohort_rejected_carrier_still_works_unchanged() -> None:
    """Regression guard: the VT-248 count-only carrier (`_CohortRejectedResult`
    shape, a real `.output` dict) must keep composing exactly as before — the
    getattr fallback must not change behaviour for objects that DO have
    `.output`."""
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    state = _state(last_owner_message_at=now - timedelta(hours=48))
    out = compose_owner_output(
        SimpleNamespace(output={"rejected_count": 5}),
        state,
        "campaign_not_sent_invalid_cohort",
        now=now,
    )
    assert out.template_params["unverified_count"] == "5"
