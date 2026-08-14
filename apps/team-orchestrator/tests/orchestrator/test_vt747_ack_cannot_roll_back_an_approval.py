"""VT-747 — an ack must never roll back the owner's approval.

THE DEFECT, and why a `try/except` was never a fix for it:

`runner.try_resume_pending_approval` wraps the whole resolution in one `conn.transaction()`. Inside it,
`_ack_owner_stalled_campaign` ran a raw `SELECT owner_phone FROM tenants` on that same connection with
no SAVEPOINT. A SERVER-SIDE error there aborts the enclosing transaction. The function's fail-soft
`except` then swallows the Python exception — so it reported success while the owner's COMMIT silently
became a ROLLBACK. The owner said yes, was told nothing was wrong, and the decision was discarded.

**A try/except cannot make a statement fail-soft on a shared transaction. Only a SAVEPOINT can.** That
sentence is the whole row, and `test_the_hazard_is_real_*` below proves it against a live Postgres
rather than asserting it — because "the except handles it" is exactly the reasoning that let this ship.

The exit gate demands the failure be FORCED, not inferred from reading the handler. These tests force a
genuine server-side error by pointing the failing statement's `search_path` at `pg_temp`, so
`SELECT ... FROM tenants` raises `UndefinedTable` **inside the server**. Injecting a Python exception
through a mock would NOT reproduce this bug: a Python raise leaves the server-side transaction perfectly
healthy, so a mocked test passes with or without the savepoint. That distinction is the trap this file
exists to avoid.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-747 transaction-semantics tests skipped",
)


@pytest.fixture(scope="module")
def dsn():
    import apply_migrations

    url = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=url)
    assert not r["failed"], r["failed"]
    return url


def _new_tenant(conn) -> str:
    return str(
        conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('vt747 rollback probe', 'founding', 'trial') RETURNING id"
        ).fetchone()[0]
    )


# --- the hazard itself, proven against a real server --------------------------------------------


@pytest.mark.integration
def test_the_hazard_is_real_an_unsavepointed_failsoft_read_DISCARDS_the_committed_write(dsn):
    """FIRST prove the bug is real in Postgres semantics — otherwise the fix below proves nothing.

    This reproduces the ORIGINAL shape exactly: a load-bearing write, then a failing read on the same
    connection wrapped in a fail-soft `except`, then COMMIT. The `except` returns normally. The write
    is GONE. No exception ever reaches the caller."""
    with psycopg.connect(dsn, autocommit=True) as setup:
        tenant_id = _new_tenant(setup)

    swallowed = None
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE tenants SET business_name = 'THE OWNER SAID YES' WHERE id = %s",
                (tenant_id,),
            )
            try:
                # NO savepoint — the original code. A real server-side error, not a Python raise.
                conn.execute("SET LOCAL search_path TO pg_temp")
                conn.execute("SELECT owner_phone FROM tenants WHERE id = %s", (tenant_id,))
            except Exception as exc:  # noqa: BLE001 — the fail-soft handler, verbatim in spirit
                swallowed = type(exc).__name__

    assert swallowed is not None, "the failing read must have raised (else the injection is wrong)"

    with psycopg.connect(dsn, autocommit=True) as check:
        name = check.execute(
            "SELECT business_name FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()[0]

    assert name != "THE OWNER SAID YES", (
        "if this assertion ever fails, Postgres stopped aborting transactions on statement error and "
        "VT-747's premise is void — check that before 'fixing' anything"
    )
    assert name == "vt747 rollback probe", (
        "THE BUG: the write was silently discarded and the caller was told nothing. This is what "
        "happened to owner approvals."
    )


@pytest.mark.integration
def test_the_same_read_inside_a_SAVEPOINT_leaves_the_write_committed(dsn):
    """THE FIX. Identical failure, identical fail-soft except — one `with conn.transaction()` around
    the read — and the owner's write survives."""
    with psycopg.connect(dsn, autocommit=True) as setup:
        tenant_id = _new_tenant(setup)

    swallowed = None
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE tenants SET business_name = 'THE OWNER SAID YES' WHERE id = %s",
                (tenant_id,),
            )
            try:
                with conn.transaction():  # SAVEPOINT — the entire fix
                    conn.execute("SET LOCAL search_path TO pg_temp")
                    conn.execute("SELECT owner_phone FROM tenants WHERE id = %s", (tenant_id,))
            except Exception as exc:  # noqa: BLE001
                swallowed = type(exc).__name__

    assert swallowed is not None, "the read must still have failed — the savepoint scopes it, not hides it"

    with psycopg.connect(dsn, autocommit=True) as check:
        name = check.execute(
            "SELECT business_name FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()[0]

    assert name == "THE OWNER SAID YES", (
        "the owner's authoritative decision must commit even though every ack failed — VT-747's invariant"
    )


# --- the real functions, driven with a forced failure -------------------------------------------


@pytest.mark.integration
def test_the_real_ack_survives_a_forced_server_side_error_and_the_resolution_commits(dsn):
    """VT-747 exit gate (a) — on the REAL `_ack_owner_stalled_campaign`, not a reimplementation.

    The search_path is poisoned before the call, so the function's own `SELECT owner_phone FROM tenants`
    fails inside the server. Under the pre-fix code this aborted the transaction and the UPDATE below
    was lost; under the fix the savepoint contains it. `send_freeform_message` is never reached (the
    read fails first), so no transport is involved.
    """
    pytest.importorskip("langgraph")
    from orchestrator.agent import approval_resume

    with psycopg.connect(dsn, autocommit=True) as setup:
        tenant_id = _new_tenant(setup)

    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE tenants SET business_name = 'RESOLVED BY OWNER' WHERE id = %s",
                (tenant_id,),
            )
            conn.execute("SET LOCAL search_path TO pg_temp")
            # Must NOT raise: the function is fail-soft by contract.
            approval_resume._ack_owner_stalled_campaign(conn, tenant_id, reset=True)
            conn.execute("SET LOCAL search_path TO public")

    with psycopg.connect(dsn, autocommit=True) as check:
        name = check.execute(
            "SELECT business_name FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()[0]

    assert name == "RESOLVED BY OWNER", (
        "the ack failed server-side and the owner's resolution was still discarded — the savepoint is "
        "missing or the failure escaped it"
    )


def _drive_under_app_role(dsn: str, tenant_id: str, call) -> str:
    """Do the owner's write, poison the search_path, run `call(conn, tenant_id)`, commit; return the
    committed business_name.

    WHY `SET LOCAL ROLE app_role` IS LOAD-BEARING HERE, and not scenery. `db/base.py::_assert_app_role`
    rejects a non-`app_role` connection with a **Python** `TenantIsolationError` before any tenant SQL
    runs (VT-306 defence-in-depth). The first version of these two tests hit that guard, so the
    fail-soft `except` caught a Python exception, the server transaction was never poisoned, and both
    tests passed **with the savepoints removed** — they were proving nothing. Assuming a real
    `app_role` gets past the guard so the wrapper's SQL actually reaches the server and fails there.

    `SET LOCAL` for both the role and the search_path: transaction-scoped, so nothing leaks into the
    next test (a leaked role is a known cause of spurious full-suite failures in this repo).
    """
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE tenants SET business_name = 'RESOLVED BY OWNER' WHERE id = %s",
                (tenant_id,),
            )
            conn.execute("SET LOCAL ROLE app_role")
            conn.execute("SELECT set_config('app.current_tenant', %s, true)", (tenant_id,))
            conn.execute("SET LOCAL search_path TO pg_temp")
            call(conn, tenant_id)  # must NOT raise — fail-soft by contract
            conn.execute("SET LOCAL search_path TO public")
            conn.execute("RESET ROLE")

    with psycopg.connect(dsn, autocommit=True) as check:
        return check.execute(
            "SELECT business_name FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()[0]


@pytest.mark.integration
def test_the_wake_seam_also_survives_a_forced_server_side_error(dsn, caplog):
    """VT-747 exit gate (b) — the sibling the ROW DID NOT NAME.

    `_wake_waiting_workflow` was found by the scope-2 audit with the identical shape: a read on the
    shared resolve connection inside a fail-soft except. Without this the class would have been closed
    on one of its live instances.
    """
    pytest.importorskip("langgraph")
    from orchestrator.agent import approval_resume

    with psycopg.connect(dsn, autocommit=True) as setup:
        tenant_id = _new_tenant(setup)

    with caplog.at_level("WARNING"):
        name = _drive_under_app_role(
            dsn, tenant_id,
            lambda conn, tid: approval_resume._wake_waiting_workflow(conn, tid, uuid4()),
        )

    assert "UndefinedTable" in caplog.text or "does not exist" in caplog.text, (
        "the injection did not reach the SERVER — this test would then pass with or without the "
        "savepoint, which is exactly how its first version was worthless. See _drive_under_app_role."
    )
    assert name == "RESOLVED BY OWNER"


@pytest.mark.integration
def test_the_consumer_guarantee_also_survives_a_forced_server_side_error(dsn, caplog):
    """VT-747 exit gate (b), third instance. `_guarantee_campaign_consumer`'s docstring already claimed
    'FULLY FAIL-SOFT: the owner's authoritative resolution must never be unwound ... (Pillar 7)' — and
    its except could not deliver that. The claim in the docstring is now true."""
    pytest.importorskip("langgraph")
    from orchestrator.agent import approval_resume

    with psycopg.connect(dsn, autocommit=True) as setup:
        tenant_id = _new_tenant(setup)

    with caplog.at_level("WARNING"):
        name = _drive_under_app_role(
            dsn, tenant_id,
            lambda conn, tid: approval_resume._guarantee_campaign_consumer(
                conn, tid, uuid4(), "approved"
            ),
        )

    assert "UndefinedTable" in caplog.text or "does not exist" in caplog.text, (
        "the injection did not reach the SERVER — see _drive_under_app_role"
    )
    assert name == "RESOLVED BY OWNER"


@pytest.mark.integration
def test_the_savepoint_does_not_hide_a_SUCCESSFUL_acks_effects(dsn):
    """The fix must scope failures, not swallow successes. A savepoint that completes RELEASES into the
    enclosing transaction — so the documented 'the redrive commits atomically with the resolution'
    still holds. This pins that: a write made inside the savepoint is visible after the outer commit.

    Without this, 'savepoint everything' would be indistinguishable from 'discard everything', and the
    VT-668 consumer guarantee would have been quietly disabled by its own safety fix."""
    with psycopg.connect(dsn, autocommit=True) as setup:
        tenant_id = _new_tenant(setup)

    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            with conn.transaction():  # a savepoint that SUCCEEDS
                conn.execute(
                    "UPDATE tenants SET business_name = 'WRITTEN INSIDE THE SAVEPOINT' WHERE id = %s",
                    (tenant_id,),
                )

    with psycopg.connect(dsn, autocommit=True) as check:
        name = check.execute(
            "SELECT business_name FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()[0]

    assert name == "WRITTEN INSIDE THE SAVEPOINT", (
        "a released savepoint must keep its writes, or the consumer guarantee's redrive was silently "
        "turned into a no-op by the VT-747 fix"
    )
