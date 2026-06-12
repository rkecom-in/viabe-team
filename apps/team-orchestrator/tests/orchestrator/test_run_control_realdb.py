"""VT-374 — run-control substrate (real Postgres). Plan §10 DB-touching acceptance.

REPLACES the VT-300 suite: ``run_controls`` is retired (mig 131, N1 RETIRE arm) and the
substrate is now ``workflow_controls`` + ``step_overrides`` + the mig-131/132 VTR views.
Covers: RLS+FORCE deny-all on both new tables (§10.1); ``app_vtr_role`` zero raw reads +
keys-only timeline with EXPLICIT per-direction key projections for the 4 audited
name-free kinds (§10.7; mig-132 / VT-376 C2 — whole-envelope passthrough is gone) + the
injected-extra-key probe; DSR purge hard-delete canary (§10.1); consume-first race + N2
recovery-idempotent re-consume + expiry sweep + the next-run-requires-expiry DB CHECK
(§10.4); allowed-keys merge enforcement (F6/I7); the F9/N4 two-tier pause posture
(§10.5); ``rerun_from`` refusals — forbidden kind + open-approval 409 (§10.6/F10/F11);
and the VT-376 rerun-slot lock — double-click serialization (BINDING acceptance) + the
direct held-window lock probe.

Gated on DATABASE_URL + dbos (CL-422 — synthetic data only). Unique tenants per test
(uuid-suffixed names, no fixed phones) so a recycled DB never collides.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

pytest.importorskip("dbos")
pytest.importorskip("fastapi")

import psycopg  # noqa: E402
from psycopg import errors as pg_errors  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-374 run-control substrate tests skipped",
)

# The audited VALUE-BEARING kind list (STEP-0 §3.2 name-free kinds) — pinned here so a
# view edit that widens the list fails this suite, not just review. Post-mig-132 (VT-376
# C2) NONE of these passes a whole envelope: each projects an EXPLICIT per-direction key
# allowlist pinned from its actual writer; the list is a CEILING that never widens
# without a fresh PII audit.
_AUDITED_PASSTHROUGH_KINDS = (
    "webhook_received",
    "agent_invocation",
    "aborted_hard_limit",
    "tenant_isolation_breach",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so get_pool() exists (purge + rerun read it)."""
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


@pytest.fixture(autouse=True)
def _purge_synthetic_breach_rows(substrate):  # type: ignore[no-untyped-def]
    """The C2 projection tests seed synthetic ``tenant_isolation_breach`` step rows, but
    the VT-79 Detector-1 suite (privacy/test_k_anonymity.py) asserts a GLOBAL zero count
    of that kind — purge on teardown so the detector invariant holds suite-wide."""
    yield
    with psycopg.connect(substrate, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM pipeline_steps WHERE step_kind = 'tenant_isolation_breach'"
        )


# --- pool shims (run_control's injectable pool seam) ---------------------------------


