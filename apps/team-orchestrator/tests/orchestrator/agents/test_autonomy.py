"""VT-369 Gap-5 PR-2 — DB-backed behavioral tests for the autonomy substrate.

Covers ``orchestrator.agents.autonomy`` (the per-(tenant, agent) L2/L3 state machine on
migration 129) plus its three hooks:

  - the streak arithmetic: clean approvals increment, a non-clean approval counts but resets;
  - the §5.4 regression table: streak→0 always, 'reject' bumps lifetime_rejections, the
    kill-switch kinds FREEZE, any kind except owner_disengaged REVOKES at L3 — and every
    freeze/revoke ATOMICALLY cancels open batches (drafted drafts → halted): the binding rule
    that a kill switch never leaves armed batches ticking;
  - grant_l3's in-txn revalidation (stale streak / frozen → no-op);
  - revoke_l3 idempotence at L2 (still cancels — a revoke means stop the work);
  - set_frozen (freeze cancels, unfreeze doesn't) + the Gap-6 VTR override provenance tag;
  - the §5.5 always-confirm floor predicate (money / bulk / first-contact / novel-template);
  - coordinator.is_frozen reading the REAL table, fail-CLOSED on error;
  - approval_glue.apply_agent_decision counting outcomes in the resolution conn;
  - consent.opt_out same-conn agent attribution → optout_spike regression, with an
    attribution failure NEVER unwinding the opt-out (the compliance write stands).

DB substrate mirrors ``tests/orchestrator/agents/test_dsr_agent_purge.py``: importorskip
psycopg+dbos(+langgraph — tenant_connection pulls orchestrator.graph), skipif no DATABASE_URL,
module fixture apply_migrations + launch_dbos; seeds via direct autocommit psycopg (service
role, RLS bypassed at seed); the module under test runs on tenant_connection conns.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")
pytest.importorskip("langgraph")  # orchestrator.db -> orchestrator.graph imports langgraph

import psycopg  # noqa: E402 — after dependency skip guards

from orchestrator.agents.autonomy import (  # noqa: E402
    L3_CLEAN_STREAK_THRESHOLD,
    AutonomyState,
    cancel_open_batches,
    get_autonomy,
    grant_l3,
    is_always_confirm,
    l3_proposal_eligible,
    record_approval_outcome,
    record_regression_event,
    revoke_l3,
    set_frozen,
    vtr_autonomy_override,
)
from orchestrator.db import tenant_connection  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-369 autonomy substrate tests skipped",
)

pytestmark = requires_db

AGENT = "sales_recovery"
_TEMPLATE = "team_winback_simple"


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so the tenant_connection pool exists."""
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
            "VALUES ('VT-369 autonomy test', 'founding', 'trial', now(), 'restaurant', %s) "
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


def _seed_customer(dsn: str, tenant: UUID, *, phone: str | None = None) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO customers (tenant_id, display_name, phone_e164) "
            "VALUES (%s, 'Autonomy Cust', %s) RETURNING id",
            (str(tenant), phone),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_batch(
    dsn: str, tenant: UUID, *, status: str = "awaiting_approval", agent: str = AGENT
) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        wi = conn.execute(
            "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
            "VALUES (%s, %s, %s, 'drafting') RETURNING id",
            (str(tenant), f"item-{uuid4().hex[:12]}", agent),
        ).fetchone()
        assert wi is not None
        row = conn.execute(
            "INSERT INTO agent_draft_batches (tenant_id, work_item_id, agent, status) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (str(tenant), str(wi[0]), agent, status),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_draft(dsn: str, tenant: UUID, batch: UUID, customer: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_drafts (tenant_id, batch_id, customer_id, template_name) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (str(tenant), str(batch), str(customer), _TEMPLATE),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_contact(
    dsn: str, tenant: UUID, customer: UUID, *, agent: str = AGENT,
    template: str = _TEMPLATE,
) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO agent_customer_contacts (tenant_id, customer_id, agent, template_name) "
            "VALUES (%s, %s, %s, %s)",
            (str(tenant), str(customer), agent, template),
        )


