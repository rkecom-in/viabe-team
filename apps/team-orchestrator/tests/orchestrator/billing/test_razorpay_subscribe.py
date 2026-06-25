"""VT-331 / VT-424 — Razorpay subscription creation (orchestrator-authoritative). Real-PG canary.

Keystones (Cowork plan-ack): idempotency BEFORE the vendor call + concurrency-safe (a
double-POST race creates EXACTLY ONE subscription / one vendor call — the VT-93-N1
lesson), and NO phase flip (conversion stays webhook-only).

VT-424 — the vendor call is now the REAL ``razorpay.subscription.create`` (was a ``sub_stub_*``
stub). Tests inject a STUB razorpay client (no network, no live key) via the injectable seam
(``_get_razorpay_client`` monkeypatched module-wide for the endpoint path; a ``client=`` arg for the
direct unit tests). The fake derives the subscription id from the Idempotency-Key header, so a
vendor RETRY with the SAME key returns the SAME id (models the VT-352 F2 orphan-avoidance) while a
new key → a new id. The plan ID comes from a TEST env var; LIVE keys/plan-IDs + the real-API canary
are NEEDS-FAZAL.
"""

from __future__ import annotations

import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")

import orchestrator.api.razorpay_subscribe as _subscribe_mod  # noqa: E402
from orchestrator.api.razorpay_subscribe import (  # noqa: E402
    RazorpaySubscribeBody,
    razorpay_subscribe,
)

_SECRET = "test-internal-secret-vt331"
# Capture the REAL client builder BEFORE the autouse fixture stubs it — fail-closed tests need the
# genuine env-reading builder, not the stub.
_REAL_GET_CLIENT = _subscribe_mod._get_razorpay_client


class _FakeSubscriptionResource:
    """Stub for ``client.subscription`` — records every create call (data + headers) and returns a
    real-shaped subscription whose id is DERIVED from the Idempotency-Key header, so the SAME key →
    the SAME id (a vendor retry is a no-op, modelling VT-352 F2) and a NEW key → a NEW id."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict, dict]] = []

    def create(self, data, headers=None):
        self.calls.append((data, headers or {}))
        key = (headers or {}).get("Idempotency-Key", "")
        digest = hashlib.sha256(key.encode()).hexdigest()[:12]
        tenant = data.get("notes", {}).get("tenant_id", "x")
        return {"id": f"sub_{digest}", "customer_id": f"cust_{tenant}"}


class _FakeRazorpayClient:
    def __init__(self) -> None:
        self.subscription = _FakeSubscriptionResource()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", _SECRET)
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "test-salt-vt331")
    monkeypatch.setenv("FOUNDING_RZP_PLAN_ID", "plan_test_founding")
    monkeypatch.setenv("STANDARD_RZP_PLAN_ID", "plan_test_standard")
    # PRO_RZP_PLAN_ID intentionally unset -> NEEDS-FAZAL 503 path.
    # VT-424: keys present so the live path resolves a client, but it's the STUB — no network.
    monkeypatch.setenv("TEAM_RAZORPAY_KEY_ID", "rzp_test_keyid")
    monkeypatch.setenv("TEAM_RAZORPAY_KEY_SECRET", "rzp_test_secret")
    # Inject the stub client into the endpoint path (no SDK import, no network).
    import orchestrator.api.razorpay_subscribe as mod

    monkeypatch.setattr(mod, "_get_razorpay_client", _FakeRazorpayClient)


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
            db_url, min_size=1, max_size=6,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return get_pool()


def _seed(pool, tid: UUID, *, phase: str = "trial") -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', %s) ON CONFLICT (id) DO NOTHING",
            (str(tid), f"vt331-{tid}", phase),
        )


def _rows(pool, tid: UUID) -> list[dict]:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT razorpay_subscription_id AS sid, razorpay_customer_id AS cid, "
            "razorpay_plan_id AS pid FROM subscriptions WHERE tenant_id=%s",
            (str(tid),),
        ).fetchall()


def _phase(pool, tid: UUID) -> str:
    with pool.connection() as conn:
        return conn.execute("SELECT phase FROM tenants WHERE id=%s", (str(tid),)).fetchone()[
            "phase"
        ]


def _post(tenant_id, plan_tier, secret=_SECRET):
    return razorpay_subscribe(
        RazorpaySubscribeBody(tenant_id=str(tenant_id), plan_tier=plan_tier),
        x_internal_secret=secret,
    )


def test_bad_secret_403() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _post(uuid4(), "founding", secret="wrong")
    assert exc.value.status_code == 403


def test_unknown_plan_400() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _post(uuid4(), "enterprise")
    assert exc.value.status_code == 400


def test_unconfigured_plan_id_503() -> None:
    """PRO_RZP_PLAN_ID unset -> 503 (NEEDS-FAZAL), not a 500."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _post(uuid4(), "pro")
    assert exc.value.status_code == 503


