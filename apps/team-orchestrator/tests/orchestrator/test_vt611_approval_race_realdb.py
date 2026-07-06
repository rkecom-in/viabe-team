"""VT-611 Phase B1 #4 — concurrency: the agent_customer_send approval-resolution race
(live Postgres). Reproduce-first (team-lead ruling), not a speculative fix.

The realistic trigger is NOT an owner double-tap — it's an owner's WhatsApp "approve" reply
RACING the 48h timeout sweep for the SAME ``agent_customer_send`` approval (``runner.py``'s
``_maybe_resume_owner_approval`` vs ``scheduled_triggers.run_approval_timeout_sweep_body``).

The gap this file investigates: ``approval_resume.mark_approval_resolved``'s non-defer branch
calls ``PendingApprovalsWrapper().mark_resolved(...)`` and DISCARDS its return value (an int
rowcount — 1 for the winner of the ``WHERE resolved_at IS NULL`` CAS, 0 for the loser) without
checking it. Both ``runner.py``'s owner-reply path and ``scheduled_triggers.py``'s timeout-sweep
path proceed unconditionally past this call. If nothing downstream re-verifies the ACTUAL DB
state before acting, a losing racer's call could still trigger its own effect — e.g. the owner's
"approved" reply losing the row race to the sweep, but the owner-reply caller still starting a
real customer send because ITS OWN locally-known ``decision == "approved"``, regardless of what
actually landed in the database.

Reading ``l2_send.start_l2_send_for_resolved_approval`` (the ONLY thing gated on
``decision == "approved"`` in ``runner.py``'s post-commit seam) shows it does NOT trust that local
variable — it re-reads the batch's ACTUAL current status via
``PendingApprovalsWrapper.approved_batch_for_send_approval`` (a fresh query, after the resolve
transaction committed) and is a safe no-op unless the batch is truly ``'approved'`` at that moment.
Since ``apply_agent_decision``'s own batch-status UPDATE is independently CAS-guarded
(``WHERE status = ANY(_RESOLVABLE_FROM)``), only the winning racer's decision can ever actually
land on the batch — so the caller-level rowcount-check omission is (per this file's proof) NOT a
live double-effect: the downstream guard already holds. These tests PROVE that, live, rather than
assuming it from reading code — and stand as the regression pin if a future change ever weakens
either guard.

HARNESS — mirrors test_vt418_l2_send_driver_realdb.py's realdb conventions + test_run_control_realdb.
py's ``threading.Barrier`` concurrent-race pattern (two threads, SEPARATE psycopg connections, no
shared connection object — the real cross-process shape a webhook reply and a cron sweep actually
have). ``l2_send.start_l2_send`` (the actual DBOS-workflow-starting call) is monkeypatched to a
counting stub — this file is about the RACE/CAS correctness, not re-proving the send-gate stack
(``test_vt418_l2_send_driver_realdb.py`` already owns that).
"""

from __future__ import annotations

import os
import threading
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after the dependency skip guards
from psycopg.types.json import Jsonb  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-611 approval-race tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "test-salt")

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield dsn
    finally:
        shutdown_dbos()


def _new_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES (%s, 'standard', 'trial') RETURNING id",
            (f"vt611-race-{uuid4().hex[:8]}",),
        ).fetchone()
    return UUID(str(row[0]))


def _seed_work_item(dsn: str, tenant: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
            "VALUES (%s, %s, 'sales_recovery', 'awaiting_approval') RETURNING id",
            (str(tenant), f"wi-{uuid4().hex[:8]}"),
        ).fetchone()
    return UUID(str(row[0]))


def _seed_batch(dsn: str, tenant: UUID, work_item: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_draft_batches (tenant_id, work_item_id, agent, status) "
            "VALUES (%s, %s, 'sales_recovery', 'awaiting_approval') RETURNING id",
            (str(tenant), str(work_item)),
        ).fetchone()
    return UUID(str(row[0]))


