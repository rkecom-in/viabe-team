#!/usr/bin/env python3
"""VT-200 hygiene bundle canary (Rule #15, DR-15).

Four deterministic assertions — one per fix:

- A1: TEAM_TWILIO_MOCK_MODE=1 yields a mock client; send() returns a
  mock SID without raising on absent TEAM_TWILIO_ACCOUNT_SID / AUTH_TOKEN.
- A2: ON DELETE CASCADE on twilio_inbound_events.tenant_id — synthetic
  parent + child rows; DELETE parent; child row gone.
- A3: register_purge_scheduler() applies @DBOS.workflow before
  @DBOS.scheduled — no DBOSWorkflowFunctionNotFoundError after scheduler
  registers the purge function.
- A4 (VT-215): register_ingestion_scheduler() applies @DBOS.workflow
  before @DBOS.scheduled for ingestion_scheduler_body — same fix shape
  as A3, different function.

Subshell-source supabase-dev.env:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      ./.venv/bin/python canaries/vt200_hygiene_bundle.py
    )

Wall-clock budget ≤ 20s.
"""

from __future__ import annotations

import logging
import os
import sys
import warnings
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_TENANTS: list[str] = []


def assertion(
    num: int, name: str, passed: bool, *, observed: Any = None, expected: Any = None
) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _preflight() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL missing", file=sys.stderr)
        sys.exit(2)
    print("PREFLIGHT OK")


