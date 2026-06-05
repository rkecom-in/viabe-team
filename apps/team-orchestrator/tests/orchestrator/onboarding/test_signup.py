"""VT-82 — create_signup_tenant canary (Rule #15, real PG, CL-422 synthetic).

The atomic service_role create: tenant row + owner consent_records + trial init in one
txn. Plus the duplicate-whatsapp_number (→ created=False, endpoint 409) and the
consent-false (Pillar-7 reject, no tenant) negatives. Mock connections hide RLS +
the ON CONFLICT, so this runs on a live DB.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-82 signup canary skipped",
)


@pytest.fixture(scope="module")
def pool():
    import apply_migrations
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from orchestrator import graph as graph_mod

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    prev = graph_mod._pool
    graph_mod._pool = ConnectionPool(
        dsn, min_size=1, max_size=4,
        kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
    )
    try:
        yield graph_mod._pool
    finally:
        graph_mod._pool.close()
        graph_mod._pool = prev


def _wa() -> str:
    return "+91" + str(uuid.uuid4().int)[:10]


def test_create_signup_tenant_atomic(pool):
    from orchestrator.onboarding.signup import create_signup_tenant

    wa = _wa()
    res = create_signup_tenant(
        business_name="Asha Kirana", whatsapp_number=wa,
        preferred_language="hi", business_type="kirana", consent_dpdpa=True, consent_residency=True,
    )
    assert res.created is True
    assert res.plan_tier == "founding"  # stub until VT-10.6

    with pool.connection() as c:
        t = c.execute(
            "SELECT phase, plan_tier, preferred_language, trial_started_at, "
            "signed_up_at, created_via, business_type FROM tenants WHERE id = %s",
            (str(res.tenant_id),),
        ).fetchone()
        assert t["phase"] == "onboarding"
        assert t["plan_tier"] == "founding"
        assert t["preferred_language"] == "hi"
        assert t["trial_started_at"] is not None
        assert t["created_via"] == "web"
        assert t["business_type"] == "kirana"

        cr = c.execute(
            "SELECT consent_dpdpa, consent_residency, dpdpa_version, residency_version "
            "FROM consent_records WHERE tenant_id = %s", (str(res.tenant_id),),
        ).fetchone()
        assert cr["consent_dpdpa"] and cr["consent_residency"]
        assert cr["dpdpa_version"] == "dpdpa_v1_2026-06"
        assert cr["residency_version"] == "residency_v1_2026-06"


def test_duplicate_whatsapp_number_not_created(pool):
    from orchestrator.onboarding.signup import create_signup_tenant

    wa = _wa()
    r1 = create_signup_tenant(
        business_name="Branch One", whatsapp_number=wa,
        preferred_language="en", business_type="kirana", consent_dpdpa=True, consent_residency=True,
    )
    r2 = create_signup_tenant(
        business_name="Branch One Again", whatsapp_number=wa,
        preferred_language="en", business_type="kirana", consent_dpdpa=True, consent_residency=True,
    )
    assert r1.created is True
    assert r2.created is False  # endpoint maps this → 409
    assert r2.tenant_id == r1.tenant_id  # same identity, no new row
    assert r2.plan_tier is None
    # exactly one consent_records row for the identity (no duplicate proof).
    with pool.connection() as c:
        n = c.execute(
            "SELECT count(*) AS n FROM consent_records WHERE tenant_id = %s",
            (str(r1.tenant_id),),
        ).fetchone()["n"]
    assert n == 1


def test_consent_false_rejected(pool):
    from orchestrator.onboarding.signup import create_signup_tenant

    with pytest.raises(ValueError):
        create_signup_tenant(
            business_name="No Consent Co", whatsapp_number=_wa(),
            preferred_language="en", business_type="kirana", consent_dpdpa=True, consent_residency=False,
        )


def test_bad_business_type_rejected(pool):
    from orchestrator.onboarding.signup import create_signup_tenant

    with pytest.raises(ValueError):
        create_signup_tenant(
            business_name="Mystery Co", whatsapp_number=_wa(),
            preferred_language="en", business_type="not_a_real_type",
            consent_dpdpa=True, consent_residency=True,
        )
