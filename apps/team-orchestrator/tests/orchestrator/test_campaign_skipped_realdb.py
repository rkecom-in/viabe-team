"""VT-261 — real-DB regression: opt-out skips record send_status='skipped'.

VT-251's opt-out path wrote send_status='error' because the send_idempotency_keys
CHECK had no 'skipped' value — polluting error telemetry and conflating a
deliberate consent skip (opted_out / blocked, CL-421) with a real send failure.
Migration 053 adds 'skipped'; `_write_opt_out_skip_ledger` now writes it.

This is a REAL-DB test (the mock unit tests in test_campaign_execute.py cannot
catch a CHECK-constraint violation — they never touch a server). Pre-053 the
INSERT below would raise a CHECK violation; post-053 it lands as 'skipped'.

Gated on DATABASE_URL + psycopg, mirroring test_migrations.py. Runs in the CI
orchestrator job. CL-422 synthetic data only; CL-390 no PII (customer_id is a
UUID; no phone).
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402 — after the psycopg gate

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-261 real-DB test skipped",
)


@pytest.fixture(scope="module")
def dsn():
    import apply_migrations  # lazy: keep module import-light for --no-project

    d = os.environ["DATABASE_URL"]
    apply_migrations.apply(dsn=d)
    return d


def _seed_tenant(conn) -> str:
    # conn uses dict_row → RETURNING id comes back keyed by name.
    return str(
        conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT261 Co', 'standard', 'onboarding') RETURNING id"
        ).fetchone()["id"]
    )


def test_opt_out_skip_records_skipped_not_error(dsn):
    from orchestrator.campaign.execute import _write_opt_out_skip_ledger

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        tenant = _seed_tenant(conn)
        # Scope the connection to the tenant (mirrors execute_approved_campaign's
        # RLS-scoped conn). The send_status CHECK is enforced regardless of role.
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant,))
        customer_id = str(uuid4())
        idem_key = "vt261-skip-1"

        # Pre-053 this raised a CHECK violation ('skipped' not allowed). Post-053
        # it succeeds and lands as 'skipped'.
        _write_opt_out_skip_ledger(conn, tenant, customer_id, idem_key)

        row = conn.execute(
            "SELECT send_status, customer_id FROM send_idempotency_keys "
            "WHERE tenant_id = %s AND idempotency_key = %s",
            (tenant, idem_key),
        ).fetchone()

    assert row is not None
    assert row["send_status"] == "skipped"  # not 'error'
    assert str(row["customer_id"]) == customer_id


def test_skipped_is_a_valid_campaign_messages_status(dsn):
    # Migration 053 also added 'skipped' to campaign_messages' CHECK (consistency).
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        tenant = _seed_tenant(conn)
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant,))
        # Insert succeeds only if 'skipped' is in the CHECK.
        conn.execute(
            "INSERT INTO campaign_messages "
            "(tenant_id, customer_id, send_status, message_type) "
            "VALUES (%s, %s, 'skipped', 'template')",
            (tenant, str(uuid4())),
        )
        n = conn.execute(
            "SELECT count(*) AS n FROM campaign_messages "
            "WHERE tenant_id = %s AND send_status = 'skipped'",
            (tenant,),
        ).fetchone()["n"]
    assert n == 1
