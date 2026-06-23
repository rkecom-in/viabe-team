"""VT-396 — tests for the DEV-ONLY Sales-Recovery e2e harness.

Substrate tests apply migrations + launch DBOS (so the pool / tenant_connection exist), then drive
the harness end-to-end: seed → detect → STOP/opt-out. Unit tests cover the prod-deny guard and the
``team_winback_simple``-only invariant without a DB. The whole module skips in the dep-less CI job
via the importorskip guards (the depless-smoke-import-trap)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after dependency skip guards

from orchestrator.agents import sales_recovery_executor as sre  # noqa: E402
from orchestrator.harness import vt396_sr_harness as harness  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-396 harness substrate tests skipped",
)

_DEV_VERSION = "dev-test-v0"


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so the harness's pool / tenant_connection exist."""
    import apply_migrations

    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt396-test-salt")
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


def _new_tenant(dsn: str, *, name: str = "VT-396 Sundaram harness") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, whatsapp_number, owner_inputs) "
            "VALUES (%s, 'founding', 'trial', now(), 'restaurant', %s, true) RETURNING id",
            (name, f"+9198{uuid4().int % 10**8:08d}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _synthetic_phone() -> str:
    return f"+9197{uuid4().int % 10**7:07d}"


# --- prod-deny guard (unit, no DB) --------------------------------------------------------------


class _FakeCur:
    def __init__(self, row: object) -> None:
        self._row = row

    def fetchone(self) -> object:
        return self._row


class _FakeConn:
    def __init__(self, row: object) -> None:
        self._row = row

    def execute(self, *_a: object, **_k: object) -> _FakeCur:
        return _FakeCur(self._row)


@pytest.mark.parametrize("name", ["prod", "production"])
def test_assert_not_prod_refuses_production(name: str) -> None:
    with pytest.raises(RuntimeError, match="DEV-ONLY"):
        harness.assert_not_prod(_FakeConn({"name": name}))


def test_assert_not_prod_allows_dev_and_unknown() -> None:
    assert harness.assert_not_prod(_FakeConn({"name": "dev"})) == "dev"
    assert harness.assert_not_prod(_FakeConn({"name": "test"})) == "test"
    # Missing sentinel row → 'unknown' → allowed (only an explicit prod name is refused).
    assert harness.assert_not_prod(_FakeConn(None)) == "unknown"


def test_harness_uses_winback_simple_only() -> None:
    """The harness must drive the non-money template only; the money-bearing offer never appears."""
    assert harness.WINBACK_TEMPLATE_NAME == "team_winback_simple"
    src = Path(harness.__file__).read_text(encoding="utf-8")
    assert "team_winback_offer" not in src


# --- seed → detect → STOP (substrate) -----------------------------------------------------------


@requires_db
def test_seed_then_detect_returns_synthetic(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """Seeding a synthetic consented lapsed customer makes lapsed-detection return exactly it —
    once the (monkeypatched dev) allowlist admits the seeded consent version."""
    monkeypatch.setattr(sre, "MARKETING_CONSENT_VERSIONS", frozenset({_DEV_VERSION}))
    tenant = _new_tenant(substrate.dsn)
    phone = _synthetic_phone()

    seed = harness.seed_synthetic_customer(tenant, phone, consent_version=_DEV_VERSION)
    assert seed.phone_token.startswith("phone_tok_")
    assert not seed.reused_existing_customer

    candidates = harness.detect_for_tenant(tenant)
    assert [c.customer_id for c in candidates] == [seed.customer_id]


@requires_db
def test_detect_is_empty_when_allowlist_empty(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """Fail-closed: with the allowlist empty (the prod default), the SAME seeded base yields zero —
    proving detection can never fire on prod (where the env is unset)."""
    monkeypatch.setattr(sre, "MARKETING_CONSENT_VERSIONS", frozenset())
    tenant = _new_tenant(substrate.dsn)
    harness.seed_synthetic_customer(tenant, _synthetic_phone(), consent_version=_DEV_VERSION)
    assert harness.detect_for_tenant(tenant) == []


@requires_db
def test_optout_removes_from_detection(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """§5 floor: a STOP (opt-out) on the synthetic number drops it from detection."""
    monkeypatch.setattr(sre, "MARKETING_CONSENT_VERSIONS", frozenset({_DEV_VERSION}))
    tenant = _new_tenant(substrate.dsn)
    phone = _synthetic_phone()
    harness.seed_synthetic_customer(tenant, phone, consent_version=_DEV_VERSION)
    assert len(harness.detect_for_tenant(tenant)) == 1

    assert harness.simulate_customer_stop(tenant, phone) is True
    assert harness.detect_for_tenant(tenant) == []


@requires_db
def test_seed_is_idempotent(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """Re-seeding the same (tenant, phone) reuses the customer + skips duplicate sales — detection
    still returns exactly one candidate."""
    monkeypatch.setattr(sre, "MARKETING_CONSENT_VERSIONS", frozenset({_DEV_VERSION}))
    tenant = _new_tenant(substrate.dsn)
    phone = _synthetic_phone()

    first = harness.seed_synthetic_customer(tenant, phone, consent_version=_DEV_VERSION)
    second = harness.seed_synthetic_customer(tenant, phone, consent_version=_DEV_VERSION)
    assert second.customer_id == first.customer_id
    assert second.reused_existing_customer is True
    assert len(harness.detect_for_tenant(tenant)) == 1
