"""VT-321 (#20) — real-DB canary: an OPEN complaint freezes ALL selling.

Fazal's NON-configurable rule #20: "an open complaint freezes ALL selling to
that customer — no exceptions." Migration 091 adds customers.complaint_status;
execute_approved_campaign skips any recipient with complaint_status='open'
BEFORE the VT-45 send, fail-closed, counting it as 'skipped_complaint_freeze'.

This is a REAL-DB test (the mock unit tests in test_campaign_execute.py never
touch the customers.complaint_status column or the CHECK constraint). It seeds a
tenant + campaign + two recipients — one with complaint_status='open', one with
'none' — runs the real execute seam against the real schema with an INJECTED
mock send_fn (TEAM_TWILIO_MOCK_MODE=1; no Twilio call), and asserts:

  - the 'open'-complaint customer is NOT sent to (send_fn never called for it),
  - a distinct 'skipped_complaint_freeze' marker is written under the
    skip:complaint_freeze: namespace,
  - the summary counts it as skipped_complaint_freeze (separate from opt_out),
  - the 'none' customer IS sent (positive control — proves the gate is targeted,
    not a blanket freeze).

Gated on DATABASE_URL + psycopg, mirroring test_campaign_skipped_realdb.py. Runs
in the CI orchestrator job. CL-422 synthetic data only; CL-390 no PII
(customer_id is a UUID; no phone in this test's assertions or seeds).
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402 — after the psycopg gate

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-321 complaint-freeze real-DB test skipped",
)

_TEMPLATE_ID = "team_weekly_approval"


@pytest.fixture(scope="module")
def dsn():
    import apply_migrations  # lazy: keep module import-light for --no-project

    d = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=d)
    assert not r["failed"], r["failed"]
    return d


def _seed_tenant(conn) -> str:
    # VT-460: execute_approved_campaign now runs the shared onboarded + WABA-live pre-gate before the
    # send loop. This test asserts the (downstream) complaint-freeze gate, so the tenant must clear
    # the pre-gate: fully onboarded (journey-complete + gstin_verified + ≥1 enabled connector) + a
    # 'live' WABA. (The ≥1-customer activation leg is satisfied by the per-test recipient seeds.)
    from uuid import uuid4

    tenant = str(
        conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, verification_status) "
            "VALUES ('VT321 Co', 'standard', 'paid_active', 'gstin_verified') RETURNING id"
        ).fetchone()["id"]
    )
    conn.execute(
        "INSERT INTO tenant_connector_status (tenant_id, connector_id, enabled, last_status, "
        "last_ingested_date) VALUES (%s, %s, TRUE, 'ok', CURRENT_DATE)",
        (tenant, f"conn-{uuid4().hex[:8]}"),
    )
    conn.execute(
        "INSERT INTO onboarding_journey (tenant_id, status, completed_at) "
        "VALUES (%s, 'complete', now())",
        (tenant,),
    )
    conn.execute(
        "INSERT INTO tenant_whatsapp_accounts (tenant_id, status, phone_number) "
        "VALUES (%s, 'live', %s)",
        (tenant, f"+9180{uuid4().int % 10**8:08d}"),
    )
    return tenant


def _seed_run(conn, tenant_id: str) -> str:
    return str(
        conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, status) "
            "VALUES (%s, 'running') RETURNING id",
            (tenant_id,),
        ).fetchone()["id"]
    )


def _seed_campaign(conn, tenant_id: str, run_id: str) -> str:
    # Post-mig-018: template lives in plan_json -> 'message_plan' ->> 'template_id'.
    plan = json.dumps(
        {"message_plan": {"template_id": _TEMPLATE_ID, "language": "en"}}
    )
    return str(
        conn.execute(
            """
            INSERT INTO campaigns (tenant_id, run_id, status, generated_at, plan_json)
            VALUES (%s, %s, 'proposed', now(), %s::jsonb)
            RETURNING id
            """,
            (tenant_id, run_id, plan),
        ).fetchone()["id"]
    )


def _seed_customer(conn, tenant_id: str, name: str, complaint_status: str) -> str:
    return str(
        conn.execute(
            "INSERT INTO customers (tenant_id, display_name, complaint_status) "
            "VALUES (%s, %s, %s) RETURNING id",
            (tenant_id, name, complaint_status),
        ).fetchone()["id"]
    )


def _add_recipient(conn, tenant_id: str, campaign_id: str, customer_id: str) -> None:
    conn.execute(
        "INSERT INTO campaign_recipients (campaign_id, customer_id, tenant_id) "
        "VALUES (%s, %s, %s)",
        (campaign_id, customer_id, tenant_id),
    )


def _ok_send_result() -> Any:
    r = MagicMock()
    r.status = "sent"
    r.message_sid = "SM" + "0" * 32
    r.error_envelope = None
    return r


def test_open_complaint_freezes_send_none_customer_sent(dsn, monkeypatch):
    """The #20 canary: open-complaint customer excluded, none customer sent."""
    monkeypatch.setenv("TEAM_TWILIO_MOCK_MODE", "1")
    from orchestrator.campaign.execute import execute_approved_campaign

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        tenant = _seed_tenant(conn)
        # RLS-scope the connection (mirrors the seam's tenant-scoped conn).
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant,))
        conn.execute("SET ROLE app_role")  # VT-306: mirror tenant_connection (SET ROLE + GUC)

        run = _seed_run(conn, tenant)
        campaign = _seed_campaign(conn, tenant, run)

        frozen = _seed_customer(conn, tenant, "Frozen", complaint_status="open")
        sellable = _seed_customer(conn, tenant, "Sellable", complaint_status="none")
        _add_recipient(conn, tenant, campaign, frozen)
        _add_recipient(conn, tenant, campaign, sellable)

        # Capture which customers VT-45 was actually invoked for. The freeze must
        # short-circuit BEFORE this is called for the frozen customer.
        sent_to: list[str] = []

        def _send_fn(payload: Any, **kwargs: Any) -> Any:
            sent_to.append(payload.customer_id)
            return _ok_send_result()

        summary = execute_approved_campaign(
            tenant, campaign, conn=conn, send_template_fn=_send_fn
        )

        # --- The non-configurable freeze (#20): open complaint → NOT sent ---
        assert frozen not in sent_to, (
            "VT-321 #20 VIOLATION: an open-complaint customer was sent a campaign "
            "message — selling must be frozen with no exceptions"
        )
        assert summary["skipped_complaint_freeze"] == 1
        assert summary["sent"] == 1
        assert summary["skipped_opt_out"] == 0
        assert summary["failed"] == 0

        # --- Positive control: the 'none' customer IS sent (targeted freeze) ---
        assert sent_to == [sellable], (
            f"only the sellable customer should be sent to; got {sent_to}"
        )

        # --- Distinct skip marker written under the complaint_freeze namespace ---
        idem = f"{campaign}:{frozen}"
        freeze_row = conn.execute(
            "SELECT send_status, customer_id FROM send_idempotency_keys "
            "WHERE tenant_id = %s AND idempotency_key = %s",
            (tenant, f"skip:complaint_freeze:{idem}"),
        ).fetchone()
        assert freeze_row is not None, "complaint-freeze skip marker not written"
        assert freeze_row["send_status"] == "skipped"
        assert str(freeze_row["customer_id"]) == frozen

        # No opt-out skip marker for the frozen customer (it was a freeze, not an
        # opt-out — the reasons live in distinct namespaces).
        optout_row = conn.execute(
            "SELECT count(*) AS n FROM send_idempotency_keys "
            "WHERE tenant_id = %s AND idempotency_key = %s",
            (tenant, f"skip:opt_out:{idem}"),
        ).fetchone()["n"]
        assert optout_row == 0


