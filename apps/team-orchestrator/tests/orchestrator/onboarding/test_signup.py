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
        preferred_language="hi", owner_name="Owner X", city="Mumbai", business_type="kirana", consent_dpdpa=True, consent_residency=True,
    )
    assert res.created is True
    assert res.plan_tier == "founding"
    assert res.city_tier == "tier_1"  # Mumbai  # stub until VT-10.6

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
        preferred_language="en", owner_name="Owner X", city="Mumbai", business_type="kirana", consent_dpdpa=True, consent_residency=True,
    )
    r2 = create_signup_tenant(
        business_name="Branch One Again", whatsapp_number=wa,
        preferred_language="en", owner_name="Owner X", city="Mumbai", business_type="kirana", consent_dpdpa=True, consent_residency=True,
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
            preferred_language="en", owner_name="Owner X", city="Mumbai", business_type="kirana", consent_dpdpa=True, consent_residency=False,
        )


def test_bad_business_type_rejected(pool):
    from orchestrator.onboarding.signup import create_signup_tenant

    with pytest.raises(ValueError):
        create_signup_tenant(
            business_name="Mystery Co", owner_name="X", whatsapp_number=_wa(),
            preferred_language="en", city="Mumbai", business_type="not_a_real_type",
            consent_dpdpa=True, consent_residency=True,
        )


def _valid_input(**over):
    from orchestrator.onboarding.signup import SignupInput

    base = dict(
        business_name="Asha Kirana", owner_name="Asha Devi", whatsapp_number=_wa_91(),
        preferred_language="hi", city="Bengaluru", business_type="kirana",
        consent_dpdpa=True, consent_residency=True,
    )
    base.update(over)
    return SignupInput(**base)


def _wa_91() -> str:
    # +91 + 10-digit mobile starting 6-9, unique per call.
    import random
    return "+919" + "".join(str(random.randint(0, 9)) for _ in range(9))


def test_run_signup_full(pool):
    from orchestrator.onboarding.signup import run_signup

    calls = []
    out = run_signup(
        _valid_input(),
        welcome_send_fn=lambda *a, **k: calls.append(a) or True,
    )
    assert out.plan_tier == "founding"
    assert out.city_tier in {"tier_1", "tier_2", "tier_3"}
    assert out.welcome_sent is True
    assert len(calls) == 1  # welcome invoked once

    with pool.connection() as c:
        t = c.execute(
            "SELECT business_type, city_tier, preferred_language FROM tenants WHERE id = %s",
            (str(out.tenant_id),),
        ).fetchone()
        assert t["business_type"] == "kirana"
        assert t["city_tier"] == out.city_tier  # VT-317 closed: city_tier populated
        cr = c.execute(
            "SELECT count(*) AS n FROM consent_records WHERE tenant_id = %s",
            (str(out.tenant_id),),
        ).fetchone()["n"]
        assert cr == 1
    # owner_name merged into business_profile (where the brain reads it).
    from orchestrator.db import tenant_connection
    with tenant_connection(out.tenant_id) as conn:
        bp = conn.execute(
            "SELECT attributes FROM l1_entities WHERE entity_type = 'business_profile'"
        ).fetchone()
    attrs = bp["attributes"] if isinstance(bp, dict) else bp[0]
    assert attrs.get("owner_name") == "Asha Devi"


def test_run_signup_discovery_kick_failure_non_blocking(pool, monkeypatch):
    """VT-366: a failing Auto-Discovery kick (post-commit, best-effort) must NEVER 500 the signup —
    the tenant is already committed; discovery is fire-and-forget."""
    import dbos

    from orchestrator.onboarding.signup import run_signup

    def _boom(*a, **k):
        raise RuntimeError("discovery kick exploded")

    monkeypatch.setattr(dbos.DBOS, "start_workflow", staticmethod(_boom), raising=False)

    out = run_signup(
        _valid_input(whatsapp_number="+919900000366"),
        welcome_send_fn=lambda *a, **k: True,
    )
    # Signup still succeeds despite the kick raising.
    assert out.tenant_id is not None
    assert out.welcome_sent is True


def test_run_signup_duplicate_409(pool):
    from orchestrator.onboarding.signup import SignupError, run_signup

    wa = _wa_91()
    run_signup(_valid_input(whatsapp_number=wa), welcome_send_fn=lambda *a, **k: True)
    with pytest.raises(SignupError) as e:
        run_signup(_valid_input(whatsapp_number=wa), welcome_send_fn=lambda *a, **k: True)
    assert e.value.code == "duplicate"


def test_run_signup_consent_false_no_tenant(pool):
    from orchestrator.onboarding.signup import SignupError, run_signup

    wa = _wa_91()
    with pytest.raises(SignupError) as e:
        run_signup(_valid_input(whatsapp_number=wa, consent_residency=False))
    assert e.value.code == "consent"
    # NO tenant created.
    with pool.connection() as c:
        n = c.execute(
            "SELECT count(*) AS n FROM tenants WHERE whatsapp_number = %s", (wa,)
        ).fetchone()["n"]
    assert n == 0


