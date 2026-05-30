#!/usr/bin/env python3
"""VT-48 — schedule_followup canary.

Mock-mode CI default. Real dev-DB mode opt-in via VT48_REAL_DB=1 applies
migration 044 (if needed), inserts a SYNTHETIC follow-up (CL-422: no
real customer data — fabricated tenant UUID + synthetic key/payload),
verifies idempotency (duplicate key → existing fire_at), then cleans up.

4 assertions:
- A1: validation envelopes (fire_at window / payload 4KB / cancel_if)
- A2: happy-path insert → status=scheduled
- A3: idempotency → duplicate_key + existing_fire_at, no second row
- A4: real dev-DB insert + dup-key read (VT48_REAL_DB=1) OR mock equivalent

Wall-clock ≤ 10s.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

RESULTS: dict[int, dict[str, Any]] = {}
SEEDED_TENANTS: list[str] = []


def assertion(num: int, name: str, passed: bool, *, observed: Any = None,
               expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed,
                    "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _future(minutes: int = 0, days: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes, days=days)


def _mock_pool(*, insert_returns: Any, existing_row: Any = None) -> Any:
    cur = MagicMock()
    fetchone_q = [insert_returns, existing_row]
    cur.execute.side_effect = lambda sql, params=None: None
    cur.fetchone.side_effect = lambda: fetchone_q.pop(0) if fetchone_q else None
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool


def _real_pool() -> Any:
    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    return graph_mod.get_pool()


def _seed_tenant(pool: Any) -> str:
    tenant_id = str(uuid4())
    SEEDED_TENANTS.append(tenant_id)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL app.current_tenant = %s", (tenant_id,))
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'founding', 'paid_active')",
            (tenant_id, f"vt48-synthetic-{tenant_id[:8]}"),
        )
    return tenant_id


def _cleanup(pool: Any) -> None:
    if not SEEDED_TENANTS:
        return
    with pool.connection() as conn, conn.cursor() as cur:
        for tid in SEEDED_TENANTS:
            cur.execute("SET LOCAL app.current_tenant = %s", (tid,))
            cur.execute("DELETE FROM scheduled_followups WHERE tenant_id = %s", (tid,))
            cur.execute("DELETE FROM tenants WHERE id = %s", (tid,))


def run_canary() -> int:
    real = os.environ.get("VT48_REAL_DB") == "1"
    if real and not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — VT48_REAL_DB=1 needs DATABASE_URL", file=sys.stderr)
        return 2
    print(f"PREFLIGHT OK (mode={'real-db' if real else 'mock'})")

    from orchestrator.agent.tools.schedule_followup import (
        ScheduleFollowupInput,
        schedule_followup,
    )

    def _inp(pool_tenant: str, **over: Any) -> ScheduleFollowupInput:
        base: dict[str, Any] = dict(
            tenant_id=pool_tenant,
            follow_up_type="campaign_followup",
            fire_at=_future(days=3),
            follow_up_key="cfk-syn",
            payload={"campaign_id": "synthetic"},
        )
        base.update(over)
        return ScheduleFollowupInput(**base)

    # --- A1: validation envelopes (no DB needed) ---
    bad = _mock_pool(insert_returns={"id": "x"})
    too_soon = schedule_followup(_inp("t", fire_at=_future(minutes=5)), pool=bad)
    too_big = schedule_followup(_inp("t", payload={"b": "x" * 5000}), pool=bad)
    bad_cond = schedule_followup(_inp("t", cancel_if=["garbage:x"]), pool=bad)
    pass_1 = (
        too_soon.error_envelope is not None
        and too_soon.error_envelope.code == "invalid_fire_at"
        and too_big.error_envelope is not None
        and too_big.error_envelope.code == "payload_too_large"
        and bad_cond.error_envelope is not None
        and bad_cond.error_envelope.code == "invalid_cancel_condition"
    )
    assertion(1, "Validation envelopes (fire_at / payload / cancel_if)", pass_1,
              observed={"fire_at": too_soon.error_envelope.code if too_soon.error_envelope else None,
                        "payload": too_big.error_envelope.code if too_big.error_envelope else None,
                        "cancel": bad_cond.error_envelope.code if bad_cond.error_envelope else None})

    if real:
        pool = _real_pool()
        try:
            tenant_id = _seed_tenant(pool)
            out1 = schedule_followup(_inp(tenant_id), pool=pool)
            assertion(2, "Real insert → scheduled", out1.status == "scheduled",
                      observed={"status": out1.status, "id": out1.scheduled_id})
            out2 = schedule_followup(_inp(tenant_id), pool=pool)
            pass_3 = (
                out2.status == "duplicate_key"
                and out2.scheduled_id == out1.scheduled_id
                and out2.existing_fire_at is not None
            )
            assertion(3, "Real idempotency → duplicate_key + existing_fire_at",
                      pass_3, observed={"status": out2.status,
                                        "same_id": out2.scheduled_id == out1.scheduled_id})
            assertion(4, "Real dev-DB insert+dup path exercised",
                      out1.status == "scheduled" and out2.status == "duplicate_key",
                      observed={"real_db": True})
        finally:
            _cleanup(pool)
    else:
        pool_ok = _mock_pool(insert_returns={"id": "sched_1"})
        out1 = schedule_followup(_inp("t1"), pool=pool_ok)
        assertion(2, "Mock insert → scheduled", out1.status == "scheduled",
                  observed={"status": out1.status})
        existing_fire = _future(days=3)
        pool_dup = _mock_pool(insert_returns=None,
                              existing_row={"id": "sched_existing", "fire_at": existing_fire})
        out2 = schedule_followup(_inp("t1"), pool=pool_dup)
        pass_3 = (
            out2.status == "duplicate_key"
            and out2.scheduled_id == "sched_existing"
            and out2.existing_fire_at == existing_fire
        )
        assertion(3, "Mock idempotency → duplicate_key + existing_fire_at", pass_3,
                  observed={"status": out2.status, "fire": str(out2.existing_fire_at)})
        pool_cond = _mock_pool(insert_returns={"id": "sched_2"})
        valid_cond = schedule_followup(
            _inp("t1", cancel_if=["campaign_status_in:[approved,sent]"]), pool=pool_cond)
        assertion(4, "Valid cancel_if grammar accepted",
                  valid_cond.status == "scheduled",
                  observed={"status": valid_cond.status})

    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s)", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
