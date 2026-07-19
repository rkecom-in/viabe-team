"""VT-254 — real-DB RLS verification for the set_config tenant-scoping sweep.

This is the criterion-2 deliverable. Each tool swept in VT-254 (and the two
forward-target tools that share the bug class) is exercised against a LIVE
Postgres under the NON-SUPERUSER ``rls_tester`` role — a superuser bypasses RLS
and would false-pass — proving two things the mock-cursor unit tests could not:

  1. ``SELECT set_config('app.current_tenant', %s, false)`` executes WITHOUT a
     SyntaxError. The prior ``SET LOCAL app.current_tenant = %s`` form was a hard
     Postgres syntax error (a parameter cannot bind into SET). The unit tests
     mocked the cursor and never sent SQL to a server, so they were false-greens
     that hid it (VT-140 surfaced the class).
  2. CROSS-TENANT DENIAL: with the GUC scoped to tenant A, tenant B's rows are
     invisible / unwritable through the real tool code path, for every tool
     whose backing table has RLS enabled.

Gated on DATABASE_URL + psycopg, mirroring tests/test_migrations.py. Runs in the
CI ``orchestrator`` job (postgres service + full project + DATABASE_URL set);
skipped elsewhere. CL-422: synthetic data only.

Scope notes — latent bugs SURFACED by these real-DB tests but flagged to Cowork,
NOT fixed here (out of VT-254's set_config-swap scope; each needs a migration or
a logic change that belongs in its own row):

  - ``customer_ledger_entries`` (match_transactions) is not in main at all
    (forward-target); the tool swallows UndefinedTable → empty result. Its test
    therefore proves only the set_config fix, not RLS denial.
  - ``customers.phone_token`` (query_customer_ledger) does not exist — the landed
    customers table (mig 045) has ``phone_e164``. The tool only swallows
    UndefinedTable, so it raises UndefinedColumn against the real schema. Its
    test proves the set_config statement runs (we reach the customers query, not
    a SET syntax error) and pins the current behaviour.
  - ``customer_ledger_entries`` / ``outbound_send_ledger`` have no RLS enabled;
    isolation there is WHERE-clause only (defence-in-depth gap).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402 — after the psycopg gate

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-254 real-DB RLS tests skipped",
)


# --------------------------------------------------------------------------- #
# Fixtures + helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def dsn():
    """Apply migrations once, then ensure a non-superuser ``rls_tester`` role
    exists with table DML + the GUC helper grant. RLS is only enforced for
    non-superusers, so the tools must run under this role to prove isolation.
    """
    import apply_migrations  # lazy: keeps module import-light for --no-project

    d = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=d)
    assert not r["failed"], r["failed"]

    with psycopg.connect(d, autocommit=True) as conn:
        # CREATE-if-absent (no DROP: a DROP would fail once privileges have been
        # granted, and other modules in the same job may share the role).
        conn.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rls_tester') THEN
                    CREATE ROLE rls_tester NOLOGIN;
                END IF;
            END
            $$;
            """
        )
        conn.execute("GRANT USAGE ON SCHEMA public TO rls_tester")
        conn.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE "
            "ON ALL TABLES IN SCHEMA public TO rls_tester"
        )
        conn.execute("GRANT EXECUTE ON FUNCTION app_current_tenant() TO rls_tester")
    return d


@contextmanager
def _rls_conn(dsn: str):
    """A fresh autocommit + dict_row connection running as the non-superuser
    ``rls_tester`` role, so RLS policies are enforced. Mirrors the orchestrator
    pool's shape (autocommit, dict_row); a new connection per call means no GUC
    leaks between tool invocations.
    """
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("SET ROLE rls_tester")
        try:
            yield conn
        finally:
            try:
                conn.execute("RESET ROLE")
            except Exception:  # noqa: BLE001 — connection may be closing
                pass


class _RlsPool:
    """Minimal ``psycopg_pool``-shaped stand-in: ``.connection()`` yields an
    rls_tester connection (so the real tool code exercises real RLS)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def connection(self):
        return _rls_conn(self._dsn)


@pytest.fixture
def seed_conn(dsn):
    """Superuser autocommit connection for seeding (bypasses RLS to plant rows
    for BOTH tenants)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        yield conn


