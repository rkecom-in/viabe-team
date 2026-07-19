"""VT-476 — dev transport send-guard: the outer gate that stops DEV from sending
real WhatsApp messages to real numbers.

THE BREACH THIS PREVENTS
------------------------
On dev, ``TEAM_TWILIO_MOCK_MODE`` is OFF, so WhatsApp sends go to REAL Twilio →
REAL phones. A test drive sent Fazal's real number live onboarding messages. The
"0 sends" check missed it because the onboarding owner-send FREEFORM path
(``onboarding/journey.py`` → ``send_freeform_message``) bypasses the
``owner_message_audit`` ledger that was checked. A ledger-level guard is therefore
INSUFFICIENT — a send path that skips the ledger escapes it.

So this guard sits at the TRANSPORT chokepoint instead: the single Twilio client
(``utils/twilio_send._client()``) every WhatsApp send funnels through
(``send_template_message`` + ``send_freeform_message`` → ``_client().messages.create``;
the agent template/freeform tools + customer-send path all delegate to those two).
Wrapping the client means NO send path — present or future — can escape the gate,
because they all obtain their transport from ``_client()``.

WHAT IT DOES
------------
When ``EXPECTED_ENV`` (the VT-362 env sentinel; ``prod`` on Prod, default ``dev``)
is NOT prod, every outbound ``messages.create`` is checked against an explicit
allowlist (``DEV_SEND_ALLOWLIST``, comma-separated E.164, DEFAULT EMPTY):

  - destination NOT in the allowlist → MOCK: log PII-safely (destination last-4 +
    a marker, CL-390) and return a SUCCESS-shaped result (a fake ``MKDEV…`` SID) so
    the calling flow proceeds normally, WITHOUT calling real Twilio.
  - destination IN the allowlist → real path: delegate to the wrapped real client
    (a genuine Twilio send).

FAIL-CLOSED: an empty/unset allowlist on dev MOCKS ALL sends — no real send
escapes. Only a number EXPLICITLY in the allowlist gets a real send.

Prod (``EXPECTED_ENV=prod``) is UNAFFECTED — the guard is inert, real sends as
today, allowlist ignored.

This is an ADDITIONAL OUTER gate. It is NOT the customer-send compliance rail
(consent / opt-out / approval / onboarded — VT-460/467/474); those stay and run
exactly as before, BENEATH this guard. The dev guard only ever turns a real send
into a mocked one on dev; it never relaxes a compliance refusal.

VT-559 addendum: ``DevSendGuardClient`` also wraps ``client.verify.v2.services(sid)
.verifications.create`` — the Twilio Verify OTP dispatch used by
``auth/twilio_verify.py``. That module built its own raw ``twilio.rest.Client`` and
called Verify directly, never funneling through ``twilio_send._client()`` — a second
rail-bypass where a dev signup could deliver a real WhatsApp OTP to a real number.
Same allowlist semantics as ``.messages``. ``.verification_checks`` (validates a code
against Twilio's own record; never sends anything to the destination) is left
UNGUARDED — it passes through to the real client unchanged.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

# VT-362 env sentinel (mirrors auth/prod_safety._ENV_SIGNAL + apply_migrations).
# Railway sets EXPECTED_ENV=prod on Prod, EXPECTED_ENV=dev on Dev. Default "dev"
# — same posture as VT-362's ${EXPECTED_ENV:-dev}; we never invent a stricter
# default. The guard is ACTIVE on every non-prod env and INERT only on prod.
_ENV_SIGNAL = "EXPECTED_ENV"
_PROD = "prod"

# Comma-separated E.164 allowlist of numbers that MAY receive a real dev send.
# DEFAULT EMPTY → fail-closed → every dev send is mocked.
_ALLOWLIST_ENV = "DEV_SEND_ALLOWLIST"

_WHATSAPP_PREFIX = "whatsapp:"


def is_prod_env() -> bool:
    """True only when the env identity POSITIVELY reads prod (VT-362 signal).

    Mirrors ``auth/prod_safety._is_prod``: default dev; an unset/unknown value is
    NOT treated as prod. On prod the dev guard is inert.
    """
    return os.environ.get(_ENV_SIGNAL, "dev").strip().lower() == _PROD


def _normalize_number(raw: str | None) -> str:
    """Normalize a destination for allowlist comparison.

    Strips the ``whatsapp:`` channel scheme, a leading ``+``, and ALL internal
    whitespace, so ``whatsapp:+91 93215 53267`` / ``+919321553267`` /
    ``919321553267`` all compare equal. Returns "" for a missing destination
    (which can never match a non-empty allowlist → mocked, fail-closed).
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if s.startswith(_WHATSAPP_PREFIX):
        s = s[len(_WHATSAPP_PREFIX):]
    s = "".join(s.split())  # drop every internal/edge space
    if s.startswith("+"):
        s = s[1:]
    return s