class _DsnPool:
    """Minimal ``.connection()`` shim over a plain superuser connect — exercises the
    run_control pool seam without depending on the launched DBOS pool's state."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @contextmanager
    def connection(self):  # type: ignore[no-untyped-def]
        with psycopg.connect(self._dsn, autocommit=True) as conn:
            yield conn


class _RaisingPool:
    """Synthetic control-store outage: every checkout raises (the F9 failure leg)."""

    def connection(self):  # type: ignore[no-untyped-def]
        raise RuntimeError("control-store unreachable (synthetic outage)")


# --- seed helpers (superuser — RLS bypassed at seed time, like every realdb suite) ----


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(
            conn.execute(
                "INSERT INTO tenants (business_name, plan_tier, phase) "
                "VALUES (%s, 'founding', 'paid_active') RETURNING id",
                (f"VT374 {uuid4().hex[:8]}",),
            ).fetchone()[0]
        )


def _run(dsn: str, tenant: str, *, run_type: str = "orchestrator") -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(
            conn.execute(
                "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
                "VALUES (%s, %s, 'completed') RETURNING id",
                (tenant, run_type),
            ).fetchone()[0]
        )


def _step(
    dsn: str,
    run: str,
    tenant: str,
    seq: int,
    kind: str,
    *,
    input_env: dict[str, Any] | None = None,
    output_env: dict[str, Any] | None = None,
) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(
            conn.execute(
                "INSERT INTO pipeline_steps (run_id, tenant_id, step_seq, step_kind, "
                "step_name, status, input_envelope, output_envelope) "
                "VALUES (%s, %s, %s, %s, %s, 'completed', %s, %s) RETURNING id",
                (
                    run,
                    tenant,
                    seq,
                    kind,
                    kind,
                    Jsonb(input_env) if input_env is not None else None,
                    Jsonb(output_env) if output_env is not None else None,
                ),
            ).fetchone()[0]
        )


def _pause(dsn: str, tenant: str, kind: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(
            conn.execute(
                "INSERT INTO workflow_controls (tenant_id, workflow_kind, set_by, reason) "
                "VALUES (%s, %s, %s, 'test hold') RETURNING id",
                (tenant, kind, str(uuid4())),
            ).fetchone()[0]
        )


def _release_all(dsn: str, tenant: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE workflow_controls SET released_at = now(), released_by = %s "
            "WHERE tenant_id = %s AND released_at IS NULL",
            (str(uuid4()), tenant),
        )


def _override(
    dsn: str,
    tenant: str,
    kind: str,
    step: str,
    *,
    workflow_id: str | None = None,
    expires_at: datetime | None = None,
    pinned_input: dict[str, Any] | None = None,
    consumed_at: datetime | None = None,
    consumed_run_id: str | None = None,
) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(
            conn.execute(
                "INSERT INTO step_overrides (tenant_id, workflow_kind, step_name, "
                "workflow_id, pinned_input, reason, created_by, expires_at, "
                "consumed_at, consumed_run_id) "
                "VALUES (%s, %s, %s, %s, %s, 'test pin', %s, %s, %s, %s) RETURNING id",
                (
                    tenant,
                    kind,
                    step,
                    workflow_id,
                    Jsonb(pinned_input) if pinned_input is not None else None,
                    str(uuid4()),
                    expires_at,
                    consumed_at,
                    consumed_run_id,
                ),
            ).fetchone()[0]
        )


def _future() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


# ---------------------------------------------------------------------------
# §10.1 — RLS + FORCE deny-all on both new tables
# ---------------------------------------------------------------------------


def test_new_tables_rls_forced_with_zero_policies(substrate):
    """Both control tables: RLS enabled + FORCED and ZERO policies — the mig-078
    deny-all construction (service pool only); a policy appearing here would silently
    open a tenant-role path to redacted-at-write pins/reasons."""
    with psycopg.connect(substrate, autocommit=True) as conn:
        for tbl in ("workflow_controls", "step_overrides"):
            row = conn.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = %s",
                (tbl,),
            ).fetchone()
            assert row is not None, f"{tbl} missing"
            assert row[0], f"{tbl}: RLS not enabled"
            assert row[1], f"{tbl}: RLS not forced"
            policies = conn.execute(
                "SELECT policyname FROM pg_policies "
                "WHERE schemaname = 'public' AND tablename = %s",
                (tbl,),
            ).fetchall()
            assert policies == [], f"{tbl}: deny-all violated by policies {policies}"


def test_app_role_deny_all_even_with_matching_tenant_guc(substrate):
    """The deny-all is real for app_role: with the CORRECT tenant GUC set, SELECT sees
    nothing (zero-policy RLS) and INSERT refuses. Control rows are reachable only
    through the privileged service pool."""
    tenant = _tenant(substrate)
    _pause(substrate, tenant, "agent_dispatch")
    _override(substrate, tenant, "agent_dispatch", "candidate_build", expires_at=_future())
    with psycopg.connect(substrate, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE app_role")
        cur.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant,))
        for tbl in ("workflow_controls", "step_overrides"):
            try:
                cur.execute(
                    f"SELECT count(*) FROM {tbl} WHERE tenant_id = %s",  # noqa: S608
                    (tenant,),
                )
                assert cur.fetchone()[0] == 0, f"{tbl}: app_role read a deny-all row"
            except pg_errors.InsufficientPrivilege:
                cur.execute("ROLLBACK")  # no grant at all — an even stricter deny
        with pytest.raises(pg_errors.InsufficientPrivilege):
            cur.execute(
                "INSERT INTO workflow_controls (tenant_id, workflow_kind, set_by) "
                "VALUES (%s, 'trial_sweep', %s)",
                (tenant, str(uuid4())),
            )
        cur.execute("ROLLBACK")
        cur.execute("RESET ROLE")


# ---------------------------------------------------------------------------
# §10.7 — app_vtr_role: zero raw reads; keys-only timeline; audited passthrough
# ---------------------------------------------------------------------------


def test_vtr_role_denied_on_raw_control_and_pipeline_tables(substrate):
    """app_vtr_role reads the world ONLY through the mig-131 views: no privilege and a
    denied direct probe on both control tables AND the raw pipeline tables."""
    forbidden = ("workflow_controls", "step_overrides", "pipeline_runs", "pipeline_steps")
    with psycopg.connect(substrate, autocommit=True) as conn:
        for tbl in forbidden:
            has = conn.execute(
                "SELECT has_table_privilege('app_vtr_role', %s, 'SELECT')", (tbl,)
            ).fetchone()[0]
            assert has is False, f"app_vtr_role unexpectedly has SELECT on {tbl}"
        with conn.cursor() as cur:
            cur.execute("SET ROLE app_vtr_role")
            for tbl in forbidden:
                with pytest.raises(pg_errors.InsufficientPrivilege):
                    cur.execute(f"SELECT 1 FROM {tbl} LIMIT 1")  # noqa: S608 — fixed allowlist
                cur.execute("ROLLBACK")
            cur.execute("RESET ROLE")


def test_timeline_keys_only_for_name_bearing_kind(substrate):
    """A name-bearing step_kind (compose_output, STEP-0 §3.2) projects KEY ARRAYS only:
    the keys show WHAT the step carried, the customer name never crosses the view."""
    tenant = _tenant(substrate)
    run = _run(substrate, tenant)
    _step(
        substrate,
        run,
        tenant,
        0,
        "compose_output",
        input_env={"body_preview": "Hi Ramesh, your order is ready", "customer_name": "Ramesh"},
        output_env={"draft_id": "d-1", "summary": "Message to Ramesh about the order"},
    )
    _step(substrate, run, tenant, 1, "compose_output")  # NULL envelopes stay NULL
    with psycopg.connect(substrate, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE app_vtr_role")
        cur.execute(
            "SELECT step_seq, input_envelope, output_envelope FROM vtr_step_timeline "
            "WHERE run_id = %s ORDER BY step_seq",
            (run,),
        )
        rows = cur.fetchall()
        cur.execute("RESET ROLE")
    assert len(rows) == 2
    _, input_env, output_env = rows[0]
    assert sorted(input_env) == ["body_preview", "customer_name"]  # keys, not values
    assert sorted(output_env) == ["draft_id", "summary"]
    assert "Ramesh" not in json.dumps(rows[0], default=str)  # the name never surfaces
    assert rows[1][1] is None and rows[1][2] is None


def test_timeline_value_projection_is_exactly_the_audited_allowlists(substrate):
    """Post-mig-132 (VT-376 C2): NO step_kind passes a whole envelope any more. Each
    value-bearing kind projects an EXPLICIT per-direction key allowlist pinned from its
    ACTUAL writer:

    - ``agent_invocation`` (agent/dispatch.py ``_write_dispatch_entry``): input
      {inbound_body_len, trigger, dispatched_at}; output {reason};
    - ``aborted_hard_limit`` (agent/dispatch.py ``_write_aborted_hard_limit``): input
      {reason, inbound_body_len}; output {axis, observed, limit};
    - ``tenant_isolation_breach`` (context_validator ``_record_breach`` — output only;
      BOTH call shapes): pre-flight {layer, offending_ids, counts} and post-flight
      {layer, expected_tenant, stray_tenants} project through the union allowlist;
    - ``webhook_received`` keeps the mig-131 4-key allowlist (unchanged);
    - everything else (state_transition control leg) stays keys-only.

    Absent allowlisted keys strip away (jsonb_strip_nulls) — present-if-present.
    """
    tenant = _tenant(substrate)
    run = _run(substrate, tenant)
    _step(
        substrate, run, tenant, 0, "agent_invocation",
        input_env={
            "inbound_body_len": 42,
            "trigger": "owner_substantive_message",
            "dispatched_at": "2026-06-12T00:00:00+00:00",
        },
        output_env={"reason": "substantive owner message — needs reasoning"},
    )
    _step(
        substrate, run, tenant, 1, "aborted_hard_limit",
        input_env={"reason": "hard_limit_exceeded:tokens", "inbound_body_len": 9},
        output_env={"axis": "tokens", "observed": 11.0, "limit": 10.0},
    )
    stray = str(uuid4())
    _step(
        substrate, run, tenant, 2, "tenant_isolation_breach",
        output_env={
            "layer": "pre_flight",
            "offending_ids": {"campaigns": ["c-1"]},
            "counts": {"campaigns": 1},
        },
    )
    _step(
        substrate, run, tenant, 3, "tenant_isolation_breach",
        output_env={"layer": "post_flight", "expected_tenant": tenant, "stray_tenants": [stray]},
    )
    # webhook_received carries BOTH allow-listed keys AND the unsafe identifiers — only
    # the 4-key allowlist must survive the projection.
    _step(
        substrate,
        run,
        tenant,
        50,
        "webhook_received",
        input_env={
            "message_type": "inbound_message",
            "num_media": 0,
            "dupe_status": False,
            "body_token": "tok-body",
            "sender_phone_token": "tok-phone",
            "twilio_message_sid": "SM-leak",
            "media_url_0": "https://leak/media",
        },
    )
    _step(substrate, run, tenant, 99, "state_transition", input_env={"state": "Ramesh dict"})
    with psycopg.connect(substrate, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE app_vtr_role")
        cur.execute(
            "SELECT step_seq, input_envelope, output_envelope FROM vtr_step_timeline "
            "WHERE run_id = %s ORDER BY step_seq",
            (run,),
        )
        rows = cur.fetchall()
        cur.execute("RESET ROLE")
    by_seq = {r[0]: (r[1], r[2]) for r in rows}
    assert by_seq[0] == (
        {
            "inbound_body_len": 42,
            "trigger": "owner_substantive_message",
            "dispatched_at": "2026-06-12T00:00:00+00:00",
        },
        {"reason": "substantive owner message — needs reasoning"},
    ), "agent_invocation: exactly the writer's keys, values intact"
    assert by_seq[1] == (
        {"reason": "hard_limit_exceeded:tokens", "inbound_body_len": 9},
        {"axis": "tokens", "observed": 11.0, "limit": 10.0},
    ), "aborted_hard_limit: exactly the writer's keys, values intact"
    assert by_seq[2] == (
        None,  # the breach writer never sets input_envelope
        {
            "layer": "pre_flight",
            "offending_ids": {"campaigns": ["c-1"]},
            "counts": {"campaigns": 1},
        },
    ), "tenant_isolation_breach pre-flight shape projects exactly"
    assert by_seq[3] == (
        None,
        {"layer": "post_flight", "expected_tenant": tenant, "stray_tenants": [stray]},
    ), "tenant_isolation_breach post-flight shape projects exactly (the verified-writer keys)"
    # webhook_received: exactly the mig-131 4-key allowlist (absent key stripped as NULL).
    assert by_seq[50][0] == {
        "message_type": "inbound_message",
        "num_media": 0,
        "dupe_status": False,
    }
    leaked = json.dumps(by_seq[50][0])
    for unsafe in ("tok-body", "tok-phone", "SM-leak", "leak/media"):
        assert unsafe not in leaked, f"webhook_received leaked {unsafe!r} past the C2 allowlist"
    assert by_seq[99][0] == ["state"]  # keys-only for everything else


def test_timeline_injected_extra_key_never_passes_the_projection(substrate):
    """THE mig-132 probe (plan ruling arm b: 'the assertion that matters'): a row carrying
    a FOREIGN key in its envelope — alongside or instead of the allowlisted keys — never
    surfaces that key (or its value) through the view, for ALL THREE newly-projected
    kinds, in BOTH directions. A row whose envelope is ONLY foreign keys projects to the
    empty object. This is what makes a writer-side leak structurally impossible rather
    than merely untested."""
    tenant = _tenant(substrate)
    run = _run(substrate, tenant)
    _step(
        substrate, run, tenant, 0, "agent_invocation",
        input_env={"inbound_body_len": 7, "smuggled_in": "Ramesh-input-leak"},
        output_env={"reason": "ok", "smuggled_out": "Ramesh-output-leak"},
    )
    _step(
        substrate, run, tenant, 1, "aborted_hard_limit",
        input_env={"reason": "hard_limit_exceeded:tools", "smuggled_in": "Ramesh-axis-leak"},
        output_env={"axis": "tools", "smuggled_out": "Ramesh-limit-leak"},
    )
    _step(
        substrate, run, tenant, 2, "tenant_isolation_breach",
        # the writer never sets input — an injected input of ONLY foreign keys → {}
        input_env={"smuggled_in": "Ramesh-breach-leak"},
        output_env={"layer": "pre_flight", "smuggled_out": "Ramesh-counts-leak"},
    )
    with psycopg.connect(substrate, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE app_vtr_role")
        cur.execute(
            "SELECT step_seq, input_envelope, output_envelope FROM vtr_step_timeline "
            "WHERE run_id = %s ORDER BY step_seq",
            (run,),
        )
        rows = cur.fetchall()
        cur.execute("RESET ROLE")
    by_seq = {r[0]: (r[1], r[2]) for r in rows}
    assert by_seq[0] == ({"inbound_body_len": 7}, {"reason": "ok"})
    assert by_seq[1] == ({"reason": "hard_limit_exceeded:tools"}, {"axis": "tools"})
    assert by_seq[2] == ({}, {"layer": "pre_flight"}), (
        "an injected envelope of ONLY foreign keys must project to the EMPTY object"
    )
    surface = json.dumps(rows, default=str)
    assert "smuggled" not in surface, "an injected KEY name crossed the projection"
    assert "Ramesh" not in surface and "leak" not in surface, (
        "an injected VALUE crossed the projection"
    )


def test_timeline_view_shape_and_companion_view(substrate):
    """Column pins: the timeline NEVER exposes the three unredacted-at-write columns
    (error / decision_rationale / tool_calls — CL-390), and vtr_workflow_controls is
    EXACTLY the 4 structural columns (no reason free-text, no operator ids)."""
    with psycopg.connect(substrate, autocommit=True) as conn:
        timeline_cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'vtr_step_timeline'"
            ).fetchall()
        }
        controls_cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'vtr_workflow_controls'"
            ).fetchall()
        }
    assert timeline_cols == {
        "tenant_id", "run_id", "run_type", "run_status", "run_started_at", "run_ended_at",
        "rerun_of_run_id", "rerun_from_step", "step_id", "step_seq", "step_kind",
        "step_name", "step_status", "started_at", "ended_at", "duration_ms",
        "override_id", "paused_ms", "input_envelope", "output_envelope",
    }, f"vtr_step_timeline columns drifted: {timeline_cols}"
    assert not ({"error", "decision_rationale", "tool_calls"} & timeline_cols)
    assert controls_cols == {"tenant_id", "workflow_kind", "set_at", "released_at"}, (
        f"vtr_workflow_controls columns drifted: {controls_cols}"
    )


def test_vtr_can_read_active_hold_via_companion_view(substrate):
    """The panel's never-shows-not-paused leg: an active hold is visible to
    app_vtr_role through vtr_workflow_controls."""
    tenant = _tenant(substrate)
    _pause(substrate, tenant, "campaign_send")
    with psycopg.connect(substrate, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE app_vtr_role")
        cur.execute(
            "SELECT workflow_kind, released_at FROM vtr_workflow_controls "
            "WHERE tenant_id = %s",
            (tenant,),
        )
        rows = cur.fetchall()
        cur.execute("RESET ROLE")
    assert rows == [("campaign_send", None)]


# ---------------------------------------------------------------------------
# §10.1 — DSR purge canary (hard-delete, cross-tenant isolation)
# ---------------------------------------------------------------------------


def test_purge_hard_deletes_control_tables_tenant_scoped(substrate):
    """Both control tables are in _PURGE_ORDER (I3): a DSR purge hard-deletes tenant
    A's pause + override rows while tenant B's survive untouched."""
    from orchestrator.dsr_purge import purge_tenant_data

    tenant_a, tenant_b = _tenant(substrate), _tenant(substrate)
    for tenant in (tenant_a, tenant_b):
        _pause(substrate, tenant, "agent_dispatch")
        _override(
            substrate, tenant, "agent_dispatch", "candidate_build", workflow_id=str(uuid4())
        )
    with psycopg.connect(substrate, autocommit=True) as conn:
        ticket = str(
            conn.execute(
                "INSERT INTO dsr_tickets (tenant_id, request_type, status, acknowledged_at) "
                "VALUES (%s, 'deletion', 'acknowledged', now()) RETURNING id",
                (tenant_a,),
            ).fetchone()[0]
        )

    result = purge_tenant_data(UUID(ticket))
    assert result.deleted_counts["workflow_controls"] == 1
    assert result.deleted_counts["step_overrides"] == 1
    with psycopg.connect(substrate, autocommit=True) as conn:
        for tbl in ("workflow_controls", "step_overrides"):
            remaining_a = conn.execute(
                f"SELECT count(*) FROM {tbl} WHERE tenant_id = %s", (tenant_a,)  # noqa: S608
            ).fetchone()[0]
            remaining_b = conn.execute(
                f"SELECT count(*) FROM {tbl} WHERE tenant_id = %s", (tenant_b,)  # noqa: S608
            ).fetchone()[0]
            assert remaining_a == 0, f"{tbl}: tenant A row survived the purge"
            assert remaining_b == 1, f"{tbl}: purge crossed tenants"


# ---------------------------------------------------------------------------
# §10.4 — overrides: DB CHECK, partial unique, consume race, N2, sweep
# ---------------------------------------------------------------------------


def test_next_run_override_requires_expires_at_db_check(substrate):
    """F8 at the DB layer: a workflow_id-NULL (next-run) pin without expires_at is a
    CHECK violation — unbounded pins are structurally inexpressible."""
    tenant = _tenant(substrate)
    with pytest.raises(pg_errors.CheckViolation):
        _override(substrate, tenant, "agent_dispatch", "candidate_build")
    # both legal shapes still insert
    _override(substrate, tenant, "agent_dispatch", "candidate_build", expires_at=_future())
    _override(substrate, tenant, "agent_dispatch", "candidate_build", workflow_id=str(uuid4()))


def test_pause_one_active_per_scope_partial_unique(substrate):
    """ONE active pause per (tenant, kind): a second active insert violates the partial
    unique; releasing the first re-opens the slot (history rows accumulate)."""
    tenant = _tenant(substrate)
    _pause(substrate, tenant, "agent_dispatch")
    with pytest.raises(pg_errors.UniqueViolation):
        _pause(substrate, tenant, "agent_dispatch")
    _pause(substrate, tenant, "plan_generate")  # different kind — independent scope
    _release_all(substrate, tenant)
    _pause(substrate, tenant, "agent_dispatch")  # released → re-pause is legal
    with psycopg.connect(substrate, autocommit=True) as conn:
        total = conn.execute(
            "SELECT count(*) FROM workflow_controls WHERE tenant_id = %s", (tenant,)
        ).fetchone()[0]
    assert total == 3  # history preserved, not upserted away


def test_consume_first_race_exactly_one_winner(substrate):
    """F8 consume-first: two concurrent runs race ONE override on separate connections;
    exactly one consumes (FOR UPDATE SKIP LOCKED + same-txn stamp), the loser proceeds
    clean with None."""
    from orchestrator.run_control import consume_override

    tenant = _tenant(substrate)
    override_id = _override(
        substrate, tenant, "agent_dispatch", "candidate_build",
        expires_at=_future(), pinned_input={"limit": 3},
    )
    run_ids = {"a": str(uuid4()), "b": str(uuid4())}
    barrier = threading.Barrier(2)
    results: dict[str, Any] = {}

    def _racer(label: str) -> None:
        try:
            with psycopg.connect(substrate, autocommit=True) as conn:
                barrier.wait(timeout=10)
                results[label] = consume_override(
                    conn,
                    tenant_id=tenant,
                    workflow_kind="agent_dispatch",
                    step_name="candidate_build",
                    run_id=run_ids[label],
                )
        except Exception as exc:  # noqa: BLE001 — surface thread failures in the assert
            results[label] = exc

    threads = [threading.Thread(target=_racer, args=(label,)) for label in run_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert set(results) == {"a", "b"}
    assert not any(isinstance(v, Exception) for v in results.values()), results
    winners = [label for label, v in results.items() if v is not None]
    assert len(winners) == 1, f"expected exactly one consumer, got {winners}"
    winner = winners[0]
    assert str(results[winner].id) == override_id
    assert results[winner].pinned_input == {"limit": 3}
    with psycopg.connect(substrate, autocommit=True) as conn:
        row = conn.execute(
            "SELECT consumed_at, consumed_run_id::text FROM step_overrides WHERE id = %s",
            (override_id,),
        ).fetchone()
    assert row[0] is not None
    assert row[1] == run_ids[winner]


def test_consume_recovery_idempotent_same_run_only(substrate):
    """N2: after the consume txn commits, the SAME run re-consumes the SAME row (DBOS
    recovery re-applies the pin, consumed_at preserved); a DIFFERENT run gets None."""
    from orchestrator.run_control import consume_override

    tenant = _tenant(substrate)
    override_id = _override(
        substrate, tenant, "agent_dispatch", "compose_drafts",
        expires_at=_future(), pinned_input={"model": "claude-test"},
    )
    run_a, run_b = str(uuid4()), str(uuid4())
    with psycopg.connect(substrate, autocommit=True) as conn:
        first = consume_override(
            conn, tenant_id=tenant, workflow_kind="agent_dispatch",
            step_name="compose_drafts", run_id=run_a,
        )
        again = consume_override(
            conn, tenant_id=tenant, workflow_kind="agent_dispatch",
            step_name="compose_drafts", run_id=run_a,
        )
        other = consume_override(
            conn, tenant_id=tenant, workflow_kind="agent_dispatch",
            step_name="compose_drafts", run_id=run_b,
        )
    assert first is not None and str(first.id) == override_id
    assert str(first.consumed_run_id) == run_a
    assert again is not None and again.id == first.id  # same row, re-applied
    assert again.consumed_at == first.consumed_at  # COALESCE kept the original stamp
    assert other is None  # a different run never inherits the consumed pin


def test_consume_run_targeted_matches_only_its_run(substrate):
    """A workflow_id-targeted pin matches ONLY that run (next-run NULL rows match any)."""
    from orchestrator.run_control import consume_override

    tenant = _tenant(substrate)
    target_run = str(uuid4())
    _override(
        substrate, tenant, "agent_dispatch", "persist_batch", workflow_id=target_run
    )
    with psycopg.connect(substrate, autocommit=True) as conn:
        miss = consume_override(
            conn, tenant_id=tenant, workflow_kind="agent_dispatch",
            step_name="persist_batch", run_id=str(uuid4()),
        )
        hit = consume_override(
            conn, tenant_id=tenant, workflow_kind="agent_dispatch",
            step_name="persist_batch", run_id=target_run,
        )
    assert miss is None
    assert hit is not None and str(hit.workflow_id) == target_run


def test_consume_unknown_step_raises(substrate):
    """A typo'd (kind, step) is a programming error — fail loud, never a silent None."""
    from orchestrator.run_control import consume_override

    tenant = _tenant(substrate)
    with psycopg.connect(substrate, autocommit=True) as conn:
        with pytest.raises(ValueError, match="unknown step"):
            consume_override(
                conn, tenant_id=tenant, workflow_kind="agent_dispatch",
                step_name="not_a_step", run_id=str(uuid4()),
            )


