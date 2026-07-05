"""VT-606 amendment A3 — ``stale_resume.is_stale``'s boundary conditions (pure, no DB, no network).
DB-backed ``last_owner_inbound_at``/``reengage_stale_task`` coverage is in ``test_stale_resume_db.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("psycopg")

from orchestrator.manager.stale_resume import is_stale  # noqa: E402


def test_is_stale_none_last_inbound_is_stale() -> None:
    """A tenant that has NEVER messaged is treated as stale (nothing to NOT re-engage against)."""
    assert is_stale(None) is True


def test_is_stale_now_is_not_stale() -> None:
    now = datetime.now(timezone.utc)
    assert is_stale(now, now=now) is False


def test_is_stale_just_under_24h_is_not_stale() -> None:
    now = datetime.now(timezone.utc)
    last = now - timedelta(hours=23, minutes=59)
    assert is_stale(last, now=now) is False


def test_is_stale_just_over_24h_is_stale() -> None:
    now = datetime.now(timezone.utc)
    last = now - timedelta(hours=24, minutes=1)
    assert is_stale(last, now=now) is True


def test_is_stale_exactly_24h_is_not_stale() -> None:
    """The window is > 24h, not >=  — exactly 24h00m00s is still IN the window (the boundary
    itself belongs to the owner, not to staleness)."""
    now = datetime.now(timezone.utc)
    last = now - timedelta(hours=24)
    assert is_stale(last, now=now) is False


def test_is_stale_one_second_past_the_boundary_is_stale() -> None:
    now = datetime.now(timezone.utc)
    last = now - timedelta(hours=24, seconds=1)
    assert is_stale(last, now=now) is True
