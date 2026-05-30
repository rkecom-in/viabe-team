"""VT-4 ship-thin — ``serialize_bundle_for_prompt`` unit tests.

The serializer renders the ``SalesRecoveryContext`` bundle plus ship-thin
scaffolding (``templates_available`` + ``target_recovered_paise``) into a
single markdown block suitable for the first user message of the
Anthropic Messages API call. These tests pin the rendered surface:

  (a) Every required section header is present.
  (b) Every bundle value the LLM needs reaches the rendered string.
  (c) Substrate-populated flags reach the rendered string (the model
      needs to know which sections are real vs CL-190 safe-empty).
  (d) Templates registry surfaces inline (ship-thin default — until the
      approved-templates registry lands as its own VT row).
  (e) ``target_recovered_paise`` derives from
      ``attribution_snapshot.last_7d_recovered_paise * 1.1`` when caller
      supplies no override and the figure exceeds the baseline default.
  (f) The owner ``user_request`` is appended LAST so the model receives
      it after the context.
  (g) Identity fields (``tenant_id``, ``run_id``) are NOT in the
      rendered block — they're orchestrator state, not agent input.

Pure Python — no DB, no LLM. Runs in the lightweight CI ``test`` job.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.context_builder import (  # noqa: E402 — post importorskip
    AttributionSnapshot,
    BusinessProfile,
    CampaignSnapshot,
    LedgerSummary,
    OwnerInput,
    SalesRecoveryContext,
    _DEFAULT_RECOVERY_TARGET_MULTIPLIER,
    _DEFAULT_TARGET_RECOVERED_PAISE,
    _PHASE1_APPROVED_TEMPLATES,
    serialize_bundle_for_prompt,
)


def _seeded_context(
    *,
    user_request: str = "Recover dormant customers this week",
    last_7d_paise: int = 0,
) -> SalesRecoveryContext:
    """Build a fully-populated bundle so every section has content.

    Substrate-populated flags are mixed (some True, some False) so the
    test can assert the rendered block reflects per-section truth.
    """
    tenant_id = uuid4()
    run_id = uuid4()
    campaign_id = uuid4()
    owner_input_id = uuid4()
    return SalesRecoveryContext(
        tenant_id=tenant_id,
        run_id=run_id,
        user_request=user_request,
        trigger_reason="weekly_cadence",
        business_profile=BusinessProfile(
            business_name="Test Cafe",
            business_type="cafe",
            locality="Indiranagar",
            current_phase="early_traction",
            founding_tier_flag=True,
        ),
        customer_ledger_summary=LedgerSummary(
            total_customers=42,
            dormant_cohorts={"30d_silent": 12, "60d_silent": 5},
            top_spenders=[uuid4(), uuid4()],
        ),
        recent_campaigns=[
            CampaignSnapshot(
                campaign_id=campaign_id,
                status="sent",
                recovered_paise=12_500,
                proposed_at=datetime(2026, 5, 10, 14, 30, tzinfo=UTC),
            )
        ],
        attribution_snapshot=AttributionSnapshot(
            cumulative_recovered_paise=240_000,
            last_7d_recovered_paise=last_7d_paise,
            last_30d_recovered_paise=85_000,
            attribution_rate_trend=[0.12, 0.18],
        ),
        pending_owner_inputs=[
            OwnerInput(
                input_id=owner_input_id,
                received_at=datetime(2026, 5, 22, 9, 0, tzinfo=UTC),
                intent="customer_recovery",
                segment="dormant",
                occasion=None,
            )
        ],
        data_completeness={
            "business_profile": False,
            "customer_ledger_summary": True,
            "recent_campaigns": True,
            "attribution_snapshot": True,
            "pending_owner_inputs": True,
        },
    )


def test_serializer_emits_every_required_section_header() -> None:
    """(a) — every section the system prompt expects must be present."""
    rendered = serialize_bundle_for_prompt(_seeded_context())
    for header in (
        "# Sales Recovery Context",
        "## Business profile",
        "## Customer ledger summary",
        "## Recent campaigns",
        "## Attribution snapshot",
        "## Pending owner inputs",
        "## Available WhatsApp templates",
        "## Expected outcome",
        "## Trigger reason",
        "## Owner request",
    ):
        assert header in rendered, f"missing section header: {header!r}"


def test_serializer_propagates_bundle_values() -> None:
    """(b) — values that affect the model's decision are in the output."""
    rendered = serialize_bundle_for_prompt(_seeded_context())
    assert "Test Cafe" in rendered
    assert "cafe" in rendered
    assert "Indiranagar" in rendered
    assert "early_traction" in rendered
    assert "founding_tier_flag: True" in rendered
    assert "total_customers: 42" in rendered
    assert "30d_silent: 12 customers" in rendered
    assert "60d_silent: 5 customers" in rendered
    assert "cumulative_recovered_paise: 240000" in rendered
    assert "last_30d_recovered_paise: 85000" in rendered
    assert "intent=customer_recovery" in rendered
    assert "segment=dormant" in rendered
    assert "weekly_cadence" in rendered


