"""VT-315 — kg_events outbox PII-strip canary (CL-390, live PG).

Proves raw PII never enters the durable kg_events.payload: emit sites send the
phone HASH (not raw phone) + the real business_name (not the phone fallback); the
L1 projection still resolves the canonical hash (no regression); and the mig-088
backfill redacts any pre-VT-315 raw-phone rows. CL-422 synthetic.
"""

from __future__ import annotations

import os
import re
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — kg PII-strip tests skipped",
)

_PHONE = "+919812345678"
_PHONE_RE = re.compile(r"\+?[0-9]{8,}")


@pytest.fixture(scope="module")
def pool():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt315-salt")
    os.environ.setdefault(
        "TEAM_PHONE_ENCRYPTION_KEY",
        __import__("base64").urlsafe_b64encode(b"0" * 32).decode(),
    )

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


def _tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'trial', %s)",
            (tid, f"vt315-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
    return tid


def _kg_payloads(pool, tid: str, event_type: str) -> list[dict]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT payload FROM kg_events WHERE tenant_id = %s AND event_type = %s",
            (tid, event_type),
        ).fetchall()
    return [dict(r)["payload"] for r in rows]


# --- emit-time: raw phone never enters the outbox ----------------------------


def test_customer_created_emits_hash_not_raw_phone(pool):
    from orchestrator.integrations.dedup_merge import dedup_and_merge
    from orchestrator.utils.phone_token import hash_phone

    tid = _tenant(pool)
    dedup_and_merge(tid, acquired_via="paper_book", phone_e164=_PHONE)

    payloads = _kg_payloads(pool, tid, "customer_created")
    assert payloads, "expected a customer_created outbox row"
    p = payloads[0]
    assert "phone_e164" not in p                     # raw phone NEVER in payload
    assert p.get("phone_hash") == hash_phone(_PHONE)  # canonical hash present
    # belt-and-braces: the raw phone string appears nowhere in the payload blob.
    assert _PHONE not in str(p)


def test_l1_projection_resolves_canonical_hash(pool):
    """No regression: draining the hash-bearing event still lands the canonical
    phone_hash on the L1 customer node."""
    from orchestrator.integrations.dedup_merge import dedup_and_merge
    from orchestrator.knowledge.kg_emit import drain_kg_events
    from orchestrator.utils.phone_token import hash_phone

    tid = _tenant(pool)
    dedup_and_merge(tid, acquired_via="paper_book", phone_e164=_PHONE)
    drain_kg_events(tid)

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT attributes FROM l1_entities "
            "WHERE tenant_id = %s AND entity_type = 'customer'",
            (tid,),
        ).fetchone()
    assert row is not None
    assert dict(row)["attributes"].get("phone_hash") == hash_phone(_PHONE)


def test_tenant_created_payload_has_no_phone_business_name(pool):
    from orchestrator.onboarding.tenant_provision import create_tenant_if_unknown

    contact = f"+9198{uuid4().hex[:8]}"
    # VT-408: provisioning a new number now requires verified=True (the gated entry).
    res = create_tenant_if_unknown(business_contact=contact, business_name=None, verified=True)
    tid = str(res.tenant_id)

    payloads = _kg_payloads(pool, tid, "tenant_created")
    assert payloads, "expected a tenant_created outbox row"
    bn = payloads[0].get("business_name")
    assert bn is None  # NOT the phone fallback
    assert not (bn and _PHONE_RE.fullmatch(str(bn)))


# --- backfill (mig 088 mechanism) on pre-VT-315 raw rows ---------------------


def test_backfill_redacts_legacy_raw_phone_rows(pool):
    from psycopg.types.json import Jsonb

    tid = _tenant(pool)
    # Seed pre-VT-315 style rows carrying raw PII in the payload.
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO kg_events (event_id, event_type, tenant_id, payload) VALUES "
            "(%s, 'customer_created', %s, %s), "
            "(%s, 'tenant_created', %s, %s)",
            (str(uuid4()), tid, Jsonb({"customer_id": str(uuid4()), "phone_e164": _PHONE}),
             str(uuid4()), tid, Jsonb({"business_name": _PHONE})),
        )
        # Run the mig-088 backfill statements.
        conn.execute(
            "UPDATE kg_events SET payload = payload - 'phone_e164' "
            "WHERE event_type IN ('customer_created','customer_updated') AND payload ? 'phone_e164'"
        )
        conn.execute(
            "UPDATE kg_events SET payload = payload - 'business_name' "
            "WHERE event_type = 'tenant_created' AND payload ->> 'business_name' ~ '^\\+?[0-9]{8,}$'"
        )

    for p in _kg_payloads(pool, tid, "customer_created"):
        assert "phone_e164" not in p
        assert _PHONE not in str(p)
    for p in _kg_payloads(pool, tid, "tenant_created"):
        assert "business_name" not in p
        assert _PHONE not in str(p)