@pytest.mark.integration
def test_keys_absent_endpoint_503_no_row(_dbpool, monkeypatch) -> None:
    """VT-424 fail-closed at the HTTP boundary: live keys absent → the endpoint 503s (NEEDS-FAZAL),
    the txn rolls back, and NO subscriptions row is bound (no stub fallback)."""
    from fastapi import HTTPException

    monkeypatch.delenv("TEAM_RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("TEAM_RAZORPAY_KEY_SECRET", raising=False)
    # Restore the REAL client builder so it actually checks for keys (the autouse fixture stubbed it).
    monkeypatch.setattr(_subscribe_mod, "_get_razorpay_client", _REAL_GET_CLIENT)

    tid = uuid4()
    _seed(_dbpool, tid, phase="trial")
    with pytest.raises(HTTPException) as exc:
        _post(tid, "founding")
    assert exc.value.status_code == 503
    assert len(_rows(_dbpool, tid)) == 0  # no row bound — fail-closed, no stub


@pytest.mark.integration
def test_create_binds_subscription_no_phase_flip(_dbpool) -> None:
    tid = uuid4()
    _seed(_dbpool, tid, phase="trial")
    out = _post(tid, "founding")
    assert out["status"] == "created"
    rows = _rows(_dbpool, tid)
    assert len(rows) == 1
    # VT-424: a REAL-shaped vendor id (sub_<hash>), NOT the retired sub_stub_* form.
    assert rows[0]["sid"].startswith("sub_") and not rows[0]["sid"].startswith("sub_stub_")
    assert rows[0]["cid"] == f"cust_{tid}"
    assert rows[0]["pid"] == "plan_test_founding"
    # NO phase flip — conversion is webhook-only (VT-89 payment.captured).
    assert _phase(_dbpool, tid) == "trial"


@pytest.mark.integration
def test_idempotent_repost_no_duplicate(_dbpool) -> None:
    tid = uuid4()
    _seed(_dbpool, tid)
    a = _post(tid, "founding")
    b = _post(tid, "founding")  # re-POST
    assert a["status"] == "created" and b["status"] == "exists"
    assert a["razorpay_subscription_id"] == b["razorpay_subscription_id"]
    assert len(_rows(_dbpool, tid)) == 1  # NOT 2


@pytest.mark.integration
def test_resubscribe_after_cancel(_dbpool) -> None:
    """A CANCELLED subscription must NOT block a new one (the status='active' check +
    unique stub IDs + the partial one-active-per-tenant index — VT-331 review)."""
    tid = uuid4()
    _seed(_dbpool, tid)
    first = _post(tid, "founding")
    assert first["status"] == "created"
    with _dbpool.connection() as conn:
        conn.execute(
            "UPDATE subscriptions SET status='cancelled' WHERE tenant_id=%s", (str(tid),)
        )
    second = _post(tid, "standard")
    assert second["status"] == "created"  # NOT blocked by the cancelled row
    assert second["razorpay_subscription_id"] != first["razorpay_subscription_id"]
    with _dbpool.connection() as conn:
        active = conn.execute(
            "SELECT count(*) AS n FROM subscriptions WHERE tenant_id=%s AND status='active'",
            (str(tid),),
        ).fetchone()["n"]
    assert active == 1  # exactly one active (the partial unique index holds)


@pytest.mark.integration
def test_concurrent_create_exactly_one_vendor_call(_dbpool, monkeypatch) -> None:
    """KEYSTONE (VT-93-N1): two concurrent creates for one tenant -> EXACTLY ONE
    subscription + one vendor call (the advisory lock + before-vendor check serialize)."""
    import orchestrator.api.razorpay_subscribe as mod

    calls: list[str] = []
    real = mod._create_razorpay_subscription

    def _counting(plan, tenant_id, idempotency_key):  # VT-352: stub now takes the idem-key
        calls.append(tenant_id)
        time.sleep(0.05)  # widen the race window so both threads contend for the lock
        return real(plan, tenant_id, idempotency_key)

    monkeypatch.setattr(mod, "_create_razorpay_subscription", _counting)

    tid = uuid4()
    _seed(_dbpool, tid)
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = [f.result() for f in [ex.submit(_post, tid, "founding") for _ in range(2)]]

    statuses = sorted(r["status"] for r in results)
    assert statuses == ["created", "exists"]  # one created, one saw the existing
    assert len(_rows(_dbpool, tid)) == 1  # exactly one subscription
    assert calls.count(str(tid)) == 1  # exactly one vendor call


@pytest.mark.integration
def test_cross_tenant_isolation(_dbpool) -> None:
    a, b = uuid4(), uuid4()
    _seed(_dbpool, a)
    _seed(_dbpool, b)
    _post(a, "founding")
    assert len(_rows(_dbpool, a)) == 1
    assert len(_rows(_dbpool, b)) == 0  # tenant_b untouched


# --------------------------------------------------------------------------- #
# VT-332 — trial-end token single-use consumption
# --------------------------------------------------------------------------- #
def _post_token(tenant_id, plan_tier, jti, secret=_SECRET):
    return razorpay_subscribe(
        RazorpaySubscribeBody(tenant_id=str(tenant_id), plan_tier=plan_tier, jti=jti),
        x_internal_secret=secret,
    )


def _consumed(pool, jti) -> int:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM consumed_subscribe_tokens WHERE jti = %s", (jti,)
        ).fetchone()["n"]


