"""VT-332 — mint the trial-end deep-link token (orchestrator self-mint, Option 2 per Cowork).

A SINGLE, audience-scoped minter — audience ``trial-end-subscribe`` ONLY, NOT a general-purpose
owner-token minter. Audience separation is the safety line: a trial-end token must never be
accepted as an owner session (the verifiers enforce ``aud``). HS256 with OWNER_JWT_SECRET — the
orchestrator ALREADY holds that secret to VERIFY team-web tokens (ops_resolve), so self-minting
does not widen where the secret lives.

Verified team-web-side by ``verify-trial-end-token.ts``; SINGLE-USE is enforced by the jti
consume in razorpay-subscribe (``consumed_subscribe_tokens``). Minting is live only behind the
trial-end nudge, which stays DORMANT until go-live (the owner-WABA send + template are gated).
"""

from __future__ import annotations

import os
import time
from uuid import uuid4

import jwt as pyjwt

# The single audience this module mints for. Must match verify-trial-end-token.ts.
_AUDIENCE = "trial-end-subscribe"
_TTL_SEC = 7 * 24 * 60 * 60  # 7 days — the trial-end nudge deep-link window.


class TrialEndSecretMissing(RuntimeError):
    """OWNER_JWT_SECRET unset — cannot mint (fail closed, never mint an unsigned/blank-key token)."""


def mint_trial_end_token(tenant_id: str, plan_tier: str) -> tuple[str, str]:
    """Mint an HS256 trial-end-subscribe token. Returns ``(token, jti)`` — the jti is the
    single-use key, forwarded to razorpay-subscribe for the atomic consume. 7-day TTL."""
    secret = os.environ.get("OWNER_JWT_SECRET", "")
    if not secret:
        raise TrialEndSecretMissing("OWNER_JWT_SECRET unset — cannot mint trial-end token")
    jti = str(uuid4())
    now = int(time.time())
    token = pyjwt.encode(
        {
            "tenant_id": tenant_id,
            "plan_tier": plan_tier,
            "aud": _AUDIENCE,
            "jti": jti,
            "iat": now,
            "exp": now + _TTL_SEC,
        },
        secret,
        algorithm="HS256",
    )
    return token, jti


def build_subscribe_deep_link(base_url: str, plan_tier: str, token: str) -> str:
    """The trial-end nudge deep-link: ``<base>/team/subscribe?plan=<tier>&token=<jwt>``. DORMANT
    — the real nudge that embeds this stays gated (owner-WABA send stub + NEEDS-FAZAL template)."""
    return f"{base_url.rstrip('/')}/team/subscribe?plan={plan_tier}&token={token}"
