"""VT-620 — synthetic test-tenant reaper.

THE NOISE
---------
The convo e2e/canary harness (``canaries/convo_harness.py``) creates a fresh tenant per run,
named ``convo-harness-…``, drives it, and tears it down. When a run is killed mid-flight (or the
teardown FK-sweep silently fails to fully delete — the ``cmd_teardown`` bug this row also fixes),
the synthetic tenant + its ``pipeline_runs`` / ``pipeline_steps`` LEAK. Their runs then flow into
the VT-202 alert detectors and page ops on pure test artifacts. P1a suppresses the alerts at the
detector; this reaper GC's the leaked tenants so they stop accumulating.

WHY A TIME FLOOR IS SAFE
------------------------
A live harness run holds its tenant for at most a few minutes (drive + teardown). Anything named
``convo-harness-…`` still present past a conservative 1-hour floor is a leaked artifact, never a
live run — so the reaper can never race an in-flight harness tenant. STRICT scope: ONLY the
``convo-harness-%`` name pattern is ever touched (never a real customer tenant).

WHAT IT DOES
------------
At boot (best-effort catch-up, alongside ``reap_orphan_runs``) and hourly (@DBOS.scheduled), delete
every ``convo-harness-…`` tenant older than the floor, FK-safely (``fk_safe_delete_tenant`` —
shared with ``convo_harness.cmd_teardown``). Service-role, autocommit, NEVER raises (a reaper
failure must not block boot or crash the scheduler; mirrors ``orphan_reaper.reap_orphan_runs``).
"""

from __future__ import annotations

import logging
from typing import Any

from orchestrator.orphan_reaper import _service_pool  # reuse the exact service-role pool helper

logger = logging.getLogger(__name__)

# STRICT scope — the reaper only ever matches tenants the harness created. Never a real tenant.
_TEST_TENANT_NAME_PREFIX = "convo-harness-"

# Floor past which a still-present convo-harness tenant is certainly a leaked artifact (>> the few
# minutes a live harness run holds its tenant). Conservative on purpose (correctness over promptness).
_REAP_AGE_HOURS = 1

# A non-cascade FK table may itself be referenced by another non-cascade FK table; bound the
# ordering passes so a genuine dependency cycle can't spin forever.
_MAX_FK_PASSES = 8


def fk_safe_delete_tenant(conn, tenant_id: str) -> list[tuple[str, str]]:
    """Delete one tenant + all its rows, FK-safely. Returns still-blocked (table,col) list
    (EMPTY == fully deleted). NEVER swallows silently (that was the cmd_teardown bug). autocommit conn."""
    conn.execute("DELETE FROM pipeline_steps WHERE tenant_id = %s", (tenant_id,))  # unblocks pipeline_runs
    noncascade = conn.execute(
        "SELECT DISTINCT cl.relname AS tbl, att.attname AS col FROM pg_constraint con "
        "JOIN pg_class cl ON cl.oid=con.conrelid "
        "JOIN pg_attribute att ON att.attrelid=con.conrelid AND att.attnum=ANY(con.conkey) "
        "WHERE con.contype='f' AND con.confrelid='public.tenants'::regclass AND con.confdeltype<>'c'"
    ).fetchall()
    remaining = [(r[0], r[1]) if not isinstance(r, dict) else (r["tbl"], r["col"]) for r in noncascade]
    for _ in range(_MAX_FK_PASSES):
        still, progressed = [], False
        for tbl, col in remaining:
            try:
                conn.execute(f'DELETE FROM "{tbl}" WHERE "{col}" = %s', (tenant_id,))  # noqa: S608 catalog-derived
                progressed = True
            except Exception:  # noqa: BLE001 — blocked by another non-cascade table; retry next pass
                still.append((tbl, col))
        remaining = still
        if not remaining or not progressed:
            break
    conn.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
    left = conn.execute("SELECT 1 FROM tenants WHERE id = %s", (tenant_id,)).fetchone()
    return remaining if left is not None else []


def reap_test_tenants(*, pool: Any = None, age_hours: int = _REAP_AGE_HOURS) -> int:
    """Delete leaked ``convo-harness-…`` tenants older than ``age_hours``, FK-safely.

    Best-effort + idempotent (only still-present harness tenants match). Returns the number of
    tenants fully deleted. NEVER raises — a reaper failure must not block boot or crash the
    scheduler (mirrors ``reap_orphan_runs``). Service-role (cross-tenant by design), autocommit
    (a per-tenant FK failure runs in its own txn and can't poison the sweep). STRICT scope: only
    the ``convo-harness-%`` name pattern is ever deleted.
    """
    try:
        deleted = 0
        with _service_pool(pool).connection() as conn:
            rows = conn.execute(
                "SELECT id FROM tenants "
                "WHERE business_name LIKE %s "
                "  AND created_at < now() - make_interval(hours => %s)",
                (f"{_TEST_TENANT_NAME_PREFIX}%", age_hours),
            ).fetchall()
            for row in rows:
                tid = str(row[0] if not isinstance(row, dict) else row["id"])
                blocked = fk_safe_delete_tenant(conn, tid)
                if blocked:
                    logger.warning(
                        "VT-620 test-tenant reaper: tenant %s not fully deleted — still blocked by %s",
                        tid, blocked,
                    )
                else:
                    deleted += 1
        if deleted:
            logger.warning(
                "VT-620 test-tenant reaper: deleted %d leaked convo-harness tenant(s) (>%dh old)",
                deleted, age_hours,
            )
        else:
            logger.info("VT-620 test-tenant reaper: no leaked convo-harness tenants to reap")
        return deleted
    except Exception:  # noqa: BLE001 — best-effort by design; must never block boot / crash scheduler
        logger.warning("VT-620 test-tenant reaper sweep failed (best-effort)", exc_info=True)
        return 0


__all__ = [
    "fk_safe_delete_tenant",
    "reap_test_tenants",
]
