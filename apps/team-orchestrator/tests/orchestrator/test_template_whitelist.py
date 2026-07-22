"""VT-683 P4 — the OWNER-template whitelist decision (SHADOW-first) + the enforce flag.

Tests the pure ``_owner_template_whitelist_action`` decision + the whitelist SET + the feature flag
directly (dep-guarded, no DB / Twilio). The full ``send_template_message`` wiring (a shadow send is
byte-identical; an enforce send returns a failed SendResult) is covered in ``test_twilio_send.py``
under DATABASE_URL."""

from __future__ import annotations

import pytest

# twilio_send imports dbos/twilio/psycopg at module top; guard so the dep-less smoke skips cleanly.
pytest.importorskip("dbos")
pytest.importorskip("twilio")
pytest.importorskip("psycopg")

from orchestrator.utils import twilio_send as ts  # noqa: E402


def test_whitelist_set_is_the_row_contract() -> None:
    """The exact whitelist shipped: welcome4 / wakeup2 / error_handler + the two belt approvals."""
    assert ts.OWNER_TEMPLATE_WHITELIST == frozenset(
        {
            "team_welcome4",
            "team_wakeup2",
            "team_error_handler",
            "team_weekly_approval",
            "team_agent_draft_approval",
        }
    )
    assert ts.TEMPLATE_NOT_WHITELISTED == "template_not_whitelisted"


@pytest.mark.parametrize("name", sorted(ts.OWNER_TEMPLATE_WHITELIST))
def test_whitelisted_owner_template_is_allowed(monkeypatch, name) -> None:
    """A whitelisted OWNER template is 'allow' regardless of the enforce flag (byte-identical)."""
    monkeypatch.setenv("TEAM_TEMPLATE_WHITELIST_ENFORCE", "1")
    assert ts._owner_template_whitelist_action(name, "owner") == "allow"


def test_nonwhitelisted_owner_template_shadow_when_flag_off(monkeypatch) -> None:
    monkeypatch.delenv("TEAM_TEMPLATE_WHITELIST_ENFORCE", raising=False)
    assert ts._owner_template_whitelist_action("team_status_ping", "owner") == "shadow"
    assert ts._owner_template_whitelist_action("team_monthly_report", "owner") == "shadow"
    assert ts._owner_template_whitelist_action("team_reengage", "owner") == "shadow"


def test_nonwhitelisted_owner_template_block_when_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("TEAM_TEMPLATE_WHITELIST_ENFORCE", "1")
    assert ts._owner_template_whitelist_action("team_status_ping", "owner") == "block"
    assert ts._owner_template_whitelist_action("team_monthly_report", "owner") == "block"


def test_customer_audience_never_gated(monkeypatch) -> None:
    """Customer-audience templates are out of scope of the OWNER whitelist — always 'allow', even
    with enforce ON (they have their own customer_send_context choke, VT-460 gap c)."""
    monkeypatch.setenv("TEAM_TEMPLATE_WHITELIST_ENFORCE", "1")
    # team_winback_simple is audience:customer and NOT on the owner whitelist — still 'allow'.
    assert ts._owner_template_whitelist_action("team_winback_simple", "customer") == "allow"
    # team_weekly_approval is audience:customer in the yaml — never gated by the owner whitelist.
    assert ts._owner_template_whitelist_action("team_weekly_approval", "customer") == "allow"


def test_blank_audience_never_gated(monkeypatch) -> None:
    """An audience-blind path (empty audience) is not an OWNER send — never gated."""
    monkeypatch.setenv("TEAM_TEMPLATE_WHITELIST_ENFORCE", "1")
    assert ts._owner_template_whitelist_action("whatever", "") == "allow"


def test_enforce_flag_reads_env(monkeypatch) -> None:
    from orchestrator.feature_flags import template_whitelist_enforce_enabled

    monkeypatch.delenv("TEAM_TEMPLATE_WHITELIST_ENFORCE", raising=False)
    assert template_whitelist_enforce_enabled() is False
    for truthy in ("1", "true", "on", "yes"):
        monkeypatch.setenv("TEAM_TEMPLATE_WHITELIST_ENFORCE", truthy)
        assert template_whitelist_enforce_enabled() is True
    monkeypatch.setenv("TEAM_TEMPLATE_WHITELIST_ENFORCE", "0")
    assert template_whitelist_enforce_enabled() is False