def _allowlist() -> set[str]:
    """Parse ``DEV_SEND_ALLOWLIST`` into a normalized set. Empty/unset → empty set.

    Read fresh on each call (NOT cached) so a test / runtime env change takes
    effect immediately and the empty-default fail-closed posture is never stale.
    """
    raw = os.environ.get(_ALLOWLIST_ENV, "")
    return {n for n in (_normalize_number(part) for part in raw.split(",")) if n}


def _last4(normalized: str) -> str:
    """PII-safe destination fragment for logs (CL-390): last 4 digits only."""
    return normalized[-4:] if len(normalized) >= 4 else normalized


class _MockMessage:
    """Success-shaped stand-in for a Twilio Message, returned by a mocked dev send.

    Shaped exactly like the fields callers read on a real Twilio Message
    (``send_template_message`` reads ``.sid``; ``send_freeform_message`` returns
    ``.sid``) so the calling flow proceeds identically to a real send. The SID
    carries a ``MKDEV`` marker so a mocked dev send is greppable in any downstream
    record and never mistaken for a real ``SM…`` SID.
    """

    def __init__(self) -> None:
        self.sid = f"MKDEV{uuid4().hex[:27]}"
        self.status = "queued"
        self.error_code = None
        self.error_message = None


class _MockVerification:
    """Success-shaped stand-in for a Twilio VerificationInstance, returned by a mocked
    dev Verify OTP send (VT-559).

    Shaped like the fields ``twilio_verify.start_verification`` reads (``.sid``,
    ``.status``) so a mocked dev OTP proceeds through the calling flow identically to
    a real one. The SID carries a ``VEDEV`` marker (mirrors ``_MockMessage``'s
    ``MKDEV``) so a mocked dev Verify send is greppable and never mistaken for a real
    Twilio ``VE…`` verification SID, nor for the ``VEmock…`` SIDs the SEPARATE full
    ``TEAM_TWILIO_VERIFY_MOCK_MODE`` path already generates.
    """

    def __init__(self) -> None:
        self.sid = f"VEDEV{uuid4().hex[:26]}"
        self.status = "pending"
        self.error_code = None
        self.error_message = None


