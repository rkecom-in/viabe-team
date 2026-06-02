"""VT-289 — OAuth-install state nonce (CSRF / tenant-binding hardening).

Replaces "state = raw tenant_id, trusted" across every OAuth-install callback. The
authenticated `/setup` path MINTS a single-use, expiring nonce bound to (tenant_id,
connector, target); the provider redirect carries the opaque `state`; the callback
ATOMICALLY CLAIMS it and derives the tenant from the stored record — NEVER from the
URL. An attacker who forges `state=<victim_tenant>` produces a nonce we never minted,
so the claim fails (the HIGH account-linking-CSRF item flagged on #227).

Storage: `oauth_install_state` (migration 068) is service-role-only (deny-all RLS) —
the callback has no tenant GUC and looks up BY state, so this uses the bare service
pool (`get_pool()`), not `tenant_connection`. The table holds only an opaque nonce +
tenant_id (no PII, CL-390).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)

_DEFAULT_TTL_MINUTES = 10  # ample for the ~5-min Embedded Signup click-through (Cowork)


@dataclass(frozen=True, slots=True)
class InstallState:
    """A claimed OAuth-install nonce. tenant_id is authoritative — read from the
    stored record, NOT from the callback URL."""

    tenant_id: UUID
    connector_id: str
    target: str | None


def mint_install_state(
    tenant_id: UUID | str,
    connector_id: str,
    *,
    target: str | None = None,
    ttl_minutes: int = _DEFAULT_TTL_MINUTES,
) -> str:
    """Mint + persist a single-use nonce for an OAuth-install flow.

    MUST be called only from an authenticated owner context (the `/setup` handlers
    guard with ``INTERNAL_API_SECRET``; team-web passes the verified tenant_id) — that
    is what makes the nonce trustworthy. Returns the opaque ``state`` string to embed
    in the provider authorize URL.
    """
    state = secrets.token_urlsafe(32)
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO oauth_install_state
                (state, tenant_id, connector_id, target, expires_at)
            VALUES (%s, %s, %s, %s, now() + make_interval(mins => %s))
            """,
            (state, str(tenant_id), connector_id, target, ttl_minutes),
        )
    logger.info(
        "oauth_install_state minted tenant=%s connector=%s", tenant_id, connector_id
    )
    return state


def claim_install_state(state: str, connector_id: str) -> InstallState | None:
    """Atomically claim a nonce: single-use, unexpired, connector-matched.

    The ``UPDATE ... WHERE used_at IS NULL AND expires_at > now()`` is the race-safe
    consume — a replay (already-used), an expired nonce, an unknown/forged state, or a
    connector mismatch all return zero rows → ``None``. On success returns the stored
    record; the caller MUST use ``record.tenant_id`` (never the URL's ``state``).
    """
    if not state:
        return None
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE oauth_install_state
               SET used_at = now()
             WHERE state = %s
               AND connector_id = %s
               AND used_at IS NULL
               AND expires_at > now()
            RETURNING tenant_id, connector_id, target
            """,
            (state, connector_id),
        )
        row = cur.fetchone()
    if row is None:
        logger.warning(
            "oauth_install_state claim REJECTED connector=%s (unknown/used/expired)",
            connector_id,
        )
        return None
    tenant_id = row["tenant_id"] if isinstance(row, dict) else row[0]
    conn_id = row["connector_id"] if isinstance(row, dict) else row[1]
    target = row["target"] if isinstance(row, dict) else row[2]
    return InstallState(
        tenant_id=UUID(str(tenant_id)),
        connector_id=str(conn_id),
        target=target if target is None else str(target),
    )


def _purge_expired(now_fn: Any = None) -> int:
    """Janitor: delete expired/used nonces. Returns rows deleted. (Optional; the
    claim is already safe against expired rows — this is housekeeping only.)"""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM oauth_install_state WHERE expires_at < now()")
        return cur.rowcount


__all__ = ["InstallState", "mint_install_state", "claim_install_state"]