def test_trial_end_jti_first_use_then_replay_403(_dbpool) -> None:
    """VT-332 KEYSTONE: first use of a jti subscribes once + consumes the jti; a replay (the SAME
    jti) → 403 and NO second subscription (the replay never reaches the vendor)."""
    from fastapi import HTTPException

    tid = uuid4()
    _seed(_dbpool, tid, phase="trial")
    jti = f"jti-{uuid4().hex}"

    out = _post_token(tid, "standard", jti)
    assert out["status"] == "created"
    assert _consumed(_dbpool, jti) == 1
    assert len(_rows(_dbpool, tid)) == 1

    with pytest.raises(HTTPException) as exc:
        _post_token(tid, "standard", jti)  # REPLAY
    assert exc.value.status_code == 403
    assert len(_rows(_dbpool, tid)) == 1  # STILL one — replay created no 2nd subscription


def test_subscribe_without_jti_unaffected(_dbpool) -> None:
    """The in-app path (no token → no jti) still creates — single-use only gates the token path."""
    tid = uuid4()
    _seed(_dbpool, tid, phase="trial")
    out = _post(tid, "standard")
    assert out["status"] == "created"
    assert len(_rows(_dbpool, tid)) == 1


def test_trial_end_jti_rolled_back_when_subscribe_fails(_dbpool) -> None:
    """The consume shares the subscribe txn: an UNKNOWN plan (400, after the consume line is
    reached? no — plan resolves before the txn) — here we assert a failed create does not
    strand a consumed jti. Use an unconfigured plan (503 BEFORE the txn) to prove the jti is
    NOT consumed when the subscribe never runs."""
    from fastapi import HTTPException

    tid = uuid4()
    _seed(_dbpool, tid, phase="trial")
    jti = f"jti-{uuid4().hex}"
    with pytest.raises(HTTPException) as exc:
        _post_token(tid, "pro", jti)  # PRO_RZP_PLAN_ID unset → 503 before the txn
    assert exc.value.status_code == 503
    assert _consumed(_dbpool, jti) == 0  # jti NOT consumed — the token stays usable on a real retry


