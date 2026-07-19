"""VT-396 step-3 — unit tests for the env-driven ``MARKETING_CONSENT_VERSIONS`` allowlist + the
two-layer prod-safety guard, in ``orchestrator.agents.sales_recovery_executor``.

These are PURE-LOGIC tests (no DB, no LLM, no send): they exercise the env parser
(``_parse_marketing_consent_versions``), the import-time prod-safety assertion
(``_assert_consent_versions_prod_safe`` / ``MarketingConsentProdSafetyError``), and the
single-sourcing of the two read sites (the detector's module-global + the send-gate helper).

The executor module pulls heavy chain deps (langchain via ``orchestrator.agent``), so — like the
sibling ``test_sales_recovery_executor.py`` — this file ``importorskip``s the executor import: in
the dep-less smoke (heavy deps absent) the whole module SKIPS at collection rather than erroring;
with the project deps installed it runs.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap

import pytest

# The executor import pulls langchain (via orchestrator.agent.tool_guardrail). If the heavy chain
# deps are absent (dep-less smoke), skip the whole module cleanly at collection.
sre = pytest.importorskip(
    "orchestrator.agents.sales_recovery_executor",
    reason="VT-396 consent-env tests need the executor's import chain (langchain et al.)",
)


# --- env parser ---------------------------------------------------------------------------------

def test_parse_unset_is_empty_frozenset(monkeypatch: pytest.MonkeyPatch) -> None:
    """UNSET env → fail-closed empty frozenset (the prod/main default)."""
    monkeypatch.delenv("MARKETING_CONSENT_VERSIONS", raising=False)
    monkeypatch.delenv("VIABE_ENV", raising=False)
    assert sre._parse_marketing_consent_versions() == frozenset()


def test_parse_empty_and_whitespace_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty / all-whitespace / all-comma value → empty frozenset (fail-closed preserved)."""
    monkeypatch.delenv("VIABE_ENV", raising=False)
    for raw in ("", "   ", ",", " , , "):
        monkeypatch.setenv("MARKETING_CONSENT_VERSIONS", raw)
        assert sre._parse_marketing_consent_versions() == frozenset(), raw


def test_parse_single_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIABE_ENV", raising=False)
    monkeypatch.setenv("MARKETING_CONSENT_VERSIONS", "dev-test-v0")
    assert sre._parse_marketing_consent_versions() == frozenset({"dev-test-v0"})


def test_parse_comma_separated_and_trims(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIABE_ENV", raising=False)
    monkeypatch.setenv("MARKETING_CONSENT_VERSIONS", " dev-test-v0 , dev-test-v1 ,, ")
    assert sre._parse_marketing_consent_versions() == frozenset({"dev-test-v0", "dev-test-v1"})


# --- single-sourcing of the two read sites ------------------------------------------------------

def test_both_read_sites_resolve_from_the_same_global(monkeypatch: pytest.MonkeyPatch) -> None:
    """The detector reads the module global directly; the send-gate helper returns
    ``frozenset(sre.MARKETING_CONSENT_VERSIONS)``. Patch the one global → both see the SAME set,
    so they can never drift. (Mirrors how the existing suite monkeypatches the global.)"""
    from orchestrator.agents import customer_send

    monkeypatch.setattr(sre, "MARKETING_CONSENT_VERSIONS", frozenset({"dev-test-v0"}))
    # Detector read site: the module global the detector sorts at call time.
    assert sre.MARKETING_CONSENT_VERSIONS == frozenset({"dev-test-v0"})
    # Send-gate read site: single-sources the SAME global via the helper.
    assert customer_send._marketing_consent_versions() == frozenset({"dev-test-v0"})

    monkeypatch.setattr(sre, "MARKETING_CONSENT_VERSIONS", frozenset())
    assert customer_send._marketing_consent_versions() == frozenset()


# --- prod-safety guard (layer b) ----------------------------------------------------------------

def test_prod_safe_allows_empty_under_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty allowlist under production is FINE — that is the prod default; no raise."""
    monkeypatch.setenv("VIABE_ENV", "production")
    sre._assert_consent_versions_prod_safe(frozenset())  # must not raise


def test_prod_safe_allows_nonempty_off_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-empty allowlist on dev/test is the whole point of the harness — no raise."""
    for env in ("test", "dev", "development", "staging"):
        monkeypatch.setenv("VIABE_ENV", env)
        sre._assert_consent_versions_prod_safe(frozenset({"dev-test-v0"}))  # must not raise


def test_prod_safe_raises_nonempty_under_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-empty allowlist AND VIABE_ENV=production → raise (the load-bearing refusal)."""
    monkeypatch.setenv("VIABE_ENV", "production")
    with pytest.raises(sre.MarketingConsentProdSafetyError):
        sre._assert_consent_versions_prod_safe(frozenset({"dev-test-v0"}))
    # Case-insensitive on the env value.
    monkeypatch.setenv("VIABE_ENV", "PRODUCTION")
    with pytest.raises(sre.MarketingConsentProdSafetyError):
        sre._assert_consent_versions_prod_safe(frozenset({"dev-test-v0"}))


def test_parse_raises_when_prod_env_var_fatfingered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parser runs the guard, so a non-empty value under production fails at parse time too."""
    monkeypatch.setenv("VIABE_ENV", "production")
    monkeypatch.setenv("MARKETING_CONSENT_VERSIONS", "dev-test-v0")
    with pytest.raises(sre.MarketingConsentProdSafetyError):
        sre._parse_marketing_consent_versions()


def test_default_shipped_constant_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env override, a fresh import of the executor binds an EMPTY allowlist — the
    prod/main fail-closed default the CI gate also enforces."""
    monkeypatch.delenv("MARKETING_CONSENT_VERSIONS", raising=False)
    monkeypatch.delenv("VIABE_ENV", raising=False)
    reloaded = importlib.reload(sre)
    try:
        assert reloaded.MARKETING_CONSENT_VERSIONS == frozenset()
    finally:
        importlib.reload(sre)  # restore the module's import-time state for other tests


def test_import_boot_fails_under_production_with_nonempty_env() -> None:
    """END-TO-END boot check: a child Python process importing the executor with
    VIABE_ENV=production + a non-empty MARKETING_CONSENT_VERSIONS FAILS TO IMPORT (non-zero exit,
    MarketingConsentProdSafetyError) — proves a fat-fingered prod env var stops the orchestrator
    from booting, never silently sends. Run in a subprocess because the assertion fires at the
    executor's import time."""
    env = dict(os.environ)
    env["VIABE_ENV"] = "production"
    env["MARKETING_CONSENT_VERSIONS"] = "dev-test-v0"
    code = textwrap.dedent(
        """
        import orchestrator.agents.sales_recovery_executor  # noqa: F401 — import must RAISE
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0, (
        "executor import did NOT fail under VIABE_ENV=production + non-empty allowlist "
        f"(stdout={proc.stdout!r} stderr={proc.stderr!r})"
    )
    assert "MarketingConsentProdSafetyError" in proc.stderr, proc.stderr


def test_import_boot_clean_under_production_with_empty_env() -> None:
    """The mirror: VIABE_ENV=production with the env var UNSET imports CLEANLY (the prod default
    is empty → no raise) — proves the guard does not break a normal prod boot."""
    env = dict(os.environ)
    env["VIABE_ENV"] = "production"
    env.pop("MARKETING_CONSENT_VERSIONS", None)
    code = "import orchestrator.agents.sales_recovery_executor as m; assert m.MARKETING_CONSENT_VERSIONS == frozenset()"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"executor import FAILED on a clean prod boot (empty default): stderr={proc.stderr!r}"
    )
