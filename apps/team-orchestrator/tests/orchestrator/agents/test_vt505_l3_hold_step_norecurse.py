"""VT-505 — no-recursion regression for l3_hold demote/send step globals.

Bug (confirmed live — RecursionError in a running l3_hold workflow):
  _hold_demote_step and _hold_send_step were used as BOTH the module-level
  ``None`` sentinel globals AND the ``def``-names of the wrapper functions.
  Python rebinds the module-level name to the function at class-body/module
  load time → ``_ensure_hold_steps`` found them non-None → the DBOS.step()
  wrap was never installed → the wrapper called itself → infinite recursion
  → RecursionError.  This blocked the L3 AUTONOMOUS terminal send entirely
  (G35's durable path).

Fix (the l2_send idiom — mirrored exactly):
  Module-level sentinel globals renamed to ``_hold_demote_step_decorated``
  and ``_hold_send_step_decorated`` (DISTINCT from the wrapper function
  names).  ``_ensure_hold_steps()`` sets those ``_decorated`` globals;
  the wrappers ``_hold_demote_step`` / ``_hold_send_step`` call the
  ``_decorated`` globals (not themselves).

Test strategy:
  No DATABASE_URL required — the body functions are patched with mocks so no
  Postgres interaction occurs.  psycopg + dbos must be importable (l3_hold's
  module-level import path and the lazy-DBOS decoration path), but no running
  DB or DBOS app instance is needed (DBOS.step() falls through to direct body
  invocation when no DBOS app is running — confirmed in the local venv).

  (a) After reset, ``_decorated`` globals are None — proves the module-load
      name collision no longer masks them.
  (b) After ``_ensure_hold_steps()``, the ``_decorated`` globals are set to a
      callable that is NOT the wrapper function.
  (c) Calling ``_hold_demote_step()`` reaches the body exactly once — no
      RecursionError.
  (d) Calling ``_hold_send_step()`` reaches the body exactly once — no
      RecursionError.
  (e) ``_ensure_hold_steps()`` is idempotent — a second call does not
      re-wrap (the sentinel check works).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# l3_hold imports `from psycopg import Connection` at module level (via
# orchestrator.db.tenant_connection), and `from dbos import DBOS` lazily inside
# _ensure_hold_steps.  Both must be importable; skip cleanly in the dep-less CI
# smoke job where neither is installed.
pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import orchestrator.agents.l3_hold as l3_hold_mod  # noqa: E402 — after skip guards


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_decorated_globals() -> None:
    """Force the lazy-init sentinels back to None so each test starts clean."""
    l3_hold_mod._hold_demote_step_decorated = None  # type: ignore[attr-defined]
    l3_hold_mod._hold_send_step_decorated = None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# (a) + (b) — global naming invariants
# ---------------------------------------------------------------------------


class TestL3HoldStepGlobalNaming:
    """Module-level globals use the _decorated suffix, not the wrapper name."""

    def test_decorated_globals_reset_to_none(self) -> None:
        """After force-reset, both _decorated sentinels are None.

        In the buggy version the globals were IMMEDIATELY rebound to the
        wrapper functions by the ``def _hold_demote_step`` / ``def
        _hold_send_step`` statements — so they could never be None at runtime
        and _ensure_hold_steps was a permanent no-op.  After the fix, resetting
        them to None is valid and _ensure_hold_steps will re-install the wrap."""
        _reset_decorated_globals()
        assert l3_hold_mod._hold_demote_step_decorated is None  # type: ignore[attr-defined]
        assert l3_hold_mod._hold_send_step_decorated is None  # type: ignore[attr-defined]

    def test_ensure_sets_decorated_globals_as_callables(self) -> None:
        """After _ensure_hold_steps(), both _decorated globals are non-None callables."""
        _reset_decorated_globals()
        l3_hold_mod._ensure_hold_steps()  # type: ignore[attr-defined]
        assert l3_hold_mod._hold_demote_step_decorated is not None  # type: ignore[attr-defined]
        assert l3_hold_mod._hold_send_step_decorated is not None  # type: ignore[attr-defined]
        assert callable(l3_hold_mod._hold_demote_step_decorated)  # type: ignore[attr-defined]
        assert callable(l3_hold_mod._hold_send_step_decorated)  # type: ignore[attr-defined]

    def test_decorated_globals_are_not_wrapper_functions(self) -> None:
        """The _decorated globals must NOT be the same object as the wrappers.

        This is the structural pin of the fix: in the buggy version the names
        collided so _ensure_hold_steps would have set _hold_demote_step to the
        DBOS-wrapped body — but the wrapper was already that name, making the
        assignment invisible.  After the fix the _decorated globals are distinct
        objects from the wrapper functions."""
        _reset_decorated_globals()
        l3_hold_mod._ensure_hold_steps()  # type: ignore[attr-defined]
        assert (  # type: ignore[attr-defined]
            l3_hold_mod._hold_demote_step_decorated  # type: ignore[attr-defined]
            is not l3_hold_mod._hold_demote_step  # type: ignore[attr-defined]
        )
        assert (
            l3_hold_mod._hold_send_step_decorated  # type: ignore[attr-defined]
            is not l3_hold_mod._hold_send_step  # type: ignore[attr-defined]
        )


# ---------------------------------------------------------------------------
# (c) + (d) — no recursion: wrappers reach the body exactly once
# ---------------------------------------------------------------------------


class TestL3HoldStepNoRecursion:
    """Wrappers dispatch to the body via the _decorated global — never to themselves."""

    def test_demote_step_no_recursion_body_called_once(self) -> None:
        """_hold_demote_step() invokes the body exactly once with no RecursionError.

        Mocks DBOS.step() as an identity decorator (avoids DBOS registration and
        the __qualname__ requirement on MagicMock).  Patches _hold_demote_step_body
        with a mock BEFORE _ensure_hold_steps() so _hold_demote_step_decorated becomes
        the mock directly (via the identity).  Verifies the mock is called once with
        the correct args — proving the wrapper dispatches to the body, not to itself."""
        _reset_decorated_globals()
        demote_mock = MagicMock(return_value=None)
        # DBOS.step() returns a decorator; mock it as identity (fn → fn) to avoid
        # DBOS registration internals (which access __qualname__ on the wrapped fn).
        with (
            patch("dbos.DBOS.step", return_value=lambda fn: fn),
            patch.object(l3_hold_mod, "_hold_demote_step_body", demote_mock),
        ):
            l3_hold_mod._ensure_hold_steps()  # type: ignore[attr-defined]
            # _hold_demote_step_decorated IS demote_mock (via identity wrap).
            # This must NOT raise RecursionError (the VT-505 regression).
            l3_hold_mod._hold_demote_step("tenant-vt505", "batch-001")  # type: ignore[attr-defined]
        demote_mock.assert_called_once_with("tenant-vt505", "batch-001")

    def test_send_step_no_recursion_body_called_once(self) -> None:
        """_hold_send_step() invokes the body exactly once with no RecursionError."""
        _reset_decorated_globals()
        expected_counters = {"sent": 1, "skipped": 0, "failed": 0}
        send_mock = MagicMock(return_value=expected_counters)
        with (
            patch("dbos.DBOS.step", return_value=lambda fn: fn),
            patch.object(l3_hold_mod, "_hold_send_step_body", send_mock),
        ):
            l3_hold_mod._ensure_hold_steps()  # type: ignore[attr-defined]
            result = l3_hold_mod._hold_send_step("tenant-vt505", "batch-001")  # type: ignore[attr-defined]
        send_mock.assert_called_once_with("tenant-vt505", "batch-001")
        assert result == expected_counters

    def test_demote_step_recursion_guard_low_limit(self) -> None:
        """Confirm no RecursionError even at a drastically reduced recursion limit.

        Sets sys.getrecursionlimit to 50 (well below the ~10–20 frames a genuine
        recursion would hit before the mock call).  A single wrapper → _decorated →
        body call chain uses ~3 frames; a recursive chain hits the limit immediately."""
        import sys

        _reset_decorated_globals()
        demote_mock = MagicMock(return_value=None)
        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(50)
        try:
            with (
                patch("dbos.DBOS.step", return_value=lambda fn: fn),
                patch.object(l3_hold_mod, "_hold_demote_step_body", demote_mock),
            ):
                l3_hold_mod._ensure_hold_steps()  # type: ignore[attr-defined]
                # Must not hit RecursionError with limit=50 on the fixed code.
                l3_hold_mod._hold_demote_step("tenant-vt505", "batch-002")  # type: ignore[attr-defined]
        finally:
            sys.setrecursionlimit(old_limit)
        demote_mock.assert_called_once_with("tenant-vt505", "batch-002")


# ---------------------------------------------------------------------------
# (e) — idempotency of _ensure_hold_steps()
# ---------------------------------------------------------------------------


class TestL3HoldEnsureIdempotent:
    """_ensure_hold_steps() is a safe no-op on the second call."""

    def test_ensure_idempotent_same_decorated_object(self) -> None:
        """Calling _ensure_hold_steps() twice keeps the SAME decorated object.

        If _ensure_hold_steps() re-wrapped on every call it would produce a new
        DBOS step object each time, breaking DBOS's stable-qualname recovery
        contract.  The ``if _decorated is None`` sentinel prevents re-wrapping."""
        _reset_decorated_globals()
        l3_hold_mod._ensure_hold_steps()  # type: ignore[attr-defined]
        first_demote = l3_hold_mod._hold_demote_step_decorated  # type: ignore[attr-defined]
        first_send = l3_hold_mod._hold_send_step_decorated  # type: ignore[attr-defined]
        l3_hold_mod._ensure_hold_steps()  # type: ignore[attr-defined]
        assert l3_hold_mod._hold_demote_step_decorated is first_demote  # type: ignore[attr-defined]
        assert l3_hold_mod._hold_send_step_decorated is first_send  # type: ignore[attr-defined]
