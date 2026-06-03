"""VT-80 — privacy_audit_log tamper-evident hash-chain (the real one).

Replaces the Phase-1 placeholder this_hash writers (phone_tokens, dsr_purge).
One canonical append path (Pillar 8): ``log_privacy_event``.

Chain model (Cowork 20260603T150000Z): GLOBAL, single chain across all rows
(every tenant + NULL-tenant workspace events), ordered by ``seq`` (BIGSERIAL,
mig 079). Each row's ``this_hash = sha256(prev_hash || canonical_json(fields))``
where prev_hash is the previous row's this_hash. Tamper-evident: changing any
row breaks every subsequent this_hash (verify in audit_verify.py).

Concurrency: the read-head + insert run under ``pg_advisory_xact_lock`` inside a
transaction, so concurrent writers serialise and the chain stays linear.

Connection: pass a BYPASSRLS pool connection (``graph.get_pool()``) — the global
head-read must see rows across all tenants (RLS would hide them). The existing
callers (phone_tokens.resolve_phone_token, dsr_purge) already hold such a conn.
CL-390: payload must carry NO raw PII (phone/body/name) — only tokens/ids/counts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

# Fixed advisory-lock key — one global chain ⇒ one lock. VT-80 mnemonic.
_CHAIN_LOCK_KEY = 8080_8080


def _canonical(
    *,
    tenant_id: UUID | str | None,
    event_type: str,
    payload: dict[str, Any],
    actor: str | None,
) -> str:
    """Deterministic serialization of the hashed fields (sorted, compact).

    ``event_at`` is deliberately NOT hashed: TIMESTAMPTZ round-trips are
    timezone/format-fragile, and the append-only trigger (mig 079) already makes
    every column — incl. event_at — immutable, so it cannot be tampered
    post-insert. The chain protects tenant_id / event_type / payload / actor +
    their order; verify recomputes from those stored columns deterministically.
    """
    return json.dumps(
        {
            "tenant_id": str(tenant_id) if tenant_id is not None else None,
            "event_type": event_type,
            "payload": payload,
            "actor": actor,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def compute_this_hash(prev_hash: str | None, canonical: str) -> str:
    """sha256(prev_hash || canonical). prev_hash NULL (genesis) → empty prefix."""
    return hashlib.sha256(((prev_hash or "") + canonical).encode("utf-8")).hexdigest()


def _scalar(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


def log_privacy_event(
    conn: Any,
    *,
    tenant_id: UUID | str | None,
    event_type: str,
    payload: dict[str, Any],
    actor: str | None,
) -> str:
    """Append one row to the global hash-chain. Returns the new this_hash.

    ``conn`` must be a BYPASSRLS pool connection (see module docstring). The
    advisory-lock + head-read + insert run in one transaction so concurrent
    appends serialise into a linear chain.
    """
    event_at = datetime.now(UTC)
    canonical = _canonical(
        tenant_id=tenant_id,
        event_type=event_type,
        payload=payload,
        actor=actor,
    )
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_CHAIN_LOCK_KEY,))
        head = conn.execute(
            "SELECT this_hash FROM privacy_audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = _scalar(head)
        this_hash = compute_this_hash(prev_hash, canonical)
        conn.execute(
            """
            INSERT INTO privacy_audit_log
              (tenant_id, event_type, payload, prev_hash, this_hash, event_at, actor)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(tenant_id) if tenant_id is not None else None,
                event_type,
                Jsonb(payload),
                prev_hash,
                this_hash,
                event_at,
                actor,
            ),
        )
    return this_hash


def list_events(
    conn: Any, *, limit: int = 100, since_seq: int | None = None
) -> list[dict[str, Any]]:
    """Read API. Tenant-scoped if ``conn`` is an RLS tenant_connection; global
    if a BYPASSRLS pool connection. Newest-first.
    """
    sql = (
        "SELECT seq, tenant_id::text AS tenant_id, event_type, payload, "
        "prev_hash, this_hash, event_at, actor FROM privacy_audit_log"
    )
    params: list[Any] = []
    if since_seq is not None:
        sql += " WHERE seq > %s"
        params.append(since_seq)
    sql += " ORDER BY seq DESC LIMIT %s"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            out.append(dict(r))
        else:
            out.append(
                {
                    "seq": r[0],
                    "tenant_id": r[1],
                    "event_type": r[2],
                    "payload": r[3],
                    "prev_hash": r[4],
                    "this_hash": r[5],
                    "event_at": r[6],
                    "actor": r[7],
                }
            )
    return out


__all__ = ["compute_this_hash", "list_events", "log_privacy_event"]
