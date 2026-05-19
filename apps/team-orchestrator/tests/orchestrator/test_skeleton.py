"""VT-3.1 skeleton tests — DBOS-wrapped LangGraph substrate.

Require a live Postgres via ``DATABASE_URL`` plus the dbos / langgraph stack.
Skipped in the lightweight unit-test job; run in the CI ``orchestrator`` job
(which provisions a Postgres service and `uv sync`s the full project).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("langgraph")

import psycopg  # noqa: E402 — imported after the dependency skip guards

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — orchestrator skeleton tests skipped",
)

_WORKER = Path(__file__).parent / "_resume_worker.py"


@pytest.fixture(scope="module")
def substrate():
    """Apply migrations, launch DBOS + the LangGraph substrate once."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    apply_migrations.apply(dsn=dsn)
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from orchestrator import runner
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn, runner=runner)
    finally:
        shutdown_dbos()


def _new_tenant(dsn: str) -> str:
    """Insert a throwaway tenant and return its id (FK target for pipeline_runs)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-3.1 Test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()
    assert row is not None
    return str(row[0])


def _wait_for_probe(dsn: str, workflow_id: str, label: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with psycopg.connect(dsn, autocommit=True) as conn:
            count = conn.execute(
                "SELECT count(*) FROM _resume_probe "
                "WHERE workflow_id = %s AND step_label = %s",
                (workflow_id, label),
            ).fetchone()
        if count and count[0] > 0:
            return
        time.sleep(0.5)
    raise AssertionError(f"probe step '{label}' not observed within {timeout}s")


def test_pipeline_run_executes_end_to_end(substrate):
    """The DBOS workflow runs the LangGraph substrate end-to-end with stub nodes."""
    tenant_id = _new_tenant(substrate.dsn)
    run_id = str(uuid4())

    result = substrate.runner.run_pipeline(tenant_id, run_id, "inbound-message")

    assert result["run_id"] == run_id
    assert result["tenant_id"] == tenant_id
    # Graph ran: inbound seeded history, placeholder_node appended its marker.
    assert result["history"] == ["inbound-message", "placeholder_node"]

    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT status FROM pipeline_runs WHERE id = %s", (run_id,)
        ).fetchall()
    assert rows == [("completed",)]


def test_pipeline_run_is_idempotent_per_run_id(substrate):
    """Invoking with the same run_id returns the first result without re-running."""
    tenant_id = _new_tenant(substrate.dsn)
    run_id = str(uuid4())

    first = substrate.runner.run_pipeline(tenant_id, run_id, "inbound")
    second = substrate.runner.run_pipeline(tenant_id, run_id, "inbound")

    assert first == second
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        count = conn.execute(
            "SELECT count(*) FROM pipeline_runs WHERE id = %s", (run_id,)
        ).fetchone()
    assert count == (1,)


def test_checkpoint_tables_rls_blocks_cross_tenant(substrate):
    """A tenant-scoped role cannot read another tenant's LangGraph checkpoints."""
    dsn = substrate.dsn
    tenant_a = _new_tenant(dsn)
    tenant_b = _new_tenant(dsn)
    run_a = str(uuid4())
    run_b = str(uuid4())
    substrate.runner.run_pipeline(tenant_a, run_a, "a")
    substrate.runner.run_pipeline(tenant_b, run_b, "b")

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP ROLE IF EXISTS rls_ckpt_tester")
        conn.execute("CREATE ROLE rls_ckpt_tester NOLOGIN")
        conn.execute("GRANT USAGE ON SCHEMA public TO rls_ckpt_tester")
        conn.execute(
            "GRANT SELECT ON ALL TABLES IN SCHEMA public TO rls_ckpt_tester"
        )
        conn.execute(
            "GRANT EXECUTE ON FUNCTION app_current_tenant() TO rls_ckpt_tester"
        )

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET ROLE rls_ckpt_tester")
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant_a,))
        threads = {
            row[0]
            for row in conn.execute("SELECT DISTINCT thread_id FROM checkpoints")
        }

    assert run_a in threads, "tenant A must see its own checkpoint rows"
    assert run_b not in threads, "tenant A must NOT see tenant B's checkpoint rows"


def test_dbos_auto_resumes_after_sigkill(substrate):
    """CRITICAL: a workflow SIGKILLed mid-execution resumes from its last step."""
    dsn = substrate.dsn
    workflow_id = f"resume-{uuid4()}"

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _resume_probe ("
            "id serial PRIMARY KEY, workflow_id text, step_label text, "
            "at timestamptz DEFAULT now())"
        )

    # Process 1: start the workflow, then SIGKILL it during the step1->step2 gap.
    proc1 = subprocess.Popen([sys.executable, str(_WORKER), dsn, workflow_id])
    try:
        _wait_for_probe(dsn, workflow_id, "step1", timeout=45)
    finally:
        proc1.kill()
    proc1.wait(timeout=15)

    # Process 2: launching DBOS recovers the PENDING workflow from step 2.
    proc2 = subprocess.Popen([sys.executable, str(_WORKER), dsn, workflow_id])
    try:
        _wait_for_probe(dsn, workflow_id, "step2", timeout=90)
    finally:
        proc2.kill()
        proc2.wait(timeout=15)

    with psycopg.connect(dsn, autocommit=True) as conn:
        step1 = conn.execute(
            "SELECT count(*) FROM _resume_probe "
            "WHERE workflow_id = %s AND step_label = 'step1'",
            (workflow_id,),
        ).fetchone()
        step2 = conn.execute(
            "SELECT count(*) FROM _resume_probe "
            "WHERE workflow_id = %s AND step_label = 'step2'",
            (workflow_id,),
        ).fetchone()

    assert step1 == (1,), f"step1 ran {step1}x — DBOS must run a completed step once"
    assert step2 and step2[0] >= 1, "workflow did not resume + finish step2 after SIGKILL"