def _seed_autonomy_row(
    dsn: str, tenant: UUID, *, agent: str = AGENT, level: str = "L2",
    streak: int = 0, frozen: bool = False,
) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_agent_autonomy "
            "(tenant_id, agent, level, clean_approval_streak, frozen, l3_granted_at) "
            "VALUES (%s, %s, %s, %s, %s, CASE WHEN %s = 'L3' THEN now() END)",
            (str(tenant), agent, level, streak, frozen, level),
        )


def _autonomy_row(dsn: str, tenant: UUID, agent: str = AGENT) -> dict[str, Any]:
    """Service-role read of the columns AutonomyState does not carry."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT level, clean_approval_streak, frozen, l3_revoked_at, revoke_reason, "
            "last_regression_kind FROM tenant_agent_autonomy "
            "WHERE tenant_id = %s AND agent = %s",
            (str(tenant), agent),
        ).fetchone()
    assert row is not None
    return {
        "level": row[0], "clean_approval_streak": row[1], "frozen": row[2],
        "l3_revoked_at": row[3], "revoke_reason": row[4], "last_regression_kind": row[5],
    }


def _batch_row(dsn: str, tenant: UUID, batch: UUID) -> dict[str, Any]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, edit_cycles FROM agent_draft_batches "
            "WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch)),
        ).fetchone()
    assert row is not None
    return {"status": row[0], "edit_cycles": row[1]}


def _draft_row(dsn: str, tenant: UUID, draft: UUID) -> dict[str, Any]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, skip_reason FROM agent_drafts WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(draft)),
        ).fetchone()
    assert row is not None
    return {"status": row[0], "skip_reason": row[1]}


def _clean_approvals(tenant: UUID, n: int) -> AutonomyState:
    with tenant_connection(tenant) as conn:
        st = get_autonomy(tenant, AGENT, conn=conn)
        for _ in range(n):
            st = record_approval_outcome(tenant, AGENT, clean=True, conn=conn)
    return st


# ---------------------------------------------------------------------------
# 1. The default: a missing row IS L2
# ---------------------------------------------------------------------------


def test_get_autonomy_missing_row_is_l2_defaults(substrate) -> None:
    tenant = _new_tenant(substrate.dsn)
    st = get_autonomy(tenant, AGENT)  # no conn → its own tenant_connection
    assert st.level == "L2"
    assert st.clean_approval_streak == 0
    assert st.lifetime_approvals == 0
    assert st.lifetime_rejections == 0
    assert st.frozen is False
    assert st.l3_grant_approval_id is None
    assert st.last_regression_kind is None


# ---------------------------------------------------------------------------
# 2. Streak arithmetic (§5.2)
# ---------------------------------------------------------------------------


def test_record_approval_outcome_clean_increments_nonclean_resets(substrate) -> None:
    tenant = _new_tenant(substrate.dsn)
    st = _clean_approvals(tenant, 3)
    assert st.clean_approval_streak == 3
    assert st.lifetime_approvals == 3

    # A non-clean approval (owner edited first) still COUNTS but resets the streak.
    with tenant_connection(tenant) as conn:
        st = record_approval_outcome(tenant, AGENT, clean=False, conn=conn)
    assert st.clean_approval_streak == 0
    assert st.lifetime_approvals == 4
    assert st.lifetime_rejections == 0


# ---------------------------------------------------------------------------
# 3. Regression kinds: 'reject' bumps lifetime_rejections, 'edit' resets only
# ---------------------------------------------------------------------------


def test_regression_reject_bumps_rejections_edit_resets_streak_only(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    assert _clean_approvals(tenant, 2).clean_approval_streak == 2

    with tenant_connection(tenant) as conn:
        st = record_regression_event(tenant, AGENT, "reject", conn=conn)
    assert st.clean_approval_streak == 0
    assert st.lifetime_rejections == 1
    assert st.frozen is False  # 'reject' is not a kill-switch kind
    assert st.level == "L2"
    assert st.last_regression_kind == "reject"

    # Rebuild a streak, then an 'edit' regression: streak reset ONLY.
    assert _clean_approvals(tenant, 1).clean_approval_streak == 1
    with tenant_connection(tenant) as conn:
        st = record_regression_event(tenant, AGENT, "edit", conn=conn)
    assert st.clean_approval_streak == 0
    assert st.lifetime_rejections == 1  # unchanged
    assert st.frozen is False
    assert st.last_regression_kind == "edit"


# ---------------------------------------------------------------------------
# 4. A FREEZING kind at L2 freezes AND atomically cancels open batches
# ---------------------------------------------------------------------------


def test_freezing_kind_freezes_and_cancels_open_batch(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    customer = _seed_customer(dsn, tenant)
    batch = _seed_batch(dsn, tenant, status="awaiting_approval")
    draft = _seed_draft(dsn, tenant, batch, customer)

    with tenant_connection(tenant) as conn:
        st = record_regression_event(tenant, AGENT, "optout_spike", conn=conn)

    assert st.frozen is True
    assert st.level == "L2"  # no revoke at L2 — freeze only
    assert st.clean_approval_streak == 0
    # The binding atomic-cancel rule: a kill switch never leaves armed batches ticking.
    assert _batch_row(dsn, tenant, batch)["status"] == "cancelled"
    d = _draft_row(dsn, tenant, draft)
    assert d["status"] == "halted"
    assert d["skip_reason"] == "halted_optout_spike"


# ---------------------------------------------------------------------------
# 5. At L3 ANY regression (except owner_disengaged) REVOKES to L2 + cancels
# ---------------------------------------------------------------------------


def test_l3_regression_revokes_to_l2_and_cancels(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_autonomy_row(dsn, tenant, level="L3", streak=25)
    customer = _seed_customer(dsn, tenant)
    batch = _seed_batch(dsn, tenant, status="awaiting_approval")
    draft = _seed_draft(dsn, tenant, batch, customer)

    with tenant_connection(tenant) as conn:
        st = record_regression_event(tenant, AGENT, "edit", conn=conn)

    assert st.level == "L2"  # revoked — one-way per incident
    assert st.clean_approval_streak == 0
    assert st.frozen is False  # 'edit' revokes but does not freeze
    row = _autonomy_row(dsn, tenant)
    assert row["l3_revoked_at"] is not None
    assert row["revoke_reason"] == "edit"
    # The revoke cancelled the in-flight batch atomically.
    assert _batch_row(dsn, tenant, batch)["status"] == "cancelled"
    assert _draft_row(dsn, tenant, draft)["status"] == "halted"


# ---------------------------------------------------------------------------
# 6. grant_l3: in-txn revalidation (streak >= 20, L2, unfrozen); stale → no-op
# ---------------------------------------------------------------------------


def test_grant_l3_grants_at_threshold_and_stores_consent_evidence(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_autonomy_row(dsn, tenant, streak=L3_CLEAN_STREAK_THRESHOLD)
    approval_id = uuid4()

    assert l3_proposal_eligible(get_autonomy(tenant, AGENT)) is True
    with tenant_connection(tenant) as conn:
        st = grant_l3(tenant, AGENT, approval_id, conn=conn)

    assert st.level == "L3"
    assert st.l3_granted_at is not None
    # The autonomy_upgrade approval row id IS the durable consent evidence (C3).
    assert st.l3_grant_approval_id == str(approval_id)


def test_grant_l3_stale_streak_is_noop(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_autonomy_row(dsn, tenant, streak=L3_CLEAN_STREAK_THRESHOLD - 1)

    with tenant_connection(tenant) as conn:
        st = grant_l3(tenant, AGENT, uuid4(), conn=conn)

    assert st.level == "L2"  # revalidation failed → no-op
    assert st.l3_grant_approval_id is None
    assert st.l3_granted_at is None


def test_grant_l3_frozen_is_noop(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _seed_autonomy_row(dsn, tenant, streak=L3_CLEAN_STREAK_THRESHOLD, frozen=True)

    assert l3_proposal_eligible(get_autonomy(tenant, AGENT)) is False
    with tenant_connection(tenant) as conn:
        st = grant_l3(tenant, AGENT, uuid4(), conn=conn)
    assert st.level == "L2"
    assert st.l3_grant_approval_id is None


# ---------------------------------------------------------------------------
# 7. revoke_l3: idempotent at L2, still cancels (a revoke means stop the work)
# ---------------------------------------------------------------------------


def test_revoke_l3_idempotent_at_l2_and_cancels_batches(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)  # NO autonomy row — agent is at the L2 default
    batch = _seed_batch(dsn, tenant, status="awaiting_approval")

    with tenant_connection(tenant) as conn:
        st = revoke_l3(tenant, AGENT, reason="ops_request", conn=conn)
    assert st.level == "L2"
    assert st.frozen is False
    assert _batch_row(dsn, tenant, batch)["status"] == "cancelled"
    row = _autonomy_row(dsn, tenant)
    assert row["l3_revoked_at"] is not None
    assert row["revoke_reason"] == "ops_request"

    # Second revoke at L2: same terminal state, no error (idempotent).
    with tenant_connection(tenant) as conn:
        st = revoke_l3(tenant, AGENT, reason="ops_request_again", conn=conn)
    assert st.level == "L2"


# ---------------------------------------------------------------------------
# 8. set_frozen: freeze cancels, unfreeze does NOT
# ---------------------------------------------------------------------------


def test_set_frozen_true_cancels_false_does_not(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    batch1 = _seed_batch(dsn, tenant, status="awaiting_approval")

    with tenant_connection(tenant) as conn:
        st = set_frozen(tenant, AGENT, True, reason="ops_kill", conn=conn)
    assert st.frozen is True
    assert _batch_row(dsn, tenant, batch1)["status"] == "cancelled"

    # Unfreezing cancels NOTHING — work re-enters via the next coordinator sweep.
    batch2 = _seed_batch(dsn, tenant, status="awaiting_approval")
    with tenant_connection(tenant) as conn:
        st = set_frozen(tenant, AGENT, False, reason="ops_clear", conn=conn)
    assert st.frozen is False
    assert _batch_row(dsn, tenant, batch2)["status"] == "awaiting_approval"


# ---------------------------------------------------------------------------
# 9. The Gap-6 VTR override seam: provenance-tagged reason
# ---------------------------------------------------------------------------


def test_vtr_override_freeze_carries_vtr_provenance(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    customer = _seed_customer(dsn, tenant)
    batch = _seed_batch(dsn, tenant, status="awaiting_approval")
    draft = _seed_draft(dsn, tenant, batch, customer)

    with tenant_connection(tenant) as conn:
        st = vtr_autonomy_override(
            tenant, AGENT, "freeze", reason="owner complaint", vtr_id="v1", conn=conn
        )

    assert st.frozen is True
    assert _batch_row(dsn, tenant, batch)["status"] == "cancelled"
    # The vtr id rides the cancel reason onto the halted draft (the audit trail).
    d = _draft_row(dsn, tenant, draft)
    assert d["status"] == "halted"
    assert "vtr:v1" in (d["skip_reason"] or "")


# ---------------------------------------------------------------------------
# 10. The §5.5 always-confirm floor
# ---------------------------------------------------------------------------


def test_is_always_confirm_floor_predicates(substrate) -> None:
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    precedented = _seed_customer(dsn, tenant)
    _seed_contact(dsn, tenant, precedented, template=_TEMPLATE)
    fresh = _seed_customer(dsn, tenant)  # NO agent_customer_contacts row

    with tenant_connection(tenant) as conn:
        # Money beats everything.
        assert is_always_confirm(
            tenant, agent=AGENT, batch_customer_ids=[str(precedented)],
            template_name=_TEMPLATE, money_bearing=True, conn=conn,
        ) == (True, "money_template")
        # Bulk: > 20 customers (checked before any DB read).
        assert is_always_confirm(
            tenant, agent=AGENT, batch_customer_ids=[str(uuid4()) for _ in range(21)],
            template_name=_TEMPLATE, money_bearing=False, conn=conn,
        ) == (True, "bulk")
        # First contact: ANY customer in the batch with no prior contact row.
        assert is_always_confirm(
            tenant, agent=AGENT, batch_customer_ids=[str(precedented), str(fresh)],
            template_name=_TEMPLATE, money_bearing=False, conn=conn,
        ) == (True, "first_contact")
        # Novel template: this tenant has never sent it before.
        assert is_always_confirm(
            tenant, agent=AGENT, batch_customer_ids=[str(precedented)],
            template_name="team_never_sent_before", money_bearing=False, conn=conn,
        ) == (True, "novel_template")
        # All-precedented + known template → the floor does not bite.
        assert is_always_confirm(
            tenant, agent=AGENT, batch_customer_ids=[str(precedented)],
            template_name=_TEMPLATE, money_bearing=False, conn=conn,
        ) == (False, "")


# ---------------------------------------------------------------------------
# 11. coordinator.is_frozen: reads the real table, fail-CLOSED
# ---------------------------------------------------------------------------


def test_coordinator_is_frozen_reads_real_table(substrate) -> None:
    dsn = substrate.dsn
    from orchestrator.agents.coordinator import is_frozen

    unfrozen_tenant = _new_tenant(dsn)
    assert is_frozen(unfrozen_tenant, AGENT) is False  # missing row = L2, unfrozen

    frozen_tenant = _new_tenant(dsn)
    _seed_autonomy_row(dsn, frozen_tenant, frozen=True)
    assert is_frozen(frozen_tenant, AGENT) is True


def test_coordinator_is_frozen_fail_closed_on_error(substrate, monkeypatch) -> None:
    from orchestrator.agents.coordinator import is_frozen

    def _boom(*_a: Any, **_k: Any) -> AutonomyState:
        raise RuntimeError("db unreachable")

    # is_frozen imports get_autonomy at call time — patch the autonomy module attr.
    monkeypatch.setattr("orchestrator.agents.autonomy.get_autonomy", _boom)
    assert is_frozen(_new_tenant(substrate.dsn), AGENT) is True  # can't verify ⇒ frozen


# ---------------------------------------------------------------------------
# 12. The approval_glue hook: outcomes counted in the resolution conn
# ---------------------------------------------------------------------------


def _seed_agent_approval(dsn: str, tenant: UUID, batch: UUID) -> UUID:
    """An OPEN agent_customer_send approval row linked to the batch (the armed shape).
    Migration 128 allows ONE open row per tenant — callers resolve before re-seeding."""
    run = _new_run(dsn, tenant)
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO pending_approvals (tenant_id, run_id, approval_type, summary, "
            "details, status, draft_batch_id, timeout_at) "
            "VALUES (%s, %s, 'agent_customer_send', 'batch: counts only', "
            "'{\"draft_count\": 1}'::jsonb, 'pending', %s, now() + interval '30 minutes') "
            "RETURNING id",
            (str(tenant), str(run), str(batch)),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _resolve_directly(dsn: str, tenant: UUID, approval_id: UUID, decision: str) -> None:
    """Service-role close of the approval row so the one-open-per-tenant index admits
    the next seed (apply_agent_decision owns the BATCH, not the approval row)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE pending_approvals SET resolved_at = now(), decision = %s "
            "WHERE tenant_id = %s AND id = %s",
            (decision, str(tenant), str(approval_id)),
        )