def _seed_open_agent_approval(dsn: str, tenant: UUID, batch: UUID) -> UUID:
    """An OPEN (unresolved) agent_customer_send approval linked to the batch — the substrate
    BOTH racers resolve concurrently. Mirrors test_vt418's ``_seed_resolved_agent_approval`` shape,
    minus the resolution (status='pending', decision=NULL, resolved_at=NULL)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        run = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, status) VALUES (%s, 'running') RETURNING id",
            (str(tenant),),
        ).fetchone()[0]
        row = conn.execute(
            "INSERT INTO pending_approvals (tenant_id, run_id, approval_type, summary, details, "
            "draft_batch_id, timeout_at) "
            "VALUES (%s, %s, 'agent_customer_send', %s, %s, %s, now() + interval '1 hour') "
            "RETURNING id",
            (str(tenant), str(run), f"Batch {batch} — approve to send?",
             Jsonb({"draft_batch_id": str(batch)}), str(batch)),
        ).fetchone()
    return UUID(str(row[0]))


def _read_final_state(dsn: str, tenant: UUID, approval: UUID, batch: UUID) -> tuple[str, str, str]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        approval_row = conn.execute(
            "SELECT decision, status FROM pending_approvals WHERE id = %s", (str(approval),)
        ).fetchone()
        batch_row = conn.execute(
            "SELECT status FROM agent_draft_batches WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch)),
        ).fetchone()
    return str(approval_row[0]), str(approval_row[1]), str(batch_row[0])


def _owner_approve_resolve(tenant: UUID, approval: UUID) -> None:
    """Mirrors runner.py's ``_maybe_resume_owner_approval`` shape: resolve in one transaction on
    its OWN pooled, tenant-scoped (app_role) connection, exactly as the real webhook-driven caller
    does — ``PendingApprovalsWrapper`` fail-closes (TenantIsolationError) against anything else."""
    from orchestrator.agent import approval_resume
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant) as conn, conn.transaction():
        approval_resume.mark_approval_resolved(
            conn, tenant, approval, "approved", owner_message_sid="SMowner1"
        )


def _timeout_sweep_resolve(tenant: UUID, approval: UUID) -> None:
    """Mirrors scheduled_triggers.py's agent_customer_send timeout-sweep branch: resolve only,
    no resume_run, no further action (VT-611 pre-work #7 already proved that half)."""
    from orchestrator.agent import approval_resume
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant) as conn, conn.transaction():
        approval_resume.mark_approval_resolved(conn, tenant, approval, "timeout")


# ---------------------------------------------------------------------------
# Deterministic single-winner proofs — force each ordering explicitly so both directions of
# the race are proven, not left to chance.
# ---------------------------------------------------------------------------


def test_owner_approve_wins_forces_send_start_exactly_once(
    substrate, monkeypatch: pytest.MonkeyPatch
):
    """Owner's approve commits FIRST (sweep never got there in time — e.g. the owner replied
    well before the 48h timeout). The batch reaches 'approved'; the post-commit seam starts
    the send exactly once."""
    from orchestrator.agents import l2_send

    dsn = substrate
    tenant = _new_tenant(dsn)
    work_item = _seed_work_item(dsn, tenant)
    batch = _seed_batch(dsn, tenant, work_item)
    approval = _seed_open_agent_approval(dsn, tenant, batch)

    send_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        l2_send, "start_l2_send", lambda tid, bid: send_calls.append((tid, bid))
    )

    _owner_approve_resolve(tenant, approval)
    l2_send.start_l2_send_for_resolved_approval(str(tenant), str(approval))
    # The timeout sweep arrives AFTER — its own mark_resolved is a CAS no-op (resolved_at is no
    # longer NULL); it must never regress the already-'approved' batch.
    _timeout_sweep_resolve(tenant, approval)

    decision, status, batch_status = _read_final_state(dsn, tenant, approval, batch)
    assert decision == "approved"
    assert status == "approved"
    assert batch_status == "approved"
    assert send_calls == [(str(tenant), str(batch))]


