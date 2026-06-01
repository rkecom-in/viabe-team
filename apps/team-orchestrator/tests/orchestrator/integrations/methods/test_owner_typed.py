"""VT-63 — owner-typed natural-language entries (Method 9).

PURE: Haiku-reply → ExtractionResult parsing (fake client), phone→E.164
normalisation, consent fail-closed, masked-phone confirmation wording. DB: a
typed entry → customer + attributed ledger row; cross-tenant isolation;
idempotent re-ingest. Real Postgres, no mock cursors. CANARY: a real Haiku call
on a SYNTHETIC message (CL-422), env-gated.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("anthropic")

from orchestrator.integrations.methods.owner_typed import (  # noqa: E402
    PARSE_FAILURE_REPLY,
    OwnerTypedExtractionError,
    _mask_phone,
    build_confirmation,
    extract_owner_typed,
)


# --- fakes --------------------------------------------------------------------

def _entry(**fields: tuple[str | None, float]) -> dict:
    return {"fields": [
        {"name": n, "value": v, "confidence": c} for n, (v, c) in fields.items()
    ]}


class _FakeClient:
    """Anthropic stand-in: returns a canned JSON body; records call count."""

    def __init__(self, body: str):
        self._body = body
        self.calls = 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **_kwargs):
        self.calls += 1
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._body)])


def _client(*entries: dict) -> _FakeClient:
    return _FakeClient(json.dumps({"entries": list(entries)}))


_OK = {"consent_check": lambda _t: True}


# --- PURE: extraction ---------------------------------------------------------

def test_english_happy_path_normalises_phone():
    body = _client(_entry(
        customer_name=("Rajesh", 0.9), phone=("9876543210", 0.9),
        amount=("800", 0.9), entry_date=("2026-05-31", 0.9),
    ))
    [res] = extract_owner_typed("Add Rajesh, 9876543210, yesterday, 800",
                                tenant_id=uuid4(), client=body, **_OK)
    f = {x.name: x for x in res.fields}
    assert f["phone"].value == "+919876543210"  # normalised to E.164
    assert f["customer_name"].value == "Rajesh"
    assert f["amount"].value == "800"
    assert res.acquired_via == "owner_typed"


def test_hindi_unicode_name_preserved():
    body = _client(_entry(
        customer_name=("सुनीता", 0.88), phone=("8765432109", 0.88),
        amount=("1200", 0.85), entry_date=("2026-05-30", 0.85),
    ))
    [res] = extract_owner_typed("नया customer सुनीता, 8765432109, परसों आया, 1200",
                                tenant_id=uuid4(), client=body, **_OK)
    f = {x.name: x.value for x in res.fields}
    assert f["customer_name"] == "सुनीता" and f["phone"] == "+918765432109"


def test_multiple_entries_in_one_message():
    body = _client(
        _entry(customer_name=("Rajesh", 0.9), phone=("9876543210", 0.9),
               amount=("800", 0.9), entry_date=(None, 0.0)),
        _entry(customer_name=("Mahesh", 0.9), phone=("8765432109", 0.9),
               amount=("500", 0.9), entry_date=(None, 0.0)),
    )
    res = extract_owner_typed("Add Rajesh 98765 800, also Mahesh 87654 500",
                              tenant_id=uuid4(), client=body, **_OK)
    assert len(res) == 2
    assert {r.fields[0].value for r in res} == {"Rajesh", "Mahesh"}


def test_bare_name_low_confidence_and_null_fields():
    # "Add Rajesh" — name only, low confidence so it routes to clarify downstream.
    body = _client(_entry(
        customer_name=("Rajesh", 0.5), phone=(None, 0.0),
        amount=(None, 0.0), entry_date=(None, 0.0),
    ))
    [res] = extract_owner_typed("Add Rajesh", tenant_id=uuid4(), client=body, **_OK)
    f = {x.name: x for x in res.fields}
    assert f["customer_name"].confidence == 0.5
    assert f["phone"].value is None and f["amount"].value is None


def test_foreign_phone_gets_low_confidence():
    # +1 US number → contacts normaliser flags low conf → clarify downstream.
    body = _client(_entry(customer_name=("Bob", 0.9), phone=("+14155550123", 0.9),
                          amount=(None, 0.0), entry_date=(None, 0.0)))
    [res] = extract_owner_typed("Add Bob +14155550123",
                                tenant_id=uuid4(), client=body, **_OK)
    phone = next(x for x in res.fields if x.name == "phone")
    assert phone.confidence < 0.7


def test_malformed_reply_raises():
    with pytest.raises(OwnerTypedExtractionError):
        extract_owner_typed("x", tenant_id=uuid4(),
                            client=_FakeClient("not json at all"), **_OK)


def test_empty_reply_raises():
    with pytest.raises(OwnerTypedExtractionError):
        extract_owner_typed("x", tenant_id=uuid4(), client=_FakeClient("   "), **_OK)


def test_consent_absent_fails_closed_no_transmission():
    from orchestrator.integrations.vision_extraction import ConsentRejectedError

    fake = _client(_entry(customer_name=("Rajesh", 0.9)))
    with pytest.raises(ConsentRejectedError):
        extract_owner_typed("Add Rajesh", tenant_id=uuid4(),
                            client=fake, consent_check=lambda _t: False)
    assert fake.calls == 0  # never transmitted


# --- PURE: confirmation wording (Fazal reviews) -------------------------------

def test_mask_phone_shows_last_four_only():
    assert _mask_phone("+919876543210") == "•••••••••3210"
    assert _mask_phone(None) is None


def test_build_confirmation_masks_and_formats():
    msg = build_confirmation(customer_name="Rajesh", phone_e164="+919876543210",
                             amount_paise=80000, entry_date="2026-05-31")
    assert "Rajesh" in msg and "₹800" in msg and "2026-05-31" in msg
    assert "9876543210" not in msg and "3210" in msg  # masked


def test_build_confirmation_minimal_no_detail():
    assert build_confirmation(customer_name="Asha", phone_e164=None,
                              amount_paise=None, entry_date=None) == "Added Asha."


def test_parse_failure_reply_is_actionable():
    assert "Add" in PARSE_FAILURE_REPLY and "₹" in PARSE_FAILURE_REPLY


# --- DB (real Postgres) -------------------------------------------------------

pytest.importorskip("dbos")
import psycopg  # noqa: E402

_DB = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — owner_typed DB tests skipped",
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
            "VALUES ('VT-63 owner-typed test', 'founding', 'onboarding') RETURNING id"
        ).fetchone()[0])


def _phone() -> str:
    return "90" + uuid4().int.__str__()[:8]


@_DB
def test_typed_entry_commits_customer_and_ledger(db_ctx):
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.methods.owner_typed import ingest_owner_typed

    tenant = _tenant(db_ctx.dsn)
    phone = _phone()
    body = _client(_entry(customer_name=("Rajesh", 0.9), phone=(phone, 0.9),
                          amount=("800", 0.9), entry_date=("2026-05-31", 0.9)))
    summary = ingest_owner_typed(tenant, "Add Rajesh", client=body, **_OK)
    assert summary.committed == 1
    with tenant_connection(tenant) as conn:
        c = conn.execute("SELECT count(*) AS n FROM customers WHERE phone_e164=%s",
                         ("+91" + phone,)).fetchone()["n"]
        led = conn.execute(
            "SELECT count(*) AS n FROM customer_ledger_entries").fetchone()["n"]
    assert c == 1 and led == 1


@_DB
def test_reingest_idempotent(db_ctx):
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.methods.owner_typed import ingest_owner_typed

    tenant = _tenant(db_ctx.dsn)
    phone = _phone()
    body = _client(_entry(customer_name=("Asha", 0.9), phone=(phone, 0.9),
                          amount=("500", 0.9), entry_date=("2026-05-31", 0.9)))
    ingest_owner_typed(tenant, "Add Asha", client=body, **_OK)
    ingest_owner_typed(tenant, "Add Asha", client=body, **_OK)  # same entry again
    with tenant_connection(tenant) as conn:
        led = conn.execute(
            "SELECT count(*) AS n FROM customer_ledger_entries").fetchone()["n"]
    assert led == 1, "re-ingest duplicated ledger (entry_key idempotency)"


@_DB
def test_cross_tenant_isolation(db_ctx):
    from orchestrator.db import tenant_connection
    from orchestrator.integrations.methods.owner_typed import ingest_owner_typed

    a, b = _tenant(db_ctx.dsn), _tenant(db_ctx.dsn)
    phone = _phone()
    body = _client(_entry(customer_name=("Rita", 0.9), phone=(phone, 0.9),
                          amount=("700", 0.9), entry_date=("2026-05-31", 0.9)))
    ingest_owner_typed(a, "Add Rita", client=body, **_OK)
    with tenant_connection(b) as conn:
        seen = conn.execute("SELECT count(*) AS n FROM customers WHERE phone_e164=%s",
                            ("+91" + phone,)).fetchone()["n"]
    assert seen == 0, "tenant B saw tenant A's typed customer (RLS breach)"


# --- CANARY: real Haiku call on SYNTHETIC data (CL-422), env-gated ------------

@pytest.mark.skipif(
    not (os.environ.get("RUN_INTEGRATION_TESTS") and os.environ.get("ANTHROPIC_API_KEY")),
    reason="RUN_INTEGRATION_TESTS + ANTHROPIC_API_KEY required for owner_typed canary",
)
def test_canary_real_haiku_extraction():
    res = extract_owner_typed(
        "Add Testuser, 9876543210, yesterday, spent 500",
        tenant_id=uuid4(), **_OK,  # real client, consent stubbed (synthetic)
    )
    assert len(res) >= 1
    f = {x.name: x for x in res[0].fields}
    assert f["phone"].value == "+919876543210"  # E.164 post-normalisation
    assert f["customer_name"].value and "Testuser" in f["customer_name"].value
