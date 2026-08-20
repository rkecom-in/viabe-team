"""VT-735 — the call-class -> service-tier mapping (`.viabe/model-tier-policy.md`, ratified).

The tests that matter here are the ones that pin the SAFETY direction of each rule, not the happy
path: an unclassified site must not drift onto the slow tier, a gate must not flex, and a Fast
degrade must never land on Flex.
"""

from __future__ import annotations

import pytest

# The policy module itself is dependency-free, but importing it through `orchestrator.llm`
# executes that package's __init__, which pulls provider.py and therefore langchain_core. The
# dep-less smoke job runs without it, so guard the import rather than fail collection there.
pytest.importorskip("langchain_core")

from orchestrator.llm import service_tier_policy as policy  # noqa: E402 — after the skip guard


def _mode(monkeypatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("TEAM_GPT_FLEX", raising=False)
    else:
        monkeypatch.setenv("TEAM_GPT_FLEX", value)


# --- the env switch -------------------------------------------------------------------------

def test_flex_mode_defaults_to_background(monkeypatch):
    _mode(monkeypatch, None)
    assert policy.flex_mode() == "background"


def test_flex_mode_falls_back_on_garbage(monkeypatch):
    _mode(monkeypatch, "yes-please")
    assert policy.flex_mode() == "background"


@pytest.mark.parametrize("value", ["off", "background", "all"])
def test_flex_mode_accepts_the_three_documented_values(monkeypatch, value):
    _mode(monkeypatch, value)
    assert policy.flex_mode() == value


# --- background -> flex ---------------------------------------------------------------------

def test_background_site_flexes_by_default(monkeypatch):
    _mode(monkeypatch, None)
    assert policy.resolve_service_tier("week_plan_revision") == policy.FLEX


def test_flex_off_forces_standard_everywhere(monkeypatch):
    _mode(monkeypatch, "off")
    assert policy.resolve_service_tier("week_plan_revision") == policy.STANDARD


def test_interactive_site_never_flexes_in_background_mode(monkeypatch):
    """The whole point of the policy: an owner waiting on a turn does not get the slow tier."""
    _mode(monkeypatch, "background")
    assert policy.resolve_service_tier("turn_brain") == policy.STANDARD


def test_mode_all_does_flex_an_interactive_site(monkeypatch):
    """`all` is a force-test posture, and it is honest about what it does."""
    _mode(monkeypatch, "all")
    assert policy.resolve_service_tier("turn_brain") == policy.FLEX


# --- the safe default -----------------------------------------------------------------------

def test_unknown_call_site_resolves_standard_not_flex(monkeypatch):
    """An unclassified site costs full price; it must never silently become slow."""
    _mode(monkeypatch, "background")
    assert policy.resolve_service_tier("some_site_nobody_classified") == policy.STANDARD


def test_none_call_site_resolves_standard(monkeypatch):
    _mode(monkeypatch, "background")
    assert policy.resolve_service_tier(None) == policy.STANDARD


# --- never-flex outranks everything ---------------------------------------------------------

def test_gate_scoring_never_flexes_even_in_all_mode(monkeypatch):
    """A gate that flakes on capacity-unavailable is a gate nobody trusts."""
    _mode(monkeypatch, "all")
    assert policy.resolve_service_tier("self_evaluate_gate") == policy.STANDARD


# --- fast -----------------------------------------------------------------------------------

def test_approval_resolution_is_fast(monkeypatch):
    _mode(monkeypatch, "background")
    assert policy.resolve_service_tier("classify_owner_message") == policy.FAST


def test_fast_survives_flex_off(monkeypatch):
    """Fast is a safety spend on the approval race; turning flex off must not disarm it."""
    _mode(monkeypatch, "off")
    assert policy.resolve_service_tier("classify_owner_message") == policy.FAST


def test_exhausted_fast_budget_degrades_to_standard_never_flex(monkeypatch):
    _mode(monkeypatch, "all")  # `all` would flex anything else — a decisive moment must not slow.
    tier = policy.resolve_service_tier(
        "classify_owner_message", tenant_id="t-1", fast_budget_check=lambda _t: False
    )
    assert tier == policy.STANDARD


def test_fast_budget_failure_fails_OPEN(monkeypatch):
    """A cost control must not become a correctness risk on the VT-734 race path."""
    _mode(monkeypatch, "background")

    def boom(_tenant):
        raise RuntimeError("budget store down")

    assert policy.resolve_service_tier(
        "classify_owner_message", tenant_id="t-1", fast_budget_check=boom
    ) == policy.FAST


# --- api + ledger mapping -------------------------------------------------------------------

def test_standard_omits_the_api_field():
    """'standard' is not an OpenAI enum value — the field must be omitted, not sent."""
    assert policy.api_service_tier(policy.STANDARD) is None


def test_flex_and_fast_are_sent_verbatim():
    # Verified 2026-08-06 against https://developers.openai.com/api/docs/pricing — the API takes
    # both 'priority' and 'fast'; 'priority' was renamed to Fast mode on 2026-07-30.
    assert policy.api_service_tier(policy.FLEX) == "flex"
    assert policy.api_service_tier(policy.FAST) == "fast"


def test_billing_tier_records_flex_and_fast_but_never_auto():
    assert policy.billing_tier_for(policy.FLEX) == policy.FLEX
    assert policy.billing_tier_for(policy.FAST) == policy.FAST
    # 'auto' lets OpenAI choose server-side; we cannot know the billed rate, so record full price.
    assert policy.billing_tier_for("auto") == policy.STANDARD


def test_fast_and_never_flex_lists_do_not_overlap():
    """A site in both lists would make the ordering in resolve_service_tier load-bearing trivia."""
    assert not (policy._FAST_CALL_SITES & policy._NEVER_FLEX_CALL_SITES)
    assert not (policy._BACKGROUND_CALL_SITES & policy._FAST_CALL_SITES)
