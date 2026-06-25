"""VT-434 — fail-closed boot guard: dangerous test/mock bypass flags are
IMPOSSIBLE in prod.

Some env flags switch the orchestrator into a test/mock posture that is fine on
dev but catastrophic in prod:

  - ``TEAM_TWILIO_VERIFY_MOCK_MODE=1`` — owner-portal OTP login goes to the mock
    path where a static code (``VT250_MOCK_OTP``, default ``123456``) approves
    ANY login attempt (``auth/twilio_verify.py``). That is a TOTAL auth bypass:
    anyone who knows the static code logs in as any owner. NEVER legitimate in
    prod.
  - ``TEAM_TWILIO_MOCK_MODE=1`` — every WhatsApp/SMS send becomes a no-op that
    logs and returns a FAKE-success SID (``utils/twilio_send.py``). In prod that
    silently swallows every real customer message while reporting success — a
    launch-safety catastrophe (owners think they messaged customers; nothing was
    sent). NEVER legitimate in prod.

Both are unconditionally-illegitimate-in-prod (auth-bypass / send-suppression),
so the guard refuses to boot if either is ON under prod.

Prod detection mirrors VT-362 (``scripts/apply_migrations.py`` /
``Dockerfile`` CMD ``--expected-env "${EXPECTED_ENV:-dev}"``): the env's identity
is the ``EXPECTED_ENV`` signal that Railway sets per environment
(``EXPECTED_ENV=prod`` on Prod, ``EXPECTED_ENV=dev`` on Dev). Same posture as
VT-362 — the default is ``dev`` (we do NOT invent a stricter default that breaks
dev/CI boot); the guard fires ONLY when prod is POSITIVELY detected. On dev the
flags stay usable (the mock OTP + mock sends are the test/canary convenience),
no refusal.

Deliberately NOT guarded here (justified):
  - ``VT250_MOCK_OTP`` — only the static code USED by mock mode; inert unless
    ``TEAM_TWILIO_VERIFY_MOCK_MODE`` is on, which this guard already blocks in
    prod. Guarding the modifier as well as the switch would be redundant.
  - ``VT250_SMS_CHANNEL_ENABLED`` — a gate that OPENS the built-but-gated SMS
    channel; an intentional toggle, not an auth-bypass / unconditional-approve.
  - ``MARKETING_CONSENT_VERSIONS`` — already has its own prod-boot refusal in
    ``agents/sales_recovery_executor._assert_consent_versions_prod_safe`` (a
    DPDP-consent concern, not an auth-bypass); not re-guarded here.
"""

from __future__ import annotations

import os

# The env-identity signal, mirroring VT-362 (apply_migrations / Dockerfile CMD).
# Railway sets EXPECTED_ENV=prod on Prod, EXPECTED_ENV=dev on Dev. The default
# is "dev" — same posture as VT-362's `${EXPECTED_ENV:-dev}` (no stricter default
# that would break dev/CI boot). Prod is detected ONLY by a positive "prod".
_ENV_SIGNAL = "EXPECTED_ENV"
_PROD = "prod"

# The set of dangerous test/mock-bypass flags that must NEVER be ON in prod.
# Each is unconditionally-illegitimate-in-prod (auth-bypass / send-suppression).
# An env value of "1" (the activation literal every reader checks) means ON.
_PROD_FORBIDDEN_FLAGS: tuple[str, ...] = (
    # Owner-portal OTP login mock: static code approves ANY login → auth bypass.
    "TEAM_TWILIO_VERIFY_MOCK_MODE",
    # Send mock: real customer sends become fake-success no-ops → silent drop.
    "TEAM_TWILIO_MOCK_MODE",
)


class ProdSafetyError(RuntimeError):
    """A dangerous test/mock bypass flag is ON under EXPECTED_ENV=prod.

    Raised at boot so the orchestrator process FAILS TO BOOT rather than serve
    requests with an auth bypass (mock OTP) or silently-dropped sends (mock
    send) active in production. Fail-closed, loud, BEFORE any effect — the same
    structural-refusal posture as the VT-362 migration env guard.
    """


def _is_prod() -> bool:
    """True only when the env identity POSITIVELY reads prod (VT-362 signal).

    Mirrors VT-362's posture: the default is dev; we do not treat an
    unset/unknown value as prod (that would break dev/CI boot). The guard fires
    only on a positive ``EXPECTED_ENV=prod``.
    """
    return os.environ.get(_ENV_SIGNAL, "dev").strip().lower() == _PROD


def _flag_on(name: str) -> bool:
    """True if env flag ``name`` is set to the activation literal ``"1"``.

    Matches exactly how each reader activates the flag (``== "1"``), so the
    guard's notion of "ON" can never diverge from the code that honours it.
    """
    return os.environ.get(name, "0") == "1"


def assert_prod_safe_flags() -> None:
    """Refuse to boot if a dangerous test/mock bypass flag is ON under prod.

    Fail-closed boot assertion (VT-434). Runs at FastAPI startup (main.py
    lifespan). On dev (``EXPECTED_ENV`` != ``prod``) it is a no-op — the mock
    flags stay usable for tests/canary. Under ``EXPECTED_ENV=prod`` it raises
    ``ProdSafetyError`` (process fails to boot) if ANY flag in
    ``_PROD_FORBIDDEN_FLAGS`` is on. Idempotent + side-effect-free; safe to call
    from every boot path.
    """
    if not _is_prod():
        return

    offenders = [name for name in _PROD_FORBIDDEN_FLAGS if _flag_on(name)]
    if offenders:
        raise ProdSafetyError(
            "EXPECTED_ENV=prod but dangerous test/mock bypass flag(s) are ON: "
            f"{', '.join(offenders)}. These are auth-bypass / send-suppression "
            "switches that must NEVER run in production (mock OTP approves any "
            "login; mock send silently drops every real customer message). "
            "Refusing to boot (VT-434 fail-closed prod guard). Unset the flag(s) "
            "on the prod environment."
        )


__all__ = [
    "ProdSafetyError",
    "assert_prod_safe_flags",
    "_PROD_FORBIDDEN_FLAGS",
]