def test_apply_agent_decision_counts_outcomes_in_resolution_conn(substrate) -> None:
    from orchestrator.agents.approval_glue import apply_agent_decision

    dsn = substrate.dsn
    tenant = _new_tenant(dsn)

    # -- approved with edit_cycles == 0 → a CLEAN approval: streak 1 ------------
    batch1 = _seed_batch(dsn, tenant, status="awaiting_approval")
    ap1 = _seed_agent_approval(dsn, tenant, batch1)
    with tenant_connection(tenant) as conn, conn.transaction():
        out = apply_agent_decision(conn, str(tenant), {"id": str(ap1)}, "approved")
    assert out is not None
    assert out.batch_status == "approved"
    assert out.edit_cycles == 0
    st = get_autonomy(tenant, AGENT)
    assert st.clean_approval_streak == 1
    assert st.lifetime_approvals == 1
    _resolve_directly(dsn, tenant, ap1, "approved")

    # -- needs_changes (first) → 'edit' regression: streak back to 0 ------------
    batch2 = _seed_batch(dsn, tenant, status="awaiting_approval")
    ap2 = _seed_agent_approval(dsn, tenant, batch2)
    with tenant_connection(tenant) as conn, conn.transaction():
        out = apply_agent_decision(
            conn, str(tenant), {"id": str(ap2)}, "needs_changes",
            owner_feedback="softer tone",
        )
    assert out is not None
    assert out.batch_status == "edit_requested"
    assert out.regeneration_requested is True
    assert out.edit_cycles == 1
    st = get_autonomy(tenant, AGENT)
    assert st.clean_approval_streak == 0
    assert st.lifetime_rejections == 0  # an edit is NOT a rejection
    assert st.last_regression_kind == "edit"

    # -- needs_changes (second, after re-arm) → terminal 'reject' regression ----
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE agent_draft_batches SET status = 'awaiting_approval' "
            "WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch2)),
        )
    with tenant_connection(tenant) as conn, conn.transaction():
        out = apply_agent_decision(
            conn, str(tenant), {"id": str(ap2)}, "needs_changes",
            owner_feedback="still wrong",
        )
    assert out is not None
    assert out.batch_status == "rejected"  # ONE regeneration max
    st = get_autonomy(tenant, AGENT)
    assert st.lifetime_rejections == 1
    assert st.last_regression_kind == "reject"


