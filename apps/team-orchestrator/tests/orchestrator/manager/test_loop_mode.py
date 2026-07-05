"""VT-606 — TEAM_MANAGER_LOOP_MODE flag. Pure, no DB, no LLM."""

from __future__ import annotations

import pytest

from orchestrator.manager.loop_mode import get_loop_mode, is_enforce, is_legacy, is_shadow


def test_default_is_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEAM_MANAGER_LOOP_MODE", raising=False)
    assert get_loop_mode() == "legacy"
    assert is_legacy()


@pytest.mark.parametrize("value", ["shadow", "SHADOW", " Shadow "])
def test_shadow_case_and_whitespace_insensitive(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", value)
    assert get_loop_mode() == "shadow"
    assert is_shadow()


def test_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", "enforce")
    assert get_loop_mode() == "enforce"
    assert is_enforce()


@pytest.mark.parametrize("bogus", ["", "typo", "ENFORCED", "shadowing"])
def test_unrecognized_value_fails_closed_to_legacy(monkeypatch: pytest.MonkeyPatch, bogus: str) -> None:
    """Unknown/malformed values must NEVER silently upgrade to a more-capable mode."""
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", bogus)
    assert get_loop_mode() == "legacy"
    assert is_legacy()


def test_explicit_mode_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_MANAGER_LOOP_MODE", "enforce")
    assert is_legacy("legacy") is True
    assert is_shadow("legacy") is False
    assert is_enforce("legacy") is False
