"""VT-281 — app_vtr_role reads ONLY the de-identified views; raw PII is UNREACHABLE. Real-PG canary.

Synthetic data only (CL-422 — no real customer PII on dev). Fail-not-skip when DATABASE_URL is set
(Rule #15). Proves Fork A's guarantee at the DB layer, not app-side: the VTR role is DENIED on every
PII-bearing table + the decrypt fn, CAN read the de-identified views, and the REF# is a stable KEYED
hash (not the raw id, not a bare hash).
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("psycopg")
import psycopg  # noqa: E402
from psycopg import errors as pg_errors  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-281 VTR substrate tests skipped",
)

_REF_KEY = "vt281-canary-hmac-key"
# Every PII-bearing / decrypt-capable object the VTR role must NOT be able to touch.
_FORBIDDEN_TABLES = ("customers", "phone_token_resolutions", "vtr_ref_secret")


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        # Seed the REF# keying secret (as the owner — app_vtr_role can't reach this table).
        conn.execute(
            "INSERT INTO vtr_ref_secret (id, secret) VALUES (true, %s) "
            "ON CONFLICT (id) DO UPDATE SET secret = EXCLUDED.secret",
            (_REF_KEY,),
        )
    return dsn


def _seed_customer(dsn: str) -> tuple[str, str]:
    """Insert a synthetic tenant + customer (WITH PII columns set, to prove they're unreachable)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        tid = str(
            conn.execute(
                "INSERT INTO tenants (business_name, plan_tier, phase) "
                "VALUES ('VT-281 test', 'founding', 'paid_active') RETURNING id"
            ).fetchone()[0]
        )
        cid = str(
            conn.execute(
                "INSERT INTO customers (tenant_id, display_name, email, opt_out_status) "
                "VALUES (%s, 'Synthetic Name', 'synthetic@example.com', 'subscribed') RETURNING id",
                (tid,),
            ).fetchone()[0]
        )
    return tid, cid


def test_vtr_role_denied_on_every_pii_table(substrate) -> None:
    """Grant hygiene (sharpening #3): app_vtr_role has NO SELECT on any PII-bearing table — proven
    BOTH by has_table_privilege (catches PUBLIC / default-privilege leakage) AND a direct probe."""
    with psycopg.connect(substrate, autocommit=True) as conn:
        for tbl in _FORBIDDEN_TABLES:
            has = conn.execute(
                "SELECT has_table_privilege('app_vtr_role', %s, 'SELECT')", (tbl,)
            ).fetchone()[0]
            assert has is False, f"app_vtr_role unexpectedly has SELECT on {tbl}"
        # Direct denied-probe (defence-in-depth: the privilege check AND the real attempt).
        for tbl in _FORBIDDEN_TABLES:
            with conn.cursor() as cur:
                cur.execute("SET ROLE app_vtr_role")
                with pytest.raises(pg_errors.InsufficientPrivilege):
                    cur.execute(f"SELECT 1 FROM {tbl} LIMIT 1")  # noqa: S608 — fixed allowlist
                cur.execute("ROLLBACK")
                cur.execute("RESET ROLE")


def test_vtr_role_cannot_decrypt(substrate) -> None:
    """app_vtr_role cannot EXECUTE the audited decrypt fn — a VTR decrypt is impossible, not merely
    audited (the OWNER path keeps the existing audited resolve)."""
    with psycopg.connect(substrate, autocommit=True) as conn:
        has = conn.execute(
            "SELECT has_function_privilege('app_vtr_role', "
            "'resolve_phone_token_audited(text, text)', 'EXECUTE')"
        ).fetchone()[0]
        assert has is False


def test_vtr_view_is_deidentified(substrate) -> None:
    """app_vtr_role CAN read vtr_customers, and the view exposes NO PII column (no display_name /
    email) — only the keyed REF# + business fields."""
    tid, cid = _seed_customer(substrate)
    with psycopg.connect(substrate, autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SET ROLE app_vtr_role")
            cur.execute(
                "SELECT * FROM vtr_customers WHERE tenant_id = %s", (tid,)
            )
            row = cur.fetchone()
            cur.execute("RESET ROLE")
    assert row is not None
    assert "display_name" not in row and "email" not in row  # PII columns absent from the view
    assert row["customer_ref"] and row["customer_ref"] != cid  # a ref, NOT the raw id
    assert row["opt_out_status"] == "subscribed"  # business field present


def test_ref_is_keyed_and_stable(substrate) -> None:
    """REF# = HMAC(customer_id, secret): stable for the same id, distinct across ids, and matches an
    independent HMAC with the same key (proves it's keyed, not a bare/guessable hash)."""
    tid, cid = _seed_customer(substrate)
    _, cid2 = _seed_customer(substrate)
    with psycopg.connect(substrate, autocommit=True) as conn:
        ref1 = conn.execute(
            "SELECT encode(hmac(%s, %s, 'sha256'), 'hex')", (cid, _REF_KEY)
        ).fetchone()[0]
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET ROLE app_vtr_role")
            cur.execute("SELECT customer_ref FROM vtr_customers WHERE tenant_id = %s", (tid,))
            view_ref = cur.fetchone()["customer_ref"]
            cur.execute("RESET ROLE")
    assert view_ref == ref1  # the view's ref IS HMAC(id, key) — keyed
    # a different customer id → different ref
    ref2 = psycopg.connect(substrate, autocommit=True).execute(
        "SELECT encode(hmac(%s, %s, 'sha256'), 'hex')", (cid2, _REF_KEY)
    ).fetchone()[0]
    assert ref1 != ref2


def test_bootstrap_secret_requires_key(substrate, monkeypatch) -> None:
    """bootstrap_vtr_ref_secret fails LOUD with no key (a missing key would NULL the refs)."""
    from orchestrator.privacy.vtr import bootstrap_vtr_ref_secret

    monkeypatch.delenv("VT_REF_HMAC_KEY", raising=False)
    with pytest.raises(RuntimeError, match="VT_REF_HMAC_KEY"):
        bootstrap_vtr_ref_secret()


def test_vtr_role_grants_are_exactly_the_two_views(substrate) -> None:
    """Grant hygiene (Cowork fold-in a): app_vtr_role's table privileges are EXACTLY SELECT on the
    two de-identified views and NOTHING else — covers ALL tables (catches any future grant that
    would widen the VTR surface), not just the probed PII set."""
    with psycopg.connect(substrate, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = 'app_vtr_role'"
        ).fetchall()
    granted = {(r[0], r[1]) for r in rows}
    assert granted == {("vtr_customers", "SELECT"), ("vtr_escalations", "SELECT")}, (
        f"app_vtr_role grants drifted beyond the 2 de-identified views: {granted}"
    )
