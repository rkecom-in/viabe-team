"""Pure-Python unit tests for ``orchestrator.dbos_purge`` (Step-0 Branch B).

Covers the retention-config parsing + cutoff-math surface in isolation.
No DB, no DBOS — the actual sweep is exercised by
``test_dbos_purge_substrate.py`` against a live Postgres in the CI
``orchestrator`` job.
"""

from __future__ import annotations

import pytest

pytest.importorskip("dbos")

from orchestrator.dbos_purge import (  # noqa: E402 — after importorskip
    _DEFAULT_RETENTION_SECONDS,
    _RETENTION_ENV_VAR,
    _PURGE_CRON,
    _retention_seconds,
    purge_terminal_workflow_inputs,
)


def test_retention_default_when_env_unset(monkeypatch):
    """No env var → 7200 (2h)."""
    monkeypatch.delenv(_RETENTION_ENV_VAR, raising=False)
    assert _retention_seconds() == _DEFAULT_RETENTION_SECONDS
    assert _DEFAULT_RETENTION_SECONDS == 7200


def test_retention_reads_valid_env(monkeypatch):
    """Positive int env var is honoured."""
    monkeypatch.setenv(_RETENTION_ENV_VAR, "3600")
    assert _retention_seconds() == 3600


def test_retention_rejects_negative_value(monkeypatch):
    """Negative retention would invert the cutoff — fall back to default
    rather than silently expand the delete window past the in-flight
    status filter."""
    monkeypatch.setenv(_RETENTION_ENV_VAR, "-1")
    assert _retention_seconds() == _DEFAULT_RETENTION_SECONDS


def test_retention_rejects_zero(monkeypatch):
    """Zero retention would set cutoff = now → terminal rows are deleted
    immediately on next sweep, which is technically valid but is almost
    certainly a config mistake. Reject and log."""
    monkeypatch.setenv(_RETENTION_ENV_VAR, "0")
    assert _retention_seconds() == _DEFAULT_RETENTION_SECONDS


def test_retention_rejects_non_integer(monkeypatch):
    """A garbled env value (e.g. ``2h``) must not raise into the
    scheduler thread; fall back to default."""
    monkeypatch.setenv(_RETENTION_ENV_VAR, "2h")
    assert _retention_seconds() == _DEFAULT_RETENTION_SECONDS


def test_purge_callable_without_dbos_launched_returns_zero(monkeypatch):
    """If ``purge_terminal_workflow_inputs`` is called before
    ``launch_dbos`` (e.g. from an admin script in an unconfigured
    process), it must not raise — return ``(cutoff_ms, 0)`` and log
    debug. Defends the direct-call surface; the scheduler itself
    cannot fire before launch."""
    from dbos import DBOS

    monkeypatch.setattr(DBOS, "_instance", None, raising=False)
    cutoff_ms, deleted = purge_terminal_workflow_inputs()
    assert isinstance(cutoff_ms, int)
    assert cutoff_ms > 0
    assert deleted == 0


def test_purge_cron_cadence_is_30_min():
    """30-min cadence is the documented contract. Locks against a
    silent edit that loosens the retention guarantee."""
    assert _PURGE_CRON == "*/30 * * * *"