def _seed_tenant(pool: Any) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'trial', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (tid, f"vt200-canary-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
    INSERTED_TENANTS.append(tid)
    return tid


def _cleanup(pool: Any) -> None:
    if not INSERTED_TENANTS:
        return
    with pool.connection() as conn:
        for tid in INSERTED_TENANTS:
            conn.execute("DELETE FROM tenants WHERE id = %s", (tid,))


def run_canary() -> int:
    _preflight()

    # ---------------- A1 — TEAM_TWILIO_MOCK_MODE ----------------
    # Mutate env in try/finally so subsequent canary runs see clean state.
    original_mock = os.environ.get("TEAM_TWILIO_MOCK_MODE")
    original_sid = os.environ.get("TEAM_TWILIO_ACCOUNT_SID")
    original_token = os.environ.get("TEAM_TWILIO_AUTH_TOKEN")
    os.environ["TEAM_TWILIO_MOCK_MODE"] = "1"
    os.environ.pop("TEAM_TWILIO_ACCOUNT_SID", None)
    os.environ.pop("TEAM_TWILIO_AUTH_TOKEN", None)
    try:
        # Clear lru_cache so the next _client() call sees the new env.
        from orchestrator.utils.twilio_send import _client

        _client.cache_clear()
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            client = _client()
            response = client.messages.create(
                to="whatsapp:+919000000000",
                from_="whatsapp:+910000000000",
                body="VT-200 canary",
            )
        pass_1 = (
            type(client).__name__ == "_MockTwilioClient"
            and getattr(response, "sid", "").startswith("MK")
            and getattr(response, "status", "") == "queued"
        )
        assertion(
            1,
            "TEAM_TWILIO_MOCK_MODE=1: mock client + mock SID + no creds required",
            pass_1,
            observed={
                "client_type": type(client).__name__,
                "sid": getattr(response, "sid", None),
                "status": getattr(response, "status", None),
            },
            expected={"client_type": "_MockTwilioClient", "sid_prefix": "MK"},
        )
    finally:
        _client.cache_clear()
        if original_mock is None:
            os.environ.pop("TEAM_TWILIO_MOCK_MODE", None)
        else:
            os.environ["TEAM_TWILIO_MOCK_MODE"] = original_mock
        if original_sid is not None:
            os.environ["TEAM_TWILIO_ACCOUNT_SID"] = original_sid
        if original_token is not None:
            os.environ["TEAM_TWILIO_AUTH_TOKEN"] = original_token

    # ---------------- A2 — twilio_inbound_events FK CASCADE ----------------
    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    pool = get_pool()

    tid = _seed_tenant(pool)
    msg_sid = f"SM{uuid4().hex}"
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO twilio_inbound_events (message_sid, tenant_id) "
            "VALUES (%s, %s)",
            (msg_sid, tid),
        )
    # Delete the tenant; child row should cascade.
    with pool.connection() as conn:
        conn.execute("DELETE FROM tenants WHERE id = %s", (tid,))
    INSERTED_TENANTS.remove(tid)  # cascaded; don't re-delete in _cleanup
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM twilio_inbound_events WHERE message_sid = %s",
            (msg_sid,),
        )
        row = cur.fetchone()
    child_count = int(row["n"]) if row else -1
    pass_2 = child_count == 0
    assertion(
        2,
        "FK CASCADE: DELETE tenant removes child twilio_inbound_events row",
        pass_2,
        observed={"child_count_after_cascade": child_count},
        expected={"child_count_after_cascade": 0},
    )

    # ---------------- A3 — DBOS purge workflow registration ----------------
    # We capture warnings emitted during a synthetic apply-decorators step
    # AND verify @DBOS.workflow is applied before @DBOS.scheduled. The
    # cheapest behavioral check: introspect the function attributes DBOS
    # stamps on registration via its decorators. After ``register_purge_
    # scheduler()`` runs the function should carry DBOS's workflow marker
    # in addition to the scheduled marker.
    captured: list[str] = []
    handler = logging.StreamHandler()
    handler.setLevel(logging.WARNING)
    handler_seen_records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            handler_seen_records.append(record)
            captured.append(self.format(record))

    cap = _Capture()
    cap.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(cap)
    try:
        # Late import: dbos module side-effects must not run before
        # we install the capture handler.
        from orchestrator.dbos_purge import (
            purge_workflow_inputs_scheduled,
            register_purge_scheduler,
        )

        # If DBOS isn't initialised, register would raise. Per the
        # docstring, the function is safe to invoke without a launched
        # DBOS instance (deferred-poller path). Skip with INCONCLUSIVE
        # if DBOS isn't importable (very heavy dep absent).
        try:
            register_purge_scheduler()
        except Exception as exc:  # noqa: BLE001 — environment-dependent
            assertion(
                3,
                "register_purge_scheduler applies @DBOS.workflow before @DBOS.scheduled",
                False,
                observed={"register_raised": repr(exc)},
                expected="no exception",
            )
            return _finalise(pool)
        # Verify DBOSRegistry.workflow_info_map now carries an entry for
        # the purge function. This is the registry the scheduler poller
        # consults; presence here is what makes the difference between
        # "scheduled-but-not-a-workflow" (the bug) and "registered as
        # both" (the fix).
        from dbos._dbos import _dbos_global_registry

        reg = _dbos_global_registry
        wf_map = getattr(reg, "workflow_info_map", {}) if reg else {}
        # The map's keys are qualified function paths; match by fn name.
        purge_qualname = purge_workflow_inputs_scheduled.__qualname__
        registered_keys = [k for k in wf_map if "purge_workflow_inputs_scheduled" in k]

        # Assertion-by-effect: no DBOSWorkflowFunctionNotFoundError
        # warning was emitted during/after registration.
        not_found_warnings = [
            r for r in handler_seen_records
            if "DBOSWorkflowFunctionNotFoundError" in r.getMessage()
        ]
        pass_3 = len(not_found_warnings) == 0 and len(registered_keys) > 0
        assertion(
            3,
            "DBOS workflow registered: function present in workflow_info_map + no NotFoundError",
            pass_3,
            observed={
                "registered_keys": registered_keys,
                "wf_map_size": len(wf_map),
                "purge_qualname": purge_qualname,
                "not_found_warnings": [r.getMessage() for r in not_found_warnings],
            },
            expected={"registered_keys_nonempty": True, "not_found_warnings": []},
        )
    finally:
        logging.getLogger().removeHandler(cap)

    # ---------------- A4 (VT-215) — DBOS ingestion workflow registration ----
    # Same shape as A3 but for ``ingestion_scheduler_body``. Calling
    # ``register_ingestion_scheduler()`` after the purge register is safe —
    # both go through idempotent module-level apply-decorator calls.
    captured_a4: list[str] = []
    handler_seen_records_a4: list[logging.LogRecord] = []

    class _CaptureA4(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            handler_seen_records_a4.append(record)
            captured_a4.append(self.format(record))

    cap_a4 = _CaptureA4()
    cap_a4.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(cap_a4)
    try:
        from orchestrator.integrations.scheduler import (
            ingestion_scheduler_body,
            register_ingestion_scheduler,
        )

        try:
            register_ingestion_scheduler()
        except Exception as exc:  # noqa: BLE001
            assertion(
                4,
                "register_ingestion_scheduler applies @DBOS.workflow before @DBOS.scheduled",
                False,
                observed={"register_raised": repr(exc)},
                expected="no exception",
            )
            return _finalise(pool)

        from dbos._dbos import _dbos_global_registry

        reg_a4 = _dbos_global_registry
        wf_map_a4 = getattr(reg_a4, "workflow_info_map", {}) if reg_a4 else {}
        ingest_qualname = ingestion_scheduler_body.__qualname__
        registered_keys_a4 = [
            k for k in wf_map_a4 if "ingestion_scheduler_body" in k
        ]
        not_found_warnings_a4 = [
            r for r in handler_seen_records_a4
            if "DBOSWorkflowFunctionNotFoundError" in r.getMessage()
            and "ingestion_scheduler_body" in r.getMessage()
        ]
        pass_4 = len(not_found_warnings_a4) == 0 and len(registered_keys_a4) > 0
        assertion(
            4,
            "DBOS workflow registered: ingestion_scheduler_body in workflow_info_map + no NotFoundError",
            pass_4,
            observed={
                "registered_keys": registered_keys_a4,
                "wf_map_size": len(wf_map_a4),
                "ingest_qualname": ingest_qualname,
                "not_found_warnings": [r.getMessage() for r in not_found_warnings_a4],
            },
            expected={"registered_keys_nonempty": True, "not_found_warnings": []},
        )
    finally:
        logging.getLogger().removeHandler(cap_a4)

    _cleanup(pool)
    return _finalise(pool)


def _finalise(_pool: Any) -> int:
    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)} assertion(s) failed", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
