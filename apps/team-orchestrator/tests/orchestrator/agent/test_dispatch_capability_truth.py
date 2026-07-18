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


# --- VT-686: the AGENT-DIRECTORY block — same insert-site family, tenant-INDEPENDENT (the
# directory describes the agent roster, not per-tenant state) ------------------------------------


def test_agent_directory_builder_returns_none_on_registry_read_error(monkeypatch) -> None:
    """A ``default_registry()`` read failure is best-effort -> None, never raises."""
    import orchestrator.agent_framework.registration as registration_mod

    def _boom():
        raise RuntimeError("registry unavailable in unit test")

    monkeypatch.setattr(registration_mod, "default_registry", _boom)
    assert dp._build_agent_directory_block() is None


def test_agent_directory_builder_returns_none_on_render_error(monkeypatch) -> None:
    """A ``render_agent_directory`` failure is best-effort -> None, never raises."""
    import orchestrator.agent_framework.directory as directory_mod

    def _boom(_registry):
        raise RuntimeError("render exploded in unit test")

    monkeypatch.setattr(directory_mod, "render_agent_directory", _boom)
    assert dp._build_agent_directory_block() is None


def test_agent_directory_builder_returns_none_for_empty_registry(monkeypatch) -> None:
    """An empty (or all-incomplete) registry renders "" -> the builder maps that to None."""
    import orchestrator.agent_framework.registration as registration_mod

    class _EmptyRegistry:
        def names(self):
            return []

    monkeypatch.setattr(registration_mod, "default_registry", lambda: _EmptyRegistry())
    assert dp._build_agent_directory_block() is None


def test_agent_directory_builder_returns_block_content(monkeypatch) -> None:
    """A registry holding one VT-686-complete module renders its identity card into the block."""
    from types import SimpleNamespace

    import orchestrator.agent_framework.registration as registration_mod
    from orchestrator.agent_framework import AgentBrief, AgentManifest, AgentRole

    manifest = AgentManifest(
        name="sales_recovery",
        version="1.0.0",
        roles=frozenset({AgentRole.PROPOSER}),
        description="x",
        category="Sales",
        tags=frozenset({"winback"}),
        brief=AgentBrief(
            what_it_does="Wins back lapsed customers.",
            actions=("draft_campaign",),
            business_activities=("win back lapsed customers",),
            when_to_use="Route here for lapsed-customer asks.",
            limits=("does not send directly — arms the approval",),
        ),
    )

    class _FakeRegistry:
        def names(self):
            return ["sales_recovery"]

        def get(self, _name):
            return SimpleNamespace(manifest=manifest)

    monkeypatch.setattr(registration_mod, "default_registry", lambda: _FakeRegistry())

    block = dp._build_agent_directory_block()

    assert block is not None
    assert "### sales_recovery [Sales]" in block
    assert "Wins back lapsed customers." in block
    assert "Route here for lapsed-customer asks." in block
    assert "does not send directly" in block
