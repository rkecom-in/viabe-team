"""VT-394 — orchestrator-side OTP request rate-limiter (Direction B).

The AUTHORITATIVE per-IP + per-phone cap on the owner-portal request-OTP flow.
team-web keeps a thin in-process first-layer cap (cost guard, defense in depth),
but on Vercel serverless that Map is per-instance → bypassable across instances.
The orchestrator is a SINGLE long-running uvicorn worker (no ``--workers``, no
replicas), so a limiter enforced HERE is **global by construction** — the exact
property the contract wants, with no shared store.

Mirrors ``apps/team-web/lib/auth/otp-rate-limit.ts`` (VT-250 Cowork ruling D4):
fixed-window counters, **5 requests / 15-minute window** for EACH of per-IP and
per-phone; both must pass; the first to trip blocks. Sits ON TOP of Twilio
Verify's own native per-number rate-limit + brute-force protection — its job is
to blunt enumeration / abuse / cost before a Verify call is even made.

CL-390: keys are HASHED (SHA-256[:16], mirroring team-web's ``_tokenizePhone``)
so the in-memory map holds NO plaintext phone / IP PII.

Degrade = **FAIL-OPEN**: if the limiter's own machinery raises, the request is
ADMITTED (logged loudly), never blocked. Justification: Twilio Verify's native
per-number limit + brute-force protection is the hard backstop; our cap is the
per-IP / enumeration / cost guard, a different and softer job — a limiter bug
must never lock real owners out of signing up.

Swap seam: the limiter lives behind the ``OtpRateLimiter`` ABC. A future
>1-replica orchestrator would break the in-process ``Map`` assumption; at that
point swap ``InProcessOtpRateLimiter`` for a Postgres/Redis-backed impl behind
the same ``.check(ip_token, phone_token)`` interface — no caller rewrite.
"""

from __future__ import annotations

import abc
import hashlib
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# D4 numbers (mirror team-web): 5 requests per 15-minute fixed window, per key.
OTP_WINDOW_SECONDS = 15 * 60
OTP_MAX_PER_IP = 5
OTP_MAX_PER_PHONE = 5


def _tokenize(value: str) -> str:
    """SHA-256[:16] token — mirrors team-web ``_tokenizePhone`` (CL-390).

    The in-process map is keyed only on this token, never the raw phone or IP,
    so no plaintext PII ever lives in the limiter's memory or a log line.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class RateLimitResult:
    """PII-safe outcome. ``blocked_by`` names only the dimension that tripped."""

    allowed: bool
    blocked_by: str | None = None  # 'ip' | 'phone' | None


class OtpRateLimiter(abc.ABC):
    """Swap seam. ``check`` consumes one token for BOTH the per-IP and per-phone
    caps and returns whether the request is allowed.

    Contract:
      - ``ip_token`` / ``phone_token`` are the RAW values (IP string, E.164
        phone); the impl is responsible for hashing them at rest (CL-390).
      - Both caps must pass; the first to trip blocks. The per-IP cap is checked
        first and, if it trips, the per-phone counter is left untouched (a
        blocked request does not asymmetrically burn the other dimension).
      - FAIL-OPEN: any internal error → ``allowed=True`` (logged loudly).
    """

    @abc.abstractmethod
    def check(self, ip_token: str, phone_token: str) -> RateLimitResult:
        raise NotImplementedError


@dataclass
class _Bucket:
    count: int
    window_start: int


@dataclass
class InProcessOtpRateLimiter(OtpRateLimiter):
    """In-process fixed-window limiter. Global because the orchestrator is one
    process; window resets on restart (acceptable — 15-min window + Twilio
    backstop). For >1 replica, swap for a shared-store impl behind this ABC.
    """

    window_seconds: int = OTP_WINDOW_SECONDS
    max_per_ip: int = OTP_MAX_PER_IP
    max_per_phone: int = OTP_MAX_PER_PHONE
    # key = f"{kind}:{token}:{window_start}" → bucket
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    def _hit(self, kind: str, token: str, max_count: int, now: float) -> bool:
        """Consume one token for (kind, token) in the current window.

        Returns True if admitted (under cap), False if the cap is exceeded.
        Raising here is a real bug; ``check`` turns it into a fail-OPEN admit.
        """
        window_start = int(now // self.window_seconds) * self.window_seconds
        key = f"{kind}:{token}:{window_start}"
        bucket = self._buckets.get(key)
        if bucket is None or bucket.window_start != window_start:
            self._buckets[key] = _Bucket(count=1, window_start=window_start)
            return True
        if bucket.count >= max_count:
            return False
        bucket.count += 1
        return True

    def check(self, ip_token: str, phone_token: str) -> RateLimitResult:
        try:
            now = time.time()
            # Per-IP first (cheap enumeration guard). Hashed at rest (CL-390).
            if not self._hit("ip", _tokenize(ip_token or "unknown"), self.max_per_ip, now):
                return RateLimitResult(allowed=False, blocked_by="ip")
            # Per-phone (tokenized — no plaintext in the map).
            if not self._hit("phone", _tokenize(phone_token), self.max_per_phone, now):
                return RateLimitResult(allowed=False, blocked_by="phone")
            return RateLimitResult(allowed=True, blocked_by=None)
        except Exception:  # noqa: BLE001 — fail-OPEN by design.
            # FAIL-OPEN: never lock out signups on a limiter bug. Log loudly;
            # Twilio Verify's native limit is the hard backstop. PII-safe (no
            # phone/IP in the message — only the tokenizer ever sees them).
            logger.error(
                "[otp-rate-limit] limiter error — FAILING OPEN (request admitted). "
                "Twilio Verify native limit is the backstop.",
                exc_info=True,
            )
            return RateLimitResult(allowed=True, blocked_by=None)


# Module-shared singleton: this IS the global counter (single-process property).
# Two ``check`` calls anywhere in the process share this state.
_LIMITER: OtpRateLimiter = InProcessOtpRateLimiter()


def get_otp_rate_limiter() -> OtpRateLimiter:
    """The module-shared limiter (the global counter for this process)."""
    return _LIMITER


def check_otp_rate_limit(ip: str, phone_e164: str) -> RateLimitResult:
    """Consume one request-OTP token for BOTH caps against the shared limiter."""
    return _LIMITER.check(ip, phone_e164)


def _reset_otp_rate_limit() -> None:
    """Test seam: reset the shared in-process limiter's counters."""
    if isinstance(_LIMITER, InProcessOtpRateLimiter):
        _LIMITER._buckets.clear()
