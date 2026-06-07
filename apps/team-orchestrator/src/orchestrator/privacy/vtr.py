"""VT-281 — VTR (Viabe Team Representative) de-identified access.

Fork A (Cowork plan-ack 20260606T231500Z): the VTR sees customer data ONLY through the
de-identified views (`vtr_customers`, `vtr_escalations`, migration 115), entered as `app_vtr_role`
— a role with NO grant on the raw PII tables (customers, phone_token_resolutions) or the decrypt
function, so PII is UNREACHABLE from the VTR even via an app bug (not merely masked app-side).

This module provides:
- :func:`bootstrap_vtr_ref_secret` — seed the REF# keying secret from env VT_REF_HMAC_KEY (the
  views compute `REF# = HMAC(customer_id, secret)` via the view owner's rights; the secret is never
  granted to app_vtr_role). Env, never client.
- :func:`vtr_connection` — a pooled connection with `SET ROLE app_vtr_role` for the duration (the
  VT-280 digest + the canary read the views through this). Mirrors `tenant_connection`'s SET ROLE.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from orchestrator.graph import get_pool

_REF_KEY_ENV = "VT_REF_HMAC_KEY"


def bootstrap_vtr_ref_secret(key: str | None = None, *, pool: Any | None = None) -> bool:
    """Idempotently seed `vtr_ref_secret` (the singleton REF# keying secret) from env (or ``key``).

    Returns True if a secret is present after the call. Service-role (the table is deny-all RLS;
    app_vtr_role can't reach it). The orchestrator calls this at startup; the env value is the
    source of truth (env, never client). Raises if no key is available — a missing key would make
    the views emit NULL refs, which must fail loud, not silently de-correlate the VTR view."""
    secret = key or os.environ.get(_REF_KEY_ENV)
    if not secret:
        raise RuntimeError(
            f"{_REF_KEY_ENV} is not set — the VTR REF# keying secret is required (VT-281)"
        )
    active_pool = pool if pool is not None else get_pool()
    with active_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO vtr_ref_secret (id, secret) VALUES (true, %s) "
            "ON CONFLICT (id) DO UPDATE SET secret = EXCLUDED.secret",
            (secret,),
        )
    return True


@contextmanager
def vtr_connection(*, pool: Any | None = None) -> Iterator[Any]:
    """Check out a pooled connection with `SET ROLE app_vtr_role` for the duration.

    The VTR path (VT-280 digest, the canary) reads ONLY the de-identified views through this — a
    raw-PII read raises permission-denied because app_vtr_role has no grant on the base tables.
    `RESET ROLE` on exit so the pooled connection carries no leaked state."""
    active_pool = pool if pool is not None else get_pool()
    with active_pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET ROLE app_vtr_role")
        try:
            yield conn
        finally:
            with conn.cursor() as cur:
                cur.execute("RESET ROLE")
