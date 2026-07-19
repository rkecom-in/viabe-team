"""VT-686 live wiring — register_all_modules: every first-party module lands in the default
registry idempotently, and the Manager's directory renders ALL identity cards afterward."""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("langchain_core")

from orchestrator.agent_framework.modules import register_all_modules  # noqa: E402


_EXPECTED = {
    "sales_recovery", "onboarding_tools", "integration_tools", "common_tools",
    "compliance_tools",
}


def test_registers_all_five_and_is_idempotent() -> None:
    first = set(register_all_modules())
    assert first == _EXPECTED
    second = set(register_all_modules())  # duplicate entry → re-enter, never crash
    assert second == _EXPECTED


def test_directory_renders_every_card_after_boot_wiring() -> None:
    register_all_modules()
    from orchestrator.agent_framework.directory import render_agent_directory
    from orchestrator.agent_framework.registration import default_registry

    text = render_agent_directory(default_registry())
    for name in _EXPECTED:
        assert f"### {name} [" in text, f"missing identity card for {name}"
    assert "[Compliance]" in text and "gstr1" in text


def test_boot_enforces_full_conformance_not_just_register(monkeypatch) -> None:
    """Codex finding 2026-07-19: ``register()`` is deliberately permissive (a defaulted category/
    tags/brief registers fine, silently omitted from the directory) — so the BOOT wiring must run
    the full conformance suite and crash loudly on the first violation. Simulate any module failing
    a check and assert boot registration refuses."""
    from types import SimpleNamespace

    import orchestrator.agent_framework.conformance as conf
    from orchestrator.agent_framework import ModuleRegistrationError

    failing_report = SimpleNamespace(
        passed=False,
        module_name="sales_recovery",
        failures=[SimpleNamespace(name="brief_complete", detail="category is default-empty")],
    )
    monkeypatch.setattr(conf, "check_module_conformance", lambda _impl: failing_report)

    with pytest.raises(ModuleRegistrationError, match="boot conformance FAILED.*brief_complete"):
        register_all_modules()
