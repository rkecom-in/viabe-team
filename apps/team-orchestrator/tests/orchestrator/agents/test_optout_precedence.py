"""VT-369 CRITICAL-1 — opt-out / DSR precedence over the approval classifier.

THE live compliance bug this pins: ``stop`` and ``cancel`` are members of
``approval_reply._REJECT_KW``, so before the guard an owner opt-out ("STOP" /
"बंद करो" / "delete my data") arriving while ANY approval was open was CONSUMED
by ``runner.try_resume_pending_approval`` as a campaign/batch rejection — the
authoritative opt-out / DSR handler never saw it (DPDP violation: an opt-out
must ALWAYS win).

Pinned behaviour (mirrors the journey-gate guard in onboarding/journey.py):
  - an opt-out / DSR body returns None from ``try_resume_pending_approval``
    (NOT consumed as a decision) — over an open AGENT approval AND over an open
    weekly-CAMPAIGN approval (separate tenants; migration 128 allows only one
    open row per tenant);
  - the open approval row STAYS OPEN (the 30-min timeout sweep owns it);
  - a real "no" still resolves as 'rejected' (the guard must not over-block).

DB substrate mirrors ``tests/orchestrator/business_plan/test_generator.py``.
No LLM: the deterministic fast-path (``classify_approval_reply``) decides "no";
opt-out bodies return BEFORE any classification.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")
pytest.importorskip("langgraph")  # runner pulls the approval/graph stack

import psycopg  # noqa: E402 — after dependency skip guards

from orchestrator import runner  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-369 opt-out precedence tests skipped",
)

pytestmark = requires_db

_SID = "SM" + "9" * 32

# The three precedence bodies from the rework delta: opt-out (EN), opt-out
# (Devanagari), DSR. Each must win over an open approval.
_OPTOUT_BODIES = ("STOP", "बंद करो", "delete my data")


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


# --- seeding helpers (direct service-role connection — RLS bypassed at seed) ---


def _new_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, whatsapp_number) "
            "VALUES ('VT-369 optout test', 'founding', 'trial', now(), 'restaurant', %s) "
            "RETURNING id",
            (f"+9198{uuid4().int % 10**8:08d}",),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _new_run(dsn: str, tenant: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
            "VALUES (%s, 'orchestrator', 'running') RETURNING id",
            (str(tenant),),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_agent_approval(dsn: str, tenant: UUID) -> tuple[UUID, UUID]:
    """An OPEN agent_customer_send approval + its awaiting batch."""
    run = _new_run(dsn, tenant)
    with psycopg.connect(dsn, autocommit=True) as conn:
        wi = conn.execute(
            "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
            "VALUES (%s, %s, 'sales_recovery', 'awaiting_approval') RETURNING id",
            (str(tenant), f"item-{uuid4().hex[:12]}"),
        ).fetchone()
        batch = conn.execute(
            "INSERT INTO agent_draft_batches (tenant_id, work_item_id, agent, status) "
            "VALUES (%s, %s, 'sales_recovery', 'awaiting_approval') RETURNING id",
            (str(tenant), str(wi[0])),
        ).fetchone()
        approval = conn.execute(
            "INSERT INTO pending_approvals (tenant_id, run_id, approval_type, "
            "draft_batch_id, summary, status, timeout_at) "
            "VALUES (%s, %s, 'agent_customer_send', %s, 'Agent batch: 2 drafts', "
            "'pending', now() + interval '2 days') RETURNING id",
            (str(tenant), str(run), str(batch[0])),
        ).fetchone()
    return UUID(str(approval[0])), UUID(str(batch[0]))


def _seed_campaign_approval(dsn: str, tenant: UUID) -> UUID:
    run = _new_run(dsn, tenant)
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO pending_approvals (tenant_id, run_id, approval_type, summary, "
            "status, timeout_at) VALUES (%s, %s, 'campaign_send', 'approve?', "
            "'pending', now() + interval '2 days') RETURNING id",
            (str(tenant), str(run)),
        ).fetchone()
    return UUID(str(row[0]))


def _approval_state(dsn: str, tenant: UUID, approval: UUID) -> dict[str, Any]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT decision, status, resolved_at FROM pending_approvals "
            "WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(approval)),
        ).fetchone()
    assert row is not None
    return {"decision": row[0], "status": row[1], "resolved_at": row[2]}


def _batch_status(dsn: str, tenant: UUID, batch: UUID) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status FROM agent_draft_batches WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch)),
        ).fetchone()
    assert row is not None
    return str(row[0])


# ---------------------------------------------------------------------------
# Opt-out / DSR wins over an OPEN AGENT approval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", _OPTOUT_BODIES)
def test_optout_wins_over_open_agent_approval(substrate, body: str) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    approval, batch = _seed_agent_approval(dsn, tenant)

    out = runner.try_resume_pending_approval(str(tenant), body, _SID)

    assert out is None, f"{body!r} must NOT be consumed as an approval decision"
    state = _approval_state(dsn, tenant, approval)
    assert state["resolved_at"] is None and state["decision"] is None, (
        f"{body!r} resolved the approval — the opt-out was eaten as a rejection"
    )
    assert _batch_status(dsn, tenant, batch) == "awaiting_approval"


# ---------------------------------------------------------------------------
# Opt-out wins over an OPEN WEEKLY-CAMPAIGN approval (separate tenant — the
# migration-128 index allows one open row per tenant)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", _OPTOUT_BODIES)
def test_optout_wins_over_open_campaign_approval(substrate, body: str) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    approval = _seed_campaign_approval(dsn, tenant)

    out = runner.try_resume_pending_approval(str(tenant), body, _SID)

    assert out is None
    state = _approval_state(dsn, tenant, approval)
    assert state["resolved_at"] is None and state["decision"] is None


# ---------------------------------------------------------------------------
# A real "no" still rejects — the guard must not over-block
# ---------------------------------------------------------------------------


def test_real_no_still_rejects_agent_approval(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    approval, batch = _seed_agent_approval(dsn, tenant)

    out = runner.try_resume_pending_approval(str(tenant), "no", _SID)

    assert out == "rejected"
    state = _approval_state(dsn, tenant, approval)
    assert state["decision"] == "rejected" and state["resolved_at"] is not None
    # Resolution glue (plan §4.3): the batch lands terminal 'rejected' in the
    # SAME transaction — no send can ever pick it up.
    assert _batch_status(dsn, tenant, batch) == "rejected"


def test_real_no_still_rejects_campaign_approval(substrate, monkeypatch) -> None:
    """The campaign path resumes the suspended LangGraph run — stub the resume
    (no checkpoint in this unit; mirrors tests/orchestrator/knowledge/
    test_l2_emit_sites.py) and assert the durable row resolves."""
    from orchestrator.agent import approval_resume

    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    approval = _seed_campaign_approval(dsn, tenant)
    monkeypatch.setattr(approval_resume, "resume_run", lambda *a, **k: None)

    out = runner.try_resume_pending_approval(str(tenant), "no", _SID)

    assert out == "rejected"
    state = _approval_state(dsn, tenant, approval)
    assert state["decision"] == "rejected" and state["resolved_at"] is not None