def _seed_tenant(conn, name: str, plan: str = "standard", ownership_verified: bool = True) -> str:
    return str(
        conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, ownership_verified) "
            "VALUES (%s, %s, 'onboarding', %s) RETURNING id",
            (name, plan, ownership_verified),
        ).fetchone()[0]
    )


def _seed_run(conn, tenant_id: str) -> str:
    return str(
        conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, status) "
            "VALUES (%s, 'running') RETURNING id",
            (tenant_id,),
        ).fetchone()[0]
    )


def _seed_campaign(
    conn,
    tenant_id: str,
    run_id: str,
    template_id: str = "tmpl_x",
    status: str = "proposed",
) -> str:
    # Post-mig-018 campaigns shape: subscriber_id/template_id/body_params/
    # proposed_by dropped, proposed_at→generated_at, template lives in
    # plan_json -> 'message_plan' ->> 'template_id' (CampaignPlan v1.0).
    plan = json.dumps({"message_plan": {"template_id": template_id}})
    return str(
        conn.execute(
            """
            INSERT INTO campaigns (tenant_id, run_id, status, generated_at, plan_json)
            VALUES (%s, %s, %s, now(), %s::jsonb)
            RETURNING id
            """,
            (tenant_id, run_id, status, plan),
        ).fetchone()[0]
    )


def _seed_customer(
    conn,
    tenant_id: str,
    phone: str,
    display: str | None = None,
    inbound_recent: bool = False,
) -> str:
    last_inbound = datetime.now(timezone.utc) if inbound_recent else None
    return str(
        conn.execute(
            "INSERT INTO customers (tenant_id, display_name, phone_e164, last_inbound_at) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (tenant_id, display, phone, last_inbound),
        ).fetchone()[0]
    )


