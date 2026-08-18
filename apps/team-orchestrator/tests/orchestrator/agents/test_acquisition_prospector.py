from __future__ import annotations

import ast
import inspect
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("langchain_core")

from orchestrator.agent_framework.capabilities import AgentRole, GATED_CAPABILITIES
from orchestrator.agent_framework.conformance import assert_conforms
from orchestrator.agent_framework.context import ModuleContext
from orchestrator.agent_framework.gate_facade import GateFacade
from orchestrator.agent_framework.modules.acquisition_prospector_module import (
    AcquisitionProspectorModule,
)
from orchestrator.agents import acquisition_prospector as prospector
from orchestrator.agents.acquisition_prospector import (
    EvidenceClass,
    OperatorFit,
    PublicLaunchSignal,
    build_prospect,
    build_prospect_list,
)


def _signal(**overrides: object) -> PublicLaunchSignal:
    values: dict[str, object] = {
        "business_name": "Test Millet Foods",
        "city": "Jaipur",
        "category": "millet snacks",
        "stage": "live_waitlist",
        "evidence_url": "https://example.org/launch",
        "evidence_class": EvidenceClass.OPERATOR_OWNED,
        "access_date": date(2026, 8, 19),
        "published_date": date(2026, 8, 10),
        "operator_fit": OperatorFit.FOUNDER_LED_NEW_VENTURE,
        "founder_name": "Example Founder",
        "has_business_email": True,
        "has_social_channel": True,
        "discovered_phone": "+91 98765 43210",
    }
    values.update(overrides)
    return PublicLaunchSignal(**values)  # type: ignore[arg-type]


def test_public_phone_is_erased_and_never_contactable() -> None:
    artifact = build_prospect(_signal())
    payload = artifact.as_dict()
    assert "phone" not in " ".join(payload).replace("phone_status", "")
    assert "+91" not in repr(payload)
    assert "98765" not in repr(payload)
    assert payload["phone_status"] == "not_contactable_without_consent_basis"
    assert payload["contact_channels_available"] == ("business_email", "social")


def test_stale_signal_is_revalidation_not_current_prelaunch() -> None:
    artifact = build_prospect(
        _signal(published_date=date(2026, 4, 1), stage="opening_soon")
    )
    assert artifact.stage == "launch_signal_revalidate"
    assert "older than 90 days" in artifact.why_now


def test_list_deduplicates_same_business_and_city_and_keeps_stronger_evidence() -> None:
    weak = _signal(
        evidence_url="https://example.org/secondary",
        evidence_class=EvidenceClass.REPUTABLE_SECONDARY,
        has_business_email=False,
    )
    strong = _signal(evidence_url="https://example.org/operator")
    result = build_prospect_list((weak, strong))
    assert len(result) == 1
    assert result[0].evidence_url == "https://example.org/operator"


@pytest.mark.parametrize(
    "url",
    ("http://example.org/launch", "/relative", "javascript:alert(1)", ""),
)
def test_evidence_url_must_be_absolute_https(url: str) -> None:
    with pytest.raises(ValueError, match="absolute https"):
        build_prospect(_signal(evidence_url=url))


def test_score_is_bounded_and_ranking_is_deterministic() -> None:
    first = _signal(business_name="Zulu Foods", city="Pune")
    second = _signal(
        business_name="Alpha Foods",
        city="Delhi",
        operator_fit=OperatorFit.MATURE_CHAIN,
        evidence_class=EvidenceClass.PUBLIC_FORUM,
        has_business_email=False,
        has_social_channel=False,
    )
    result = build_prospect_list((second, first))
    assert result[0].business_name == "Zulu Foods"
    assert all(0 <= item.score <= 100 for item in result)


def test_agent_core_holds_empty_guarded_tool_surface() -> None:
    assert prospector.AGENT_TOOLS == ()
    source = inspect.getsource(prospector)
    assert "assert_agent_tools_safe(AGENT_TOOLS" in source


def test_agent_module_imports_no_send_or_ads_transport() -> None:
    forbidden = {
        "customer_send",
        "agent_send_draft",
        "twilio",
        "resend",
        "facebook_business",
        "google.ads",
    }
    for module in (prospector, __import__(
        "orchestrator.agent_framework.modules.acquisition_prospector_module",
        fromlist=["*"],
    )):
        path = Path(inspect.getsourcefile(module) or "")
        tree = ast.parse(path.read_text())
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        assert not any(bad in imported for bad in forbidden for imported in imports)


def test_acf_module_is_inert_proposer_and_returns_no_outreach_authority() -> None:
    module = AcquisitionProspectorModule()
    assert module.manifest.roles == frozenset({AgentRole.PROPOSER})
    assert module.manifest.tools == ()
    assert not (module.manifest.capabilities & GATED_CAPABILITIES)
    module.manifest.validate()
    assert assert_conforms(module).passed

    ctx = ModuleContext.for_proposer(
        tenant_model_value=uuid4(),
        module_name=module.manifest.name,
        data={
            "public_launch_signals": [
                {
                    "business_name": "Test Millet Foods",
                    "city": "Jaipur",
                    "category": "millet snacks",
                    "stage": "live_waitlist",
                    "evidence_url": "https://example.org/launch",
                    "evidence_class": "operator_owned",
                    "access_date": "2026-08-19",
                    "published_date": "2026-08-10",
                    "operator_fit": "founder_led_new_venture",
                    "has_business_email": True,
                    "discovered_phone": "+91 98765 43210",
                }
            ]
        },
    )
    gate = GateFacade(
        tenant_id=ctx.tenant_id,
        capabilities=module.manifest.capabilities_for_role(AgentRole.PROPOSER),
    )
    result = module.propose(ctx, gate)
    assert result.status == "completed"
    assert result.proposal is not None
    assert result.proposal["outreach_authorized"] is False
    assert "+91" not in repr(result.proposal)
    assert "98765" not in repr(result.proposal)
