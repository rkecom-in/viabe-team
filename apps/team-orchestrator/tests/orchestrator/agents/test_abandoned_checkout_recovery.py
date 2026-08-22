from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from orchestrator.agent_framework.capabilities import AgentRole
from orchestrator.agent_framework.conformance import assert_conforms
from orchestrator.agent_framework.context import ModuleContext, ModuleResult
from orchestrator.agent_framework.gate_facade import GateFacade
from orchestrator.agent_framework.modules.abandoned_checkout_recovery_module import (
    COHORT_KEY,
    AbandonedCheckoutRecoveryModule,
    S2IntegrationNotReady,
)
from orchestrator.agents import abandoned_checkout_recovery as s2


NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)


def _reports_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "checkout_attempt_id": "reports-attempt-1",
        "attempt_version": "click-event-1",
        "clicked_at": "2026-08-19T09:00:00+00:00",
        "source_updated_at": "2026-08-19T09:01:00+00:00",
        "purchased_at": None,
        "amount_inr": "2499.50",
        "currency": "INR",
        "item_count": 1,
        "contact_token": "phone_tok_reports",
        "destination_ref": "opaque_reports_destination",
        "evidence_url_or_export_ref": "reports-event:event-1",
    }
    row.update(overrides)
    return row


def _attempt(tenant_id=None, **overrides: object) -> s2.CheckoutAttempt:
    values: dict[str, object] = {
        "tenant_id": tenant_id or uuid4(),
        "source": s2.CheckoutSourceKind.VIABE_REPORTS,
        "attempt_id": "attempt-1",
        "attempt_version": "v1",
        "created_at": NOW - timedelta(hours=4),
        "updated_at": NOW - timedelta(hours=3),
        "completed_at": None,
        "total_paise": 249_950,
        "currency": "INR",
        "item_count": 1,
        "contact_token": "phone_tok_1",
        "destination_ref": "opaque_destination",
        "evidence_ref": "reports-event:event-1",
    }
    values.update(overrides)
    return s2.CheckoutAttempt(**values)  # type: ignore[arg-type]


def _consent(attempt: s2.CheckoutAttempt, **overrides: object) -> s2.CommerceConsentSnapshot:
    values: dict[str, object] = {
        "tenant_id": attempt.tenant_id,
        "contact_token": attempt.contact_token,
        "channel": "whatsapp",
        "purpose": "checkout_recovery",
        "notice_version": "checkout-recovery-v1",
        "affirmative_at": NOW - timedelta(days=1),
        "state": "active",
        "evidence_ref": "consent-event:1",
    }
    values.update(overrides)
    return s2.CommerceConsentSnapshot(**values)  # type: ignore[arg-type]


def _safety(attempt: s2.CheckoutAttempt, **overrides: object) -> s2.CustomerSafetySnapshot:
    values: dict[str, object] = {
        "tenant_id": attempt.tenant_id,
        "contact_token": attempt.contact_token,
        "subscribed": True,
        "globally_opted_out": False,
        "complaint_blocked": False,
    }
    values.update(overrides)
    return s2.CustomerSafetySnapshot(**values)  # type: ignore[arg-type]


def _cohort(attempt: s2.CheckoutAttempt, **overrides: object):
    kwargs = {
        "tenant_id": attempt.tenant_id,
        "now": NOW,
        "abandonment_delay": timedelta(hours=1),
        "allowed_notice_versions": frozenset({"checkout-recovery-v1"}),
        "consents": {attempt.contact_token: _consent(attempt)},
        "safety": {attempt.contact_token: _safety(attempt)},
        "already_contacted": frozenset(),
    }
    kwargs.update(overrides)
    return s2.build_recovery_cohort([attempt], **kwargs)


def test_reports_source_normalises_bridge_record_without_raw_contact_or_url() -> None:
    tenant = uuid4()
    source = s2.ReportsFunnelSource(reader=lambda _tenant, _as_of: [_reports_row()])

    (attempt,) = source.read_attempts(tenant, as_of=NOW)

    assert attempt.source is s2.CheckoutSourceKind.VIABE_REPORTS
    assert attempt.total_paise == 249_950
    assert attempt.contact_token == "phone_tok_reports"
    assert attempt.destination_ref == "opaque_reports_destination"
    assert not hasattr(attempt, "phone")
    assert not hasattr(attempt, "checkout_url")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt_version", ""),
        ("contact_token", ""),
        ("evidence_url_or_export_ref", ""),
        ("currency", "USD"),
        ("item_count", 0),
    ],
)
def test_reports_source_quarantines_incomplete_or_out_of_scope_records(
    field: str, value: object
) -> None:
    row = _reports_row(**{field: value})
    source = s2.ReportsFunnelSource(reader=lambda _tenant, _as_of: [row])

    with pytest.raises(s2.CheckoutSourceError):
        source.read_attempts(uuid4(), as_of=NOW)