def test_serializer_includes_data_completeness_per_section() -> None:
    """(c) — completeness flags reach the model so it knows safe-empty
    vs real-substrate before relying on a section."""
    rendered = serialize_bundle_for_prompt(_seeded_context())
    bp_block = rendered.split("## Business profile", 1)[1].split("##", 1)[0]
    assert "substrate_populated: False" in bp_block

    ledger_block = rendered.split("## Customer ledger summary", 1)[1].split(
        "##", 1
    )[0]
    assert "substrate_populated: True" in ledger_block


def test_serializer_lists_phase1_approved_templates_inline() -> None:
    """(d) — ship-thin default: orchestrator-approved templates appear
    inline so the model can pick a legal ``template_id``."""
    rendered = serialize_bundle_for_prompt(_seeded_context())
    for tid in _PHASE1_APPROVED_TEMPLATES:
        assert tid in rendered, f"approved template {tid!r} missing"
    assert "Inventing a template_id is a contract violation" in rendered


def test_serializer_uses_caller_supplied_template_list_when_given() -> None:
    """Caller can override the default registry (later VT row will read
    the approved-templates yaml; this proves the seam exists)."""
    custom = ("team_custom_a", "team_custom_b")
    rendered = serialize_bundle_for_prompt(
        _seeded_context(), templates_available=custom
    )
    assert "team_custom_a" in rendered
    assert "team_custom_b" in rendered
    for tid in _PHASE1_APPROVED_TEMPLATES:
        assert tid not in rendered, "default leaked when caller overrode"


def test_serializer_target_defaults_to_baseline_when_attribution_low() -> None:
    """(e) — when 7d_paise * 1.1 < baseline, target uses baseline."""
    rendered = serialize_bundle_for_prompt(
        _seeded_context(last_7d_paise=0)
    )
    assert f"target_recovered_paise: {_DEFAULT_TARGET_RECOVERED_PAISE}" in rendered


def test_serializer_target_derives_from_attribution_when_above_baseline() -> None:
    """(e cont.) — when 7d_paise * 1.1 > baseline, target derives from
    attribution. last_7d=100_000 → 100_000 * 1.1 = 110_000 > 50_000."""
    rendered = serialize_bundle_for_prompt(
        _seeded_context(last_7d_paise=100_000)
    )
    assert "target_recovered_paise: 110000" in rendered


def test_serializer_honors_caller_supplied_target_override() -> None:
    """Caller may force a specific target (test seam + future
    per-tenant attribution-target wiring)."""
    rendered = serialize_bundle_for_prompt(
        _seeded_context(), target_recovered_paise=999_999
    )
    assert "target_recovered_paise: 999999" in rendered


def test_serializer_appends_user_request_last() -> None:
    """(f) — owner request is the last section so the model reads the
    context first and the ask last (mirrors how a human briefer talks)."""
    rendered = serialize_bundle_for_prompt(
        _seeded_context(user_request="please send winback to dormant cohort")
    )
    assert rendered.rstrip().endswith("please send winback to dormant cohort")


def test_serializer_omits_identity_fields() -> None:
    """(g) — ``tenant_id`` / ``run_id`` are orchestrator state, not
    agent input. They must NOT reach the rendered block (the agent
    has no use for them; output coercion injects identity at the
    orchestrator boundary)."""
    ctx = _seeded_context()
    rendered = serialize_bundle_for_prompt(ctx)
    assert str(ctx.tenant_id) not in rendered
    assert str(ctx.run_id) not in rendered


def test_serializer_handles_empty_campaign_history() -> None:
    """A first-run tenant has zero campaigns. The block must still
    render with a clear "no prior recovery campaigns recorded" line so
    the model doesn't mistake absence for fetch failure."""
    ctx = _seeded_context()
    ctx_no_campaigns = SalesRecoveryContext(
        tenant_id=ctx.tenant_id,
        run_id=ctx.run_id,
        user_request=ctx.user_request,
        trigger_reason=ctx.trigger_reason,
        business_profile=ctx.business_profile,
        customer_ledger_summary=ctx.customer_ledger_summary,
        recent_campaigns=[],
        attribution_snapshot=ctx.attribution_snapshot,
        pending_owner_inputs=ctx.pending_owner_inputs,
        meta=ctx.meta,
        data_completeness=ctx.data_completeness,
    )
    rendered = serialize_bundle_for_prompt(ctx_no_campaigns)
    assert "no prior recovery campaigns recorded" in rendered