def test_run_signup_validation_negatives(pool):
    from orchestrator.onboarding.signup import SignupError, run_signup

    for over, code in [
        ({"whatsapp_number": "+1202555"}, "invalid_phone"),
        ({"preferred_language": "ta"}, "invalid_language"),
        ({"city": "  "}, "invalid_city"),
        ({"business_type": "spaceship"}, "invalid_business_type"),
        ({"business_name": "viabe team"}, "invalid_name"),  # blocklist
    ]:
        with pytest.raises(SignupError) as e:
            run_signup(_valid_input(**over))
        assert e.value.code == code, f"{over} → expected {code}, got {e.value.code}"


def test_signup_route_status_mapping(pool, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.api.signup import router

    monkeypatch.setenv("INTERNAL_API_SECRET", "vt326-test-secret")
    hdr = {"X-Internal-Secret": "vt326-test-secret"}

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    body = {
        "business_name": "Asha Kirana", "owner_name": "Asha Devi",
        "whatsapp_number": _wa_91(), "preferred_language": "en",
        "city": "Mumbai", "business_type": "kirana",
        "consent_dpdpa": True, "consent_residency": True,
    }
    r = client.post("/api/signup", json=body, headers=hdr)
    assert r.status_code == 201, r.text
    assert r.json()["tenant_id"]
    assert r.json()["city_tier"] == "tier_1"  # Mumbai → tier_1; VT-317 closed

    # Duplicate → 409.
    r_dup = client.post("/api/signup", json=body, headers=hdr)
    assert r_dup.status_code == 409

    # VT-326 A2: only team-web (holding INTERNAL_API_SECRET) may reach this BYPASSRLS
    # create surface — a missing or wrong secret is 403 (closes flooding at the source).
    assert client.post("/api/signup", json=body).status_code == 403
    assert (
        client.post("/api/signup", json=body, headers={"X-Internal-Secret": "wrong"}).status_code
        == 403
    )
    assert r_dup.json()["detail"]["code"] == "duplicate"

    # Consent false → 400, no tenant.
    r_consent = client.post("/api/signup", json={**body, "whatsapp_number": _wa_91(),
                                                 "consent_residency": False}, headers=hdr)
    assert r_consent.status_code == 400
    assert r_consent.json()["detail"]["code"] == "consent"

    # Bad phone → 400.
    r_phone = client.post("/api/signup", json={**body, "whatsapp_number": "+1202555"}, headers=hdr)
    assert r_phone.status_code == 400
    assert r_phone.json()["detail"]["code"] == "invalid_phone"


def test_signup_kg_event_has_no_business_name_pii(pool):
    """Review/CL-390: the TENANT_CREATED outbox payload (durable, NOT DSR-purged)
    must NOT carry business_name (owner subject data) — only the non-PII business_type."""
    from orchestrator.onboarding.signup import create_signup_tenant

    res = create_signup_tenant(
        business_name="Secret Biz Name", owner_name="X", whatsapp_number=_wa(),
        preferred_language="en", city="Mumbai", business_type="kirana",
        consent_dpdpa=True, consent_residency=True,
    )
    with pool.connection() as c:
        rows = c.execute(
            "SELECT payload FROM kg_events WHERE tenant_id = %s "
            "AND event_type = 'tenant_created'", (str(res.tenant_id),),
        ).fetchall()
    assert rows, "no tenant_created event emitted"
    for r in rows:
        p = r["payload"]
        assert "business_name" not in p, "business_name PII leaked into durable kg_events"
        assert p.get("business_type") == "kirana"


def test_consent_records_is_pii_free_schema(pool):
    """Review: consent_records' DSR-retention safety RESTS on it being PII-free.
    Enforce that — only the known booleans/versions/timestamps, no name/phone/email."""
    allowed = {
        "id", "tenant_id", "consent_dpdpa", "consent_residency",
        "dpdpa_version", "residency_version", "signed_up_at", "created_at",
    }
    with pool.connection() as c:
        cols = {
            r["column_name"] for r in c.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'consent_records'"
            ).fetchall()
        }
    assert cols <= allowed, f"consent_records has unexpected (possibly-PII) columns: {cols - allowed}"


def test_business_types_endpoint_serves_taxonomy(pool):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.api.signup import router

    app = FastAPI()
    app.include_router(router)
    r = TestClient(app).get("/api/signup/business-types")
    assert r.status_code == 200
    opts = r.json()["business_types"]
    keys = {o["key"] for o in opts}
    assert "kirana" in keys and "other" in keys
    # every option carries both language labels (no PII).
    assert all(o.get("label_en") and o.get("label_hi") for o in opts)