def test_shopify_source_tokenises_phone_and_protects_destination() -> None:
    tenant = uuid4()
    raw = {
        "id": 42,
        "attempt_version": "update-7",
        "created_at": "2026-08-19T08:00:00Z",
        "updated_at": "2026-08-19T09:00:00Z",
        "completed_at": None,
        "total_price": "999.00",
        "currency": "INR",
        "line_items": [{"id": 1}, {"id": 2}],
        "customer": {"phone": "+919999999999"},
        "abandoned_checkout_url": "https://shop.test/checkouts/secret",
        "evidence_ref": "shopify-webhook:7",
    }
    seen: dict[str, str] = {}

    def tokenise(value: str) -> str:
        seen["phone"] = value
        return "phone_tok_shopify"

    def protect(value: str) -> str:
        seen["url"] = value
        return "encrypted_destination_ref"

    source = s2.ShopifyAbandonedCheckoutSource(
        reader=lambda _tenant, _as_of: [raw],
        tokenise_phone=tokenise,
        protect_destination=protect,
    )
    (attempt,) = source.read_attempts(tenant, as_of=NOW)

    assert seen == {
        "phone": "+919999999999",
        "url": "https://shop.test/checkouts/secret",
    }
    assert attempt.contact_token == "phone_tok_shopify"
    assert attempt.destination_ref == "encrypted_destination_ref"
    assert attempt.item_count == 2


def test_happy_path_requires_exact_purpose_consent_and_safety() -> None:
    attempt = _attempt()
    result = _cohort(attempt)

    assert len(result) == 1
    assert result[0].attempt.key == attempt.key
    assert result[0].consent_evidence_ref == "consent-event:1"


@pytest.mark.parametrize(
    "overrides",
    [
        {"abandonment_delay": None},
        {"allowed_notice_versions": frozenset()},
        {"consents": {}},
        {"safety": {}},
        {"already_contacted": frozenset()},
    ],
)
def test_unset_activation_or_missing_evidence_fails_closed(overrides: dict[str, object]) -> None:
    attempt = _attempt()
    if "already_contacted" in overrides:
        overrides["already_contacted"] = frozenset({attempt.key})
    assert _cohort(attempt, **overrides) == ()


@pytest.mark.parametrize(
    "consent_change",
    [
        {"purpose": "replenishment_reminder"},
        {"channel": "email"},
        {"state": "withdrawn"},
        {"notice_version": "unreviewed"},
        {"affirmative_at": NOW + timedelta(minutes=1)},
    ],
)
def test_wrong_or_withdrawn_consent_never_enters_cohort(
    consent_change: dict[str, object]
) -> None:
    attempt = _attempt()
    consent = _consent(attempt, **consent_change)
    assert _cohort(attempt, consents={attempt.contact_token: consent}) == ()


@pytest.mark.parametrize(
    "safety_change",
    [
        {"subscribed": False},
        {"globally_opted_out": True},
        {"complaint_blocked": True},
    ],
)
def test_live_customer_safety_vetoes_cohort(safety_change: dict[str, object]) -> None:
    attempt = _attempt()
    customer = _safety(attempt, **safety_change)
    assert _cohort(attempt, safety={attempt.contact_token: customer}) == ()


def test_completion_and_not_yet_aged_attempts_are_excluded() -> None:
    completed = _attempt(completed_at=NOW - timedelta(minutes=5))
    recent = _attempt(updated_at=NOW - timedelta(minutes=30))

    assert _cohort(completed) == ()
    assert _cohort(recent) == ()


def test_cross_tenant_attempt_and_evidence_are_excluded() -> None:
    attempt = _attempt()
    other = uuid4()

    assert _cohort(attempt, tenant_id=other) == ()
    assert _cohort(attempt, consents={attempt.contact_token: _consent(attempt, tenant_id=other)}) == ()
    assert _cohort(attempt, safety={attempt.contact_token: _safety(attempt, tenant_id=other)}) == ()


