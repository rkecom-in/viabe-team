"""VT-224 admin rate limit — per-token sliding window 10 req/sec.

In-process via dict[fingerprint, deque[ts_monotonic]]. Phase-1 single-
orchestrator deploy; multi-orchestrator needs Redis-backed bucket (filed
as VT-22N future work). Per CL-132 the dict is idempotent + state-only;
worst case after restart is a brief burst window that resets.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Annotated

from fastapi import Depends, HTTPException

from orchestrator.api.admin._auth import AdminAuth

_WINDOW_SEC = 1.0
_MAX_REQ_PER_WINDOW = 10

_state: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def _check(fingerprint: str) -> None:
    now = time.monotonic()
    with _lock:
        bucket = _state[fingerprint]
        cutoff = now - _WINDOW_SEC
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= _MAX_REQ_PER_WINDOW:
            raise HTTPException(
                status_code=429,
                detail=f"admin token rate limit ({_MAX_REQ_PER_WINDOW}/sec)",
            )
        bucket.append(now)


def rate_limited(fp: AdminAuth) -> str:
    """FastAPI dependency. Chains require_admin_token + applies per-fp limit."""
    _check(fp)
    return fp


RateLimitedAdmin = Annotated[str, Depends(rate_limited)]


def _reset_for_tests() -> None:
    """Test helper. Not called from prod paths."""
    with _lock:
        _state.clear()