def test_expire_overrides_sweep_cancels_only_expired_unconsumed(substrate):
    """F8 sweep: the expired-unconsumed pin is cancelled; future-expiry and
    already-consumed rows are untouched (the sweep bounds next-run pins, nothing else)."""
    from orchestrator.run_control import expire_overrides_sweep

    tenant = _tenant(substrate)
    past = datetime.now(UTC) - timedelta(hours=1)
    expired = _override(
        substrate, tenant, "agent_dispatch", "candidate_build", expires_at=past
    )
    live = _override(
        substrate, tenant, "agent_dispatch", "candidate_build", expires_at=_future()
    )
    consumed = _override(
        substrate, tenant, "agent_dispatch", "candidate_build", expires_at=past,
        consumed_at=past, consumed_run_id=str(uuid4()),
    )
    cancelled = expire_overrides_sweep(pool=_DsnPool(substrate))
    assert cancelled >= 1  # >= — the sweep is global; other suites' expired rows may ride
    with psycopg.connect(substrate, autocommit=True) as conn:
        states = dict(
            conn.execute(
                "SELECT id::text, cancelled_at FROM step_overrides WHERE tenant_id = %s",
                (tenant,),
            ).fetchall()
        )
    assert states[expired] is not None, "expired unconsumed pin must be cancelled"
    assert states[live] is None, "future-expiry pin must survive the sweep"
    assert states[consumed] is None, "consumed pin is history, never cancelled"


