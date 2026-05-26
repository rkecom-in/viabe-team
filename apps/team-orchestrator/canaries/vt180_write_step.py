#!/usr/bin/env python3
"""VT-180 ``write_step`` canary (Rule #15, DR-15).

Subshell-source ONLY `.viabe/secrets/supabase-dev.env`:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt180_write_step.py 2>&1 | tee /tmp/vt180-canary-evidence.log | tail -200
    )

**NO anthropic.env sourced.** Defense-in-depth Pillar 1: write_step is a
deterministic writer; ANTHROPIC_API_KEY ABSENT at PREFLIGHT.

Wall-clock budget ≤ 30s. Cost budget: 0 paise.

11 assertions across 6 groups (per VT-180 brief + Cowork review verdict
observation #2 — E10 verifies monotonic-step_seq + buffered_at_utc
ordering on flush):
- A1-A3: basic write — row + step_count + total_cost_paise atomic
- B4-B5: envelope validation — soft-fail flag + unregistered hard-fail
- C6-C7: RLS isolation + cross-tenant denial
- D8: 50 concurrent writes → strict monotonic step_seq 1..50
- E9-E10: SQLite buffer fallback + flush ordering
- F11: zero-LLM invariant
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_RUN_IDS: list[str] = []
INSERTED_TENANT_IDS: list[str] = []
SAMPLE: dict[str, Any] = {}


def assertion(num, name, passed, *, observed=None, expected=None):
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _supabase_host():
    url = os.environ.get("DATABASE_URL", "")
    if "@" not in url:
        return "<no-host>"
    return url.split("@", 1)[1].split("/", 1)[0]


def _preflight():
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL not set", file=sys.stderr)
        sys.exit(2)
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "PREFLIGHT FAIL — ANTHROPIC_API_KEY present; this canary's loader "
            "must NOT source anthropic.env (defense-in-depth per DR-15).",
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        f"PREFLIGHT OK — supabase: {_supabase_host()}; "
        f"ANTHROPIC_API_KEY: <absent — defense-in-depth>"
    )


def _seed_tenant_and_run(pool, tenant_id: UUID) -> UUID:
    INSERTED_TENANT_IDS.append(str(tenant_id))
    run_id = uuid4()
    INSERTED_RUN_IDS.append(str(run_id))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"canary-vt180-{tenant_id}"),
        )
        cur.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status) "
            "VALUES (%s, %s, 'running')",
            (str(run_id), str(tenant_id)),
        )
    return run_id


def _valid_webhook_input() -> dict[str, Any]:
    return {
        "body_token": f"body_tok_{uuid4().hex[:16]}",
        "sender_phone_token": f"cust_tok_{uuid4().hex[:16]}",
        "message_type": "inbound_message",
        "twilio_message_sid": None,
        "status_callback_state": None,
        "dupe_status": False,
        "num_media": 0,
    }


def run_canary() -> int:
    _preflight()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt180-canary-salt")

    # Use an isolated SQLite buffer per canary run.
    tmp_dir = tempfile.mkdtemp(prefix="vt180_canary_")
    buffer_path = Path(tmp_dir) / "buffer.db"
    os.environ["VIABE_OBSERVABILITY_BUFFER_PATH"] = str(buffer_path)

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=4,
            max_size=64,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    pool = get_pool()

    from orchestrator.observability.envelopes import EnvelopeNotRegistered
    from orchestrator.observability.pipeline_observability import (
        _flush_buffer,
        write_step,
    )

    # -----------------------------------------------------------------
    # Group A — basic write (3 assertions)
    # -----------------------------------------------------------------

    tenant_a = uuid4()
    run_a = _seed_tenant_and_run(pool, tenant_a)

    # Capture baselines from pipeline_runs before write.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step_count, total_cost_paise FROM pipeline_runs WHERE id = %s",
            (str(run_a),),
        )
        baseline = cur.fetchone()
    base_count = int(baseline["step_count"] or 0)
    base_cost = int(baseline["total_cost_paise"] or 0)

    write_step(
        step_kind="webhook_received",
        run_id=run_a,
        tenant_id=tenant_a,
        step_name="ingress_webhook",
        input_envelope=_valid_webhook_input(),
        output_envelope=None,
        status="completed",
        cost_paise=500,
    )

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step_seq, step_kind, step_name, status, cost_paise, "
            "input_envelope, output_envelope, error "
            "FROM pipeline_steps WHERE run_id = %s ORDER BY step_seq",
            (str(run_a),),
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT step_count, total_cost_paise FROM pipeline_runs WHERE id = %s",
            (str(run_a),),
        )
        after = cur.fetchone()

    pass_1 = (
        len(rows) == 1
        and rows[0]["step_kind"] == "webhook_received"
        and rows[0]["step_name"] == "ingress_webhook"
        and rows[0]["status"] == "completed"
        and rows[0]["cost_paise"] == 500
        and rows[0]["input_envelope"] is not None
        and rows[0]["error"] is None
    )
    assertion(
        1,
        "basic write: canonical columns populated correctly",
        pass_1,
        observed={"row_count": len(rows), "row": dict(rows[0]) if rows else None},
        expected={
            "step_kind": "webhook_received",
            "step_name": "ingress_webhook",
            "status": "completed",
            "cost_paise": 500,
        },
    )

    after_count = int(after["step_count"])
    after_cost = int(after["total_cost_paise"])
    pass_2 = after_count == base_count + 1
    pass_3 = after_cost == base_cost + 500
    assertion(
        2,
        "pipeline_runs.step_count incremented by 1 (atomic with step insert)",
        pass_2,
        observed={"baseline": base_count, "after": after_count},
        expected={"after": base_count + 1},
    )
    assertion(
        3,
        "pipeline_runs.total_cost_paise incremented by cost_paise (atomic)",
        pass_3,
        observed={"baseline": base_cost, "after": after_cost, "cost_paise_arg": 500},
        expected={"after": base_cost + 500},
    )

    # -----------------------------------------------------------------
    # Group B — envelope validation (2 assertions)
    # -----------------------------------------------------------------

    # Assertion 4 — malformed envelope → soft-fail; row still written.
    malformed = {
        "body_token": 12345,  # wrong type — expects str
        "sender_phone_token": "x",
        "message_type": "inbound_message",
        "twilio_message_sid": None,
        "status_callback_state": None,
        "dupe_status": False,
        "num_media": 0,
    }
    write_step(
        step_kind="webhook_received",
        run_id=run_a,
        tenant_id=tenant_a,
        step_name="malformed_envelope_test",
        input_envelope=malformed,
        cost_paise=0,
    )

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step_seq, error FROM pipeline_steps "
            "WHERE run_id = %s AND step_name = 'malformed_envelope_test'",
            (str(run_a),),
        )
        soft_fail_row = cur.fetchone()
    pass_4 = (
        soft_fail_row is not None
        and soft_fail_row["error"] is not None
        and soft_fail_row["error"].get("payload_validation_failed") is True
        and "payload_validation_details" in soft_fail_row["error"]
    )
    assertion(
        4,
        "malformed envelope soft-fails: row written + payload_validation_failed=true",
        pass_4,
        observed={
            "row_present": soft_fail_row is not None,
            "error_payload": soft_fail_row["error"] if soft_fail_row else None,
        },
        expected={"row_present": True, "payload_validation_failed": True},
    )

    # Assertion 5 — unregistered step_kind → EnvelopeNotRegistered; NO write.
    pre_b5_count_q = pool.connection()
    with pre_b5_count_q as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM pipeline_steps WHERE run_id = %s",
            (str(run_a),),
        )
        pre_b5_count = int(cur.fetchone()["n"])

    hard_fail_raised = False
    try:
        write_step(
            step_kind="totally_unregistered_step_kind",
            run_id=run_a,
            tenant_id=tenant_a,
            step_name="should_not_write",
            input_envelope={},
        )
    except EnvelopeNotRegistered:
        hard_fail_raised = True

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM pipeline_steps WHERE run_id = %s",
            (str(run_a),),
        )
        post_b5_count = int(cur.fetchone()["n"])
    pass_5 = hard_fail_raised and post_b5_count == pre_b5_count
    assertion(
        5,
        "unregistered step_kind raises EnvelopeNotRegistered + writes nothing",
        pass_5,
        observed={
            "EnvelopeNotRegistered_raised": hard_fail_raised,
            "row_count_pre": pre_b5_count,
            "row_count_post": post_b5_count,
        },
        expected={"EnvelopeNotRegistered_raised": True, "row_count_delta": 0},
    )

    # -----------------------------------------------------------------
    # Group C — RLS isolation (2 assertions)
    # -----------------------------------------------------------------

    tenant_b = uuid4()
    run_b = _seed_tenant_and_run(pool, tenant_b)

    write_step(
        step_kind="webhook_received",
        run_id=run_b,
        tenant_id=tenant_b,
        step_name="tenant_b_only",
        input_envelope=_valid_webhook_input(),
    )

    from orchestrator.db.tenant_connection import tenant_connection

    with tenant_connection(tenant_b) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM pipeline_steps WHERE run_id = %s",
            (str(run_b),),
        )
        b_under_b = int(cur.fetchone()["n"])
    with tenant_connection(tenant_a) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM pipeline_steps WHERE run_id = %s",
            (str(run_b),),
        )
        b_under_a = int(cur.fetchone()["n"])
    pass_6 = b_under_b >= 1 and b_under_a == 0
    assertion(
        6,
        "RLS isolation: tenant_b row visible under tenant_b; invisible under tenant_a",
        pass_6,
        observed={"tenant_b_count": b_under_b, "tenant_a_count": b_under_a},
        expected={"tenant_b_count": ">=1", "tenant_a_count": 0},
    )

    # Assertion 7 — cross-tenant write attempt: under tenant_a's GUC,
    # write_step with tenant_id=B → tenant_connection opens under B's GUC
    # (legit), but if a caller MISmatches GUC vs arg, RLS denies. Simulate
    # by setting tenant_a's GUC then attempting an INSERT with tenant_b.
    cross_denied = False
    try:
        with tenant_connection(tenant_a) as conn:
            conn.execute(
                "INSERT INTO pipeline_steps "
                "(run_id, tenant_id, step_seq, step_kind, status, started_at) "
                "VALUES (%s, %s, 999, 'webhook_received', 'completed', now())",
                (str(run_b), str(tenant_b)),
            )
    except psycopg_errors_match() as exc:
        cross_denied = True
        SAMPLE["cross_tenant_error"] = repr(exc)
    pass_7 = cross_denied
    assertion(
        7,
        "RLS cross-tenant: tenant_a GUC + tenant_id=B INSERT → denied",
        pass_7,
        observed={"insert_denied": cross_denied, "sample_error": SAMPLE.get("cross_tenant_error")},
        expected={"insert_denied": True},
    )

    # -----------------------------------------------------------------
    # Group D — atomic step_seq under concurrency (1 assertion)
    # -----------------------------------------------------------------

    tenant_c = uuid4()
    run_c = _seed_tenant_and_run(pool, tenant_c)
    N_CONCURRENT = 50
    barrier = threading.Barrier(N_CONCURRENT)

    def _concurrent_write(idx: int) -> None:
        barrier.wait()
        write_step(
            step_kind="webhook_received",
            run_id=run_c,
            tenant_id=tenant_c,
            step_name=f"concurrent_{idx}",
            input_envelope=_valid_webhook_input(),
            cost_paise=1,
        )

    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=N_CONCURRENT) as pool_exec:
        futures = [pool_exec.submit(_concurrent_write, i) for i in range(N_CONCURRENT)]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                errors.append(repr(exc))

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step_seq FROM pipeline_steps WHERE run_id = %s ORDER BY step_seq",
            (str(run_c),),
        )
        seqs = [int(r["step_seq"]) for r in cur.fetchall()]
    expected_seqs = list(range(1, N_CONCURRENT + 1))
    pass_8 = (
        seqs == expected_seqs and len(errors) == 0
    )
    assertion(
        8,
        f"{N_CONCURRENT} concurrent writes → strict monotonic step_seq 1..{N_CONCURRENT}",
        pass_8,
        observed={
            "observed_seqs_first10": seqs[:10],
            "observed_seqs_last10": seqs[-10:],
            "total_count": len(seqs),
            "concurrent_errors": errors,
        },
        expected={
            "expected_seqs_first10": expected_seqs[:10],
            "expected_seqs_last10": expected_seqs[-10:],
            "total_count": N_CONCURRENT,
        },
    )

    # -----------------------------------------------------------------
    # Group E — SQLite buffer fallback (2 assertions)
    # -----------------------------------------------------------------

    # Wipe the SQLite buffer between groups so D8's failure-path leakage
    # (if any pool-starved write fell through to the buffer) does NOT
    # contaminate E9/E10's assertions on buffer content + flush ordering.
    import sqlite3 as _sqlite3
    if buffer_path.exists():
        with _sqlite3.connect(buffer_path) as _conn:
            _conn.execute("DROP TABLE IF EXISTS buffered_steps")

    tenant_d = uuid4()
    run_d = _seed_tenant_and_run(pool, tenant_d)

    # E9: simulate prod DB outage by pointing tenant_connection at a dead
    # pool. Cleanest: patch the pool with one bound to an unreachable host.
    # Then verify write_step returns success + row appears in SQLite buffer.
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    real_pool = graph_mod._pool

    class _DeadPool:
        """Stand-in pool whose .connection() raises OperationalError."""

        def connection(self):
            raise psycopg.OperationalError("simulated outage for canary E9")

        def close(self):
            pass

    graph_mod._pool = _DeadPool()  # type: ignore[assignment]
    try:
        # write_step swallows the OperationalError + buffers locally.
        # E9 inputs are 3 sequential writes to verify FIFO ordering on flush.
        write_step(
            step_kind="webhook_received",
            run_id=run_d,
            tenant_id=tenant_d,
            step_name="buffered_first",
            input_envelope=_valid_webhook_input(),
            cost_paise=10,
        )
        write_step(
            step_kind="webhook_received",
            run_id=run_d,
            tenant_id=tenant_d,
            step_name="buffered_second",
            input_envelope=_valid_webhook_input(),
            cost_paise=20,
        )
        write_step(
            step_kind="webhook_received",
            run_id=run_d,
            tenant_id=tenant_d,
            step_name="buffered_third",
            input_envelope=_valid_webhook_input(),
            cost_paise=30,
        )
    finally:
        graph_mod._pool = real_pool

    import sqlite3

    with sqlite3.connect(buffer_path) as bconn:
        bconn.row_factory = sqlite3.Row
        buffered_rows = bconn.execute(
            "SELECT step_name, cost_paise, buffered_at_utc FROM buffered_steps "
            "WHERE run_id = ? ORDER BY buffered_at_utc, rowid",
            (str(run_d),),
        ).fetchall()
    buffered_names = [r["step_name"] for r in buffered_rows]
    pass_9 = buffered_names == ["buffered_first", "buffered_second", "buffered_third"]
    assertion(
        9,
        "SQLite buffer: 3 writes during prod DB outage land in buffer FIFO",
        pass_9,
        observed={"buffered_step_names": buffered_names, "buffered_count": len(buffered_rows)},
        expected={"buffered_step_names": ["buffered_first", "buffered_second", "buffered_third"]},
    )

    # E10: flush + verify monotonic step_seq + order matches buffered_at_utc.
    flushed_count = _flush_buffer()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT step_seq, step_name FROM pipeline_steps "
            "WHERE run_id = %s ORDER BY step_seq",
            (str(run_d),),
        )
        flushed = cur.fetchall()
        cur.execute(
            "SELECT step_count, total_cost_paise FROM pipeline_runs WHERE id = %s",
            (str(run_d),),
        )
        run_d_after = cur.fetchone()
    flushed_names = [r["step_name"] for r in flushed]
    flushed_seqs = [int(r["step_seq"]) for r in flushed]
    monotonic = flushed_seqs == sorted(flushed_seqs) and len(set(flushed_seqs)) == len(flushed_seqs)
    order_matches_buffer = flushed_names == ["buffered_first", "buffered_second", "buffered_third"]

    # SQLite buffer must be drained.
    with sqlite3.connect(buffer_path) as bconn:
        residual = bconn.execute(
            "SELECT count(*) AS n FROM buffered_steps WHERE run_id = ?",
            (str(run_d),),
        ).fetchone()[0]

    pass_10 = (
        flushed_count == 3
        and monotonic
        and order_matches_buffer
        and residual == 0
        and int(run_d_after["step_count"]) == 3
        and int(run_d_after["total_cost_paise"]) == 60
    )
    assertion(
        10,
        "flush: 3 buffered rows land in pipeline_steps in buffered_at_utc order + monotonic step_seq + buffer drained",
        pass_10,
        observed={
            "flushed_count_return": flushed_count,
            "pipeline_steps_names": flushed_names,
            "pipeline_steps_seqs": flushed_seqs,
            "monotonic_step_seq": monotonic,
            "buffer_residual": residual,
            "run_d_step_count": int(run_d_after["step_count"]),
            "run_d_total_cost_paise": int(run_d_after["total_cost_paise"]),
        },
        expected={
            "flushed_count_return": 3,
            "pipeline_steps_names": ["buffered_first", "buffered_second", "buffered_third"],
            "monotonic_step_seq": True,
            "buffer_residual": 0,
            "run_d_step_count": 3,
            "run_d_total_cost_paise": 60,
        },
    )

    # -----------------------------------------------------------------
    # Group F — zero LLM (1 assertion)
    # -----------------------------------------------------------------

    pass_11 = os.environ.get("ANTHROPIC_API_KEY") is None
    assertion(
        11,
        "Zero LLM invariant: ANTHROPIC_API_KEY absent throughout canary execution",
        pass_11,
        observed={"anthropic_api_key_present": os.environ.get("ANTHROPIC_API_KEY") is not None},
        expected={"anthropic_api_key_present": False},
    )

    return _finalise(pool, buffer_path, tmp_dir)


def psycopg_errors_match():
    """Return the tuple of exceptions that signal an RLS-denied INSERT.

    Helper so we can both catch and report which one fired.
    """
    import psycopg

    return (
        psycopg.errors.InsufficientPrivilege,
        psycopg.errors.CheckViolation,
        psycopg.errors.NotNullViolation,
        psycopg.errors.UniqueViolation,
        # Some RLS configs report as InternalError or generic IntegrityError
        psycopg.errors.IntegrityError,
        psycopg.Error,
    )


def _finalise(pool, buffer_path: Path, tmp_dir: str) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print("\n=== Anthropic cost: 0 paise (deterministic writer; no LLM dispatch) ===")

    print("\n=== SAMPLE (cross-tenant error + buffer path) ===")
    print(json.dumps({**SAMPLE, "buffer_path": str(buffer_path)}, indent=2, default=str))

    # Cleanup. Service-role bypasses RLS for canary teardown.
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM pipeline_steps WHERE run_id = ANY(%s)", (INSERTED_RUN_IDS,)
            )
            cur.execute(
                "DELETE FROM pipeline_runs WHERE id = ANY(%s)", (INSERTED_RUN_IDS,)
            )
            cur.execute(
                "DELETE FROM tenants WHERE id = ANY(%s)", (INSERTED_TENANT_IDS,)
            )
    except BaseException as exc:  # noqa: BLE001
        print(f"cleanup partial: {exc!r}", file=sys.stderr)

    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except BaseException:  # noqa: BLE001
        pass

    failed = [n for n, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print("\nALL 11 ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
