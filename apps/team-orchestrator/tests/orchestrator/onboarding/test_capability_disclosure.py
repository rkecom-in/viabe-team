"""D2 (Fazal 2026-07-12 #2) — capability-disclosure detector + copy for an unsupported paid ad-boost.

Pure detector + copy coverage. The detector's opt-out/DSR guard imports ``pre_filter_gate`` (dbos)
and the emission-gate tokenizer/₹-regex (agent pkg -> anthropic); importorskip so the dep-less smoke
SKIPS cleanly (the runner-wired net behavior is validated on deployed dev)."""

from __future__ import annotations

import pytest

pytest.importorskip("dbos")
pytest.importorskip("anthropic")

from orchestrator.onboarding import capability_disclosure as cd  # noqa: E402


# ----------------------------- detector: PAID ad-boost fires -----------------------------
def test_paid_boost_asks_fire() -> None:
    for msg in [
        "mere last Instagram post ko 500 rupaye dekar boost kar do",   # sr_spend_ceiling
        "boost my instagram post for ₹500",
        "run a facebook ad for me, budget 5000",
        "chalo google ads laga do 5000 ka",                            # bare amount + place-verb combo
        "meta pe ad chalao, 2000 rupaye",
    ]:
        assert cd.detect_unsupported_action(msg) is True, msg


# ----------------------------- detector: must NOT fire -----------------------------------
def test_supported_winback_does_not_fire() -> None:
    # A WhatsApp win-back is SUPPORTED (owned channel, no ad platform, no spend) -> D3 handles it.
    assert cd.detect_unsupported_action("run a win-back campaign for my lapsed customers") is False


def test_platform_without_money_does_not_fire() -> None:
    # Platform token but no spend intent -> not a paid-ad ask.
    assert cd.detect_unsupported_action("my instagram post got 500 likes today") is False
    assert cd.detect_unsupported_action("boost my sales this month") is False


def test_optout_dsr_never_read_as_paid_ad() -> None:
    for msg in ["stop all my ads", "please delete my facebook data", "STOP"]:
        assert cd.detect_unsupported_action(msg) is False, msg


def test_empty_blank_none_do_not_fire() -> None:
    assert cd.detect_unsupported_action("") is False
    assert cd.detect_unsupported_action("   ") is False
    assert cd.detect_unsupported_action(None) is False  # type: ignore[arg-type]


# ----------------------------- copy: honest + money-safe ---------------------------------
def test_disclosure_copy_is_honest_and_spend_safe() -> None:
    from orchestrator.agent.emission_gate import contains_spend_completion_claim

    for locale in ("en", "hi"):
        body = cd.compose_capability_disclosure(locale=locale)
        # NEVER a same-turn spend/boost completion claim.
        assert contains_spend_completion_claim(body) is False, locale
        # States the limit + pivots to the supported win-back (actionable, not a dead end).
        assert "win-back" in body


def test_disclosure_en_states_the_limit() -> None:
    body = cd.compose_capability_disclosure(locale="en")
    low = body.lower()
    assert "can't" in low or "cannot" in low or "isn't something i can" in low
    assert "boost" in low


# ----------------------- VT-681 phase 3: registry-gated auto-retire -----------------------
def test_net_retires_when_ad_boost_graduates(monkeypatch) -> None:
    """The day marketing.paid_ad_boost flips live in the capability registry, the hand-rolled
    decline must STOP firing — no stale disclosure over a real feature."""
    import orchestrator.capability.registry as reg

    monkeypatch.setitem(
        reg.CAPABILITY_REGISTRY, "marketing.paid_ad_boost",
        reg.CapabilitySpec(
            key="marketing.paid_ad_boost", lane="marketing", effect_class="campaign",
            mode="live", policy_rail=True, summary="graduated (test)",
            verifier="real_send_evidence", environments=reg.KNOWN_ENVS,
        ),
    )
    assert cd.detect_unsupported_action("boost my instagram post for ₹500") is False


def test_net_fires_while_registry_says_disabled() -> None:
    """Baseline re-assert THROUGH the registry gate: shipped registry declares it disabled, so
    the detector still fires on a paid-boost ask (same case as test_paid_boost_asks_fire)."""
    assert cd.detect_unsupported_action("boost my instagram post for ₹500") is True