@pytest.fixture(scope="module")
def tenants(dsn):
    """Two synthetic tenants with distinct business names (module-scoped)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        a = _seed_tenant(conn, "Alpha Audio", plan="founding")
        b = _seed_tenant(conn, "Bravo Books", plan="standard", ownership_verified=False)
    return a, b


def _count_other(
    dsn: str, scoped_to: str, table: str, other: str, col: str = "tenant_id"
) -> int:
    """Under GUC scoped to ``scoped_to`` (rls_tester role), how many of
    ``other``'s rows are visible in ``table``? Must be 0 under RLS. ``col`` is
    the tenant-identity column (``id`` for the tenants table itself)."""
    with _rls_conn(dsn) as conn:
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (scoped_to,))
        return conn.execute(
            f"SELECT count(*) AS n FROM {table} WHERE {col} = %s",  # noqa: S608 — test literals
            (other,),
        ).fetchone()["n"]


# --------------------------------------------------------------------------- #
# RLS-enabled tools — full cross-tenant denial through the real tool path
# --------------------------------------------------------------------------- #
def test_get_business_profile_scopes_and_denies(dsn, tenants):
    from orchestrator.agent.tools.get_business_profile import (
        GetBusinessProfileInput,
        get_business_profile,
    )

    a, b = tenants
    pool = _RlsPool(dsn)

    out = get_business_profile(GetBusinessProfileInput(tenant_id=a), pool=pool)
    assert out is not None
    assert out.business_name == "Alpha Audio"

    # Denial: scoped to A, B's tenant row is invisible at the RLS layer.
    # (tenants keys tenant identity on its own ``id`` column.)
    assert _count_other(dsn, scoped_to=a, table="tenants", other=b, col="id") == 0

    # VT-342: get_business_profile now runs under tenant_connection (SET ROLE app_role + GUC),
    # so RLS is a REAL second layer, not WHERE-id-only. Prove the SAME primitive it uses
    # blocks cross-tenant — under GUC=A, B's row is invisible EVEN WHEN the WHERE matches B's
    # id (RLS denies it, not the WHERE predicate).
    from orchestrator.db.tenant_connection import tenant_connection

    with tenant_connection(a, pool=pool) as conn:
        assert (
            conn.execute(
                "SELECT business_name FROM tenants WHERE id = %s", (b,)
            ).fetchone()
            is None
        )


def test_schedule_followup_writes_and_denies(dsn, tenants, seed_conn):
    from orchestrator.agent.tools.schedule_followup import (
        ScheduleFollowupInput,
        schedule_followup,
    )

    a, b = tenants
    pool = _RlsPool(dsn)
    fire_at = datetime.now(timezone.utc) + timedelta(days=2)

    out = schedule_followup(
        ScheduleFollowupInput(
            tenant_id=a,
            follow_up_type="campaign_followup",
            fire_at=fire_at,
            follow_up_key="vt254-a-1",
        ),
        pool=pool,
    )
    assert out.status == "scheduled"
    assert out.scheduled_id is not None

    # Seed a B-owned row (superuser), then confirm it is invisible under GUC=A.
    seed_conn.execute(
        "INSERT INTO scheduled_followups "
        "(tenant_id, follow_up_type, follow_up_key, fire_at, payload) "
        "VALUES (%s, 'campaign_followup', 'vt254-b-1', %s, '{}'::jsonb)",
        (b, fire_at),
    )
    assert _count_other(dsn, scoped_to=a, table="scheduled_followups", other=b) == 0


def test_send_whatsapp_message_sends_and_denies(dsn, tenants, seed_conn):
    from orchestrator.agent.tools.send_whatsapp_message import (
        SendWhatsAppMessageInput,
        send_whatsapp_message,
    )

    a, b = tenants
    pool = _RlsPool(dsn)
    cust_a = _seed_customer(seed_conn, a, "+919900000001", "Asha", inbound_recent=True)
    cust_b = _seed_customer(seed_conn, b, "+919900000002", "Bhavna", inbound_recent=True)

    def _fake_send(body: str, recipient_phone: str) -> str:
        return "SM" + "a" * 32

    # VT-306: _resolve_customer reads via CustomersWrapper on its own
    # tenant_connection (SET ROLE app_role + GUC). The send flow keeps its
    # injected pool for send_idempotency_keys; point the GLOBAL pool at the test
    # dsn so the wrapper read works. Cross-tenant denial is then enforced by the
    # wrapper's tenant_connection RLS (the customers cross-tenant-negative canary).
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from orchestrator import graph as graph_mod

    prev_pool = graph_mod._pool
    graph_mod._pool = ConnectionPool(
        dsn, min_size=1, max_size=2,
        kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
    )
    try:
        out = send_whatsapp_message(
            SendWhatsAppMessageInput(
                tenant_id=a, customer_id=cust_a, body="hello", idempotency_key="k-a-1"
            ),
            pool=pool,
            send_fn=_fake_send,
        )
        assert out.status == "sent"
        assert out.message_sid is not None

        # Cross-tenant denial THROUGH the tool: A sends to B's customer → the
        # wrapper's tenant_connection RLS makes B's row invisible → 'unauthorized'.
        out_x = send_whatsapp_message(
            SendWhatsAppMessageInput(
                tenant_id=a, customer_id=cust_b, body="hello", idempotency_key="k-a-x"
            ),
            pool=pool,
            send_fn=_fake_send,
        )
        assert out_x.status == "unauthorized"
    finally:
        graph_mod._pool.close()
        graph_mod._pool = prev_pool

    # VT-263: make the RLS backstops REAL (the review found these were vacuous /
    # WHERE-clause-shaped — they passed even with RLS disabled).
    # (1) The 'unauthorized' above is WHERE-clause-enforced; prove RLS *also*
    #     hides B's customer row under A's GUC (B's customer genuinely exists).
    assert _count_other(dsn, scoped_to=a, table="customers", other=b) == 0
    # (2) Seed a B-owned send_idempotency_keys row (superuser bypasses RLS) so the
    #     count below tests RLS, not table-emptiness — A's send wrote only A's row.
    seed_conn.execute(
        "INSERT INTO send_idempotency_keys "
        "(tenant_id, idempotency_key, customer_id, message_sid, send_status) "
        "VALUES (%s, 'k-b-decoy', %s, 'SMb', 'sent')",
        (b, cust_b),
    )
    assert _count_other(dsn, scoped_to=a, table="send_idempotency_keys", other=b) == 0


def test_cohort_resolve_denies_cross_tenant(dsn, tenants, seed_conn):
    from orchestrator.privacy.cohort import resolve_cohort_recipients

    a, b = tenants
    camp_a = _seed_campaign(seed_conn, a, _seed_run(seed_conn, a))
    cust_a = _seed_customer(seed_conn, a, "+919900000010")
    cust_b = _seed_customer(seed_conn, b, "+919900000011")

    # VT-306 (bounce fix): the standalone path opens its own tenant_connection
    # (SET ROLE app_role + GUC); pool is vestigial. Point the global pool at the
    # test dsn so it works; cross-tenant denial is the tenant_connection RLS.
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from orchestrator import graph as graph_mod

    prev_pool = graph_mod._pool
    graph_mod._pool = ConnectionPool(
        dsn, min_size=1, max_size=2,
        kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
    )
    try:
        res = resolve_cohort_recipients(
            tenant_id=a,
            campaign_id=camp_a,
            customer_ids=[cust_a, cust_b],
            pool=object(),
        )
    finally:
        graph_mod._pool.close()
        graph_mod._pool = prev_pool

    # A's customer resolves; B's customer is invisible under A's GUC → rejected,
    # never linked (Fazal requirement: never silently dropped).
    assert cust_a in res.resolved
    assert cust_b in res.rejected
    assert cust_b not in res.resolved
    # VT-263: the rejection above is WHERE-clause-enforced (tenant_id = A filter);
    # prove RLS *also* hides B's customer row under A's GUC (real RLS backstop —
    # B's customer genuinely exists, so 0 means RLS hid it).
    assert _count_other(dsn, scoped_to=a, table="customers", other=b) == 0


def test_customer_registry_denies_cross_tenant(dsn, tenants, seed_conn):
    from orchestrator.privacy import customer_registry

    a, b = tenants
    _seed_customer(seed_conn, a, "+919900000020", display="Asha")
    _seed_customer(seed_conn, b, "+919900000021", display="Bhavna")
    customer_registry.invalidate_all()

    # VT-306: get_customer_names_for_tenant reads via CustomersWrapper.list_display_names
    # on its own tenant_connection (pool arg vestigial). Point the global pool at the
    # test dsn; cross-tenant denial is then the wrapper's tenant_connection RLS.
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from orchestrator import graph as graph_mod

    prev_pool = graph_mod._pool
    graph_mod._pool = ConnectionPool(
        dsn, min_size=1, max_size=2,
        kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
    )
    try:
        names = customer_registry.get_customer_names_for_tenant(a, use_cache=False)
    finally:
        graph_mod._pool.close()
        graph_mod._pool = prev_pool

    # Registry lower-cases names for redaction matching.
    assert "asha" in names
    assert "bhavna" not in names  # the wrapper's tenant_connection RLS excludes B
    # VT-263: real RLS backstop — B's customer row exists, so 0 means RLS (not
    # just the WHERE clause) hides it under A's GUC.
    assert _count_other(dsn, scoped_to=a, table="customers", other=b) == 0


# --------------------------------------------------------------------------- #
# Forward-target tools — prove the set_config fix; RLS denial N/A (see header)
# --------------------------------------------------------------------------- #
def test_match_transactions_set_config_runs_no_syntax_error(dsn, tenants):
    from orchestrator.agent.tools.match_transactions import (
        MatchTransactionsInput,
        TransactionInput,
        match_transactions,
    )

    a, _ = tenants
    pool = _RlsPool(dsn)
    txn = TransactionInput(
        txn_id="t1", amount_paise=1000, timestamp=datetime.now(timezone.utc)
    )

    # customer_ledger_entries is not in main (forward-target) → the tool swallows
    # UndefinedTable and returns empty. Reaching that path proves set_config ran
    # (pre-fix the SET LOCAL form SyntaxError'd before the table query).
    out = match_transactions(
        MatchTransactionsInput(tenant_id=a, transactions=[txn]), pool=pool
    )
    assert out.matches == []
    assert [u.txn_id for u in out.unmatched] == ["t1"]


def test_get_recent_campaigns_denies_cross_tenant(dsn, tenants, seed_conn):
    # VT-256: get_recent_campaigns now reads generated_at + plan_json->
    # 'message_plan'->>'template_id' (mig-018-reconciled). This was VT-254's
    # pytest.raises(UndefinedColumn) placeholder — now a real success+denial
    # assertion. Seeds a campaign per tenant; scoped to A, only A's is visible.
    from orchestrator.agent.tools.get_recent_campaigns import (
        GetRecentCampaignsInput,
        get_recent_campaigns,
    )

    a, b = tenants
    camp_a = _seed_campaign(seed_conn, a, _seed_run(seed_conn, a), template_id="tmpl_a")
    camp_b = _seed_campaign(seed_conn, b, _seed_run(seed_conn, b), template_id="tmpl_b")

    # VT-306: get_recent_campaigns now reads via CampaignsWrapper (tenant_connection
    # = SET ROLE app_role + GUC). Point the global pool at the test dsn so the
    # wrapper works; the cross-tenant denial is enforced by RLS through the wrapper
    # (this is the wrapper's cross-tenant-negative canary for campaigns).
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from orchestrator import graph as graph_mod

    prev = graph_mod._pool
    graph_mod._pool = ConnectionPool(
        dsn, min_size=1, max_size=2,
        kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
    )
    try:
        out = get_recent_campaigns(
            GetRecentCampaignsInput(tenant_id=a, days_back=365, limit=200)
        )
    finally:
        graph_mod._pool.close()
        graph_mod._pool = prev

    by_id = {c.campaign_id: c for c in out.campaigns}
    assert camp_a in by_id
    assert by_id[camp_a].template_id == "tmpl_a"  # plan_json read works
    assert camp_b not in by_id  # the wrapper's tenant_connection RLS hides B from A
    assert _count_other(dsn, scoped_to=a, table="campaigns", other=b) == 0


def test_query_customer_ledger_degrades_gracefully(dsn, tenants):
    # VT-257: customer_ledger_entries (table) AND customers.phone_token (column)
    # are both forward-target/unlanded. The tool now swallows UndefinedColumn as
    # well as UndefinedTable → graceful empty instead of the runtime crash VT-254
    # caught. (Was VT-254's pytest.raises(UndefinedColumn) placeholder.)
    from orchestrator.agent.tools.query_customer_ledger import (
        QueryCustomerLedgerInput,
        query_customer_ledger,
    )

    a, _ = tenants
    pool = _RlsPool(dsn)

    out = query_customer_ledger(
        QueryCustomerLedgerInput(tenant_id=a, customer_phone_token="tok-x"),
        pool=pool,
    )
    assert out.customer_id is None
    assert out.ledger_entries == []
    assert out.total_balance_paise == 0


def test_get_attribution_data_denies_cross_tenant(dsn, tenants, seed_conn):
    """VT-306 bounce-2 e2e: get_attribution_data reads `attributions` DIRECTLY on
    its outer connection — now a tenant_connection (SET ROLE app_role + GUC), so
    that read is RLS-enforced. Tenant A querying B's campaign id must see NOTHING
    (no campaign, no ARRR) — proves the direct attributions read can't cross
    tenants even though `attributions` is not a VT-306 gate-flagged table."""
    from orchestrator.agent.tools.get_attribution_data import (
        GetAttributionDataInput,
        get_attribution_data,
    )

    a, b = tenants
    camp_b = _seed_campaign(seed_conn, b, _seed_run(seed_conn, b), status="sent")
    seed_conn.execute(
        "UPDATE campaigns SET attribution_close_at = now(), attribution_closed_at = now() "
        "WHERE id = %s", (camp_b,),
    )
    # B-owned attribution — a cross-tenant leak would surface this 55555.
    seed_conn.execute(
        "INSERT INTO attributions (tenant_id, campaign_id, attributed_paise) "
        "VALUES (%s, %s, 55555)", (b, camp_b),
    )

    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from orchestrator import graph as graph_mod

    prev_pool = graph_mod._pool
    graph_mod._pool = ConnectionPool(
        dsn, min_size=1, max_size=2,
        kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
    )
    try:
        out = get_attribution_data(
            GetAttributionDataInput(tenant_id=a, campaign_id=camp_b)
        )
    finally:
        graph_mod._pool.close()
        graph_mod._pool = prev_pool

    assert out.campaign is not None
    # A cannot see B's campaign → not-found path; B's 55555 ARRR is NOT leaked.
    assert out.campaign.attribution_status == "unknown"
    assert out.campaign.arrr_paise == 0
