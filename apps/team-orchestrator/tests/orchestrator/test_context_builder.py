"""VT-3.4 PR 2/3 — Context Composer bundle-constructor tests (§4.1/§4.2/§4.3).

Pure-Python: exercises ``build_sales_recovery_context`` with the current
safe-empty ``_build_*`` stubs (CL-190 — L1 KG / L2 episodic / campaigns /
owner_inputs substrates absent). No DB, no LLM.

Brief §4.1 listed the bundle fields loosely ("pending_owner_questions",
"slack"). The asserted set below is the ACTUAL ``SalesRecoveryContext``
dataclass — ``pending_owner_inputs`` (not _questions), and there is no
``slack`` field (``slack`` is a reserved-headroom line in
context_budgets.yaml, not a bundle section). CL-209 correction.
"""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

import orchestrator.context_builder as cb
from orchestrator.context_builder import (
    AttributionSnapshot,
    BusinessProfile,
    L3Priors,
    L4Skills,
    LedgerSummary,
    SalesRecoveryContext,
    build_sales_recovery_context,
)


@pytest.fixture(autouse=True)
def _stub_db_backed_builders(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_build_recent_campaigns`` (VT-138), ``_build_pending_owner_inputs``
    (VT-146) and ``_build_ledger_summary`` (VT-67, L2) are all live DB reads
    via ``tenant_connection``. The pure-Python tests in this file
    exercise the bundle constructor's dispatcher + safe-empty contract;
    they must not require a DB. Monkeypatch the DB-backed builders
    back to safe-empty for every test here. ``_build_ledger_summary`` returns
    flag ``True`` (empty-but-live) to mirror the real L2 read on a fresh tenant.

    The DB read paths themselves are covered by the substrate-fixture
    suites (campaigns/owner_inputs readpath + ``knowledge/test_l2_episodic.py``).
    """
    monkeypatch.setattr(cb, "_build_recent_campaigns", lambda tid: ([], False))
    monkeypatch.setattr(
        cb, "_build_pending_owner_inputs", lambda tid: ([], False)
    )
    monkeypatch.setattr(cb, "_build_ledger_summary", lambda tid: (LedgerSummary(), True))
    # VT-490: _build_dormant_cohort is a live DB read (CL-425 gate + tenant_connection).
    # Stub it safe-empty so the pure-Python tests here need no DB / pool.
    monkeypatch.setattr(cb, "_build_dormant_cohort", lambda tid: ([], False))
    monkeypatch.setattr(cb, "_build_l3_priors", lambda tid, rid: (L3Priors(), False))
    monkeypatch.setattr(cb, "_build_l4_skills", lambda tid, req: (L4Skills(), False))

# §4.1 — the actual SalesRecoveryContext dataclass fields.
# Exec-6.85: ``user_request`` joins the bundle so the specialist receives
# the orchestrator-supplied owner message inside the same payload.
_EXPECTED_FIELDS = {
    "tenant_id",
    "run_id",
    "user_request",
    "trigger_reason",
    "business_profile",
    "customer_ledger_summary",
    "dormant_cohort",  # VT-490
    "recent_campaigns",
    "attribution_snapshot",
    "pending_owner_inputs",
    "l3_priors",
    "l4_skills",
    # VT-607 (Loop Package 6): the manager loop's own step framing, threaded through
    "manager_desired_outcome",
    "manager_acceptance_criteria",
    "meta",
    "data_completeness",
    # VT-164: per-tenant recovery-target config fields
    "recovery_target_multiplier",
    "recovery_target_floor_paise",
}


def test_build_sales_recovery_context_returns_expected_top_level_fields() -> None:
    """§4.1 — the constructor returns a SalesRecoveryContext with exactly the
    documented top-level fields."""
    bundle = build_sales_recovery_context(
        uuid4(), uuid4(), "weekly_cadence", "recover dormant customers"
    )

    assert isinstance(bundle, SalesRecoveryContext)
    assert {f.name for f in fields(bundle)} == _EXPECTED_FIELDS


def test_build_sales_recovery_context_defaults_manager_framing_safe_empty() -> None:
    """VT-607 (Loop Package 6): a non-loop caller (no manager_desired_outcome/
    manager_acceptance_criteria kwargs) gets the CL-190 safe-empty default — never a crash, never
    a stray None."""
    bundle = build_sales_recovery_context(
        uuid4(), uuid4(), "weekly_cadence", "recover dormant customers"
    )
    assert bundle.manager_desired_outcome == ""
    assert bundle.manager_acceptance_criteria == []


def test_build_sales_recovery_context_threads_manager_framing() -> None:
    """VT-607 (Loop Package 6): when the manager loop supplies its own step framing, the bundle
    carries it through unchanged."""
    bundle = build_sales_recovery_context(
        uuid4(), uuid4(), "weekly_cadence", "recover dormant customers",
        manager_desired_outcome="win back the dormant cohort within budget",
        manager_acceptance_criteria=["cohort grounded in real customers", "expected recovery cited"],
    )
    assert bundle.manager_desired_outcome == "win back the dormant cohort within budget"
    assert bundle.manager_acceptance_criteria == [
        "cohort grounded in real customers", "expected recovery cited",
    ]


def test_build_sales_recovery_context_safe_empty_when_substrates_absent() -> None:
    """§4.2 — for a fresh tenant with no data, every section is its empty form.

    business_profile (L1) + attribution_snapshot remain CL-190 safe-empty +
    incomplete (substrates absent). customer_ledger_summary (L2, VT-67) now reads
    the LIVE episodic_events substrate (mig 083): empty for a fresh tenant, but
    the completeness flag is TRUE because the read ran (no placeholder field).
    """
    bundle = build_sales_recovery_context(
        uuid4(), uuid4(), "weekly_cadence", "recover dormant customers"
    )

    assert bundle.business_profile == BusinessProfile()
    assert bundle.customer_ledger_summary == LedgerSummary()  # empty: no L2 events yet
    assert bundle.dormant_cohort == []  # VT-490: no lapsed candidates (stubbed)
    assert bundle.recent_campaigns == []
    assert bundle.pending_owner_inputs == []
    assert bundle.attribution_snapshot == AttributionSnapshot()
    assert bundle.data_completeness == {
        "business_profile": False,
        "customer_ledger_summary": True,  # VT-67: L2 read ran (empty-but-live)
        "dormant_cohort": False,  # VT-490: gate stubbed safe-empty
        "recent_campaigns": False,
        "attribution_snapshot": False,
        "pending_owner_inputs": False,
        "l3_priors": False,  # VT-69: no prior (stubbed unavailable)
        "l4_skills": False,  # VT-70: no corpus docs (stubbed unavailable)
    }


def test_build_sales_recovery_context_no_cross_tenant_leak() -> None:
    """§4.3 — a bundle built for tenant A carries no tenant B identifier.

    NOTE (CL-190): real cross-tenant ROW isolation — ``assert_tenant_scoped``
    on raw DB reads inside ``_build_*`` — is deferred until substrates exist.
    With safe-empty stubs the only tenant-bearing field is ``tenant_id``; this
    test guards the dispatcher's tenant attribution, not end-to-end isolation.
    """
    tenant_a = uuid4()
    tenant_b = uuid4()

    bundle_a = build_sales_recovery_context(
        tenant_a, uuid4(), "owner_initiated", "recover lapsed buyers"
    )

    assert bundle_a.tenant_id == tenant_a
    serialised = json.dumps(asdict(bundle_a), default=str)
    assert str(tenant_b) not in serialised


def test_build_sales_recovery_context_requires_non_empty_user_request() -> None:
    """Exec-6.85: bundle constructor fails loud on empty / whitespace
    ``user_request`` — the orchestrator must never spawn the specialist
    without a real owner message. Mirrors the existing tenant_id / run_id
    fail-loud discipline."""
    with pytest.raises(ValueError, match="user_request"):
        build_sales_recovery_context(uuid4(), uuid4(), "weekly_cadence", "")
    with pytest.raises(ValueError, match="user_request"):
        build_sales_recovery_context(uuid4(), uuid4(), "weekly_cadence", "   ")