def test_resolved_and_default_complaint_status_are_sellable(dsn, monkeypatch):
    """Fail-closed boundary: ONLY 'open' freezes. 'resolved'/'none' → sellable."""
    monkeypatch.setenv("TEAM_TWILIO_MOCK_MODE", "1")
    from orchestrator.campaign.execute import execute_approved_campaign

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        tenant = _seed_tenant(conn)
        conn.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant,))
        conn.execute("SET ROLE app_role")  # VT-306: mirror tenant_connection (SET ROLE + GUC)
        run = _seed_run(conn, tenant)
        campaign = _seed_campaign(conn, tenant, run)

        resolved = _seed_customer(conn, tenant, "Resolved", complaint_status="resolved")
        none_c = _seed_customer(conn, tenant, "None", complaint_status="none")
        _add_recipient(conn, tenant, campaign, resolved)
        _add_recipient(conn, tenant, campaign, none_c)

        sent_to: list[str] = []

        def _send_fn(payload: Any, **kwargs: Any) -> Any:
            sent_to.append(payload.customer_id)
            return _ok_send_result()

        summary = execute_approved_campaign(
            tenant, campaign, conn=conn, send_template_fn=_send_fn
        )

        assert summary["skipped_complaint_freeze"] == 0
        assert summary["sent"] == 2
        assert set(sent_to) == {resolved, none_c}