def _run_status(dsn: str, tenant: str, status: str) -> str:
    """Seed a pipeline_runs row in an explicit status (the orphaned-pin sweep keys on it)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(
            conn.execute(
                "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
                "VALUES (%s, 'agent_dispatch', %s) RETURNING id",
                (tenant, status),
            ).fetchone()[0]
        )


def test_expire_overrides_sweep_cancels_orphaned_run_bound_pins(substrate):
    """VT-375 hygiene: the sweep cancels an unconsumed run-BOUND pin (workflow_id NOT NULL,
    no expiry) whose pipeline_runs row reached a genuinely-FINISHED status — the run will
    never re-execute, so the pin can never fire.

    FINISHED here = NOT IN ('running', 'paused'). 'paused' is RESUMABLE, not terminal
    (runner.py:285 parks the SAME run on 'paused'; approval_resume drives it onward to
    'completed') — so a pin bound to a paused run must SURVIVE (it may still fire on resume),
    exactly like a pin on a still-running run. Only the finished statuses orphan a pin. A pin
    bound to a finished run AND already consumed is history (untouched); a next-run (NULL
    workflow_id) pin is governed by expiry only, not by this leg.
    """
    from orchestrator.run_control import expire_overrides_sweep

    tenant = _tenant(substrate)
    running_run = _run_status(substrate, tenant, "running")
    finished_run = _run_status(substrate, tenant, "completed")
    paused_run = _run_status(substrate, tenant, "paused")  # RESUMABLE — NOT terminal-for-sweep

    # orphaned: bound to a finished run, unconsumed, NO expiry → cancelled.
    orphaned = _override(
        substrate, tenant, "agent_dispatch", "candidate_build", workflow_id=finished_run
    )
    # bound to a 'paused' (RESUMABLE) run, unconsumed → must SURVIVE: the run may yet resume
    # and consume it (the sweep must never cancel a pin a resuming run still needs).
    live_paused = _override(
        substrate, tenant, "agent_dispatch", "candidate_build", workflow_id=paused_run
    )
    # bound to a still-running run → must SURVIVE (the run may yet consume it).
    live_bound = _override(
        substrate, tenant, "agent_dispatch", "candidate_build", workflow_id=running_run
    )
    # bound to a finished run but ALREADY consumed → history, never re-cancelled.
    consumed_bound = _override(
        substrate, tenant, "agent_dispatch", "candidate_build",
        workflow_id=finished_run, consumed_at=_future() - timedelta(hours=2),
        consumed_run_id=finished_run,
    )

    cancelled = expire_overrides_sweep(pool=_DsnPool(substrate))
    assert cancelled >= 1  # >= — the sweep is global; other suites' rows may ride along
    with psycopg.connect(substrate, autocommit=True) as conn:
        states = dict(
            conn.execute(
                "SELECT id::text, cancelled_at FROM step_overrides WHERE tenant_id = %s",
                (tenant,),
            ).fetchall()
        )
    assert states[orphaned] is not None, "orphaned run-bound pin (finished run) must be cancelled"
    assert states[live_paused] is None, (
        "pin on a 'paused' (RESUMABLE) run must SURVIVE — paused is not terminal"
    )
    assert states[live_bound] is None, "pin on a still-running run must survive the sweep"
    assert states[consumed_bound] is None, "a consumed run-bound pin is history, never cancelled"


# ---------------------------------------------------------------------------
# F6/I7 — allowed-keys merge enforcement (apply_pinned_input)
# ---------------------------------------------------------------------------


def test_apply_pinned_input_allowed_keys():
    """Registry-driven merge: allowed key replaces, non-allowed raises (the API's 422
    substrate), nested dicts deep-merge, and neither input dict is mutated."""
    from orchestrator.run_control import apply_pinned_input
    from orchestrator.run_control.registry import REGISTRY, StepEntry

    entry = REGISTRY[("agent_dispatch", "candidate_build")]  # allowed_keys={'limit'}
    base = {"limit": 50, "tenant_scope": "x"}
    merged = apply_pinned_input(entry, base, {"limit": 5})
    assert merged == {"limit": 5, "tenant_scope": "x"}
    assert base == {"limit": 50, "tenant_scope": "x"}  # base never mutated

    with pytest.raises(ValueError, match="customer_name"):
        apply_pinned_input(entry, base, {"customer_name": "Ramesh"})
    with pytest.raises(ValueError):  # even alongside a legal key
        apply_pinned_input(entry, base, {"limit": 5, "phone": "+91"})
    assert apply_pinned_input(entry, base, {}) == base  # empty pin is a clean copy

    nested = StepEntry("agent_dispatch", "synthetic", "controllable",
                       allowed_keys=frozenset({"cfg"}))
    out = apply_pinned_input(nested, {"cfg": {"a": 1, "b": 2}}, {"cfg": {"b": 3}})
    assert out == {"cfg": {"a": 1, "b": 3}}  # deep-merge under an allowed key


# ---------------------------------------------------------------------------
# §10.5 — F9/N4 two-tier pause posture
# ---------------------------------------------------------------------------


def test_check_pause_two_tier_f9(substrate):
    """Cold cache + control outage → fail OPEN (False); acknowledged pause + outage →
    fail CLOSED (True); a successful read refreshes the cache so a released scope fails
    OPEN again. The outage is a pool whose checkout raises (the injectable seam)."""
    import orchestrator.run_control as rc

    live, broken = _DsnPool(substrate), _RaisingPool()
    # cold: this scope has never been read — fail OPEN, never raise
    assert rc.check_pause(str(uuid4()), "agent_dispatch", pool=broken) is False
    # acknowledged: a real read observed the hold; the outage now fails CLOSED
    tenant = _tenant(substrate)
    _pause(substrate, tenant, "agent_dispatch")
    assert rc.is_paused(tenant, "agent_dispatch", pool=live) is True
    assert rc.check_pause(tenant, "agent_dispatch", pool=broken) is True
    # release observed on a good read → cache refreshed → outage fails OPEN again
    _release_all(substrate, tenant)
    assert rc.check_pause(tenant, "agent_dispatch", pool=live) is False
    assert rc.check_pause(tenant, "agent_dispatch", pool=broken) is False


def test_warm_pause_cache_restores_fail_closed_after_restart(substrate):
    """N4: the cache is per-process and empty on boot; warm_pause_cache reloads active
    holds so a post-restart outage still fails CLOSED for scopes paused pre-restart."""
    import orchestrator.run_control as rc

    tenant = _tenant(substrate)
    _pause(substrate, tenant, "plan_generate")
    # simulate the restart: this scope's known-state entry is gone
    rc._KNOWN_PAUSED.pop((tenant, "plan_generate"), None)
    assert rc.check_pause(tenant, "plan_generate", pool=_RaisingPool()) is False  # the N4 gap
    rc.warm_pause_cache(pool=_DsnPool(substrate))
    assert rc.check_pause(tenant, "plan_generate", pool=_RaisingPool()) is True


def test_check_pause_unknown_kind_fails_loud(substrate):
    """A typo'd workflow_kind would silently never match a pause row — it must raise,
    never be laundered into 'not paused' by the fail-open handler."""
    import orchestrator.run_control as rc

    with pytest.raises(ValueError, match="workflow_kind"):
        rc.check_pause(str(uuid4()), "not_a_kind", pool=_RaisingPool())


# ---------------------------------------------------------------------------
# §10.6 — rerun_from refusals (F10/F11)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("run_type", "from_step"),
    [
        ("twilio_inbound", "dispatch_brain"),  # webhook_inbound — MessageSid semantics
        ("trial_sweep", "evaluate_tenant"),  # warn-path has no send ledger
        ("campaign_send", "execute_fanout"),  # KG outbox dup, no value
    ],
)
def test_rerun_refuses_forbidden_kinds(substrate, run_type, from_step):
    """I8/F11: forbidden-on-rerun kinds REFUSE (422) — never a silent no-op, and never
    a dispatch."""
    from orchestrator.run_control.rerun import RerunRefused, rerun_from

    tenant = _tenant(substrate)
    run = _run(substrate, tenant, run_type=run_type)
    with pytest.raises(RerunRefused, match="not re-runnable") as exc:
        rerun_from(run, from_step, requested_by=str(uuid4()))
    assert exc.value.code == 422


def test_rerun_refuses_unknown_step_and_unknown_run(substrate):
    from orchestrator.run_control.rerun import RerunRefused, rerun_from

    tenant = _tenant(substrate)
    run = _run(substrate, tenant, run_type="plan_generate")
    with pytest.raises(RerunRefused, match="unknown step") as exc:
        rerun_from(run, "not_a_step", requested_by=str(uuid4()))
    assert exc.value.code == 422
    with pytest.raises(RerunRefused, match="unknown source run") as exc2:
        rerun_from(str(uuid4()), "generate_validate", requested_by=str(uuid4()))
    assert exc2.value.code == 422


def test_rerun_409_while_tenant_has_open_approval(substrate):
    """F10: ANY open pending approval for the tenant → 409 BEFORE any dispatch arm runs
    (the owner's YES must never be ambiguous); resolving it clears the gate (the next
    refusal is the arm's own preflight, not the approval 409)."""
    from orchestrator.run_control.rerun import RerunRefused, rerun_from

    tenant = _tenant(substrate)
    run = _run(substrate, tenant, run_type="plan_generate")
    with psycopg.connect(substrate, autocommit=True) as conn:
        approval = str(
            conn.execute(
                "INSERT INTO pending_approvals (tenant_id, run_id, approval_type, summary, "
                "timeout_at) VALUES (%s, %s, 'other', 'VT-374 open approval', "
                "now() + interval '30 minutes') RETURNING id",
                (tenant, run),
            ).fetchone()[0]
        )
    with pytest.raises(RerunRefused, match="open pending approval") as exc:
        rerun_from(run, "generate_validate", requested_by=str(uuid4()))
    assert exc.value.code == 409
    # resolve it: the 409 clears (the synthetic tenant then fails the grounding
    # preflight instead — proving the approval gate, not the arm, produced the 409)
    with psycopg.connect(substrate, autocommit=True) as conn:
        conn.execute(
            "UPDATE pending_approvals SET resolved_at = now(), status = 'rejected', "
            "decision = 'rejected' WHERE id = %s",
            (approval,),
        )
    with pytest.raises(RerunRefused) as exc2:
        rerun_from(run, "generate_validate", requested_by=str(uuid4()))
    assert exc2.value.code == 422
    assert "approval" not in str(exc2.value)


# ---------------------------------------------------------------------------
# C1 fold-in (VT-375, Option A per the Cowork ruling 20260611T234500Z) — F10
# RACE: rerun_from vs request_owner_approval's arm. Guarantee stack: mig-128
# partial unique (never two open pending_approvals) + detect-and-escalate (an
# approval that arms DURING the rerun ⇒ the rerun closes 'escalated' + a
# run_control_rerun_overlap alert — never silently kept). The /rerun 409 is
# the UX gate on top — plan §12 F10.
# ---------------------------------------------------------------------------


def _seed_grounded_profile(dsn: str, tenant: str) -> None:
    """Seed a CONFIRMED L1 business_profile so generator._gather_grounding returns a non-empty
    bundle — this is what lets the rerun WIN the race and actually SUCCEED (without it the rerun
    always 422s on grounding, and the race never exercises the success arm). RLS-scoped write
    through the real upsert path."""
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn  # tenant_connection reads this
    from orchestrator.knowledge import upsert_business_profile

    upsert_business_profile(
        tenant,
        {"business_name": "VT375 Race Diner", "business_type": "restaurant", "city": "Pune"},
    )


def _poll_overlap_alert(
    dsn: str, run_id: Any, *, timeout_s: float = 12.0
) -> dict[str, Any] | None:
    """Poll pipeline_log for the ``run_control_rerun_overlap`` alert row. ``log_event``
    is fire-and-forget (the INSERT lands on a daemon thread), so the assertion must
    wait for it rather than read-once."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(
                "SELECT severity, component, payload FROM pipeline_log "
                "WHERE event_type = 'run_control_rerun_overlap' AND run_id = %s",
                (str(run_id),),
            ).fetchone()
        if row is not None:
            return {"severity": row[0], "component": row[1], "payload": row[2]}
        time.sleep(0.25)
    return None


def _rerun_row_state(dsn: str, run_id: Any) -> tuple[str, dict[str, Any] | None]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, terminal_state_metadata FROM pipeline_runs WHERE id = %s",
            (str(run_id),),
        ).fetchone()
    assert row is not None, "the rerun's lineage row must exist"
    return row[0], row[1]


def test_f10_rerun_races_approval_arm_never_two_open_approvals(substrate, monkeypatch):
    """C1 (plan §12 F10, re-pointed to Option A's guarantee — Cowork ruling
    20260611T234500Z): a ``rerun_from`` racing ``arm_pause_request`` for the SAME tenant
    converges to one of the ALLOWED outcomes, never the forbidden third.

    Allowed, per interleaving:
      (a) the rerun REFUSED-409 (the F10 open-approval gate; the arm won), or
      (b) the rerun RAN, and EITHER no approval was open at its completion re-check
          (outcome='completed', row closed 'completed') OR the overlap was detected:
          the run row closed 'escalated' with final_outcome='rerun_overlapped_open_approval'
          (the minted version STANDS — no rollback) + the ``run_control_rerun_overlap``
          pipeline_log alert row present, outcome='escalated_overlap'.
    FORBIDDEN: the rerun's run row 'completed' while an approval the rerun overlapped is
    open and no escalation/alert — a silently-kept overlap. Always: open approvals ≤ 1
    (the mig-128 ``pending_approvals_one_open_per_tenant`` partial unique, untouched).

    Why Option A and not the B advisory lock (the C1 ruling's B-analysis, recorded here):
    an advisory lock spanning only rerun's GATE window vs the arm's insert txn would NOT
    deliver "converges to refusal" — an approval created after the gate window but during
    rerun execution still lands, exactly the overlap A detects. Delivering full mutual
    exclusion would mean holding the lock for the rerun's ENTIRE execution, which blocks
    the LIVE owner-approval arm path behind an ops re-run — an unacceptable inversion
    (ops convenience outranking the owner's money-adjacent control surface). The honest,
    complete guarantee stack is therefore: mig-128 (never two open approvals) +
    detect-and-escalate (an overlap is escalated + alerted, never silently kept). C
    (xfail) was rejected — no xfail masks on money-adjacent paths.

    Falsifiability is kept two ways: (1) the rerun CAN genuinely succeed (grounded
    profile seeded + ``generator._generate_and_validate`` stubbed — no LLM), so the 6
    barrier interleavings exercise the real race, and (2) a DETERMINISTIC forced-overlap
    leg holds the rerun inside the (stubbed) generator until the arm has committed its
    approval — the re-check MUST then see it, so an implementation that skips the
    post-completion re-check fails that leg hard (row 'completed' + open approval +
    no alert = the forbidden third, asserted directly).
    """
    from orchestrator.agent.tools.request_owner_approval import (
        RequestOwnerApprovalInput,
        arm_pause_request,
    )
    from orchestrator.business_plan import delivery, generator
    from orchestrator.run_control.rerun import RerunRefused, RerunResult, rerun_from

    # Stub the LLM transmit + the owner-facing delivery burst so a WINNING rerun completes
    # fast and offline. The stub returns a schema-shaped, citation-free degraded plan (the
    # generator's own floor shape) — write_new_version persists it against the real DB, so
    # the rerun returns a real new-run id = genuine success, not a mocked-away one. The
    # ``hold`` events are armed ONLY by the forced-overlap leg below; the 6 racing
    # interleavings leave them None (stub returns immediately).
    hold: dict[str, threading.Event | None] = {"entered": None, "release": None}

    def _stub_generate(_tenant_id: Any, grounding: Any, _llm: Any = None) -> dict[str, Any]:
        entered, release = hold["entered"], hold["release"]
        if entered is not None and release is not None:
            entered.set()  # the rerun is past the 409 gate, mid-execution
            assert release.wait(timeout=20), "forced-overlap release never came"
        return {
            "summary": {"text": "Race-test plan.", "headline": "Race", "citations": []},
            "roadmap": [],
            "model_id": "stub-no-llm",
        }

    monkeypatch.setattr(generator, "_generate_and_validate", _stub_generate)
    monkeypatch.setattr(delivery, "deliver_plan", lambda *a, **k: None)

    def _arm_call(tenant: str, approval_run: str) -> Any:
        return arm_pause_request(
            RequestOwnerApprovalInput(
                tenant_id=UUID(tenant),
                run_id=UUID(approval_run),
                approval_type="other",
                summary="VT-375 C1 race",
            ),
            dry_run=True,  # no Twilio — the INSERT is the only effect we race
        )

    def _open_count(tenant: str) -> int:
        with psycopg.connect(substrate, autocommit=True) as conn:
            return conn.execute(
                "SELECT count(*) FROM pending_approvals "
                "WHERE tenant_id = %s AND resolved_at IS NULL",
                (tenant,),
            ).fetchone()[0]

    # --- leg 1: the 6 barrier interleavings (the live race) --------------------------
    for _ in range(6):
        tenant = _tenant_brain_ready(substrate, whatsapp=f"+1{uuid4().int % 10**10:010d}")
        _seed_grounded_profile(substrate, tenant)  # grounding present → the rerun CAN win
        rerun_source = _run_typed(substrate, tenant, "plan_generate")
        approval_run = _run_typed(substrate, tenant, "plan_generate")  # the arm's target run

        barrier = threading.Barrier(2)
        results: dict[str, Any] = {}

        def _arm() -> None:
            try:
                barrier.wait(timeout=10)
                results["arm"] = _arm_call(tenant, approval_run)
            except Exception as exc:  # noqa: BLE001 — surface in the assert
                results["arm"] = exc

        def _rerun() -> None:
            try:
                barrier.wait(timeout=10)
                results["rerun"] = rerun_from(
                    rerun_source, "generate_validate", requested_by=str(uuid4())
                )
            except RerunRefused as exc:
                results["rerun"] = exc
            except Exception as exc:  # noqa: BLE001 — any other failure must surface, not pass
                results["rerun"] = exc

        threads = [threading.Thread(target=_arm), threading.Thread(target=_rerun)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert set(results) == {"arm", "rerun"}, f"a racer never returned: {results}"
        # The arm itself must not have raised (it either armed, or refused 'queue busy' —
        # both are clean PauseRequestResults, never an exception).
        assert not isinstance(results["arm"], Exception), f"arm raised: {results['arm']!r}"

        rr = results["rerun"]
        # invariant 1 — the structural backstop: never more than one OPEN approval.
        open_count = _open_count(tenant)
        assert open_count <= 1, (
            f"F10 structural backstop violated: {open_count} open approvals for the tenant "
            "(the mig-128 one-open-per-tenant partial unique must converge the race)"
        )

        # invariant 2 — the rerun outcome is RAN (RerunResult) xor REFUSED-409, nothing
        # else (grounded now, so never the old blanket 422; never a crash).
        rerun_ran = isinstance(rr, RerunResult)
        rerun_refused_409 = isinstance(rr, RerunRefused) and rr.code == 409
        assert rerun_ran ^ rerun_refused_409, (
            f"rerun outcome must be RAN xor 409-refusal; got {rr!r}"
        )

        # invariant 3 — A's guarantee, per outcome.
        if rerun_refused_409:
            assert open_count == 1, (
                "a 409 refusal must be backed by an actually-open approval (the arm won)"
            )
        elif rr.outcome == "escalated_overlap":
            # overlap detected mid-flight: escalated + alerted, never silently kept.
            status, meta = _rerun_row_state(substrate, rr.run_id)
            assert status == "escalated", f"overlap must close the run 'escalated', got {status!r}"
            assert meta and meta.get("final_outcome") == "rerun_overlapped_open_approval"
            assert "version" in (meta or {}), "the version mint STANDS on overlap (no rollback)"
            assert open_count == 1, "an escalated overlap implies the arm's approval is open"
            alert = _poll_overlap_alert(substrate, rr.run_id)
            assert alert is not None, "the run_control_rerun_overlap alert row must land"
            assert alert["severity"] == "error" and alert["component"] == "run_control"
        else:
            # outcome='completed': the completion re-check saw no open approval. An
            # approval may STILL be open now (the arm landed after the re-check) — that
            # is sequential, not an overlap; the next arm/rerun hits the 409/refused gate.
            assert rr.outcome == "completed", f"unknown outcome {rr.outcome!r}"
            status, meta = _rerun_row_state(substrate, rr.run_id)
            assert status == "completed" and meta and meta.get("final_outcome") == "completed"

    # --- leg 2: DETERMINISTIC forced overlap (the falsifiability anchor) -------------
    # Hold the rerun inside the stubbed generator (past the 409 gate), commit the arm's
    # approval, release. The completion re-check is now GUARANTEED to face an open
    # approval — the forbidden third (row 'completed', approval open, no escalation/alert)
    # is asserted impossible directly, not probabilistically.
    tenant = _tenant_brain_ready(substrate, whatsapp=f"+1{uuid4().int % 10**10:010d}")
    _seed_grounded_profile(substrate, tenant)
    rerun_source = _run_typed(substrate, tenant, "plan_generate")
    approval_run = _run_typed(substrate, tenant, "plan_generate")
    hold["entered"], hold["release"] = threading.Event(), threading.Event()
    forced: dict[str, Any] = {}

    def _forced_rerun() -> None:
        try:
            forced["rerun"] = rerun_from(
                rerun_source, "generate_validate", requested_by=str(uuid4())
            )
        except Exception as exc:  # noqa: BLE001 — surface in the assert
            forced["rerun"] = exc

    t = threading.Thread(target=_forced_rerun)
    t.start()
    try:
        assert hold["entered"].wait(timeout=20), "rerun never reached the generator"
        armed = _arm_call(tenant, approval_run)  # gate already passed → arm lands mid-flight
        assert armed.status == "armed", f"the forced arm must arm, got {armed.status!r}"
    finally:
        hold["release"].set()
        t.join(timeout=30)
        hold["entered"] = hold["release"] = None  # disarm for any later stub call

    rr = forced.get("rerun")
    assert isinstance(rr, RerunResult), f"forced-overlap rerun must RUN (gate passed): {rr!r}"
    assert rr.outcome == "escalated_overlap", (
        "FORBIDDEN THIRD: an approval armed mid-flight and the rerun did not escalate "
        f"(outcome={rr.outcome!r}) — the overlap would be silently kept"
    )
    status, meta = _rerun_row_state(substrate, rr.run_id)
    assert status == "escalated" and meta is not None
    assert meta.get("final_outcome") == "rerun_overlapped_open_approval"
    assert "version" in meta, "the version mint STANDS on overlap (no rollback — the ruling)"
    assert _open_count(tenant) == 1, "exactly the arm's approval is open (mig-128 holds)"
    alert = _poll_overlap_alert(substrate, rr.run_id)
    assert alert is not None, "the run_control_rerun_overlap alert row must land in pipeline_log"
    assert alert["severity"] == "error" and alert["component"] == "run_control"
    assert alert["payload"].get("workflow_kind") == "plan_generate"


# ---------------------------------------------------------------------------
# VT-376 — rerun-slot lock (Cowork plan ruling 20260612T015000Z arm a; build
# contract §B1.2 + §B1.5). The lock serializes rerun-vs-RERUN per tenant across
# the gate-check → lineage-insert window ONLY; arm-vs-rerun stays the C1
# detect-and-escalate guarantee above.
# ---------------------------------------------------------------------------


def _seed_delivered_plan(dsn: str, tenant: str) -> None:
    """An active plan with every part delivered (bitmap 3) — the cheapest synchronous
    rerun-arm substrate (mirrors the C1 API-test seeding)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO business_plan (tenant_id, version, summary_json, roadmap_json, "
            "fact_bundle_json, generated_by, delivered_parts) "
            "VALUES (%s, 1, %s, '[]', '{}', 'test', 3)",
            (tenant, Jsonb({"text": "plan"})),
        )


def test_rerun_double_click_serializes_exactly_one_proceeds(substrate, monkeypatch):
    """BINDING acceptance addition #1 (build contract §B1.5 / plan ruling): two concurrent
    rerun calls (threads + barrier) for the SAME tenant + SAME source run → EXACTLY ONE
    proceeds; the second serializes behind the rerun-slot advisory lock and refuses 409 on
    the in-flight gate re-check; NEVER two lineage rows for the one double-clicked source.

    Determinism: the winner is parked inside its (lock-free) dispatch phase by a blocking
    ``deliver_plan`` stub, so its lineage row is still 'running' when the loser's gate
    re-check runs — the loser MUST observe it and refuse. Falsifiability: WITHOUT the lock
    the barrier puts both threads through the gate-check together (neither lineage insert
    has committed yet), both insert, both park in the stub — ZERO refusals and TWO lineage
    rows, failing the exactly-one assertions below. The refusal itself is the
    serialization evidence (the loser's gate read observed the winner's committed insert
    despite the simultaneous start); the held-window lock probe is the next test."""
    from orchestrator.business_plan import delivery
    from orchestrator.run_control.rerun import RerunRefused, RerunResult, rerun_from

    tenant = _tenant_brain_ready(substrate, whatsapp=f"+1{uuid4().int % 10**10:010d}")
    _seed_delivered_plan(substrate, tenant)
    source = _run_typed(substrate, tenant, "plan_deliver")

    entered, release = threading.Event(), threading.Event()

    def _blocking_deliver(*_a: Any, **_k: Any) -> None:
        entered.set()  # the winner is past its locked window, parked mid-dispatch
        assert release.wait(timeout=30), "double-click release never came"

    monkeypatch.setattr(delivery, "deliver_plan", _blocking_deliver)

    barrier = threading.Barrier(2)
    results: dict[str, Any] = {}

    def _click(label: str) -> None:
        try:
            barrier.wait(timeout=10)
            results[label] = rerun_from(source, "deliver_parts", requested_by=str(uuid4()))
        except RerunRefused as exc:
            results[label] = exc
        except Exception as exc:  # noqa: BLE001 — surface thread failures in the asserts
            results[label] = exc

    threads = [threading.Thread(target=_click, args=(label,)) for label in ("a", "b")]
    for t in threads:
        t.start()
    try:
        # The loser returns FIRST (the winner is parked in the deliver stub): wait for a
        # refusal to land, then inspect the in-flight state BEFORE releasing the winner.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not any(
            isinstance(v, RerunRefused) for v in results.values()
        ):
            time.sleep(0.1)
        assert entered.wait(timeout=20), "no rerun ever reached the dispatch phase"
        refusals = {k: v for k, v in results.items() if isinstance(v, RerunRefused)}
        assert len(refusals) == 1, (
            f"exactly ONE click must refuse (the lock serialized the pair); got {results!r}"
        )
        loser = next(iter(refusals.values()))
        assert loser.code == 409, f"the loser refuses 409, got {loser.code}"
        assert "already in flight" in str(loser)
        with psycopg.connect(substrate, autocommit=True) as conn:
            inflight = conn.execute(
                "SELECT count(*) FROM pipeline_runs WHERE rerun_of_run_id = %s", (source,)
            ).fetchone()[0]
        assert inflight == 1, f"NEVER two lineage rows for one double-click (got {inflight})"
    finally:
        release.set()
        for t in threads:
            t.join(timeout=30)

    winners = [v for v in results.values() if isinstance(v, RerunResult)]
    assert len(winners) == 1, f"exactly one click proceeds; results={results!r}"
    assert winners[0].outcome == "completed"
    status, meta = _rerun_row_state(substrate, winners[0].run_id)
    assert status == "completed" and meta and meta.get("final_outcome") == "completed"
    with psycopg.connect(substrate, autocommit=True) as conn:
        total = conn.execute(
            "SELECT count(*) FROM pipeline_runs WHERE rerun_of_run_id = %s", (source,)
        ).fetchone()[0]
    assert total == 1, "the double-click landed exactly one lineage row, start to finish"


def test_rerun_slot_lock_held_across_gate_to_lineage_window_only(substrate, monkeypatch):
    """Direct lock evidence (plan ruling: 'assert the lock actually serialized'): while a
    rerun is parked INSIDE its gate→lineage window (the lineage insert is wrapped to
    block after the real insert), a second session CANNOT acquire
    ``pg_advisory_xact_lock(hashtext('rerun-slot:<tenant>'))`` — the try-probe returns
    false. Once the rerun finishes (dispatch + close run OUTSIDE the lock), the same
    probe acquires immediately — proving the lock spans exactly the pinned window and is
    never held across dispatch work. The probe string is built from the production
    ``_RERUN_SLOT_NS`` constant, pinning the documented namespace."""
    from orchestrator.business_plan import delivery
    from orchestrator.run_control import rerun as rerun_mod

    tenant = _tenant_brain_ready(substrate, whatsapp=f"+1{uuid4().int % 10**10:010d}")
    _seed_delivered_plan(substrate, tenant)
    source = _run_typed(substrate, tenant, "plan_deliver")

    monkeypatch.setattr(delivery, "deliver_plan", lambda *a, **k: None)

    entered, release = threading.Event(), threading.Event()
    real_insert = rerun_mod._insert_lineage_row

    def _parked_insert(*args: Any, **kwargs: Any) -> None:
        real_insert(*args, **kwargs)
        entered.set()  # inside the locked window, lineage row committed
        assert release.wait(timeout=30), "lock-probe release never came"

    monkeypatch.setattr(rerun_mod, "_insert_lineage_row", _parked_insert)

    def _probe() -> bool:
        # Autocommit: the try-lock's implicit txn ends with the statement, so a SUCCESSFUL
        # probe self-releases — probing never wedges the production lock.
        with psycopg.connect(substrate, autocommit=True) as conn:
            return conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
                (f"{rerun_mod._RERUN_SLOT_NS}{tenant}",),
            ).fetchone()[0]

    result: dict[str, Any] = {}

    def _run_rerun() -> None:
        try:
            result["rr"] = rerun_mod.rerun_from(
                source, "deliver_parts", requested_by=str(uuid4())
            )
        except Exception as exc:  # noqa: BLE001 — surface in the asserts
            result["rr"] = exc

    t = threading.Thread(target=_run_rerun)
    t.start()
    try:
        assert entered.wait(timeout=20), "rerun never reached the lineage insert"
        assert _probe() is False, (
            "the rerun-slot lock must be HELD while the rerun sits in its gate→lineage window"
        )
    finally:
        release.set()
        t.join(timeout=30)
    rr = result.get("rr")
    assert isinstance(rr, rerun_mod.RerunResult) and rr.outcome == "completed", f"{rr!r}"
    assert _probe() is True, "the lock must be RELEASED once the locked window exits"


def test_run_control_event_schemas_registered():
    """VT-376 item 4: the run-control alerts the panel surfaces are REGISTERED
    pipeline_log schemas. validate() passes the EXACT payload shapes the two production
    emitters write (rerun._emit_overlap_alert / run_control._emit_degraded), so the
    writer never annotates them ``payload_validation_failed``."""
    from orchestrator.observability.event_schemas import EVENT_SCHEMAS, validate

    assert "run_control_rerun_overlap" in EVENT_SCHEMAS
    assert "run_control_degraded" in EVENT_SCHEMAS
    ok, errors = validate(
        "run_control_rerun_overlap",
        {
            "workflow_kind": "plan_generate",
            "source_run_id": str(uuid4()),
            "approval_id": None,  # optional — the emitter's no-approval-id shape
            "final_outcome": "rerun_overlapped_open_approval",
        },
    )
    assert ok, errors
    ok, errors = validate(
        "run_control_degraded", {"workflow_kind": "agent_dispatch", "posture": "fail_open"}
    )
    assert ok, errors


# ===========================================================================
# Test-A additions (T-A1..T-A6) — pause holds, STOP-while-paused, rerun success
# legs, N2 kill-and-recover, supervisor N1, C2 alignment. The Fixer-A/B/C
# behaviour changes are the POST-FIX reality tested here.
# ===========================================================================

_WEBHOOK_PAUSE_WORKER = Path(__file__).parent / "_webhook_pause_resume_worker.py"


# --- extra seed helpers (used by the live-path + rerun legs) -------------------------


def _tenant_brain_ready(dsn: str, *, whatsapp: str | None = None) -> str:
    """A tenant whose owner_inputs is TRUE (so a non-STOP inbound routes to the brain arm,
    where the webhook_inbound pause boundary lives) and, optionally, a whatsapp_number
    (plan delivery needs one)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(
            conn.execute(
                "INSERT INTO tenants (business_name, plan_tier, phase, owner_inputs, "
                "whatsapp_number) VALUES (%s, 'founding', 'paid_active', true, %s) "
                "RETURNING id",
                (f"VT374 {uuid4().hex[:8]}", whatsapp),
            ).fetchone()[0]
        )


def _run_typed(dsn: str, tenant: str, run_type: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(
            conn.execute(
                "INSERT INTO pipeline_runs (tenant_id, run_type, status) "
                "VALUES (%s, %s, 'completed') RETURNING id",
                (tenant, run_type),
            ).fetchone()[0]
        )


def _run_row(dsn: str, run_id: str) -> tuple[Any, ...]:
    """The columns a rerun must never mutate on the SOURCE row (byte-identical check)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "SELECT status, rerun_of_run_id, rerun_from_step, ended_at, "
            "terminal_state_metadata FROM pipeline_runs WHERE id = %s",
            (run_id,),
        ).fetchone()