# ---------------------------------------------------------------------------
# 13. The opt-out attribution hook (consent.opt_out, same-conn)
# ---------------------------------------------------------------------------

_SALT = "vt369-pr2-autonomy-test-salt"


def _seed_consented_contacted_customer(
    dsn: str, tenant: UUID, *, opted_out: bool
) -> tuple[UUID, str]:
    """A customer with phone + a record_of_consent row + a ≤30d agent contact."""
    from orchestrator.utils.phone_token import hash_phone

    phone = f"+9197{uuid4().int % 10**8:08d}"
    customer = _seed_customer(dsn, tenant, phone=phone)
    token = hash_phone(phone)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO record_of_consent (tenant_id, phone_token, consent_text_version, "
            "opted_out_at) VALUES (%s, %s, 'v1', CASE WHEN %s THEN now() END)",
            (str(tenant), token, opted_out),
        )
    _seed_contact(dsn, tenant, customer)
    return customer, token


def test_opt_out_spike_fires_optout_spike_regression(substrate, monkeypatch) -> None:
    from orchestrator.privacy import consent

    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", _SALT)
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)

    # 3 customers ALREADY opted out in the spike window, each agent-contacted ≤30d.
    for _ in range(3):
        _seed_consented_contacted_customer(dsn, tenant, opted_out=True)
    # The opting-out customer: active consent + a ≤30d sales_recovery contact.
    _, token = _seed_consented_contacted_customer(dsn, tenant, opted_out=False)

    assert consent.opt_out(tenant, token) is True

    # The same-conn attribution found the agent + the >=3 spike → frozen.
    st = get_autonomy(tenant, AGENT)
    assert st.frozen is True
    assert st.last_regression_kind == "optout_spike"
    # And the opt-out itself is stamped.
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT opted_out_at FROM record_of_consent "
            "WHERE tenant_id = %s AND phone_token = %s",
            (str(tenant), token),
        ).fetchone()
    assert row is not None and row[0] is not None


