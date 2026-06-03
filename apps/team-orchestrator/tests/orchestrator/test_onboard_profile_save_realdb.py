"""VT-267 PR-C — owner wizard business_profile save endpoint (real Postgres).

The wizard's Review-&-Confirm save goes through `save_business_profile`, which validates the
owner-editable allowlist + MERGEs (not clobbers) into the tenant's single L1 business_profile via
upsert_business_profile. Real PG (no mocks): proves the secret gate, the allowlist rejection, and
that a save PRESERVES enrichment siblings (the MERGE-not-clobber contract). Gated on DATABASE_URL +
dbos; CL-422 synthetic.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("fastapi")

import psycopg  # noqa: E402
from fastapi import HTTPException  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-267 PR-C profile-save canary skipped",
)

_SECRET = "vt267-prc-test-secret"


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ["INTERNAL_API_SECRET"] = _SECRET
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield dsn
    finally:
        shutdown_dbos()


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT267 PRC', 'founding', 'paid_active') RETURNING id"
        ).fetchone()[0])


def _body(tenant_id: str, attributes: dict):  # type: ignore[no-untyped-def]
    from orchestrator.api.onboard_step import BusinessProfileSaveBody

    return BusinessProfileSaveBody(tenant_id=tenant_id, attributes=attributes)


def test_save_merges_not_clobbers_enrichment_siblings(substrate):
    from orchestrator.api.onboard_step import save_business_profile
    from orchestrator.knowledge import BUSINESS_PROFILE_ENTITY_TYPE, search_entities, upsert_business_profile

    tenant = _tenant(substrate)
    # Pre-seed enrichment (apify_gbp-style) sibling keys the wizard must NOT destroy.
    upsert_business_profile(UUID(tenant), {"archetype": "saree_retail", "gbp_context": {"rating": 4.6}})

    out = save_business_profile(
        _body(tenant, {"business_name": "Asha Sarees", "preferred_language": "hi"}),
        x_internal_secret=_SECRET,
    )
    assert out["ok"] is True

    ents = search_entities(UUID(tenant), entity_type=BUSINESS_PROFILE_ENTITY_TYPE, limit=1)
    attrs = ents[0].attributes
    assert attrs["business_name"] == "Asha Sarees"       # owner edit applied
    assert attrs["preferred_language"] == "hi"
    assert attrs["archetype"] == "saree_retail"          # enrichment sibling PRESERVED
    assert attrs["gbp_context"] == {"rating": 4.6}       # nested enrichment PRESERVED


def test_secret_gate_rejects_bad_secret(substrate):
    from orchestrator.api.onboard_step import save_business_profile

    tenant = _tenant(substrate)
    with pytest.raises(HTTPException) as exc:
        save_business_profile(_body(tenant, {"business_name": "X"}), x_internal_secret="wrong")
    assert exc.value.status_code == 401


def test_allowlist_rejects_non_editable_key(substrate):
    from orchestrator.api.onboard_step import save_business_profile

    tenant = _tenant(substrate)
    with pytest.raises(HTTPException) as exc:
        save_business_profile(
            _body(tenant, {"archetype": "evil_override"}), x_internal_secret=_SECRET
        )
    assert exc.value.status_code == 400


def test_empty_attributes_rejected(substrate):
    from orchestrator.api.onboard_step import save_business_profile

    tenant = _tenant(substrate)
    with pytest.raises(HTTPException) as exc:
        save_business_profile(_body(tenant, {}), x_internal_secret=_SECRET)
    assert exc.value.status_code == 400


def test_invalid_tenant_id_rejected(substrate):
    from orchestrator.api.onboard_step import save_business_profile

    with pytest.raises(HTTPException) as exc:
        save_business_profile(_body("not-a-uuid", {"business_name": "X"}), x_internal_secret=_SECRET)
    assert exc.value.status_code == 400
    _ = uuid4  # silence unused in some lint configs
