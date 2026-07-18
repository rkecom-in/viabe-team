"""VT-681 phase 3 — the capability-truth context block: pure renderer buckets + the best-effort
builder's fail-soft contract. The dispatch module pulls langchain/dbos at import; importorskip so
the dep-less smoke skips cleanly (block PRESENCE on a live turn is proven by the ×3 dev re-drive)."""

from __future__ import annotations

import pytest

pytest.importorskip("dbos")
pytest.importorskip("anthropic")
pytest.importorskip("langgraph")

from orchestrator.agent import dispatch as dp  # noqa: E402
from orchestrator.capability.registry import ResolvedCapability  # noqa: E402


def _rc(key: str, mode: str, available: bool) -> ResolvedCapability:
    return ResolvedCapability(key=key, mode=mode, available=available, reasons=("t",))


def test_render_buckets_all_four() -> None:
    text = dp._render_capability_truth([
        _rc("sales_recovery.winback_send", "live", True),
        _rc("integration.gst_verify", "live", False),           # activation bar unmet
        _rc("finance.advice", "advisory", True),
        _rc("marketing.paid_ad_boost", "disabled", False),
    ])
    assert text is not None
    assert "Live now: win-back campaigns" in text
    assert "Not yet available for this owner" in text and "GST verification" in text
    assert "Prepare-only" in text and "cash-flow" in text
    assert "Not supported: paid ad boosts" in text
    assert "never promise" in text.lower()


def test_render_skips_undisplayable_keys_and_empties_to_none() -> None:
    # onboarding.conduct_journey is deliberately not promisable (no display name) — and an
    # all-undisplayable list renders NO block at all.
    assert dp._render_capability_truth([_rc("onboarding.conduct_journey", "live", True)]) is None
    assert dp._render_capability_truth([]) is None


def test_render_disabled_wins_over_availability_flag() -> None:
    # A disabled capability is bucketed 'Not supported' even if a caller hands available=True
    # by mistake — mode is the declared truth.
    text = dp._render_capability_truth([_rc("marketing.paid_ad_boost", "disabled", True)])
    assert text is not None and "Not supported" in text and "Live now" not in text


def test_builder_fail_soft_returns_none(monkeypatch) -> None:
    import orchestrator.db as db_pkg

    def _boom(_tid):
        raise RuntimeError("no db in unit test")

    monkeypatch.setattr(db_pkg, "tenant_connection", _boom)
    from uuid import uuid4

    assert dp._build_capability_truth_block(uuid4()) is None