# --- VT-424 — REAL razorpay.subscription.create (replaces sub_stub_*) unit tests ----------------
# These call _create_razorpay_subscription directly with an INJECTED stub client (the seam) — no
# network, no live key, no SDK import. Pure (non-integration) so they run in dep-less smoke.
def test_create_calls_vendor_with_plan_id_customer_and_idempotency_header() -> None:
    """VT-424 happy: the real call hits the vendor ONCE with the resolved plan_id, the per-attempt
    Idempotency-Key header, tenant binding in notes, total_count + quantity — and returns the REAL
    subscription id (NOT a sub_stub_*)."""
    from orchestrator.api.razorpay_subscribe import _IDEMPOTENCY_HEADER, _create_razorpay_subscription
    from orchestrator.billing.plans import resolve_plan

    plan = resolve_plan("founding")  # plan_test_founding, total_count 120 (config)
    client = _FakeRazorpayClient()
    tid = str(uuid4())
    key = f"subscribe:{tid}:jti-abc"

    out = _create_razorpay_subscription(plan, tid, key, client=client)

    assert len(client.subscription.calls) == 1  # exactly one vendor create
    data, headers = client.subscription.calls[0]
    assert data["plan_id"] == "plan_test_founding"
    assert data["total_count"] == 120 and data["quantity"] == 1
    assert data["notes"]["tenant_id"] == tid  # customer/tenant binding at the vendor
    assert headers == {_IDEMPOTENCY_HEADER: key}  # the Idempotency-Key header is sent
    assert out["subscription_id"].startswith("sub_")
    assert not out["subscription_id"].startswith("sub_stub_")  # the stub is gone


def test_idempotency_key_same_key_same_sub() -> None:
    """VT-352 F2 / VT-424: the SAME Idempotency-Key → the SAME header sent every retry, so Razorpay
    (if it honours it) returns the SAME subscription — a retry after a commit-after-vendor failure
    does NOT create an orphan; a NEW key (new authorized attempt) → a different header → a NEW
    subscription (a re-subscribe after cancel can't collide on the UNIQUE)."""
    from orchestrator.api.razorpay_subscribe import _IDEMPOTENCY_HEADER, _create_razorpay_subscription
    from orchestrator.billing.plans import resolve_plan

    plan = resolve_plan("founding")
    client = _FakeRazorpayClient()
    tid = str(uuid4())
    key = f"subscribe:{tid}:jti-abc"
    a = _create_razorpay_subscription(plan, tid, key, client=client)
    b = _create_razorpay_subscription(plan, tid, key, client=client)  # retry — SAME key
    # The SAME Idempotency-Key header is sent on the retry (so the vendor would dedupe).
    assert client.subscription.calls[0][1] == client.subscription.calls[1][1] == {
        _IDEMPOTENCY_HEADER: key
    }
    assert a["subscription_id"] == b["subscription_id"]  # SAME sub → no vendor orphan
    c = _create_razorpay_subscription(
        plan, tid, f"subscribe:{tid}:jti-xyz", client=client
    )  # new attempt → new key
    assert client.subscription.calls[2][1] == {_IDEMPOTENCY_HEADER: f"subscribe:{tid}:jti-xyz"}
    assert c["subscription_id"] != a["subscription_id"]  # new key → new sub