def test_opt_out_survives_attribution_failure(substrate, monkeypatch) -> None:
    """An attribution failure NEVER unwinds the compliance write: opt_out still
    returns True and the row is stamped (the spike trigger is the only loss)."""
    from orchestrator.db.wrappers import CustomersWrapper
    from orchestrator.privacy import consent

    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", _SALT)
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    _, token = _seed_consented_contacted_customer(dsn, tenant, opted_out=False)

    def _boom(self: Any, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
        raise RuntimeError("attribution query exploded")

    monkeypatch.setattr(CustomersWrapper, "agent_optout_attribution", _boom)

    assert consent.opt_out(tenant, token) is True  # the opt-out stands

    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT opted_out_at FROM record_of_consent "
            "WHERE tenant_id = %s AND phone_token = %s",
            (str(tenant), token),
        ).fetchone()
    assert row is not None and row[0] is not None
    # No regression fired — the agent is untouched.
    assert get_autonomy(tenant, AGENT).frozen is False


# ---------------------------------------------------------------------------
# Extras: the eligibility predicate + cancel sweeps every non-terminal status
# ---------------------------------------------------------------------------


def test_l3_proposal_eligible_predicate() -> None:
    def _st(**kw: Any) -> AutonomyState:
        base: dict[str, Any] = {
            "tenant_id": uuid4(), "agent": AGENT, "level": "L2",
            "clean_approval_streak": L3_CLEAN_STREAK_THRESHOLD,
            "lifetime_approvals": 30, "lifetime_rejections": 0, "frozen": False,
        }
        base.update(kw)
        return AutonomyState(**base)

    assert l3_proposal_eligible(_st()) is True
    assert l3_proposal_eligible(_st(clean_approval_streak=19)) is False
    assert l3_proposal_eligible(_st(frozen=True)) is False
    assert l3_proposal_eligible(_st(level="L3")) is False


