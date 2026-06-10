"""VT-328 / VT-365 — real-DB: a lapsed/cancelled tenant cannot dispatch outbound campaigns.

The mock unit tests in test_campaign_execute.py prove the guard logic + short-circuit; THIS proves
it against the REAL schema — the `SELECT phase FROM tenants` read under RLS, the block returning
before any fan-out, and ZERO send_idempotency_keys rows written. The guard is THE single chokepoint
(inside execute_approved_campaign), so the canary calls that fn directly — proving (A) alone blocks
(per the VT-328 plan-ack). CL-422 synthetic data; CL-390 no PII.

VT-365 (Fazal 2026-06-09): the refund subsystem is GONE — `refunded` is no longer a phase and
tenants.refunded_at is dropped (migration 121). The dormant `lapsed` phase (30-day trial expired
without subscribe) is the new non-terminal blocked phase alongside terminal `cancelled`.

Gated on DATABASE_URL + psycopg; runs in the CI orchestrator job.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402 — after the psycopg gate

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-328 real-DB test skipped",
)


@pytest.fixture(scope="module")
def dsn():
    import apply_migrations  # lazy: keep module import-light for --no-project

    d = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=d)
    assert not r["failed"], r["failed"]
    return d


def _seed_tenant(conn, phase: str) -> str:
    row = conn.execute(
        "INSERT INTO tenants (business_name, plan_tier, phase) "
        "VALUES ('VT328 Co', 'standard', %s) RETURNING id",
        (phase,),
    ).fetchone()
    return str(row["id"])


def _run_blocked(dsn, phase: str):
    from orchestrator.campaign.execute import execute_approved_campaign

    # A send fn that MUST NOT be called for a blocked tenant (belt over the count assertion).
    never_send = MagicMock(side_effect=AssertionError("send must not fire for a blocked tenant"))
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        tenant = _seed_tenant(conn, phase)
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant,))
        summary = execute_approved_campaign(
            tenant, str(uuid4()), conn=conn, send_template_fn=never_send
        )
        ledger = conn.execute(
            "SELECT count(*) AS n FROM send_idempotency_keys WHERE tenant_id = %s", (tenant,)
        ).fetchone()["n"]
    return summary, ledger, never_send


def test_lapsed_dispatch_blocked(dsn):
    # VT-365: a 30-day-expired (lapsed) tenant — dormant, no active subscription — is blocked.
    summary, ledger, never_send = _run_blocked(dsn, "lapsed")
    assert summary["dispatch_blocked"] == 1 and summary["sent"] == 0
    assert ledger == 0  # ZERO send-ledger rows — the block happened before any fan-out
    assert never_send.call_count == 0


def test_cancelled_dispatch_blocked(dsn):
    summary, ledger, _ = _run_blocked(dsn, "cancelled")
    assert summary["dispatch_blocked"] == 1 and ledger == 0
