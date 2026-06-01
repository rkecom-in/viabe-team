"""VT-62 — Google Business Profile aggregate context (Apify) tests.

PURE: the aggregate allowlist drops reviews/reviewer PII; graceful-degrade on
missing query/token. DB (real Postgres, no mock cursors): context written to L1
business_profile.gbp_context; MANDATORY no-PII negative test (a synthetic actor
response carrying reviews + reviewer identity → ZERO of it reaches storage);
merge-not-clobber of sibling profile keys; actor-failure/empty degrade;
cross-tenant isolation. Apify FAKED. Synthetic data only (CL-422).
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")

from orchestrator.integrations.methods.apify_gbp import _aggregate, ingest_gbp  # noqa: E402

# A synthetic actor place result that DOES carry verbatim reviews + reviewer PII —
# none of which may ever reach storage.
_PLACE_WITH_PII = {
    "title": "Asha Kirana Store",
    "totalScore": 4.6,
    "reviewsCount": 247,
    "categoryName": "Grocery store",
    "price": "$$",
    "neighborhood": "Koramangala",
    "city": "Bengaluru",
    # --- PII that MUST be stripped ---
    "reviews": [
        {"name": "Ramesh Kumar", "text": "Great shop, owner is super helpful",
         "reviewerUrl": "https://maps.google.com/contrib/9988776655",
         "reviewerId": "9988776655", "reviewerPhotoUrl": "https://x/p.jpg"},
    ],
    "reviewsTags": [{"title": "Ramesh recommends", "count": 3}],
}
_PII_NEEDLES = ["Ramesh", "Great shop", "9988776655", "contrib", "reviewerUrl", "p.jpg"]


def _fetch(*places):
    return lambda run_input, token: list(places)


# --- PURE ---------------------------------------------------------------------

def test_aggregate_allowlist_drops_pii():
    agg = _aggregate(_PLACE_WITH_PII)
    blob = json.dumps(agg)
    assert agg["rating"] == 4.6 and agg["reviews_count"] == 247
    assert "reviews" not in agg and "reviewsTags" not in agg
    for needle in _PII_NEEDLES:
        assert needle not in blob, f"PII {needle!r} leaked into aggregate"


def test_no_query_degrades():
    s = ingest_gbp(uuid4(), token="t", fetch_fn=_fetch(_PLACE_WITH_PII))
    assert s.committed == 0 and s.dropped == 1  # no place_url/business_name


def test_no_token_degrades(monkeypatch):
    monkeypatch.delenv("APIFY_API_TOKEN", raising=False)
    s = ingest_gbp(uuid4(), business_name="Asha Kirana", token=None,
                   fetch_fn=_fetch(_PLACE_WITH_PII))
    assert s.committed == 0 and s.dropped == 1


# --- DB (real Postgres) -------------------------------------------------------

pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — apify_gbp DB tests skipped",
)


@pytest.fixture(scope="module")
def db_ctx():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


def _tenant(dsn: str) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return str(conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('VT-62 gbp test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0])


def _profile_attrs(tenant: str):
    from orchestrator.db import tenant_connection

    with tenant_connection(tenant) as conn:
        row = conn.execute(
            "SELECT attributes FROM l1_entities WHERE entity_type = 'business_profile'"
        ).fetchone()
    if row is None:
        return None
    return row["attributes"] if isinstance(row, dict) else row[0]


@_DB
def test_context_written_to_l1(db_ctx):
    tenant = _tenant(db_ctx.dsn)
    s = ingest_gbp(tenant, business_name="Asha Kirana", locality="Koramangala",
                   token="t", fetch_fn=_fetch(_PLACE_WITH_PII))
    assert s.committed == 1 and s.dropped == 0
    attrs = _profile_attrs(tenant)
    assert attrs and attrs["gbp_context"]["rating"] == 4.6
    assert attrs["gbp_context"]["reviews_count"] == 247
    assert attrs["gbp_context"]["acquired_via"] == "apify_gbp"


@_DB
def test_no_pii_reaches_storage(db_ctx):
    """MANDATORY: a response carrying reviews + reviewer identity → ZERO PII stored."""
    tenant = _tenant(db_ctx.dsn)
    ingest_gbp(tenant, place_url="https://maps.google.com/?cid=123",
               token="t", fetch_fn=_fetch(_PLACE_WITH_PII))
    stored = json.dumps(_profile_attrs(tenant))
    for needle in _PII_NEEDLES:
        assert needle not in stored, f"PII {needle!r} reached L1 storage"


@_DB
def test_merge_does_not_clobber_existing_profile(db_ctx):
    from orchestrator.knowledge.l1 import upsert_business_profile

    tenant = _tenant(db_ctx.dsn)
    upsert_business_profile(tenant, {"archetype": "kirana", "owner_persona": "value"})
    ingest_gbp(tenant, business_name="Asha Kirana", token="t",
               fetch_fn=_fetch(_PLACE_WITH_PII))
    attrs = _profile_attrs(tenant)
    assert attrs["archetype"] == "kirana"  # sibling key preserved
    assert attrs["owner_persona"] == "value"
    assert attrs["gbp_context"]["rating"] == 4.6  # gbp merged in


@_DB
def test_actor_failure_degrades(db_ctx):
    def _boom(run_input, token):
        raise RuntimeError("apify 503")

    tenant = _tenant(db_ctx.dsn)
    s = ingest_gbp(tenant, business_name="Asha Kirana", token="t", fetch_fn=_boom)
    assert s.committed == 0 and s.dropped == 1
    assert _profile_attrs(tenant) is None  # nothing written on failure


@_DB
def test_empty_result_degrades(db_ctx):
    tenant = _tenant(db_ctx.dsn)
    s = ingest_gbp(tenant, business_name="Nonexistent", token="t", fetch_fn=_fetch())
    assert s.committed == 0 and s.dropped == 1


@_DB
def test_cross_tenant_isolation(db_ctx):
    a, b = _tenant(db_ctx.dsn), _tenant(db_ctx.dsn)
    ingest_gbp(a, business_name="Asha Kirana", token="t", fetch_fn=_fetch(_PLACE_WITH_PII))
    assert _profile_attrs(b) is None  # B cannot see A's GBP context (RLS)
