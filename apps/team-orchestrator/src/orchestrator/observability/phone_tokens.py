"""VT-184 phone-token resolution writer + audit logging.

⚠️  WARNING: ``phone_number_encrypted`` column stores PLAINTEXT in
    Phase-1 — encryption follow-up tracked under VT-191 (filed by
    Cowork post-merge per plan-review Cond 1). Pre-prod gate: encrypt
    all rows before first production tenant onboarding. Three
    defense-in-depth layers warn against shipping the placeholder:
      Layer 1 — migration 026 (``COMMENT ON COLUMN`` runtime-visible)
      Layer 2 — this module docstring
      Layer 3 — ``register_phone_token`` function docstring

Companion to VT-104's PII redactor: when a phone number is redacted to
``phone_tok_<hash>``, this module persists the token → phone_e164
mapping in ``phone_token_resolutions`` (VT-178 + VT-187 schema). The
resolution path (operator-only read; VT-188 substrate) writes an audit
row to ``privacy_audit_log`` (CL-150 / migration 008) for DPDP
compliance.

Per CL-417 / VT-187: canonical columns only (phone_token, tenant_id,
customer_id, phone_number_encrypted, resolved_count, last_accessed_at,
created_at). customer_id has NO FK per CL-417 Cond 1.

Per CL-416: NO delete code path. VT-185 owns DSR purge.
Per CL-82: tenant_connection RLS GUC for writes.
Per CL-104: token format byte-identical with VT-104 redactor's
``_hash_phone`` (``phone_tok_<sha256[:16]>``).
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from orchestrator.db.tenant_connection import tenant_connection
from orchestrator.graph import get_pool

logger = logging.getLogger(__name__)


def _salt() -> str:
    """Same salt as ``orchestrator.privacy.pii_redactor._salt`` so tokens
    produced here are byte-identical with the redactor's output."""
    salt = os.environ.get("TEAM_PHONE_HASH_SALT", "")
    if not salt:
        return "vt-101-fallback-salt-not-for-prod"
    return salt


def _hash_phone(phone_e164: str) -> str:
    """Byte-identical with VT-104 ``_hash_phone`` (``pii_redactor.py:140``)."""
    digest = hashlib.sha256(f"{_salt()}:phone:{phone_e164}".encode()).hexdigest()
    return f"phone_tok_{digest[:16]}"


def _audit_this_hash(operator_id: str, phone_token: str) -> str:
    """Phase-1 stub ``this_hash`` for ``privacy_audit_log``.

    VT-150 owns the real hash-chain computation (prev_hash + this_hash
    forming a tamper-evident chain). This writer emits a minimal
    deterministic hash so the column constraint (NOT NULL) is
    satisfied; full chain integrity becomes VT-150's responsibility
    when that row's writer lands.
    """
    raw = f"{operator_id}:{phone_token}:{_salt()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def register_phone_token(
    *,
    tenant_id: UUID,
    phone_e164: str,
    customer_id: UUID | None = None,
) -> str:
    """UPSERT a ``phone_token_resolutions`` row. Idempotent.

    ⚠️  WARNING: ``phone_e164`` is stored AS PLAINTEXT in the
        ``phone_number_encrypted`` column in Phase-1. VT-191 wires
        real encryption (Fernet/AES + ``TEAM_PHONE_ENCRYPTION_KEY``)
        + back-fills existing rows. **DO NOT promote this code to
        production without VT-191 shipped.**

    Returns the deterministic token (byte-identical with VT-104
    redactor's ``_hash_phone`` output). Multiple calls with the same
    ``(tenant_id, phone_e164)`` produce the same token and do NOT
    duplicate the row (``ON CONFLICT (phone_token) DO NOTHING``).

    ``resolved_count`` stays at 0 on register; only ``resolve_phone_token``
    increments it.

    Per CL-417 / VT-187 canonical column shape.

    Connection: ``phone_token_resolutions`` follows the BY-GRANT-EXCLUSION
    pattern (VT-178) — only service-role has INSERT/UPDATE/SELECT. The
    write path uses the service-role pool directly. ``tenant_id`` is
    bound as a column value (not via GUC) so the RLS policy's
    ``app_current_tenant()`` check still validates correctly when
    operator-role SELECTs read the row (VT-188 substrate).
    """
    token = _hash_phone(phone_e164)
    pool = get_pool()
    with pool.connection() as conn, conn.transaction():
        conn.execute(
            """
            INSERT INTO phone_token_resolutions (
              phone_token, tenant_id, phone_number_encrypted, customer_id,
              resolved_count, last_accessed_at, created_at
            ) VALUES (%s, %s, %s, %s, 0, NULL, now())
            ON CONFLICT (phone_token) DO NOTHING
            """,
            (
                token,
                str(tenant_id),
                phone_e164,
                str(customer_id) if customer_id else None,
            ),
        )
    return token


def resolve_phone_token(
    *,
    tenant_id: UUID,
    phone_token: str,
    operator_id: str,
) -> str | None:
    """Fetch phone_e164 from the row + increment counters + audit log.

    Returns the stored phone_e164 (PLAINTEXT in Phase-1 per Cond 1),
    or ``None`` when the token is unresolvable under the current GUC
    (RLS-denied or doesn't exist).

    EVERY call (including misses) writes a row to ``privacy_audit_log``
    with ``event_type='phone_token_resolved'`` so the audit trail
    captures attempted resolutions per CL-150 DPDP retention.

    Connection: both ``phone_token_resolutions`` and
    ``privacy_audit_log`` follow the BY-GRANT-EXCLUSION pattern (no
    app_role grants per migrations 007 + 008 — created before
    migration 015's default-privileges grant). Service-role pool runs
    BOTH the UPDATE and the audit INSERT in a single transaction so a
    resolution never lands without a paired audit row. The UPDATE's
    WHERE clause carries ``tenant_id = %s`` for cross-tenant isolation
    at the SQL layer (RLS would normally enforce this, but service-role
    bypasses RLS; explicit predicate preserves the contract).

    VT-188 (parallel batch) adds an operator-role policy +
    ``app_operator_audit_enabled()`` helper for client-direct JWT
    resolution path. Until VT-188 ships, the backend service-role +
    explicit audit log is the resolution path.
    """
    pool = get_pool()
    phone_e164: str | None = None
    payload_resolved: bool = False
    with pool.connection() as conn, conn.transaction():
        row = conn.execute(
            """
            UPDATE phone_token_resolutions
               SET resolved_count = COALESCE(resolved_count, 0) + 1,
                   last_accessed_at = now()
             WHERE phone_token = %s
               AND tenant_id = %s
            RETURNING phone_number_encrypted
            """,
            (phone_token, str(tenant_id)),
        ).fetchone()
        if row:
            phone_e164 = row["phone_number_encrypted"]
            payload_resolved = True

        payload: dict[str, Any] = {
            "phone_token": phone_token,
            "resolved": payload_resolved,
            "operator_id": operator_id,
        }
        conn.execute(
            """
            INSERT INTO privacy_audit_log (
              tenant_id, event_type, payload, this_hash, actor
            ) VALUES (
              %s, 'phone_token_resolved', %s, %s, %s
            )
            """,
            (
                str(tenant_id),
                Jsonb(payload),
                _audit_this_hash(operator_id, phone_token),
                operator_id,
            ),
        )
    return phone_e164


__all__ = ["register_phone_token", "resolve_phone_token"]