def test_cancel_open_batches_skips_terminal_batches(substrate) -> None:
    """Only non-terminal batches are killed; sent/rejected history is untouched."""
    dsn = substrate.dsn
    tenant = _new_tenant(dsn)
    open_batch = _seed_batch(dsn, tenant, status="drafting")
    sent_batch = _seed_batch(dsn, tenant, status="sent")

    with tenant_connection(tenant) as conn:
        n = cancel_open_batches(tenant, AGENT, reason="test_sweep", conn=conn)

    assert n == 1
    assert _batch_row(dsn, tenant, open_batch)["status"] == "cancelled"
    assert _batch_row(dsn, tenant, sent_batch)["status"] == "sent"


def test_opt_out_survives_attribution_SQL_error(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """The Cowork prod-gate fix: a SERVER-SIDE SQL error in the attribution (not just a Python
    raise) must NOT lose the opt-out. Without the SAVEPOINT the shared txn aborts, the except
    swallows it, and the commit downgrades to rollback — the opt-out itself is lost. The nested
    transaction scopes the failure to the attribution; the opt-out COMMITS."""
    import psycopg as _psycopg

    from orchestrator.db.wrappers import CustomersWrapper
    from orchestrator.privacy import consent

    tenant = _new_tenant(substrate.dsn)
    token = "phone_tok_" + "a" * 64
    with _psycopg.connect(substrate.dsn, autocommit=True) as c:
        c.execute(
            "INSERT INTO record_of_consent (tenant_id, phone_token, consent_text_version) "
            "VALUES (%s, %s, 'v1')",
            (str(tenant), token),
        )

    def _sql_error(self, *a, **k):  # a real server-side error INSIDE the shared txn
        conn = k.get("conn")
        conn.execute("SELECT no_such_column FROM tenant_agent_autonomy")
        raise AssertionError("unreachable")

    monkeypatch.setattr(CustomersWrapper, "agent_optout_attribution", _sql_error)
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "test-salt")

    assert consent.opt_out(tenant, token) is True  # must not raise

    with _psycopg.connect(substrate.dsn, autocommit=True) as c:
        row = c.execute(
            "SELECT opted_out_at FROM record_of_consent WHERE tenant_id = %s AND phone_token = %s",
            (str(tenant), token),
        ).fetchone()
    assert row is not None and row[0] is not None, (
        "the opt-out must COMMIT despite the attribution SQL error (the SAVEPOINT)"
    )