def test_serializer_handles_empty_owner_inputs() -> None:
    """No pending owner inputs is the steady state outside a triggered
    week — block must still render with ``count: 0`` and a clear empty
    marker."""
    ctx = _seeded_context()
    ctx_no_owner = SalesRecoveryContext(
        tenant_id=ctx.tenant_id,
        run_id=ctx.run_id,
        user_request=ctx.user_request,
        trigger_reason=ctx.trigger_reason,
        business_profile=ctx.business_profile,
        customer_ledger_summary=ctx.customer_ledger_summary,
        recent_campaigns=ctx.recent_campaigns,
        attribution_snapshot=ctx.attribution_snapshot,
        pending_owner_inputs=[],
        meta=ctx.meta,
        data_completeness=ctx.data_completeness,
    )
    rendered = serialize_bundle_for_prompt(ctx_no_owner)
    owner_block = rendered.split("## Pending owner inputs", 1)[1].split(
        "##", 1
    )[0]
    assert "count: 0" in owner_block


# --- VT-164: per-tenant recovery-target config tests --------------------------


def _seeded_context_with_config(
    *,
    last_7d_paise: int = 0,
    multiplier: float = _DEFAULT_RECOVERY_TARGET_MULTIPLIER,
    floor_paise: int = _DEFAULT_TARGET_RECOVERED_PAISE,
) -> SalesRecoveryContext:
    """Seeded context with explicit per-tenant recovery-target config fields."""
    base = _seeded_context(last_7d_paise=last_7d_paise)
    return SalesRecoveryContext(
        tenant_id=base.tenant_id,
        run_id=base.run_id,
        user_request=base.user_request,
        trigger_reason=base.trigger_reason,
        business_profile=base.business_profile,
        customer_ledger_summary=base.customer_ledger_summary,
        recent_campaigns=base.recent_campaigns,
        attribution_snapshot=base.attribution_snapshot,
        pending_owner_inputs=base.pending_owner_inputs,
        meta=base.meta,
        data_completeness=base.data_completeness,
        recovery_target_multiplier=multiplier,
        recovery_target_floor_paise=floor_paise,
    )


def test_target_uses_multiplier_when_above_floor() -> None:
    """(VT-164-e1) last_7d * multiplier > floor → multiplier dominates.
    last_7d=80_000, multiplier=1.1 → round(88_000) = 88_000 > 50_000."""
    ctx = _seeded_context_with_config(last_7d_paise=80_000, multiplier=1.1, floor_paise=50_000)
    rendered = serialize_bundle_for_prompt(ctx)
    assert "target_recovered_paise: 88000" in rendered


def test_target_uses_floor_when_7d_low() -> None:
    """(VT-164-e2) last_7d * multiplier < floor → floor dominates.
    last_7d=10_000, multiplier=1.1 → round(11_000) < 50_000 → 50_000."""
    ctx = _seeded_context_with_config(last_7d_paise=10_000, multiplier=1.1, floor_paise=50_000)
    rendered = serialize_bundle_for_prompt(ctx)
    assert "target_recovered_paise: 50000" in rendered


def test_target_override_multiplier_and_floor() -> None:
    """(VT-164-e3) Custom multiplier + floor → override reflected.
    last_7d=80_000, multiplier=1.5 → round(120_000) > 100_000 → 120_000."""
    ctx = _seeded_context_with_config(last_7d_paise=80_000, multiplier=1.5, floor_paise=100_000)
    rendered = serialize_bundle_for_prompt(ctx)
    assert "target_recovered_paise: 120000" in rendered


def test_target_floor_dominates_at_override() -> None:
    """(VT-164-e3b) Custom floor larger than multiplier result → floor wins.
    last_7d=80_000, multiplier=1.5 → 120_000 but floor=200_000 → 200_000."""
    ctx = _seeded_context_with_config(last_7d_paise=80_000, multiplier=1.5, floor_paise=200_000)
    rendered = serialize_bundle_for_prompt(ctx)
    assert "target_recovered_paise: 200000" in rendered


def test_target_zero_last_7d_uses_floor() -> None:
    """(VT-164-e4) Zero last_7d → multiplier produces 0 → floor wins."""
    ctx = _seeded_context_with_config(last_7d_paise=0, multiplier=1.1, floor_paise=50_000)
    rendered = serialize_bundle_for_prompt(ctx)
    assert f"target_recovered_paise: {_DEFAULT_TARGET_RECOVERED_PAISE}" in rendered


def test_default_context_fields_reproduce_pre_vt164_behaviour() -> None:
    """(VT-164 behavioural-change guard) Default-config context must produce
    the same target as the pre-VT-164 inline formula:
    max(int(last_7d * 1.1), 50_000). last_7d=100_000 → 110_000."""
    ctx = _seeded_context(last_7d_paise=100_000)
    rendered = serialize_bundle_for_prompt(ctx)
    # Pre-VT-164: int(100_000 * 1.1) = 110_000 (int truncates, identical to round here)
    assert "target_recovered_paise: 110000" in rendered


def test_salesrecoverycontext_default_fields() -> None:
    """(VT-164) SalesRecoveryContext default fields match module constants."""
    from uuid import uuid4
    ctx = SalesRecoveryContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        user_request="test",
    )
    assert ctx.recovery_target_multiplier == _DEFAULT_RECOVERY_TARGET_MULTIPLIER
    assert ctx.recovery_target_floor_paise == _DEFAULT_TARGET_RECOVERED_PAISE