def test_timeout_sweep_wins_owner_approve_call_becomes_a_safe_no_op(
    substrate, monkeypatch: pytest.MonkeyPatch
):
    """Timeout sweep commits FIRST (the owner's reply arrives too late — already past the 48h
    window, or the reply and the sweep's resolve land back-to-back with the sweep winning the row
    lock). The batch reaches 'cancelled'. The owner-reply caller's OWN ``mark_approval_resolved``
    call still runs (its return value is discarded — the exact gap under test) and its post-commit
    seam call still fires (gated on the caller's LOCAL 'approved' decision, not on whether its
    resolve actually won) — but it MUST be a safe no-op: zero sends over a batch the sweep already
    cancelled."""
    from orchestrator.agents import l2_send

    dsn = substrate
    tenant = _new_tenant(dsn)
    work_item = _seed_work_item(dsn, tenant)
    batch = _seed_batch(dsn, tenant, work_item)
    approval = _seed_open_agent_approval(dsn, tenant, batch)

    send_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        l2_send, "start_l2_send", lambda tid, bid: send_calls.append((tid, bid))
    )

    _timeout_sweep_resolve(tenant, approval)
    # The owner's reply arrives after — mark_approval_resolved's CAS no-ops (rowcount 0,
    # discarded), but the caller still unconditionally calls the post-commit seam next, exactly
    # like runner.py's real "if decision == 'approved': start_l2_send_for_resolved_approval(...)".
    _owner_approve_resolve(tenant, approval)
    l2_send.start_l2_send_for_resolved_approval(str(tenant), str(approval))

    decision, status, batch_status = _read_final_state(dsn, tenant, approval, batch)
    assert decision == "timeout"
    assert status == "timed_out"
    assert batch_status == "cancelled"
    # THE LOAD-BEARING ASSERT: the owner-reply path's send-trigger call fired (the caller-level
    # gap is real) but the fresh re-read inside it caught the batch's true state — no send.
    assert send_calls == []


# ---------------------------------------------------------------------------
# The genuine concurrent race — regression pin. Non-deterministic on WHICH side wins (that's the
# point of a real race), deterministic on the INVARIANT: exactly one decision lands, the batch's
# terminal status matches it, and the send-trigger fires at most once and only when warranted.
# ---------------------------------------------------------------------------


def test_owner_approve_races_timeout_sweep_barrier_exactly_one_effect(
    substrate, monkeypatch: pytest.MonkeyPatch
):
    """The real shape: a webhook-driven owner reply and a cron-driven timeout sweep are separate
    processes with separate connections, racing the SAME approval row with no coordination other
    than Postgres's own row lock. threading.Barrier synchronizes both threads to hit their resolve
    at (as close to) the same instant as this harness can arrange — mirrors
    test_run_control_realdb.py's own ``test_consume_first_race_exactly_one_winner``."""
    from orchestrator.agents import l2_send

    dsn = substrate
    tenant = _new_tenant(dsn)
    work_item = _seed_work_item(dsn, tenant)
    batch = _seed_batch(dsn, tenant, work_item)
    approval = _seed_open_agent_approval(dsn, tenant, batch)

    send_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        l2_send, "start_l2_send", lambda tid, bid: send_calls.append((tid, bid))
    )

    barrier = threading.Barrier(2)
    results: dict[str, Any] = {}

    def _owner() -> None:
        try:
            barrier.wait(timeout=10)
            _owner_approve_resolve(tenant, approval)
            l2_send.start_l2_send_for_resolved_approval(str(tenant), str(approval))
            results["owner"] = "done"
        except Exception as exc:  # noqa: BLE001 — surface thread failures in the assert
            results["owner"] = exc

    def _sweep() -> None:
        try:
            barrier.wait(timeout=10)
            _timeout_sweep_resolve(tenant, approval)
            results["sweep"] = "done"
        except Exception as exc:  # noqa: BLE001
            results["sweep"] = exc

    threads = [threading.Thread(target=_owner), threading.Thread(target=_sweep)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert results.get("owner") == "done", results.get("owner")
    assert results.get("sweep") == "done", results.get("sweep")

    decision, status, batch_status = _read_final_state(dsn, tenant, approval, batch)
    assert decision in ("approved", "timeout"), "exactly one decision must win — never corrupted"
    if decision == "approved":
        assert status == "approved"
        assert batch_status == "approved"
        assert send_calls == [(str(tenant), str(batch))]
    else:
        assert status == "timed_out"
        assert batch_status == "cancelled"
        assert send_calls == []  # the load-bearing invariant, whichever side actually won
