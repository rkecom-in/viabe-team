"""Pure-Python unit tests for ``dbos_config._build_dbos_config`` (VT-161).

Covers the env-driven app-name + opt-in Conductor wiring in isolation — no DBOS
launch, no DB. The live Conductor connection is the gated canary, not a unit test.
"""

from __future__ import annotations

import pytest

pytest.importorskip("dbos")

from dbos_config import _DEFAULT_APP_NAME, _build_dbos_config  # noqa: E402 — after importorskip

_DSN = "postgresql://u@localhost:5432/db"


def test_app_name_defaults_to_viabe_team(monkeypatch):
    """No DBOS_APPLICATION_NAME → default 'viabe-team' (the registered Conductor app name)."""
    monkeypatch.delenv("DBOS_APPLICATION_NAME", raising=False)
    monkeypatch.delenv("DBOS_CONDUCTOR_KEY", raising=False)
    cfg = _build_dbos_config(_DSN)
    assert cfg["name"] == _DEFAULT_APP_NAME == "viabe-team"
    assert cfg["database_url"] == _DSN


def test_app_name_env_override(monkeypatch):
    monkeypatch.setenv("DBOS_APPLICATION_NAME", "viabe-team-staging")
    cfg = _build_dbos_config(_DSN)
    assert cfg["name"] == "viabe-team-staging"


def test_blank_app_name_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("DBOS_APPLICATION_NAME", "")
    assert _build_dbos_config(_DSN)["name"] == _DEFAULT_APP_NAME


def test_conductor_key_present_is_wired(monkeypatch):
    """DBOS_CONDUCTOR_KEY present → conductor_key set on the config (Conductor-connected mode)."""
    monkeypatch.setenv("DBOS_CONDUCTOR_KEY", "dbos_key_abc")
    cfg = _build_dbos_config(_DSN)
    assert cfg.get("conductor_key") == "dbos_key_abc"


def test_conductor_key_absent_is_local_only(monkeypatch):
    """No key → no conductor_key → local-recovery only (graceful-degrade, no crash path)."""
    monkeypatch.delenv("DBOS_CONDUCTOR_KEY", raising=False)
    assert "conductor_key" not in _build_dbos_config(_DSN)


def test_blank_conductor_key_is_local_only(monkeypatch):
    """A blank/whitespace key is treated as absent (never passes an empty key to Conductor)."""
    monkeypatch.setenv("DBOS_CONDUCTOR_KEY", "   ")
    assert "conductor_key" not in _build_dbos_config(_DSN)