class _SeqPausePool:
    """A run_control pool whose control reads report PAUSED for the first ``paused_reads``
    checkouts, then RELEASED — a shared counter across connections (each ``check_pause``
    opens its own connection). The unit-level analogue of a pause being lifted mid-hold."""

    def __init__(self, paused_reads: int) -> None:
        self._remaining = paused_reads

    @contextmanager
    def connection(self):  # type: ignore[no-untyped-def]
        outer = self

        class _Conn:
            def execute(self, *_a: Any, **_k: Any):  # type: ignore[no-untyped-def]
                return self

            def fetchone(self) -> tuple[int] | None:
                if outer._remaining > 0:
                    outer._remaining -= 1
                    return (1,)
                return None

        yield _Conn()


def _wait_status(dsn: str, run_id: str, want: str, timeout: float) -> str | None:
    """Poll pipeline_runs.status until it equals ``want`` or the timeout elapses."""
    deadline = time.time() + timeout
    last: str | None = None
    while time.time() < deadline:
        row = _run_row(dsn, run_id)
        last = row[0] if row else None
        if last == want:
            return last
        time.sleep(1.0)
    return last


def _intervention_steps(dsn: str, run_id: str) -> list[tuple[Any, ...]]:
    """The B1 run_control_intervention timeline rows for a run (service role — RLS bypassed):
    (step_name, override_id, paused_ms, input_envelope), ordered."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "SELECT step_name, override_id::text, paused_ms, input_envelope FROM pipeline_steps "
            "WHERE run_id = %s AND step_kind = 'run_control_intervention' ORDER BY step_seq",
            (run_id,),
        ).fetchall()


# ---------------------------------------------------------------------------
# T-A1a — hold_while_paused unit (fake sleep_fn): pause→release vs unpaused
# ---------------------------------------------------------------------------


def test_hold_while_paused_unit_pause_then_release(substrate):
    """§10.2/N3 (unit): the plain-code hold blocks while paused and returns a positive
    paused_ms once released; an unpaused scope returns 0 with NO poll. ``sleep_fn`` is a
    fake that advances real monotonic time a hair so paused_ms is measurable yet the test
    stays sub-second (the hold measures wall-clock, not the sleep argument)."""
    import orchestrator.run_control as rc

    sleeps: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        time.sleep(0.002)  # tiny REAL sleep — monotonic must advance for paused_ms > 0

    paused_ms = rc.hold_while_paused(
        str(uuid4()),
        "webhook_inbound",
        sleep_fn=_fake_sleep,
        poll_s=5.0,
        pool=_SeqPausePool(2),  # paused for 2 reads, then released
    )
    assert paused_ms > 0, "a held-then-released scope must report a positive paused_ms"
    assert len(sleeps) == 2, "one poll per paused read until release"
    assert sleeps == [5.0, 5.0], "the hold sleeps poll_s each iteration"

    no_sleeps: list[float] = []
    unheld_ms = rc.hold_while_paused(
        str(uuid4()),
        "webhook_inbound",
        sleep_fn=lambda s: no_sleeps.append(s),
        pool=_SeqPausePool(0),  # never paused
    )
    assert unheld_ms == 0, "an unpaused scope returns 0"
    assert no_sleeps == [], "an unpaused scope never sleeps"


# ---------------------------------------------------------------------------
# T-A1b — live-path pause: kill-and-recover on webhook_pipeline_run (N3 seam)
# ---------------------------------------------------------------------------


def test_webhook_pause_parks_survives_restart_and_releases(substrate):
    """§10.2/N3 (live): a tenant paused on ``webhook_inbound`` holds the live
    ``webhook_pipeline_run`` at the pre-dispatch_brain seam (runner.py:591) — a
    CHECKPOINTED durable wait. The worker is SIGKILLed mid-park; the workflow is
    observed PENDING; a second launch lets DBOS recovery re-enter the body and resume
    the hold; releasing the pause drains the recovered run to ``completed``.

    The seam holds before the brain, so the parked run never completes while paused —
    proving the boundary is real (not post-hoc). The brain is stubbed in the worker
    (no LLM); we assert the PAUSE behaviour, not the brain.
    """
    from dbos import DBOSClient

    tenant = _tenant_brain_ready(substrate)
    _pause(substrate, tenant, "webhook_inbound")
    message_sid = f"SM{uuid4().hex}"
    run_id = str(uuid5(NAMESPACE_URL, message_sid))
    workflow_id = f"vt374-pause-{message_sid}"
    argv = [sys.executable, str(_WEBHOOK_PAUSE_WORKER), substrate, workflow_id, tenant, run_id]

    proc1 = subprocess.Popen(argv)
    try:
        parked = _wait_status(substrate, run_id, "running", timeout=45)
        assert parked == "running", f"run never parked at the pause seam (status={parked})"
    finally:
        proc1.kill()
    proc1.wait(timeout=15)

    # Between crash and resume the workflow is PENDING (the checkpointed hold is durable).
    pending = {str(w.workflow_id) for w in DBOSClient(substrate).list_workflows(status="PENDING")}
    assert workflow_id in pending, "the paused workflow was not left PENDING by the crash"

    proc2 = subprocess.Popen(argv)  # DBOS recovery re-enters the held workflow
    try:
        # Still paused → still parked after recovery (the hold survived the restart).
        time.sleep(8)
        mid = _run_row(substrate, run_id)
        assert mid[0] == "running", "recovered run completed while still paused — hold lost"
        _release_all(substrate, tenant)
        done = _wait_status(substrate, run_id, "completed", timeout=60)
        assert done == "completed", f"released run never drained (status={done})"
    finally:
        proc2.kill()
        proc2.wait(timeout=15)


# ---------------------------------------------------------------------------
# T-A2 — §10.3/I6 STOP-while-paused (pause-EXEMPT by construction)
# ---------------------------------------------------------------------------


def test_stop_processed_end_to_end_while_paused(substrate, monkeypatch):
    """§10.3/I6: a fully-paused (webhook_inbound) tenant still processes STOP end-to-end.
    The opt-out routes through the direct-handler branch (runner.py:570) which sits
    BEFORE the brain pause seam — pause-exempt BY CONSTRUCTION (I6). The run completes
    UNHELD and the tenant opt_out flag is set. Control leg: a non-STOP inbound for the
    SAME tenant parks at the brain seam (the pause IS active — STOP is the only thing
    that bypasses it)."""
    import importlib

    import dbos as _dbos

    from orchestrator.runner import webhook_pipeline_run

    # opt_out_handler sends a confirmation template — stub the send (no network). The
    # package re-exports the function under the module's own name, so import the REAL
    # submodule object via importlib to patch its ``send_template_message`` binding.
    class _SendStub:
        def model_dump(self) -> dict[str, Any]:
            return {"stubbed": True}

    opt_out_mod = importlib.import_module("orchestrator.direct_handlers.opt_out_handler")
    monkeypatch.setattr(opt_out_mod, "send_template_message", lambda *a, **k: _SendStub())
    # stub the brain so the control-leg park doesn't need an LLM (it parks BEFORE the brain,
    # but the stub guarantees no transmit if the seam were ever bypassed).
    import orchestrator.agent.dispatch as dispatch_mod

    monkeypatch.setattr(
        dispatch_mod,
        "dispatch_brain",
        lambda **k: dispatch_mod.DispatchResult(final_status="completed", terminal_path=None),
    )

    tenant = _tenant_brain_ready(substrate)
    _pause(substrate, tenant, "webhook_inbound")

    # --- STOP leg: routes to opt_out_handler, completes unheld ---
    stop_sid = f"SM{uuid4().hex}"
    stop_run = str(uuid5(NAMESPACE_URL, stop_sid))
    stop_fields = {
        "MessageSid": stop_sid,
        "From": "+15551110001",
        "To": "+15552220001",
        "Body": "STOP",
        "NumMedia": "0",
    }
    with _dbos.SetWorkflowID(f"vt374-stop-{stop_sid}"):
        handle = _dbos.DBOS.start_workflow(webhook_pipeline_run, tenant, stop_run, stop_fields)
    result = handle.get_result()  # STOP never parks → this returns promptly
    assert result["routed"] == "direct_handler"
    assert result["handler"] == "opt_out_handler"
    assert _run_row(substrate, stop_run)[0] == "completed", "STOP run did not complete unheld"
    with psycopg.connect(substrate, autocommit=True) as conn:
        opt_out = conn.execute(
            "SELECT opt_out FROM tenants WHERE id = %s", (tenant,)
        ).fetchone()[0]
    assert opt_out is True, "opt-out was not processed for the paused tenant (I6 violated)"

    # --- control leg: a non-STOP inbound for the SAME tenant PARKS at the brain seam ---
    brain_sid = f"SM{uuid4().hex}"
    brain_run = str(uuid5(NAMESPACE_URL, brain_sid))
    brain_fields = {
        "MessageSid": brain_sid,
        "From": "+15551110002",
        "To": "+15552220002",
        "Body": "hello what is my campaign status please",
        "NumMedia": "0",
    }
    with _dbos.SetWorkflowID(f"vt374-ctrl-{brain_sid}"):
        _dbos.DBOS.start_workflow(webhook_pipeline_run, tenant, brain_run, brain_fields)
    parked = _wait_status(substrate, brain_run, "running", timeout=20)
    assert parked == "running", f"non-STOP inbound did not park while paused (status={parked})"
    _release_all(substrate, tenant)  # drain it so the worker pool doesn't park forever
    assert _wait_status(substrate, brain_run, "completed", timeout=40) == "completed"

    # B1 dead-columns assertion: the released brain-seam hold landed exactly one
    # run_control_intervention timeline row on this run — paused_ms COLUMN set (> 0),
    # override_id NULL (dispatch_brain is pause-only, never override-consumed), and the
    # envelope IDs/enums-only ({action, workflow_kind, step_name}; no free text — CL-390).
    steps = _intervention_steps(substrate, brain_run)
    assert len(steps) == 1, f"expected one run_control_intervention row, got {steps}"
    step_name, override_id, paused_ms, envelope = steps[0]
    assert step_name == "webhook_inbound:dispatch_brain"
    assert override_id is None, "the pause-only brain seam never carries an override_id"
    assert paused_ms is not None and paused_ms > 0, "released hold must stamp paused_ms > 0"
    assert set(envelope) == {"action", "workflow_kind", "step_name"}, (
        f"intervention envelope must be IDs/enums-only, got keys {sorted(envelope)}"
    )
    assert envelope == {
        "action": "released",
        "workflow_kind": "webhook_inbound",
        "step_name": "dispatch_brain",
    }


def test_webhook_max_hold_closes_paused_without_dispatching_brain(substrate, monkeypatch):
    """B2 bounded durable hold: with ``_RUN_CONTROL_MAX_HOLD_S`` ~0, a paused non-STOP inbound
    exceeds the cap on the FIRST check — the seam closes the run ``status='paused'`` with
    ``terminal_state_metadata.paused_by_run_control=true`` and returns WITHOUT dispatching the
    brain (no worker parks forever). dispatch_brain is monkeypatched to record calls; it must
    never fire. The pause stays active throughout (no release) — proving the close is the
    max-hold path, not a drain."""
    import dbos as _dbos

    from orchestrator import runner as runner_mod
    from orchestrator.runner import webhook_pipeline_run

    # tighten the bound so the hold trips on the first iteration (paused_ms 0 >= 0).
    monkeypatch.setattr(runner_mod, "_RUN_CONTROL_MAX_HOLD_S", 0.0)
    monkeypatch.setattr(runner_mod, "_RUN_CONTROL_POLL_S", 0.05)

    # record any brain dispatch — the max-hold path returns BEFORE the lazy import, so zero calls.
    brain_calls: list[Any] = []
    import orchestrator.agent.dispatch as dispatch_mod

    monkeypatch.setattr(
        dispatch_mod,
        "dispatch_brain",
        lambda **k: brain_calls.append(k)
        or dispatch_mod.DispatchResult(final_status="completed", terminal_path=None),
    )

    tenant = _tenant_brain_ready(substrate)
    _pause(substrate, tenant, "webhook_inbound")  # stays active — never released

    sid = f"SM{uuid4().hex}"
    run = str(uuid5(NAMESPACE_URL, sid))
    fields = {
        "MessageSid": sid,
        "From": "+15551110003",
        "To": "+15552220003",
        "Body": "what is my campaign status please",
        "NumMedia": "0",
    }
    with _dbos.SetWorkflowID(f"vt374-maxhold-{sid}"):
        handle = _dbos.DBOS.start_workflow(webhook_pipeline_run, tenant, run, fields)
    result = handle.get_result()  # max-hold returns promptly (no sleep on the first-trip break)

    assert result["routed"] == "run_control_max_hold", f"not the max-hold path: {result}"
    assert brain_calls == [], "B2: the brain must NOT be dispatched on a max-hold close"
    status, _, _, _, terminal_meta = _run_row(substrate, run)
    assert status == "paused", f"max-hold run must close status='paused' (got {status})"
    assert terminal_meta.get("paused_by_run_control") is True, (
        "terminal_state_metadata.paused_by_run_control must mark the parked run"
    )
    # B1 companion: the max-hold close also records the 'held' intervention row.
    steps = _intervention_steps(substrate, run)
    assert len(steps) == 1 and steps[0][3]["action"] == "held", (
        f"max-hold must record one 'held' intervention row, got {steps}"
    )


def test_agent_dispatch_override_consumed_seam_records_override_id(substrate, monkeypatch):
    """B1 (override-consumed seam): driving ``agent_dispatch_workflow`` in-process with a
    pre-registered (agent_dispatch, execute_item) override for the run consumes it at the
    coordinator boundary and lands ONE run_control_intervention timeline row with the
    ``override_id`` COLUMN populated (the consumed row's id), ``action='override_consumed'``,
    and the IDs/enums-only envelope. Called as a plain function (no DBOS.start_workflow) so the
    seam runs synchronously; the specialist executor is stubbed (no LLM, no network)."""
    from orchestrator.agents import coordinator as coord
    from orchestrator.agents.coordinator import (
        AgentItemContext,
        ItemExecutionResult,
        _agent_run_id,
        agent_dispatch_workflow,
    )

    tenant = _tenant_brain_ready(substrate)
    with psycopg.connect(substrate, autocommit=True) as conn:
        work_item = str(
            conn.execute(
                "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
                "VALUES (%s, %s, 'sales_recovery', 'awaiting_approval') RETURNING id",
                (tenant, f"cust-{uuid4().hex[:8]}"),
            ).fetchone()[0]
        )
    run_id = _agent_run_id(work_item)  # the deterministic id this dispatch will open
    # a run-targeted override for execute_item (no pinned keys — the step allow-lists none;
    # the consumed row records the intervention, the override_id is the signal).
    override_id = _override(
        substrate, tenant, "agent_dispatch", "execute_item", workflow_id=run_id
    )

    class _StubAgent:
        name = "sales_recovery"

        def execute_item(self, ctx: AgentItemContext) -> ItemExecutionResult:
            return ItemExecutionResult(work_item_status="awaiting_approval")

    monkeypatch.setattr(coord, "get_registry", lambda: {"sales_recovery": _StubAgent()})

    out = agent_dispatch_workflow(tenant, "cust-x", "sales_recovery", work_item)
    assert out["status"] == "awaiting_approval", f"stubbed dispatch did not run cleanly: {out}"

    steps = _intervention_steps(substrate, run_id)
    assert len(steps) == 1, f"expected one run_control_intervention row, got {steps}"
    step_name, recorded_override_id, paused_ms, envelope = steps[0]
    assert step_name == "agent_dispatch:execute_item"
    assert recorded_override_id == override_id, "B1: the consumed override_id COLUMN must be set"
    assert paused_ms is None, "no pause here — paused_ms stays NULL on a pure override consume"
    assert envelope == {
        "action": "override_consumed",
        "workflow_kind": "agent_dispatch",
        "step_name": "execute_item",
    }


# ---------------------------------------------------------------------------
# T-A3 — §10.6 rerun SUCCESS legs: plan_deliver (fresh uuid4 + bitmap) and
#         agent_dispatch (NEW Fixer-A4 fresh run id ≠ uuid5(work_item))
# ---------------------------------------------------------------------------


def test_rerun_plan_deliver_fresh_run_lineage_source_untouched_no_dup_parts(
    substrate, monkeypatch
):
    """§10.6: rerun_from on plan_deliver mints a NEW uuid4 run row with
    rerun_of_run_id=source + rerun_from_step stamped, leaves the SOURCE row
    byte-identical, and sends NO duplicate parts — the delivered_parts bitmap (all bits
    set) suppresses every send. Closes ``status='completed'`` with
    final_outcome='completed' (Fixer-A2/A3 house pattern)."""
    from orchestrator.business_plan import store as plan_store
    from orchestrator.run_control.rerun import rerun_from

    # count any freeform send the delivery loop would make (lazy import inside deliver_plan).
    sends: list[str] = []
    import orchestrator.utils.twilio_send as twilio_mod

    monkeypatch.setattr(twilio_mod, "send_freeform_message", lambda body, recip: sends.append(body))

    # Unique phone per run (the tenants.whatsapp_number unique constraint + a recycled DB).
    tenant = _tenant_brain_ready(substrate, whatsapp=f"+1{uuid4().int % 10**10:010d}")
    source = _run_typed(substrate, tenant, "plan_deliver")
    source_before = _run_row(substrate, source)
    # empty roadmap → compose_parts = 2 parts (summary + hint); delivered_parts=3 (0b11)
    # marks BOTH delivered, so a re-delivery sends nothing.
    with psycopg.connect(substrate, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO business_plan (tenant_id, version, summary_json, roadmap_json, "
            "fact_bundle_json, generated_by, delivered_parts) "
            "VALUES (%s, 1, %s, '[]', '{}', 'test', 3)",
            (tenant, Jsonb({"text": "your plan headline"})),
        )

    result = rerun_from(source, "deliver_parts", requested_by=str(uuid4()))
    new_run = result.run_id

    assert result.outcome == "completed", "no approval armed → the C1 outcome is 'completed'"
    assert str(new_run) != source, "rerun must mint a FRESH run id, not reuse the source"
    with psycopg.connect(substrate, autocommit=True) as conn:
        new_row = conn.execute(
            "SELECT run_type, rerun_of_run_id::text, rerun_from_step, status, "
            "terminal_state_metadata FROM pipeline_runs WHERE id = %s",
            (str(new_run),),
        ).fetchone()
    assert new_row[0] == "plan_deliver"
    assert new_row[1] == source, "rerun_of_run_id must point at the source run"
    assert new_row[2] == "deliver_parts", "rerun_from_step must be stamped"
    assert new_row[3] == "completed", "Fixer-A2: rerun rows always close status='completed'"
    assert new_row[4]["final_outcome"] == "completed"
    assert _run_row(substrate, source) == source_before, "the SOURCE run row was mutated"
    assert sends == [], "the delivered_parts bitmap must suppress all (already-sent) parts"
    # the bitmap on the (single) version is untouched — no double-marking.
    plan = plan_store.get_active_plan(tenant)
    assert plan is not None and plan.delivered_parts == 3


def test_rerun_agent_dispatch_fresh_run_id_not_uuid5(substrate, monkeypatch):
    """§10.6 / Fixer-A4: rerun_from on agent_dispatch mints a FRESH uuid4 run id (NOT
    uuid5(work_item_id)), stamps lineage on THAT row, and passes it into
    agent_dispatch_workflow as the final ``rerun_run_id`` arg. The DBOS dispatch is
    intercepted (no LLM/executor); we assert the identity wiring, the lineage, and that
    the fresh id is NOT the deterministic uuid5 the normal path would use."""
    import dbos as _dbos

    from orchestrator.agents.coordinator import _agent_run_id
    from orchestrator.run_control.rerun import rerun_from

    captured: dict[str, Any] = {}

    def _capture_start(fn: Any, *args: Any, **_kw: Any) -> None:
        captured["fn"] = getattr(fn, "__name__", str(fn))
        captured["args"] = args
        return None

    monkeypatch.setattr(_dbos.DBOS, "start_workflow", staticmethod(_capture_start))

    tenant = _tenant_brain_ready(substrate)
    source = _run_typed(substrate, tenant, "agent_dispatch")
    with psycopg.connect(substrate, autocommit=True) as conn:
        work_item = str(
            conn.execute(
                "INSERT INTO agent_work_items (tenant_id, item_id, agent, run_id, status) "
                "VALUES (%s, %s, 'sales_recovery', %s, 'awaiting_approval') RETURNING id",
                (tenant, f"cust-{uuid4().hex[:8]}", source),
            ).fetchone()[0]
        )

    result = rerun_from(source, "execute_item", requested_by=str(uuid4()))
    new_run = result.run_id

    assert result.outcome == "completed", "no approval armed → the C1 outcome is 'completed'"
    assert captured["fn"] == "agent_dispatch_workflow"
    # signature: (tenant_id, item_id, agent, work_item_id, rerun_run_id)
    rerun_run_id_arg = captured["args"][-1]
    assert rerun_run_id_arg == str(new_run), "the fresh run id must be threaded as rerun_run_id"
    deterministic = _agent_run_id(work_item)
    assert str(new_run) != deterministic, (
        "Fixer-A4: the rerun id must be a FRESH uuid4, NOT uuid5(work_item_id)"
    )
    assert str(new_run) != source
    with psycopg.connect(substrate, autocommit=True) as conn:
        lineage = conn.execute(
            "SELECT run_type, rerun_of_run_id::text, rerun_from_step, status "
            "FROM pipeline_runs WHERE id = %s",
            (str(new_run),),
        ).fetchone()
    assert lineage == ("agent_dispatch", source, "execute_item", "running"), (
        "lineage must land on the FRESH row (not the source, not the uuid5)"
    )
    # the source run is the work-item's deterministic identity — never the rerun's.
    assert _run_row(substrate, source)[1] is None, "source run must carry no rerun lineage"


def test_open_agent_run_adopts_rerun_run_id_not_uuid5(substrate):
    """A4 (coordinator leg, direct): ``_open_agent_run(tenant, work_item, rerun_run_id=<uuid4>)``
    ADOPTS the passed id verbatim — it returns that id (NOT ``_agent_run_id(work_item)``), the
    pipeline_runs row exists under it, and NO row is minted under the deterministic uuid5. This
    is the unit-level proof of the Fixer-A4 adopt path the rerun threads through (the rerun.py
    pre-insert + ON CONFLICT DO NOTHING means the row may already exist; here we exercise the
    coordinator entry directly, with no pre-insert, so the INSERT lands)."""
    from orchestrator.agents.coordinator import _agent_run_id, _open_agent_run

    tenant = _tenant_brain_ready(substrate)
    with psycopg.connect(substrate, autocommit=True) as conn:
        work_item = str(
            conn.execute(
                "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
                "VALUES (%s, %s, 'sales_recovery', 'awaiting_approval') RETURNING id",
                (tenant, f"cust-{uuid4().hex[:8]}"),
            ).fetchone()[0]
        )
    rerun_run_id = str(uuid4())
    deterministic = _agent_run_id(work_item)
    assert rerun_run_id != deterministic  # sanity: the two identities are distinct

    returned = _open_agent_run(tenant, work_item, rerun_run_id=rerun_run_id)
    assert returned == rerun_run_id, "the adopted id must be the passed rerun_run_id, not uuid5"

    with psycopg.connect(substrate, autocommit=True) as conn:
        adopted = conn.execute(
            "SELECT run_type, status FROM pipeline_runs WHERE id = %s", (rerun_run_id,)
        ).fetchone()
        uuid5_row = conn.execute(
            "SELECT 1 FROM pipeline_runs WHERE id = %s", (deterministic,)
        ).fetchone()
    assert adopted == ("agent_dispatch", "running"), "the run row lands under the adopted id"
    assert uuid5_row is None, "A4: no second row minted under uuid5(work_item)"


# ---------------------------------------------------------------------------
# T-A4 — N2 recovery-idempotent consume DRIVEN THROUGH consume_override, with
#        the A5 ordering, including the post-expiry re-apply leg
# ---------------------------------------------------------------------------


def test_consume_reapply_survives_expiry_for_same_run_a5(substrate):
    """N2 + Fixer-A5: once a run has consumed an override, DBOS recovery re-entering the
    body with the SAME run_id re-applies the SAME override EVEN AFTER it has expired —
    the re-apply arm (``consumed_run_id = run_id``) is NOT expiry-gated (A5), so a worker
    restart minutes later still honours the pin. A DIFFERENT run, by contrast, can never
    inherit an expired pin (the unconsumed arm IS expiry-gated)."""
    from orchestrator.run_control import consume_override

    tenant = _tenant(substrate)
    run = str(uuid4())
    # next-run pin that has ALREADY expired but was consumed by `run` (the post-restart
    # shape: the consume txn committed, then time passed beyond expiry).
    past = datetime.now(UTC) - timedelta(minutes=5)
    override_id = _override(
        substrate,
        tenant,
        "agent_dispatch",
        "compose_drafts",
        expires_at=past,
        pinned_input={"model": "claude-test"},
        consumed_at=past,
        consumed_run_id=run,
    )
    with psycopg.connect(substrate, autocommit=True) as conn:
        reapplied = consume_override(
            conn,
            tenant_id=tenant,
            workflow_kind="agent_dispatch",
            step_name="compose_drafts",
            run_id=run,
        )
        # a DIFFERENT run gets nothing — an expired, consumed pin is not up for grabs.
        other = consume_override(
            conn,
            tenant_id=tenant,
            workflow_kind="agent_dispatch",
            step_name="compose_drafts",
            run_id=str(uuid4()),
        )
    assert reapplied is not None, "A5: the consuming run re-applies its pin even post-expiry"
    assert str(reapplied.id) == override_id
    assert reapplied.pinned_input == {"model": "claude-test"}
    assert other is None, "a different run must never inherit an expired consumed pin"


def test_consume_reapply_wins_ordering_over_earlier_fresh_pin_a5(substrate):
    """A5 ORDER BY mutation-killer: with BOTH a fresh unconsumed pin (EARLIER created_at) and an
    already-consumed-by-the-recovering-run pin for the same (tenant, kind, step), a re-consume by
    that run must return the CONSUMED row — the ``(consumed_run_id = run_id) DESC`` primary sort
    key beats the fresh pin's ``created_at ASC`` advantage. Drop that term and the earlier fresh
    pin would win and steal the slot; this test fails in that mutation. The fresh pin must stay
    UNCONSUMED (the recovery re-apply never touches it)."""
    from orchestrator.run_control import consume_override

    tenant = _tenant(substrate)
    run = str(uuid4())
    # fresh, unconsumed, LIVE pin — created FIRST, then back-dated so its created_at is strictly
    # earlier than the consumed pin's (makes the ORDER BY term load-bearing, not coincidental).
    fresh = _override(
        substrate, tenant, "agent_dispatch", "compose_drafts",
        expires_at=_future(), pinned_input={"model": "fresh-loser"},
    )
    with psycopg.connect(substrate, autocommit=True) as conn:
        conn.execute(
            "UPDATE step_overrides SET created_at = now() - interval '1 hour' WHERE id = %s",
            (fresh,),
        )
    # the recovering run's already-consumed pin — created LATER (default now()).
    consumed = _override(
        substrate, tenant, "agent_dispatch", "compose_drafts",
        expires_at=_future(), pinned_input={"model": "recovered-winner"},
        consumed_at=datetime.now(UTC), consumed_run_id=run,
    )

    with psycopg.connect(substrate, autocommit=True) as conn:
        reapplied = consume_override(
            conn, tenant_id=tenant, workflow_kind="agent_dispatch",
            step_name="compose_drafts", run_id=run,
        )
    assert reapplied is not None, "the recovering run must re-apply its consumed pin"
    assert str(reapplied.id) == consumed, (
        "A5: the consumed-by-this-run row wins ordering over the earlier fresh pin"
    )
    assert reapplied.pinned_input == {"model": "recovered-winner"}
    with psycopg.connect(substrate, autocommit=True) as conn:
        fresh_state = conn.execute(
            "SELECT consumed_at, consumed_run_id FROM step_overrides WHERE id = %s", (fresh,)
        ).fetchone()
    assert fresh_state == (None, None), "the fresh pin must stay unconsumed (re-apply never took it)"


_OVERRIDE_CONSUME_WORKER = Path(__file__).parent / "_override_consume_resume_worker.py"


def _consume_probe_ids(dsn: str, run_id: str) -> list[str | None]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT override_id FROM _vt374_consume_probe WHERE run_id = %s ORDER BY id",
            (run_id,),
        ).fetchall()
    return [r[0] for r in rows]


def test_consume_recovery_reapply_kill_and_recover_live(substrate):
    """N2 (live kill-and-recover): a DBOS workflow consumes a pre-registered override
    inside a step (commits), is SIGKILLed mid-run (workflow left PENDING), and on the
    second launch DBOS recovery RE-ENTERS the body — the step re-executes and re-consumes
    the SAME override for the SAME run_id (the A5 ``consumed_run_id = run_id`` arm). The
    probe table accrues one row per consume; both carry the same override id, and
    consumed_run_id stays bound to the recovering run.

    This is the genuine crash shape (process SIGKILL → PENDING → recovery), not an
    in-process raise (which would mark the workflow ERROR, not PENDING).
    """
    from dbos import DBOSClient

    with psycopg.connect(substrate, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _vt374_consume_probe ("
            "id serial PRIMARY KEY, run_id text, override_id text, at timestamptz DEFAULT now())"
        )

    tenant = _tenant(substrate)
    override_id = _override(
        substrate,
        tenant,
        "agent_dispatch",
        "compose_drafts",
        expires_at=_future(),
        pinned_input={"model": "claude-test"},
    )
    run_id = str(uuid4())
    workflow_id = f"vt374-n2-{run_id}"
    argv = [sys.executable, str(_OVERRIDE_CONSUME_WORKER), substrate, workflow_id, tenant, run_id]

    proc1 = subprocess.Popen(argv)
    try:
        deadline = time.time() + 45
        while time.time() < deadline and not _consume_probe_ids(substrate, run_id):
            time.sleep(1.0)
        first = _consume_probe_ids(substrate, run_id)
        assert first == [override_id], f"first consume did not claim the override (got {first})"
    finally:
        proc1.kill()
    proc1.wait(timeout=15)

    pending = {str(w.workflow_id) for w in DBOSClient(substrate).list_workflows(status="PENDING")}
    assert workflow_id in pending, "the crashed consume workflow was not left PENDING"

    proc2 = subprocess.Popen(argv)  # DBOS recovery re-enters the workflow body
    try:
        deadline = time.time() + 45
        while time.time() < deadline and len(_consume_probe_ids(substrate, run_id)) < 2:
            time.sleep(1.0)
        probed = _consume_probe_ids(substrate, run_id)
        assert len(probed) >= 2, f"recovery did not re-run the consume step (probed={probed})"
        assert probed[0] == override_id, "first attempt consumed the override"
        assert probed[1] == override_id, "N2: recovery re-applied the SAME override (same run)"
    finally:
        proc2.kill()
        proc2.wait(timeout=15)

    with psycopg.connect(substrate, autocommit=True) as conn:
        consumed_run = conn.execute(
            "SELECT consumed_run_id::text FROM step_overrides WHERE id = %s",
            (override_id,),
        ).fetchone()[0]
    assert consumed_run == run_id, "consumed_run_id stayed bound to the recovering run"


# ---------------------------------------------------------------------------
# T-A5 — supervisor N1 regression: campaign-send hold via check_pause
# ---------------------------------------------------------------------------


def test_supervisor_campaign_send_held_by_run_control(substrate):
    """N1 regression (mirrors the retired run_controls suite): an active
    ``campaign_send`` pause holds the supervisor fan-out node BEFORE any customer send —
    the node returns the ``held_by_run_control`` summary (same shape VT-300 returned, so
    downstream readers are unchanged). The hold is via ``check_pause`` on the new
    workflow_controls substrate (the run_controls table is dropped by mig-131)."""
    pytest.importorskip("langgraph")
    from orchestrator.supervisor import _campaign_execute_node

    tenant = _tenant(substrate)
    _pause(substrate, tenant, "campaign_send")
    state = {
        "tenant_id": tenant,
        "pending_approval_request": {"campaign_id": str(uuid4())},
    }
    out = _campaign_execute_node(state)
    summary = out["campaign_execution_summary"]
    assert summary["status"] == "held_by_run_control"
    assert summary["control_type"] == "pause"
    # control leg: with NO pause, the node proceeds past the hold (it then fails the
    # campaign lookup for the synthetic id — proving the hold, not the lookup, gates).
    _release_all(substrate, tenant)
    out2 = _campaign_execute_node(state)
    assert "campaign_execution_summary" not in out2 or out2.get(
        "campaign_execution_summary", {}
    ).get("status") != "held_by_run_control", "released tenant must not report held"
