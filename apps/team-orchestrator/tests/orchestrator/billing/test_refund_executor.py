"""VT-93 — refund execution + 30-day graceful exit.

Pure tests cover the graceful-exit predicate + idempotency key + input guard.
Integration tests (gated on DATABASE_URL) exercise the real-PG state machine:
idempotency, partial-failure, immutability trigger, DSR hard-delete bypass, and
cross-tenant RLS. Razorpay is a FAKE injected client — no vendor call.

apply_transition is a @DBOS.step (no DBOS context under a direct test call), so it
is monkeypatched with a fake that applies the real phase+refunded_at effect — the
same pattern test_trial_sweep uses. The refunded_at behaviour of the REAL
apply_transition is covered in the transitions test suite.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

from orchestrator.billing.graceful_exit import (  # noqa: E402
    GRACEFUL_EXIT_WINDOW,
    portal_access_allowed,
)
from orchestrator.billing.razorpay_refund import (  # noqa: E402
    CancelResult,
    RazorpayRefundError,
    RefundResult,
)
from orchestrator.billing.refund_executor import (  # noqa: E402
    _idem_key,
    execute_refund,
)


@pytest.fixture(autouse=True)
def _phone_salt(monkeypatch):
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "test-salt-vt93")


# ---------------------------------------------------------------------------
# Pure tests
# ---------------------------------------------------------------------------


def test_portal_access_allowed_non_refunded_always_true() -> None:
    assert portal_access_allowed("paid_active", None) is True
    assert portal_access_allowed("trial", None) is True


def test_portal_access_allowed_within_window() -> None:
    now = datetime(2026, 6, 5, tzinfo=timezone.utc)
    refunded = now - timedelta(days=10)
    assert portal_access_allowed("refunded", refunded, now) is True


def test_portal_access_revoked_after_window() -> None:
    now = datetime(2026, 6, 5, tzinfo=timezone.utc)
    refunded = now - (GRACEFUL_EXIT_WINDOW + timedelta(days=1))
    assert portal_access_allowed("refunded", refunded, now) is False


def test_portal_access_refunded_null_anchor_fails_closed() -> None:
    assert portal_access_allowed("refunded", None) is False


def test_idem_key_is_deterministic_and_step_scoped() -> None:
    t = uuid4()
    assert _idem_key(t, "day39_eligibility", "refund") == _idem_key(
        t, "day39_eligibility", "refund"
    )
    assert _idem_key(t, "day39_eligibility", "refund") != _idem_key(
        t, "day39_eligibility", "cancel"
    )


def test_execute_refund_rejects_bad_reason() -> None:
    with pytest.raises(ValueError):
        execute_refund(uuid4(), "not_a_reason")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRazorpay:
    def __init__(self, *, refund_ok=True, cancel_ok=True, refund_raises=False, cancel_raises=False):
        self.refund_ok = refund_ok
        self.cancel_ok = cancel_ok
        self.refund_raises = refund_raises
        self.cancel_raises = cancel_raises
        self.refund_calls = 0
        self.cancel_calls = 0

    def refund(self, *, amount_paise, idempotency_key, subscription_id):
        self.refund_calls += 1
        if self.refund_raises:
            raise RazorpayRefundError("boom-refund")
        return RefundResult(
            ok=self.refund_ok, refund_id="rfnd_fake_1", raw={"amount": amount_paise}
        )

    def cancel_subscription(self, subscription_id, *, idempotency_key):
        self.cancel_calls += 1
        if self.cancel_raises:
            raise RazorpayRefundError("boom-cancel")
        return CancelResult(ok=self.cancel_ok, raw={"sub": subscription_id})


# ---------------------------------------------------------------------------
# Integration — gated on DATABASE_URL
# ---------------------------------------------------------------------------


@pytest.fixture
def _dbpool():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        pytest.skip("DATABASE_URL not set; integration test requires real DB")
    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            db_url,
            min_size=1,
            max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    yield get_pool()


@pytest.fixture
def _patch_transition(monkeypatch):
    """Replace the @DBOS.step apply_transition with a fake that applies the real
    phase+refunded_at effect (so the canary asserts the graceful-exit anchor)."""

    def _fake(state, event, context):
        from orchestrator.graph import get_pool

        now = datetime.now(timezone.utc)
        with get_pool().connection() as conn:
            if event == "day39_refund_triggered":
                conn.execute(
                    "UPDATE tenants SET phase='refunded', refunded_at=%s WHERE id=%s",
                    (now, str(state["tenant_id"])),
                )
        return {**state, "phase": "refunded"}

    monkeypatch.setattr("orchestrator.transitions.apply_transition", _fake)


def _seed(pool, tenant_id: UUID, *, fees_paise: int) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, paid_conversion_at, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'paid_active', now() - interval '40 days', %s) "
            "ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"canary-refund-{tenant_id}", f"+9199{str(tenant_id.int)[:8]}"),
        )
        cur.execute(
            "INSERT INTO subscriptions (tenant_id, razorpay_subscription_id, status, started_at, cumulative_fees_paid_paise) "
            "VALUES (%s, %s, 'active', now() - interval '40 days', %s)",
            (str(tenant_id), f"sub_{tenant_id.hex[:12]}", fees_paise),
        )


@pytest.mark.integration
def test_refund_happy_path(_dbpool, _patch_transition) -> None:
    tenant = uuid4()
    _seed(_dbpool, tenant, fees_paise=250000)
    fake = _FakeRazorpay()

    out = execute_refund(tenant, "day39_eligibility", razorpay=fake)

    assert out.status == "completed"
    assert out.completed is True
    assert out.total_refund_paise == 250000
    assert fake.refund_calls == 1 and fake.cancel_calls == 1
    # phase flipped + graceful-exit anchored
    with _dbpool.connection() as conn:
        trow = conn.execute(
            "SELECT phase, refunded_at FROM tenants WHERE id=%s", (str(tenant),)
        ).fetchone()
    assert trow["phase"] == "refunded"
    assert trow["refunded_at"] is not None
    # templates fail-closed (null SID) -> notification_pending recorded
    with _dbpool.connection() as conn:
        rrow = conn.execute(
            "SELECT notification_pending, refund_responses FROM refund_executions WHERE tenant_id=%s",
            (str(tenant),),
        ).fetchone()
    assert rrow["notification_pending"] is True
    steps = [r["step"] for r in rrow["refund_responses"]]
    assert steps == ["refund", "cancel"]


@pytest.mark.integration
def test_refund_idempotent_no_double(_dbpool, _patch_transition) -> None:
    tenant = uuid4()
    _seed(_dbpool, tenant, fees_paise=100000)
    fake = _FakeRazorpay()

    first = execute_refund(tenant, "day39_eligibility", razorpay=fake)
    second = execute_refund(tenant, "day39_eligibility", razorpay=fake)

    assert first.status == "completed" and second.status == "completed"
    assert fake.refund_calls == 1  # second call short-circuits on completed
    assert fake.cancel_calls == 1


@pytest.mark.integration
def test_refund_partial_failed_halts(_dbpool, _patch_transition) -> None:
    tenant = uuid4()
    _seed(_dbpool, tenant, fees_paise=50000)
    fake = _FakeRazorpay(refund_raises=True)

    out = execute_refund(tenant, "day39_eligibility", razorpay=fake)

    assert out.status == "partial_failed"
    assert fake.cancel_calls == 0  # never attempted after the refund failure
    with _dbpool.connection() as conn:
        trow = conn.execute("SELECT phase FROM tenants WHERE id=%s", (str(tenant),)).fetchone()
    assert trow["phase"] == "paid_active"  # no transition on failure


@pytest.mark.integration
def test_refund_cancel_failure_pends(_dbpool, _patch_transition) -> None:
    tenant = uuid4()
    _seed(_dbpool, tenant, fees_paise=50000)
    fake = _FakeRazorpay(cancel_raises=True)

    out = execute_refund(tenant, "day39_eligibility", razorpay=fake)

    assert out.status == "pending_subscription_cancel"
    assert fake.refund_calls == 1 and fake.cancel_calls == 1


@pytest.mark.integration
def test_completed_row_is_immutable(_dbpool, _patch_transition) -> None:
    from orchestrator.db import tenant_connection

    tenant = uuid4()
    _seed(_dbpool, tenant, fees_paise=50000)
    execute_refund(tenant, "day39_eligibility", razorpay=_FakeRazorpay())

    with pytest.raises(Exception):
        with tenant_connection(tenant) as conn:
            conn.execute(
                "UPDATE refund_executions SET total_refund_paise=0 WHERE tenant_id=%s",
                (str(tenant),),
            )


@pytest.mark.integration
def test_dsr_hard_delete_bypasses_immutability(_dbpool, _patch_transition) -> None:
    """Completed row: a normal delete is blocked (immutable); the DSR purge
    session flag exempts the delete (right-to-erasure)."""
    from orchestrator.db import tenant_connection
    from orchestrator.dsr_purge import _PURGE_ORDER

    assert "refund_executions" in _PURGE_ORDER

    tenant = uuid4()
    _seed(_dbpool, tenant, fees_paise=50000)
    execute_refund(tenant, "day39_eligibility", razorpay=_FakeRazorpay())

    # normal delete blocked by the immutability trigger
    with pytest.raises(Exception):
        with tenant_connection(tenant) as conn:
            conn.execute("DELETE FROM refund_executions WHERE tenant_id=%s", (str(tenant),))

    # DSR purge session (flag set) deletes it
    with _dbpool.connection() as conn, conn.transaction():
        conn.execute("SET LOCAL orchestrator.dsr_purge_in_progress = 'on'")
        conn.execute("DELETE FROM refund_executions WHERE tenant_id=%s", (str(tenant),))
    with _dbpool.connection() as conn:
        gone = conn.execute(
            "SELECT count(*) AS n FROM refund_executions WHERE tenant_id=%s", (str(tenant),)
        ).fetchone()
    assert gone["n"] == 0


@pytest.mark.integration
def test_cross_tenant_isolation(_dbpool, _patch_transition) -> None:
    from orchestrator.db import tenant_connection

    tenant_a = uuid4()
    tenant_b = uuid4()
    _seed(_dbpool, tenant_a, fees_paise=50000)
    _seed(_dbpool, tenant_b, fees_paise=50000)
    execute_refund(tenant_a, "day39_eligibility", razorpay=_FakeRazorpay())

    # under tenant_b's RLS scope, tenant_a's refund row is invisible
    with tenant_connection(tenant_b) as conn:
        row = conn.execute(
            "SELECT * FROM refund_executions WHERE tenant_id=%s", (str(tenant_a),)
        ).fetchone()
    assert row is None


@pytest.mark.integration
def test_refund_resumes_after_cancel_failure_no_double_refund(_dbpool, _patch_transition) -> None:
    """A retry after a cancel failure must NOT re-refund — it resumes from the
    cancel step (the double-refund the adversarial review caught)."""
    tenant = uuid4()
    _seed(_dbpool, tenant, fees_paise=70000)

    first = execute_refund(tenant, "day39_eligibility", razorpay=_FakeRazorpay(cancel_raises=True))
    assert first.status == "pending_subscription_cancel"

    fake2 = _FakeRazorpay()  # cancel succeeds this time
    second = execute_refund(tenant, "day39_eligibility", razorpay=fake2)
    assert second.status == "completed"
    assert fake2.refund_calls == 0  # refund already succeeded — NOT re-called
    assert fake2.cancel_calls == 1  # cancel retried


@pytest.mark.integration
def test_retry_on_partial_failed_returns_no_recall(_dbpool, _patch_transition) -> None:
    """partial_failed is terminal for auto-retry: a retry returns the existing row
    and never re-attempts the refund (manual resolution only)."""
    tenant = uuid4()
    _seed(_dbpool, tenant, fees_paise=40000)
    execute_refund(tenant, "day39_eligibility", razorpay=_FakeRazorpay(refund_raises=True))

    fake2 = _FakeRazorpay()
    out = execute_refund(tenant, "day39_eligibility", razorpay=fake2)
    assert out.status == "partial_failed"
    assert fake2.refund_calls == 0  # never re-attempted


@pytest.mark.integration
def test_dsr_anonymize_retain_keeps_amount_scrubs_vendor(_dbpool, _patch_transition) -> None:
    """Anonymize-retain DSR mode keeps amount+date (tax/accounting) but scrubs the
    Razorpay vendor detail — the parameterized DSR path Cowork required."""
    from orchestrator.db import refund_executions as ledger

    tenant = uuid4()
    _seed(_dbpool, tenant, fees_paise=90000)
    execute_refund(tenant, "day39_eligibility", razorpay=_FakeRazorpay())

    # retain runs under the DSR flag (completed-row immutability exemption)
    with _dbpool.connection() as conn, conn.transaction():
        conn.execute("SET LOCAL orchestrator.dsr_purge_in_progress = 'on'")
        scrubbed = ledger.anonymize_retain(conn, tenant)
    assert scrubbed == 1
    with _dbpool.connection() as conn:
        row = conn.execute(
            "SELECT total_refund_paise, refund_responses FROM refund_executions WHERE tenant_id=%s",
            (str(tenant),),
        ).fetchone()
    assert row["total_refund_paise"] == 90000  # amount retained
    assert row["refund_responses"] == []  # vendor detail scrubbed
