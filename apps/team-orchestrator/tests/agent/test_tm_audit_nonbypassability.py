"""VT-514 — Team-Manager audit NO-ORPHAN-ACTION invariant proof (set E).

Mirrors tests/agent/test_rail_harness_nonbypassability.py Layer B (the VT-460
rails non-bypassability proof): a real DB-backed harness — importorskip
psycopg+dbos, skipif no DATABASE_URL, migrations applied + DBOS launched
module-scoped, rows seeded via a direct service-role connection, the REAL
production choke (`_persist_draft_batch`) exercised through `tenant_connection`
(the real RLS path).

Three proof shapes for the FAIL-CLOSED action layer:

- **E-per-action (completeness-by-construction).** Driving the real
  `_persist_draft_batch` choke once writes EXACTLY ONE ``tm_audit_log`` row
  (``draft_created``) scoped to (tenant_id) — the audit is emitted at the choke,
  not bolted on.
- **E-fail-closed (non-bypassability — the load-bearing proof).** When the audit
  INSERT fails INSIDE the action's transaction, the WHOLE ACTION rolls back:
  zero ``agent_draft_batches`` / zero ``agent_drafts`` AND zero ``tm_audit_log``
  rows commit. An action cannot commit without its audit row — the
  can't-audit ⇒ can't-act analog of the VT-460 transport-fails-closed proof.
- **E-sweep (no orphan at rest).** A LEFT JOIN of the committed action rows
  against ``tm_audit_log`` returns no orphans.

Plus a structural check that every fail-closed choke is actually wired
(``emit_tm_audit(... conn=conn ...)`` present in its source) so an accidental
removal/decoupling fails the suite.

HONEST SCOPE: the HARD invariant covers the DB-transactional ACTION layer only.
SPAWN / ROUTE / reasoning turns are complete-by-construction at a single choke
but emit best-effort (conn=None, fail-soft) — there is no business transaction
to bind to; losing one degrades replay, it is not an un-audited side-effect.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after the dependency skip guards

from orchestrator.db import tenant_connection  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-514 audit no-orphan proof tests skipped",
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "orchestrator"


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so the tenant_connection pool exists
    (mirrors test_rail_harness_nonbypassability / test_customer_send)."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt514-auditproof-salt")
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

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
            "business_type, verification_status, whatsapp_number) "
            "VALUES (%s, 'founding', 'paid_active', now(), 'restaurant', 'gstin_verified', %s) "
            "RETURNING id",
            ("VT-514 auditproof", f"+9198{uuid4().int % 10**8:08d}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_customer(dsn: str, tenant: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO customers (tenant_id, display_name, phone_e164, opt_out_status, "
            "complaint_status) VALUES (%s, 'Ravi', %s, 'subscribed', 'none') RETURNING id",
            (str(tenant), f"+9197{uuid4().int % 10**8:08d}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_work_item(dsn: str, tenant: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
            "VALUES (%s, %s, 'sales_recovery', 'approved') RETURNING id",
            (str(tenant), f"item-{uuid4().hex[:12]}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _count(dsn: str, sql: str, params: tuple[Any, ...]) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def _drive_draft_batch(tenant: UUID, work_item: UUID, customer: UUID) -> UUID:
    """Exercise the REAL _persist_draft_batch choke through tenant_connection."""
    from orchestrator.agents import sales_recovery_executor as sre

    drafts = [(customer, {"customer_name": "Ravi", "business_name": "Test Cafe"})]
    with tenant_connection(tenant) as conn:
        return sre._persist_draft_batch(
            tenant, work_item_id=work_item, drafts=drafts, conn=conn
        )


# --- E-per-action: the real choke writes exactly one audit row -----------------


@requires_db
def test_draft_choke_emits_one_audit_row(substrate):  # type: ignore[no-untyped-def]
    t = _new_tenant(substrate.dsn)
    wi = _seed_work_item(substrate.dsn, t)
    cust = _seed_customer(substrate.dsn, t)

    batch_id = _drive_draft_batch(t, wi, cust)

    assert _count(
        substrate.dsn, "SELECT count(*) FROM agent_draft_batches WHERE id = %s", (str(batch_id),)
    ) == 1, "the action's batch row must have committed"
    assert _count(
        substrate.dsn,
        "SELECT count(*) FROM tm_audit_log WHERE tenant_id = %s AND event_kind = 'draft_created'",
        (str(t),),
    ) == 1, "exactly one draft_created audit row must exist for the driven choke"


# --- E-fail-closed: an audit-insert failure rolls back the whole action --------


@requires_db
def test_audit_failure_rolls_back_the_action(
    substrate, monkeypatch: pytest.MonkeyPatch
):  # type: ignore[no-untyped-def]
    """THE load-bearing proof: force the in-txn audit INSERT to fail and assert
    the action commits NOTHING — no batch, no drafts, no audit row. An action
    cannot commit without its audit row (can't-audit ⇒ can't-act)."""
    import orchestrator.observability.tm_audit as tm_audit

    t = _new_tenant(substrate.dsn)
    wi = _seed_work_item(substrate.dsn, t)
    cust = _seed_customer(substrate.dsn, t)

    def _poisoned_execute(conn: Any, params: tuple[Any, ...]) -> None:
        raise RuntimeError("VT-514 forced audit-insert failure")

    monkeypatch.setattr(tm_audit, "_execute", _poisoned_execute)

    with pytest.raises(RuntimeError):
        _drive_draft_batch(t, wi, cust)

    assert _count(
        substrate.dsn, "SELECT count(*) FROM agent_draft_batches WHERE tenant_id = %s", (str(t),)
    ) == 0, "the batch INSERT must have rolled back when the audit insert failed"
    assert _count(
        substrate.dsn, "SELECT count(*) FROM agent_drafts WHERE tenant_id = %s", (str(t),)
    ) == 0, "the draft INSERTs must have rolled back when the audit insert failed"
    assert _count(
        substrate.dsn, "SELECT count(*) FROM tm_audit_log WHERE tenant_id = %s", (str(t),)
    ) == 0, "no audit row may persist when the insert itself failed"


# --- E-sweep: no committed action without a corresponding audit row ------------


@requires_db
def test_no_orphan_action_sweep(substrate):  # type: ignore[no-untyped-def]
    t = _new_tenant(substrate.dsn)
    wi = _seed_work_item(substrate.dsn, t)
    cust = _seed_customer(substrate.dsn, t)
    _drive_draft_batch(t, wi, cust)

    orphans = _count(
        substrate.dsn,
        """
        SELECT count(*)
        FROM agent_draft_batches b
        LEFT JOIN tm_audit_log a
          ON a.tenant_id = b.tenant_id AND a.event_kind = 'draft_created'
        WHERE b.tenant_id = %s AND a.id IS NULL
        """,
        (str(t),),
    )
    assert orphans == 0, "every committed draft batch must have a draft_created audit row"


# --- structural: every fail-closed choke is actually wired ---------------------


@pytest.mark.parametrize(
    ("rel_path", "func_marker"),
    [
        ("agents/sales_recovery_executor.py", "def _persist_draft_batch"),
        ("agent/tools/request_owner_approval.py", "def arm_pause_request"),
        ("agent/approval_resume.py", "def mark_approval_resolved"),
        ("escalations.py", "def record_escalation"),
        ("agents/autonomy.py", "def grant_l3"),
        ("agents/autonomy.py", "def revoke_l3"),
        ("agents/autonomy.py", "def set_frozen"),
        ("agents/autonomy.py", "def record_regression_event"),
    ],
)
def test_fail_closed_chokes_are_wired(rel_path: str, func_marker: str) -> None:
    """Source-level guard: each fail-closed choke must carry a fail-closed
    emit_tm_audit (conn=conn). Catches an accidental removal/decoupling that the
    DB-backed tests would miss when DATABASE_URL is unset."""
    src = (_SRC / rel_path).read_text()
    assert "emit_tm_audit(" in src, f"{rel_path} lost its emit_tm_audit call"
    assert "conn=conn" in src, f"{rel_path} must emit fail-closed (conn=conn)"
    # the named choke function still exists (anchor sanity)
    assert re.search(re.escape(func_marker), src), f"{rel_path} missing {func_marker}"
