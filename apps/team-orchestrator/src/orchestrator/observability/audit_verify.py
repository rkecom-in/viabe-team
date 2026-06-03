"""VT-80 — privacy_audit_log chain verification (ops/service function).

``verify_chain`` walks the global chain in ``seq`` order and recomputes each
row's ``this_hash`` from its stored fields, checking both (a) the recomputed
hash matches the stored ``this_hash`` and (b) each row's ``prev_hash`` equals
the previous row's ``this_hash``. The first mismatch is reported (tamper /
corruption detection).

Run on a BYPASSRLS pool connection — verification is global (all tenants +
NULL-tenant rows), an ops concern, not a per-tenant read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from orchestrator.observability.audit_log import _canonical, compute_this_hash

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChainVerifyResult:
    ok: bool
    rows_checked: int
    broken_seq: int | None = None
    reason: str | None = None


def verify_chain(conn: Any, *, since_seq: int | None = None) -> ChainVerifyResult:
    """Verify the hash-chain. ``conn`` must see all rows (BYPASSRLS pool conn)."""
    sql = (
        "SELECT seq, tenant_id::text AS tenant_id, event_type, payload, actor, "
        "prev_hash, this_hash FROM privacy_audit_log"
    )
    params: list[Any] = []
    if since_seq is not None:
        sql += " WHERE seq >= %s"
        params.append(since_seq)
    sql += " ORDER BY seq ASC"

    rows = conn.execute(sql, params).fetchall()

    prev_this: str | None = None
    checked = 0
    for r in rows:
        if isinstance(r, dict):
            seq = r["seq"]
            tenant_id = r["tenant_id"]
            event_type = r["event_type"]
            payload = r["payload"]
            actor = r["actor"]
            row_prev = r["prev_hash"]
            row_this = r["this_hash"]
        else:
            seq, tenant_id, event_type, payload, actor, row_prev, row_this = r

        # (a) linkage: prev_hash must equal the previous row's this_hash. When
        # verifying a suffix (since_seq), seed from the first row's stored prev.
        if checked == 0 and since_seq is not None:
            prev_this = row_prev
        if row_prev != prev_this:
            return ChainVerifyResult(
                ok=False,
                rows_checked=checked,
                broken_seq=seq,
                reason=(
                    f"prev_hash linkage broken at seq={seq}: "
                    f"stored prev={row_prev!r} expected={prev_this!r}"
                ),
            )

        # (b) integrity: recomputed this_hash must match the stored one.
        canonical = _canonical(
            tenant_id=tenant_id,
            event_type=event_type,
            payload=payload,
            actor=actor,
        )
        recomputed = compute_this_hash(row_prev, canonical)
        if recomputed != row_this:
            return ChainVerifyResult(
                ok=False,
                rows_checked=checked,
                broken_seq=seq,
                reason=(
                    f"this_hash mismatch at seq={seq}: "
                    f"stored={row_this!r} recomputed={recomputed!r}"
                ),
            )

        prev_this = row_this
        checked += 1

    return ChainVerifyResult(ok=True, rows_checked=checked)


def run_audit_chain_verify_body(now: Any = None) -> ChainVerifyResult:
    """Nightly verification body (testable directly; ``now`` accepted for parity
    with the other scheduled bodies). Verifies the full chain on a BYPASSRLS
    pool connection and logs CRITICAL on a break.

    NOTE (VT-80 / flagged): the ``@DBOS.scheduled`` registration into the VT-3.5
    scheduler is a tight fast-follow — the scheduled registration shifts DBOS
    ``app_version`` (recovery-sensitive; see scheduled_triggers.register docstring)
    and deserves its own scheduler canary. This body is wired + tested so the
    follow-up is a one-line ``DBOS.scheduled(CRON)(handler)`` + alert routing.
    """
    from orchestrator.graph import get_pool

    with get_pool().connection() as conn:
        result = verify_chain(conn)
    if result.ok:
        logger.info(
            "VT-80 audit-chain verify OK (%d rows)", result.rows_checked
        )
    else:
        logger.critical(
            "VT-80 audit-chain verify FAILED at seq=%s: %s",
            result.broken_seq,
            result.reason,
        )
    return result


__all__ = [
    "ChainVerifyResult",
    "run_audit_chain_verify_body",
    "verify_chain",
]
