"""VT-280 — VTR digest reads ONLY the de-identified view as app_vtr_role. Real-PG canary.

Synthetic only (CL-422). Proves: the digest counts ONLY route='vtr' open escalations (owner-routed
+ resolved excluded), includes the decay trend, and — the guarantee probe (Cowork) — the digest's
role CANNOT read the raw escalations table: the de-identified view is the only door.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("dbos")
import psycopg  # noqa: E402
from psycopg import errors as pg_errors  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-280 digest tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield dsn
    finally:
        shutdown_dbos()


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-280 test', 'founding', 'paid_active') RETURNING id"
        ).fetchone()[0])


def _esc(dsn, tenant, *, kind, route, status="open", days_ago=1) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO escalations (tenant_id, kind, severity, status, route, opened_at) "
            "VALUES (%s, %s, 'medium', %s, %s, now() - make_interval(days => %s))",
            (tenant, kind, status, route, days_ago),
        )


def test_digest_counts_only_vtr_routed(substrate):
    """VT-280: the digest counts route='vtr' OPEN escalations only — owner-routed + resolved excluded."""
    from orchestrator.owner_surface.vtr_digest import run_vtr_digest_body

    t = _tenant(substrate)
    _esc(substrate, t, kind="how_to_gap", route="vtr")
    _esc(substrate, t, kind="how_to_gap", route="vtr")
    _esc(substrate, t, kind="policy_gap", route="vtr")
    _esc(substrate, t, kind="pricing", route="owner")          # owner-routed → excluded
    _esc(substrate, t, kind="how_to_gap", route="vtr", status="resolved")  # resolved → excluded

    text = run_vtr_digest_body(send=False)
    assert "how_to_gap:2" in text and "policy_gap:1" in text  # only the 3 open vtr items
    assert "pricing" not in text  # owner-routed never surfaces
    assert "trend:" in text or "prior=" in text  # decay trend present


def test_digest_role_cannot_read_raw_escalations(substrate):
    """The GUARANTEE (Cowork): the digest's role (app_vtr_role, via vtr_connection) is DENIED on the
    raw escalations table — the de-identified view is the only door, so PII is unreachable."""
    from orchestrator.privacy.vtr import vtr_connection

    with vtr_connection() as conn, conn.cursor() as cur:
        with pytest.raises(pg_errors.InsufficientPrivilege):
            cur.execute("SELECT notes FROM escalations LIMIT 1")  # raw table → denied
        cur.execute("ROLLBACK")
        # …but the de-identified view IS readable (the digest's actual path).
        cur.execute("SELECT count(*) FROM vtr_escalations")
        assert cur.fetchone() is not None
