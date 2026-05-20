"""Cancel coordinator — first-wins cancellation across the four enforcers
(VT-35).

Any of the four enforcers may signal a cancel. The coordinator records
the FIRST signal and ignores subsequent ones — there is no double-fire,
no race. ``terminated_by`` on the final ``AgentResult`` reflects the
winning axis; later signals do not overwrite it.

Thread-safe via a coarse lock — signals from the wall-clock-timer path
may originate on a different thread than the main loop. Contention is
negligible (signals are rare relative to loop iteration).
"""

from __future__ import annotations

from threading import Lock

from orchestrator.failures import HardLimitAxis


class CancellationContext:
    """First-wins cancellation registry.

    Loop wiring pattern:
      ctx = CancellationContext()
      ... enforcers attach to ctx ...
      for each turn:
          enforcer.check()
          if ctx.is_cancelled: break
          do_turn()
          for each tool dispatch:
              enforcer.record_dispatch()
              if ctx.is_cancelled: break

    The check-then-break pattern is repeated at every place a budget can
    be exceeded: turn boundary (token meter, wall-clock), tool dispatch
    (tool counter, depth tracker, wall-clock). Coarse but explicit —
    each check site is documented next to the enforcer it serves.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._cancelled_by: HardLimitAxis | None = None
        self._reason: str | None = None

    def signal(self, axis: HardLimitAxis, reason: str) -> None:
        """Record a cancel signal. Idempotent after the first call —
        subsequent signals (any axis, any reason) are ignored. This is
        the first-wins guarantee."""
        with self._lock:
            if self._cancelled_by is None:
                self._cancelled_by = axis
                self._reason = reason

    @property
    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled_by is not None

    @property
    def cancelled_by(self) -> HardLimitAxis | None:
        with self._lock:
            return self._cancelled_by

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason


__all__ = ["CancellationContext"]
