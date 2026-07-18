"""VT-683 P1 — the ONE session-window truth: pure predicate + fail-closed DB wrappers, and the
stale_resume delegation (one definition, never re-derived)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("psycopg")

from orchestrator.owner_surface import session_window as sw  # noqa: E402

_NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


def test_window_open_pure_predicate() -> None:
    assert sw.window_open(_NOW - timedelta(hours=1), now=_NOW) is True
    assert sw.window_open(_NOW - timedelta(hours=24), now=_NOW) is True   # boundary inclusive
    assert sw.window_open(_NOW - timedelta(hours=24, seconds=1), now=_NOW) is False
    assert sw.window_open(None, now=_NOW) is False                        # never messaged


def test_session_open_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_tid):
        raise RuntimeError("db down")

    monkeypatch.setattr(sw, "last_owner_inbound_at", _boom)
    assert sw.session_open("t-1") is False


def test_idle_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sw, "last_owner_inbound_at", lambda _t: _NOW - timedelta(minutes=7))
    assert sw.idle_minutes("t-1", now=_NOW) == pytest.approx(7.0)
    monkeypatch.setattr(sw, "last_owner_inbound_at", lambda _t: None)
    assert sw.idle_minutes("t-1", now=_NOW) is None


def test_stale_resume_delegates_to_session_window() -> None:
    """is_stale must be the exact inverse of window_open — one truth (VT-683 P1)."""
    pytest.importorskip("dbos")
    from orchestrator.manager import stale_resume as sr

    for delta in (timedelta(hours=1), timedelta(hours=24), timedelta(hours=25)):
        last = _NOW - delta
        assert sr.is_stale(last, now=_NOW) == (not sw.window_open(last, now=_NOW))
    assert sr.is_stale(None, now=_NOW) is True
