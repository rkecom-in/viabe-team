"""VT-191 phone-token resolution writer with Fernet encryption-at-rest.

VT-184 Phase-1 stored phone_e164 as plaintext in
``phone_token_resolutions.phone_number_encrypted`` with a 3-layer
⚠️ WARNING flagging it for VT-191. VT-191 closes that loophole:
phone numbers are now Fernet-encrypted (symmetric AES-128-CBC + HMAC-
SHA256) via the ``TEAM_PHONE_ENCRYPTION_KEY`` env var.

Key generation::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Dev key lives in ``.viabe/secrets/supabase-dev.env`` (gitignored). Prod
key is generated separately during deployment; never reuse the dev key.

Key rotation::

    from orchestrator.observability.phone_tokens import _rotate_encryption_key
    rotated_count = _rotate_encryption_key(old_key, new_key)

``_rotate_encryption_key`` reads every row, decrypts with ``old_key``,
re-encrypts with ``new_key``, UPDATEs in a single transaction. Phase 1
= single static key per env per VT-191 Q1 Option A (Cowork plan-review
locked). Versioned keys are a Phase-2 follow-up if scale demands.

Companion to VT-104's PII redactor: when a phone number is redacted to
``phone_tok_<hash>``, this module persists the token → phone_e164
mapping in ``phone_token_resolutions`` (VT-178 + VT-187 schema). The
resolution path (operator-only read; VT-188 substrate) writes an audit
row to ``privacy_audit_log`` (CL-150 / migration 008) for DPDP
compliance.

Per CL-390: PII de-anonymization auditable; ciphertext-at-rest closes
the local-plaintext loophole.
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

from cryptography.fernet import Fernet, InvalidToken

from orchestrator.graph import get_pool
from orchestrator.observability.audit_log import log_privacy_event

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


def _fernet() -> Fernet:
    """Return Fernet instance keyed by ``TEAM_PHONE_ENCRYPTION_KEY`` env.

    Fail loud on missing key: encryption is mandatory in production.
    Dev env loads the key from ``.viabe/secrets/supabase-dev.env`` via
    subshell-source.
    """
    key = os.environ.get("TEAM_PHONE_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError(
            "TEAM_PHONE_ENCRYPTION_KEY env not set. Generate via: "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_phone(plaintext: str) -> str:
    """Fernet-encrypt a phone_e164 string.

    Returns a base64-URL-safe string (Fernet's native format), suitable
    for the TEXT ``phone_number_encrypted`` column. Each invocation
    produces a distinct ciphertext (Fernet randomizes the IV per call)
    even for the same plaintext.
    """
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_phone(ciphertext: str) -> str:
    """Fernet-decrypt a base64-URL-safe ciphertext.

    Raises ``cryptography.fernet.InvalidToken`` on wrong key, corrupted
    ciphertext, or plaintext input (the latter is how the back-fill
    script detects plaintext orphans).
    """
    return _fernet().decrypt(ciphertext.encode()).decode()


def register_phone_token(
    *,
    tenant_id: UUID,
    phone_e164: str,
    customer_id: UUID | None = None,
) -> str:
    """UPSERT a ``phone_token_resolutions`` row. Idempotent.

    Phone is Fernet-encrypted before INSERT (VT-191 per CL-390). The
    stored ciphertext is base64-URL-safe text suitable for the TEXT
    column. Each call's ciphertext differs (Fernet IV randomization),
    so re-registering the same (tenant_id, phone_e164) doesn't update
    the row (ON CONFLICT (phone_token) DO NOTHING preserves the
    original ciphertext).

    Returns the deterministic token (byte-identical with VT-104
    redactor's ``_hash_phone`` output). Multiple calls with the same
    ``(tenant_id, phone_e164)`` produce the same token and do NOT
    duplicate the row.

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
    encrypted = encrypt_phone(phone_e164)
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
                encrypted,
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
    """Fetch + decrypt phone_e164; increment counters; audit log.

    Returns the decrypted phone_e164 string (Fernet-decrypted from the
    column ciphertext), or ``None`` when the token is unresolvable
    under the current GUC (RLS-denied or doesn't exist) or when
    decryption fails (key rotation in flight; corrupted row).

    EVERY call (including misses + decryption failures) writes a row
    to ``privacy_audit_log`` with ``event_type='phone_token_resolved'``
    so the audit trail captures attempted resolutions per CL-150 DPDP
    retention.

    Connection: both ``phone_token_resolutions`` and
    ``privacy_audit_log`` follow the BY-GRANT-EXCLUSION pattern (no
    app_role grants per migrations 007 + 008 — created before
    migration 015's default-privileges grant). Service-role pool runs
    BOTH the UPDATE and the audit INSERT in a single transaction so a
    resolution never lands without a paired audit row. The UPDATE's
    WHERE clause carries ``tenant_id = %s`` for cross-tenant isolation
    at the SQL layer (RLS would normally enforce this, but service-role
    bypasses RLS; explicit predicate preserves the contract).

    VT-188 added the operator-role JWT-client-direct path. Until that
    UI lands, the backend service-role + explicit audit log is the
    resolution path.
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
            ciphertext = row["phone_number_encrypted"]
            try:
                phone_e164 = decrypt_phone(ciphertext)
                payload_resolved = True
            except InvalidToken:
                logger.error(
                    "VT-191 decrypt failed; key rotation in flight or row corrupt",
                    extra={
                        "phone_token": phone_token,
                        "tenant_id": str(tenant_id),
                    },
                )
                phone_e164 = None
                payload_resolved = False

        payload: dict[str, Any] = {
            "phone_token": phone_token,
            "resolved": payload_resolved,
            "operator_id": operator_id,
        }
        # VT-80: tamper-evident hash-chain append (replaces the Phase-1 stub).
        # Runs on the same BYPASSRLS service conn + transaction.
        log_privacy_event(
            conn,
            tenant_id=tenant_id,
            event_type="phone_token_resolved",
            payload=payload,
            actor=operator_id,
        )
    return phone_e164


def _rotate_encryption_key(old_key: str, new_key: str) -> int:
    """Re-encrypt every ``phone_token_resolutions`` row from ``old_key`` to ``new_key``.

    Reads each row's ciphertext, decrypts with ``old_key``, re-encrypts
    with ``new_key``, UPDATEs in a single transaction. Returns the
    count of rows rotated.

    Rows that fail to decrypt under ``old_key`` are skipped + logged
    (already on the new key OR corrupt). Caller can detect partial
    rotation by comparing the return value against the table row
    count.

    Phase-1 single-key rotation procedure:
      1. Generate new_key via ``Fernet.generate_key()``.
      2. Call ``_rotate_encryption_key(old, new)`` (this function).
      3. Update ``TEAM_PHONE_ENCRYPTION_KEY`` env to new_key.
      4. Restart workers.

    Phase-2 versioned-keys path (VT-N follow-up) replaces this with
    overlapping-key support so step 3 doesn't require simultaneous
    rotation across all workers.
    """
    old_fernet = Fernet(old_key.encode() if isinstance(old_key, str) else old_key)
    new_fernet = Fernet(new_key.encode() if isinstance(new_key, str) else new_key)
    pool = get_pool()
    rotated = 0
    with pool.connection() as conn, conn.transaction():
        rows = conn.execute(
            "SELECT phone_token, phone_number_encrypted "
            "FROM phone_token_resolutions "
            "WHERE phone_number_encrypted IS NOT NULL"
        ).fetchall()
        for row in rows:
            try:
                plain = old_fernet.decrypt(
                    row["phone_number_encrypted"].encode()
                ).decode()
            except InvalidToken:
                logger.warning(
                    "VT-191 rotation skip: row not on old_key",
                    extra={"phone_token": row["phone_token"]},
                )
                continue
            new_ct = new_fernet.encrypt(plain.encode()).decode()
            conn.execute(
                "UPDATE phone_token_resolutions "
                "SET phone_number_encrypted = %s "
                "WHERE phone_token = %s",
                (new_ct, row["phone_token"]),
            )
            rotated += 1
    return rotated


__all__ = [
    "register_phone_token",
    "resolve_phone_token",
    "encrypt_phone",
    "decrypt_phone",
]