def test_keys_absent_fail_closed_raises_keys_not_configured(monkeypatch) -> None:
    """VT-424 fail-closed: with live keys absent, the live client builder raises
    RazorpayKeysNotConfiguredError (→503 at the endpoint) — never a stub fallback. Importantly it
    raises BEFORE importing the SDK, so the dep is genuinely lazy/NEEDS-FAZAL."""
    from orchestrator.api.razorpay_subscribe import RazorpayKeysNotConfiguredError

    monkeypatch.delenv("TEAM_RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("TEAM_RAZORPAY_KEY_SECRET", raising=False)
    # _REAL_GET_CLIENT is the genuine env-reading builder (captured before the autouse stub).
    with pytest.raises(RazorpayKeysNotConfiguredError):
        _REAL_GET_CLIENT()


def test_vendor_error_surfaced_no_partial_state() -> None:
    """VT-424 error: if the razorpay client raises, the error surfaces cleanly (no stub fallback,
    no malformed return) — the caller's txn rolls back, leaving no partial state."""
    from orchestrator.api.razorpay_subscribe import _create_razorpay_subscription
    from orchestrator.billing.plans import resolve_plan

    class _RaisingClient:
        class subscription:
            @staticmethod
            def create(data, headers=None):
                raise RuntimeError("simulated razorpay 5xx")

    plan = resolve_plan("founding")
    with pytest.raises(RuntimeError, match="simulated razorpay 5xx"):
        _create_razorpay_subscription(plan, str(uuid4()), "k", client=_RaisingClient())


def test_vendor_empty_id_surfaced_as_clean_error() -> None:
    """VT-424: a malformed vendor response (no subscription id) is a clean RuntimeError, NOT a row
    bound to an empty id."""
    from orchestrator.api.razorpay_subscribe import _create_razorpay_subscription
    from orchestrator.billing.plans import resolve_plan

    class _EmptyClient:
        class subscription:
            @staticmethod
            def create(data, headers=None):
                return {"id": None}

    plan = resolve_plan("founding")
    with pytest.raises(RuntimeError, match="no subscription id"):
        _create_razorpay_subscription(plan, str(uuid4()), "k", client=_EmptyClient())


@pytest.mark.integration
def test_reconcile_detects_orphan_no_autocancel(_dbpool, monkeypatch) -> None:
    """VT-352 F2 DETECT-ONLY: a vendor subscription with no DB row is flagged to Fazal; the known
    one is left alone; NO auto-cancel (Cowork: unattended money actions create new incidents)."""
    alerts: list[str] = []
    # VT-365 removed refund_executor; reconcile_subscription_orphans alerts via
    # orchestrator.alerts.clients.alert_fazal (lazily imported inside the fn).
    monkeypatch.setattr(
        "orchestrator.alerts.clients.alert_fazal", lambda m: alerts.append(m)
    )
    from orchestrator.api.razorpay_subscribe import reconcile_subscription_orphans

    tid = uuid4()
    _seed(_dbpool, tid, phase="trial")
    known = _post(tid, "founding")["razorpay_subscription_id"]  # a real bound subscription

    orphans = reconcile_subscription_orphans([known, "sub_orphan_xyz"])
    assert orphans == ["sub_orphan_xyz"]  # only the vendor sub with no DB row
    assert alerts and "sub_orphan_xyz" in alerts[0]  # Fazal alerted, names the orphan


@pytest.mark.integration
def test_commit_after_vendor_retry_no_orphan(_dbpool, monkeypatch) -> None:
    """VT-352 F3 (Cowork bounce): vendor create succeeds but the txn fails ONCE; the retry (same
    jti → same Idempotency-Key) returns the SAME vendor sub id → EXACTLY ONE subscriptions row, no
    orphan. Two stub calls, identical key."""
    import orchestrator.api.razorpay_subscribe as mod

    real = mod._create_razorpay_subscription
    calls: list[str] = []
    state = {"fail_next": True}

    def _flaky(plan, tenant_id, idempotency_key):
        calls.append(idempotency_key)
        result = real(plan, tenant_id, idempotency_key)  # deterministic id from the key
        if state["fail_next"]:
            state["fail_next"] = False
            raise RuntimeError("simulated commit-after-vendor failure")
        return result

    monkeypatch.setattr(mod, "_create_razorpay_subscription", _flaky)
    tid = uuid4()
    _seed(_dbpool, tid, phase="trial")
    jti = f"jti-retry-{tid.hex[:8]}"

    def _post_jti():
        return mod.razorpay_subscribe(
            RazorpaySubscribeBody(tenant_id=str(tid), plan_tier="founding", jti=jti),
            x_internal_secret=_SECRET,
        )

    # attempt 1 — vendor "succeeds" then the txn fails → rollback (no row; the jti consume rolls
    # back too, so the token is reusable).
    with pytest.raises(RuntimeError):
        _post_jti()
    assert len(_rows(_dbpool, tid)) == 0

    # attempt 2 — same jti → same Idempotency-Key → same vendor sub id → exactly ONE row.
    out = _post_jti()
    assert out["status"] == "created"
    assert len(_rows(_dbpool, tid)) == 1
    assert len(calls) == 2 and calls[0] == calls[1]  # same key both attempts (no orphan at vendor)