class _DevSendGuardVerifications:
    """The ``.verifications`` namespace of a guarded Verify ``ServiceContext`` (VT-559).

    Guards ONLY ``.create`` — the call that actually dispatches an OTP to the
    destination (``twilio_verify.start_verification``). Same allowlist semantics as
    ``_DevSendGuardMessages.create``: a destination not in ``DEV_SEND_ALLOWLIST`` is
    mocked (no real Twilio call); an allowlisted destination reaches the real inner
    ``verifications.create``.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def create(self, **kwargs: Any) -> Any:
        to_norm = _normalize_number(kwargs.get("to"))
        if to_norm and to_norm in _allowlist():
            logger.info(
                "[VT-559 dev-send-guard] ALLOWLISTED dev Verify OTP -> ..%s (real Twilio)",
                _last4(to_norm),
            )
            return self._inner.create(**kwargs)
        logger.warning(
            "[VT-559 dev-send-guard] MOCKED dev Verify OTP -> ..%s "
            "(EXPECTED_ENV!=prod, destination not in DEV_SEND_ALLOWLIST). "
            "NO real OTP was sent; returned a mock verification.",
            _last4(to_norm) or "<none>",
        )
        return _MockVerification()


class _DevSendGuardVerifyServiceContext:
    """Proxy for ``client.verify.v2.services(sid)`` (VT-559).

    Guards ONLY the ``.verifications`` resource (the OTP dispatch). Every other
    attribute — notably ``.verification_checks``, which validates a code against
    Twilio's own record and never sends anything to the destination — passes
    through UNGUARDED to the real service context via ``__getattr__``.
    """

    def __init__(self, real_context: Any) -> None:
        self._real = real_context

    @property
    def verifications(self) -> _DevSendGuardVerifications:
        return _DevSendGuardVerifications(self._real.verifications)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _DevSendGuardVerifyV2:
    """Proxy for ``client.verify.v2`` (VT-559). Guards ``.services(sid)``; every other
    attribute passes through UNGUARDED via ``__getattr__``."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def services(self, sid: str) -> _DevSendGuardVerifyServiceContext:
        return _DevSendGuardVerifyServiceContext(self._inner.services(sid))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _DevSendGuardVerify:
    """Proxy for ``client.verify`` (VT-559). Guards ``.v2``; every other attribute
    passes through UNGUARDED via ``__getattr__``."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def v2(self) -> _DevSendGuardVerifyV2:
        return _DevSendGuardVerifyV2(self._inner.v2)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class DevSendGuardClient:
    """Transport wrapper that MOCKS non-allowlisted sends on dev (VT-476, VT-559).

    Wraps the real Twilio REST client. Exposes the same ``client.messages.create``
    surface every send path uses, PLUS the ``client.verify.v2.services(sid)
    .verifications.create`` surface the Twilio Verify OTP path uses (VT-559). On a
    non-prod env (the only env this wrapper is ever installed on), each guarded
    ``create(to=…)`` call is allowlist-checked:

      - ``to`` normalized ∉ ``DEV_SEND_ALLOWLIST`` → return a mock (fake success SID),
        NO real Twilio call. The breach-stopping default: an empty allowlist mocks
        EVERYTHING.
      - ``to`` ∈ the allowlist → delegate to the wrapped real client (real send).

    The allowlist is read fresh per call, so the fail-closed default can never be
    a stale-cache artifact.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def messages(self) -> _DevSendGuardMessages:
        return _DevSendGuardMessages(self._inner)

    @property
    def verify(self) -> _DevSendGuardVerify:
        """VT-559: guards the Twilio Verify OTP dispatch with the SAME allowlist
        semantics as ``.messages`` — closes the rail-bypass where
        ``auth/twilio_verify.py`` built its own raw client and never funneled
        through this wrapper."""
        return _DevSendGuardVerify(self._inner.verify)


class _DevSendGuardMessages:
    """The ``.messages`` namespace of :class:`DevSendGuardClient`."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def create(self, **kwargs: Any) -> Any:
        to_norm = _normalize_number(kwargs.get("to"))
        if to_norm and to_norm in _allowlist():
            # Allowlisted on dev — real send path. Delegate to the wrapped client.
            logger.info(
                "[VT-476 dev-send-guard] ALLOWLISTED dev send -> ..%s (real Twilio)",
                _last4(to_norm),
            )
            return self._inner.messages.create(**kwargs)
        # Not allowlisted (or no destination) → MOCK. No real Twilio call.
        # PII-safe: only the last-4 + a marker reach the log (CL-390).
        logger.warning(
            "[VT-476 dev-send-guard] MOCKED dev send -> ..%s "
            "(EXPECTED_ENV!=prod, destination not in DEV_SEND_ALLOWLIST). "
            "NO real WhatsApp message was sent; returned a mock SID.",
            _last4(to_norm) or "<none>",
        )
        return _MockMessage()


def maybe_wrap_for_dev(client: Any) -> Any:
    """Wrap ``client`` in the dev send-guard UNLESS the env is prod.

    The single install point, called from ``twilio_send._client()``:

      - prod (``EXPECTED_ENV=prod``) → return the client UNWRAPPED (guard inert;
        real sends exactly as today).
      - any other env (dev / CI / unset-default-dev) → return a
        :class:`DevSendGuardClient` so non-allowlisted sends are mocked.

    Wrapping a mock client (``TEAM_TWILIO_MOCK_MODE=1``) is harmless — a mocked
    transport that gets further allowlist-gated still makes no network call; the
    guard simply adds the explicit dev-allowlist semantics on top.
    """
    if is_prod_env():
        return client
    return DevSendGuardClient(client)


__all__ = [
    "DevSendGuardClient",
    "maybe_wrap_for_dev",
    "is_prod_env",
]
