#!/usr/bin/env python3
"""VT-79 breach-detection Phase-1 canary (Rule #15, DR-15).

Exercises the 3 Phase-1 detectors + notify_owner against a REAL Postgres.

- A1: Detector-1 — a tenant_isolation_breach pipeline_step → critical trigger
- A2: Detector-5 — unredacted phone in a pipeline_step payload → pii_in_log trigger
- A3: Detector-5 — clean payload → no trigger
- A4: Detector-3 — >threshold DSR tickets → dsr_rate_anomaly trigger
- A5: notify_owner — tenant with whatsapp_number → sent (mock)

Subshell-source supabase-dev.env (see vt196).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

RESULTS: dict[int, dict[str, Any]] = {}
INSERTED: list[str] = []


def assertion(num: int, name: str, passed: bool, *, observed: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")


def _tenant(pool: Any) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'trial', %s) ON CONFLICT (id) DO NOTHING",
            (tid, f"vt79-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
    INSERTED.append(tid)
    return tid


def _step(pool: Any, tid: str, *, kind: str, env: str) -> None:
    rid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'twilio_inbound', 'completed')",
            (rid, tid),
        )
        conn.execute(
            "INSERT INTO pipeline_steps (run_id, tenant_id, step_seq, step_kind, "
            "input_envelope, status) VALUES (%s, %s, 0, %s, %s::jsonb, 'completed')",
            (rid, tid, kind, env),
        )


def run_canary() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL missing", file=sys.stderr)
        sys.exit(2)
    print("PREFLIGHT OK")

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"], min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    os.environ.setdefault("TEAM_SUPABASE_DB_URL", os.environ["DATABASE_URL"])
    pool = graph_mod.get_pool()

    from orchestrator.alerts import breach_notification as bn
    from orchestrator.alerts.triggers import detect_pii_in_logs, detect_slow_triggers

    # A1 — tenant-isolation breach.
    tid = _tenant(pool)
    _step(pool, tid, kind="tenant_isolation_breach", env='{"v": "cross-tenant"}')
    t1 = detect_slow_triggers(tid)
    a1 = any(t.trigger_kind == "tenant_isolation_breach" and t.severity == "critical" for t in t1)
    assertion(1, "Detector-1 tenant-isolation breach → critical trigger", a1,
              observed=[t.trigger_kind for t in t1])

    # A2 — PII in log.
    tid2 = _tenant(pool)
    _step(pool, tid2, kind="webhook_received", env='{"leak": "+919812345678"}')
    p = detect_pii_in_logs(tid2)
    assertion(2, "Detector-5 unredacted phone → pii_in_log", any(t.trigger_kind == "pii_in_log" for t in p),
              observed=[t.trigger_kind for t in p])

    # A3 — clean payload, no fire.
    tid3 = _tenant(pool)
    _step(pool, tid3, kind="webhook_received", env='{"ok": "clean"}')
    assertion(3, "Detector-5 clean payload → no trigger", detect_pii_in_logs(tid3) == [],
              observed="clean")

    # A4 — DSR rate.
    tid4 = _tenant(pool)
    with pool.connection() as conn:
        for _ in range(11):
            conn.execute(
                "INSERT INTO dsr_tickets (tenant_id, request_type, status, acknowledged_at) "
                "VALUES (%s, 'deletion', 'acknowledged', now())",
                (tid4,),
            )
    a4 = any(t.trigger_kind == "dsr_rate_anomaly" for t in detect_slow_triggers(tid4))
    assertion(4, "Detector-3 >threshold DSR → dsr_rate_anomaly", a4, observed=a4)

    # A5 — notify_owner (mock send).
    import unittest.mock as mock

    with mock.patch.object(bn, "send_freeform_message", lambda body, phone: "SMfake"):
        res = bn.notify_owner(tid, "P1", "canary breach summary")
    assertion(5, "notify_owner → sent (mock)", res.get("sent") is True, observed=res)

    # cleanup
    with pool.connection() as conn:
        for t in INSERTED:
            conn.execute("DELETE FROM dsr_tickets WHERE tenant_id = %s", (t,))
            conn.execute("DELETE FROM pipeline_steps WHERE tenant_id = %s", (t,))
            conn.execute("DELETE FROM pipeline_runs WHERE tenant_id = %s", (t,))
            conn.execute("DELETE FROM tenants WHERE id = %s", (t,))
    graph_mod._pool.close()
    graph_mod._pool = None

    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s)", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(run_canary())
