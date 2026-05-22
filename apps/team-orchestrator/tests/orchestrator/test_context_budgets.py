"""VT-3.4 PR 2/3 — Context Composer budget/truncation tests (§4.4).

Pure-Python. The safe-empty ``_build_*`` stubs (CL-190) never overflow, so
each test monkeypatches the relevant builder to inject oversized data — the
whitebox approach the context_builder docstring prescribes.

Effective cap = total_cap (8000) * safety margin (0.8) = 6400 tokens.
Per-section budgets in context_budgets.yaml are ADVISORY — they key the
truncation ORDER, but only the total is enforced (CL-204 tech debt).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

import orchestrator.context_builder as cb
from orchestrator.context_builder import (
    BusinessProfile,
    CampaignSnapshot,
    ContextOverflowError,
    OwnerInput,
    _estimate_tokens,
    build_sales_recovery_context,
)


@pytest.fixture(autouse=True)
def _stub_db_backed_builders(monkeypatch: pytest.MonkeyPatch) -> None:
    """VT-138: ``_build_recent_campaigns`` is now a live DB read. Tests
    in this file exercise the bundle constructor's truncation /
    budget logic with synthetic monkeypatched data, no DB required.
    Force the DB-backed builder back to safe-empty unless the
    individual test explicitly overrides it.
    """
    monkeypatch.setattr(cb, "_build_recent_campaigns", lambda tid: ([], False))


_EFFECTIVE_CAP = 6400


def _owner_inputs(n: int, content_len: int) -> list[OwnerInput]:
    return [
        OwnerInput(
            input_id=uuid4(),
            received_at=datetime.now(UTC),
            content="x" * content_len,
        )
        for _ in range(n)
    ]


def _campaigns(n: int) -> list[CampaignSnapshot]:
    return [
        CampaignSnapshot(
            campaign_id=uuid4(),
            status="proposed",
            recovered_paise=0,
            proposed_at=datetime.now(UTC),
        )
        for _ in range(n)
    ]


def test_overflow_truncates_to_fit_total_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.4 — a bundle sized past the total cap is truncated until it fits."""
    monkeypatch.setattr(
        cb, "_build_pending_owner_inputs",
        lambda tid: (_owner_inputs(300, 100), False),
    )
    bundle = build_sales_recovery_context(uuid4(), uuid4(), "weekly_cadence", "test request")

    # Total cap enforced.
    assert bundle.meta.token_count <= _EFFECTIVE_CAP
    # Oldest-first partial drop: some survived, some dropped.
    assert 0 < len(bundle.pending_owner_inputs) < 300


def test_truncation_order_owner_inputs_before_campaigns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.4 — per-section budgets govern truncation ORDER: owner inputs are
    trimmed before campaigns are touched.

    Surviving owner inputs prove campaigns were never reached — the loop only
    trims campaigns once ``pending_owner_inputs`` is exhausted.
    """
    monkeypatch.setattr(
        cb, "_build_pending_owner_inputs",
        lambda tid: (_owner_inputs(300, 100), False),
    )
    monkeypatch.setattr(
        cb, "_build_recent_campaigns",
        lambda tid: (_campaigns(10), False),
    )
    bundle = build_sales_recovery_context(uuid4(), uuid4(), "weekly_cadence", "test request")

    assert len(bundle.pending_owner_inputs) > 0
    assert len(bundle.recent_campaigns) == 10  # untouched
    assert bundle.meta.token_count <= _EFFECTIVE_CAP


def test_unbounded_section_raises_context_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.4 — when a non-truncatable field alone exceeds the cap, the
    constructor raises ContextOverflowError after maximum truncation."""
    monkeypatch.setattr(
        cb, "_build_business_profile",
        lambda tid: (BusinessProfile(business_name="x" * 40000), False),
    )
    with pytest.raises(ContextOverflowError):
        build_sales_recovery_context(uuid4(), uuid4(), "weekly_cadence", "test request")


def test_meta_token_count_is_sum_of_five_content_sections() -> None:
    """§4.4 — meta.token_count is the sum of the five CONTENT sections only.

    It excludes the meta + slack reservations in context_budgets.yaml
    (CL-184 / CL-204) — a downstream reader comparing it to 8000 compares a
    subset to the total.
    """
    bundle = build_sales_recovery_context(uuid4(), uuid4(), "weekly_cadence", "test request")

    expected = (
        _estimate_tokens(bundle.business_profile)
        + _estimate_tokens(bundle.customer_ledger_summary)
        + _estimate_tokens(bundle.recent_campaigns)
        + _estimate_tokens(bundle.attribution_snapshot)
        + _estimate_tokens(bundle.pending_owner_inputs)
    )
    assert bundle.meta.token_count == expected
