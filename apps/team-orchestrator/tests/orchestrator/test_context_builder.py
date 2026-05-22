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
    LedgerSummary,
    SalesRecoveryContext,
    build_sales_recovery_context,
)


@pytest.fixture(autouse=True)
def _stub_db_backed_builders(monkeypatch: pytest.MonkeyPatch) -> None:
    """VT-138: ``_build_recent_campaigns`` is now a live DB read via
    ``tenant_connection``. The pure-Python tests in this file exercise
    the bundle constructor's dispatcher + safe-empty contract; they
    must not require a DB. Monkeypatch the DB-backed builder back to
    safe-empty for every test here.

    The DB read path itself is covered by the substrate-fixture suite
    in ``test_context_builder_campaigns_readpath.py``.
    """
    monkeypatch.setattr(cb, "_build_recent_campaigns", lambda tid: ([], False))

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
    "recent_campaigns",
    "attribution_snapshot",
    "pending_owner_inputs",
    "meta",
    "data_completeness",
}


def test_build_sales_recovery_context_returns_expected_top_level_fields() -> None:
    """§4.1 — the constructor returns a SalesRecoveryContext with exactly the
    documented top-level fields."""
    bundle = build_sales_recovery_context(
        uuid4(), uuid4(), "weekly_cadence", "recover dormant customers"
    )

    assert isinstance(bundle, SalesRecoveryContext)
    assert {f.name for f in fields(bundle)} == _EXPECTED_FIELDS


def test_build_sales_recovery_context_safe_empty_when_substrates_absent() -> None:
    """§4.2 — with L1 KG + L2 episodic + campaigns/owner_inputs tables absent
    (CL-190), every section is its safe-empty fallback and data_completeness
    reports every section incomplete."""
    bundle = build_sales_recovery_context(
        uuid4(), uuid4(), "weekly_cadence", "recover dormant customers"
    )

    assert bundle.business_profile == BusinessProfile()
    assert bundle.customer_ledger_summary == LedgerSummary()
    assert bundle.recent_campaigns == []
    assert bundle.pending_owner_inputs == []
    assert bundle.attribution_snapshot == AttributionSnapshot()
    assert bundle.data_completeness == {
        "business_profile": False,
        "customer_ledger_summary": False,
        "recent_campaigns": False,
        "attribution_snapshot": False,
        "pending_owner_inputs": False,
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