def test_duplicate_attempt_key_creates_one_candidate() -> None:
    attempt = _attempt()
    duplicate = _attempt(tenant_id=attempt.tenant_id)
    consent = _consent(attempt)
    customer = _safety(attempt)

    result = s2.build_recovery_cohort(
        [attempt, duplicate],
        tenant_id=attempt.tenant_id,
        now=NOW,
        abandonment_delay=timedelta(hours=1),
        allowed_notice_versions=frozenset({"checkout-recovery-v1"}),
        consents={attempt.contact_token: consent},
        safety={attempt.contact_token: customer},
        already_contacted=frozenset(),
    )

    assert len(result) == 1


def test_grounding_accepts_only_exact_frozen_template_values() -> None:
    candidate = _cohort(_attempt())[0]
    bundle = s2.freeze_recovery_facts(
        candidate,
        customer_name="Asha",
        business_name="Viabe Market Intelligence",
        recovery_link="https://viabe.ai/r/opaque",
    )
    params = {
        "customer_name": "Asha",
        "business_name": "Viabe Market Intelligence",
        "recovery_link": "https://viabe.ai/r/opaque",
    }

    assert s2.validate_template_params(bundle, params) == (
        "Asha",
        "Viabe Market Intelligence",
        "https://viabe.ai/r/opaque",
    )

    with pytest.raises(ValueError):
        s2.validate_template_params(bundle, {**params, "business_name": "50% off today"})


def test_module_is_sendless_and_default_execution_stops_at_missing_seam() -> None:
    source = ast.parse(inspect.getsource(s2))
    imports = {
        alias.name
        for node in ast.walk(source)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any(
        forbidden in imported
        for imported in imports
        for forbidden in ("customer_send", "twilio", "send_whatsapp")
    )
    assert s2.AGENT_TOOLS == ()

    tenant = uuid4()
    module = AbandonedCheckoutRecoveryModule()
    ctx = ModuleContext(
        tenant_id=tenant,
        role=AgentRole.EXECUTOR,
        run_id="run-1",
        item_id="item-1",
        work_item_id="work-1",
    )
    gate = GateFacade(tenant_id=tenant, capabilities=frozenset(), run_id="run-1")
    with pytest.raises(S2IntegrationNotReady):
        module.execute(ctx, gate)


def test_module_proposal_is_explicitly_non_authorising() -> None:
    tenant = uuid4()
    module = AbandonedCheckoutRecoveryModule()
    ctx = ModuleContext(
        tenant_id=tenant,
        role=AgentRole.PROPOSER,
        data={
            COHORT_KEY: [
                {
                    "source": "reports_funnel",
                    "attempt_id": "attempt-1",
                    "total_paise": 199_900,
                    "item_count": 1,
                    "age_minutes": 90,
                    "phone": "+919999999999",
                }
            ]
        },
    )
    gate = GateFacade(tenant_id=tenant, capabilities=frozenset())

    result = module.propose(ctx, gate)

    assert result.proposal is not None
    assert result.proposal["effect_authorized"] is False
    assert result.proposal["candidate_count"] == 1
    assert "phone" not in result.proposal["candidates"][0]


def test_injected_executor_must_return_executor_result() -> None:
    tenant = uuid4()
    good = ModuleResult(
        role=AgentRole.EXECUTOR,
        status="awaiting_approval",
        work_item_status="awaiting_approval",
        batch_id=str(uuid4()),
        counters={"drafted": 1},
    )
    module = AbandonedCheckoutRecoveryModule(executor=lambda _ctx: good)
    ctx = ModuleContext(
        tenant_id=tenant,
        role=AgentRole.EXECUTOR,
        run_id="run-1",
        item_id="item-1",
        work_item_id="work-1",
    )
    gate = GateFacade(tenant_id=tenant, capabilities=frozenset(), run_id="run-1")

    assert module.execute(ctx, gate) == good


def test_module_conforms_to_acf_contract() -> None:
    # The dependency-light smoke intentionally omits LangChain. Keep the pure
    # cohort/consent tests active there while reserving ACF reachability for the
    # fully provisioned orchestrator environment.
    pytest.importorskip("langchain_core")
    report = assert_conforms(AbandonedCheckoutRecoveryModule())
    assert report.passed
