"""VT-138 / VT-563 — _build_recent_campaigns DB-substrate tests.

Exercises the live campaigns-table read path. Requires Postgres via
``DATABASE_URL`` + the dbos stack; runs in the CI ``orchestrator`` job.

VT-563 update: ``recovered_paise`` is now the campaign's REAL attributed ARRR
(SUM ``attributions.attributed_paise``) — 0 when a campaign has no attribution
rows yet, which is complete knowledge, not a placeholder. The section
completeness flag is ``True`` whenever the attribution read succeeds (the stale
"always False" contract is gone).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after the dependency skip guard

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-138 campaigns-readpath tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations (incl. 016 + 018 campaigns) and launch DBOS so
    ``get_pool()`` is initialised — ``tenant_connection`` needs the
    pool."""
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


def _new_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-138 Test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _new_pipeline_run(dsn: str, tenant_id: UUID) -> UUID:
    run_id = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running')",
            (str(run_id), str(tenant_id)),
        )
    return run_id


def _seed_campaign(
    dsn: str,
    tenant_id: UUID,
    run_id: UUID,
    *,
    generated_at: datetime,
    status: str = "proposed",
) -> UUID:
    campaign_id = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO campaigns (id, tenant_id, run_id, status, generated_at, "
            "plan_json) VALUES (%s, %s, %s, %s, %s, '{}'::jsonb)",
            (
                str(campaign_id),
                str(tenant_id),
                str(run_id),
                status,
                generated_at,
            ),
        )
    return campaign_id


def _seed_attribution(dsn: str, tenant_id: UUID, campaign_id: UUID, paise: int) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO attributions (tenant_id, campaign_id, attributed_paise) "
            "VALUES (%s, %s, %s)",
            (str(tenant_id), str(campaign_id), paise),
        )


# --- Tests -------------------------------------------------------------------


def test_returns_real_snapshots_with_real_recovered_paise(substrate):  # type: ignore[no-untyped-def]
    """Seeded campaigns return as CampaignSnapshot rows with the three
    derivable fields mapped + the REAL ``recovered_paise`` (SUM of the
    campaign's attributions) and section completeness True (VT-563)."""
    from orchestrator.context_builder import _build_recent_campaigns

    tenant_id = _new_tenant(substrate.dsn)
    run_id = _new_pipeline_run(substrate.dsn, tenant_id)
    now = datetime.now(UTC)
    campaign_id = _seed_campaign(
        substrate.dsn, tenant_id, run_id, generated_at=now, status="proposed"
    )
    _seed_attribution(substrate.dsn, tenant_id, campaign_id, 300)
    _seed_attribution(substrate.dsn, tenant_id, campaign_id, 450)

    snapshots, complete = _build_recent_campaigns(tenant_id)

    assert complete is True, "completeness True once the attribution read succeeds"
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.campaign_id == campaign_id
    assert snap.status == "proposed"
    assert snap.recovered_paise == 750  # 300 + 450, real SUM
    # generated_at round-trips through psycopg as a tz-aware datetime; equality
    # comparison tolerates microsecond precision per Postgres TIMESTAMPTZ.
    assert snap.proposed_at == now


def test_recovered_paise_is_real_zero_without_attributions(substrate):  # type: ignore[no-untyped-def]
    """A campaign with no attribution rows reports ``recovered_paise=0`` — real
    zero (complete knowledge), with completeness True."""
    from orchestrator.context_builder import _build_recent_campaigns

    tenant_id = _new_tenant(substrate.dsn)
    run_id = _new_pipeline_run(substrate.dsn, tenant_id)
    now = datetime.now(UTC)
    _seed_campaign(substrate.dsn, tenant_id, run_id, generated_at=now, status="proposed")

    snapshots, complete = _build_recent_campaigns(tenant_id)

    assert complete is True
    assert len(snapshots) == 1
    assert snapshots[0].recovered_paise == 0


def test_returns_safe_empty_when_no_rows_for_tenant(substrate):  # type: ignore[no-untyped-def]
    """A tenant with no campaigns rows returns ``([], True)`` — no error. The
    read succeeded (nothing to recover), so completeness is True (VT-563)."""
    from orchestrator.context_builder import _build_recent_campaigns

    tenant_id = _new_tenant(substrate.dsn)

    snapshots, complete = _build_recent_campaigns(tenant_id)

    assert snapshots == []
    assert complete is True


def test_tenant_isolation_no_cross_tenant_leak(substrate):  # type: ignore[no-untyped-def]
    """Tenant B's campaigns rows must not appear in tenant A's read.
    Mirrors the CL-71 cross-tenant proof — RLS does the filtering, the
    builder relies on it."""
    from orchestrator.context_builder import _build_recent_campaigns

    tenant_a = _new_tenant(substrate.dsn)
    tenant_b = _new_tenant(substrate.dsn)
    run_a = _new_pipeline_run(substrate.dsn, tenant_a)
    run_b = _new_pipeline_run(substrate.dsn, tenant_b)
    now = datetime.now(UTC)
    a_campaign = _seed_campaign(
        substrate.dsn, tenant_a, run_a, generated_at=now, status="proposed"
    )
    b_campaign = _seed_campaign(
        substrate.dsn, tenant_b, run_b, generated_at=now, status="proposed"
    )

    a_snaps, _ = _build_recent_campaigns(tenant_a)
    b_snaps, _ = _build_recent_campaigns(tenant_b)

    a_ids = {snap.campaign_id for snap in a_snaps}
    b_ids = {snap.campaign_id for snap in b_snaps}
    assert a_campaign in a_ids
    assert b_campaign in b_ids
    assert b_campaign not in a_ids, "RLS leak: tenant A saw tenant B's campaign"
    assert a_campaign not in b_ids, "RLS leak: tenant B saw tenant A's campaign"


def test_orders_most_recent_first_and_limits_to_five(substrate):  # type: ignore[no-untyped-def]
    """Seed 6 rows with distinct generated_at; exactly 5 returned,
    newest first."""
    from orchestrator.context_builder import _build_recent_campaigns

    tenant_id = _new_tenant(substrate.dsn)
    run_id = _new_pipeline_run(substrate.dsn, tenant_id)
    base = datetime.now(UTC)
    # 6 rows: oldest (offset 5h) to newest (offset 0h). Seed in
    # non-monotonic order so the test does not rely on insertion order.
    offsets_h = [3, 0, 5, 1, 4, 2]
    seeded: list[tuple[UUID, datetime]] = []
    for h in offsets_h:
        ts = base - timedelta(hours=h)
        cid = _seed_campaign(
            substrate.dsn, tenant_id, run_id, generated_at=ts, status="proposed"
        )
        seeded.append((cid, ts))

    snapshots, complete = _build_recent_campaigns(tenant_id)

    assert complete is True
    assert len(snapshots) == 5, "LIMIT 5 not enforced"
    # Newest-first: the offset=5h row (oldest) must be the one dropped.
    returned_ts = [snap.proposed_at for snap in snapshots]
    assert returned_ts == sorted(returned_ts, reverse=True), (
        "rows not ordered most-recent-first"
    )
    expected_top_five = sorted(
        (ts for _, ts in seeded), reverse=True
    )[:5]
    assert returned_ts == expected_top_five
