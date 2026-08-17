"""VT-742 §1 — resolve_sender precedence against real Postgres, plus migration 207's two invariants.

Exit gate (b) wants "a tenant with a provisioned WABA demonstrably sends from their own number and
another tenant does not" proven, not asserted; gate (c) wants the unresolvable case forced; gate (g)
wants the pin provable with ONE number. All three are here.

The precedence reads run on an injected connection (mirroring ``wa_send_allowed``'s VT-460 contract),
EXCEPT ``test_own_waba_resolves_through_tenant_connection`` which deliberately takes the no-conn
path so the RLS-scoped read is exercised too — an injected superuser connection would let a broken
RLS path pass.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-742 sender-resolution substrate tests skipped",
)

_DEFAULT_SHARED = "+910000000000"
_PIN = "+910000000009"


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    yield SimpleNamespace(dsn=dsn)


@pytest.fixture(autouse=True)
def _shared_env(monkeypatch):
    monkeypatch.setenv("TEAM_TWILIO_FROM_NUMBER", _DEFAULT_SHARED)
    monkeypatch.delenv("TEAM_TWILIO_SHARED_SENDER_NUMBERS", raising=False)


def _unique_number(prefix: str = "+9199") -> str:
    return f"{prefix}{uuid4().int % 10**8:08d}"


def _tenant(conn, *, pin: str | None = None) -> str:
    # Some callers hand us a dict_row connection and some a tuple one — index accordingly rather
    # than assuming position (a dict_row conn raises KeyError: 0 on [0]).
    row = conn.execute(
        "INSERT INTO tenants (business_name, plan_tier, phase, pinned_sender_e164) "
        "VALUES ('VT-742 sender test', 'founding', 'onboarding', %s) RETURNING id",
        (pin,),
    ).fetchone()
    return str(row["id"] if isinstance(row, dict) else row[0])


def _waba(conn, tenant_id: str, *, status: str, number: str | None) -> None:
    conn.execute(
        "INSERT INTO tenant_whatsapp_accounts (tenant_id, status, phone_number) "
        "VALUES (%s, %s, %s) ON CONFLICT (tenant_id) DO UPDATE SET "
        "status = EXCLUDED.status, phone_number = EXCLUDED.phone_number",
        (tenant_id, status, number),
    )


# --- precedence ---------------------------------------------------------------------------


def test_own_live_waba_wins_over_pin_and_default(substrate):
    from orchestrator.integrations.sender_resolution import KIND_OWN_WABA, resolve_sender

    own = _unique_number()
    with psycopg.connect(substrate.dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn, pin=_PIN)
        _waba(conn, tid, status="live", number=own)

        sender = resolve_sender(tid, conn=conn)

    assert sender.phone_number == own, "step 1 — the tenant's own live WABA outranks everything"
    assert sender.kind == KIND_OWN_WABA
    assert sender.is_shared is False


def test_a_second_tenant_without_a_waba_does_not_get_the_first_tenants_number(substrate):
    """Exit gate (b)'s second half: the tenants must be distinguishable, not merely resolvable."""
    from orchestrator.integrations.sender_resolution import KIND_DEFAULT_SHARED, resolve_sender

    own = _unique_number()
    with psycopg.connect(substrate.dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        with_waba = _tenant(conn)
        _waba(conn, with_waba, status="live", number=own)
        without_waba = _tenant(conn)

        a = resolve_sender(with_waba, conn=conn)
        b = resolve_sender(without_waba, conn=conn)

    assert a.phone_number == own
    assert b.phone_number == _DEFAULT_SHARED
    assert b.kind == KIND_DEFAULT_SHARED
    assert a.phone_number != b.phone_number


def test_pin_wins_when_the_waba_is_not_live(substrate):
    """Gate (g): precedence step 2 is provable with ONE number in the estate — no second number
    has to be bought for the pin to be testable."""
    from orchestrator.integrations.sender_resolution import KIND_PINNED_SHARED, resolve_sender

    with psycopg.connect(substrate.dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn, pin=_PIN)
        _waba(conn, tid, status="verifying", number=_unique_number())

        sender = resolve_sender(tid, conn=conn)

    assert sender.phone_number == _PIN, "a non-live WABA must not be used as a sender"
    assert sender.kind == KIND_PINNED_SHARED
    assert sender.is_shared is True


def test_default_shared_when_there_is_no_waba_and_no_pin(substrate):
    from orchestrator.integrations.sender_resolution import KIND_DEFAULT_SHARED, resolve_sender

    with psycopg.connect(substrate.dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn)
        sender = resolve_sender(tid, conn=conn)

    assert sender.phone_number == _DEFAULT_SHARED
    assert sender.kind == KIND_DEFAULT_SHARED


def test_live_waba_with_a_null_number_falls_through_to_shared(substrate):
    """`wa_send_allowed` only checks the STATUS, so a live row with no number exists as far as the
    send gate is concerned. The resolver must not return None as a sender."""
    from orchestrator.integrations.sender_resolution import KIND_DEFAULT_SHARED, resolve_sender

    with psycopg.connect(substrate.dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn)
        _waba(conn, tid, status="live", number=None)

        sender = resolve_sender(tid, conn=conn)

    assert sender.kind == KIND_DEFAULT_SHARED


# --- fail-closed (gate (c)) ---------------------------------------------------------------


def test_customer_send_refuses_when_the_tenant_has_no_live_waba(substrate):
    """The reframe: a customer messaged from the shared number cannot reply to us, because customer
    inbound routes by the number they messaged TO. So a customer send with no own WABA is refused,
    not downgraded."""
    from orchestrator.integrations.sender_resolution import SenderUnresolvable, resolve_sender

    with psycopg.connect(substrate.dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn, pin=_PIN)
        _waba(conn, tid, status="verifying", number=_unique_number())

        with pytest.raises(SenderUnresolvable):
            resolve_sender(tid, conn=conn, require_own_waba=True)

        # …and the same tenant's OWNER sends are unaffected.
        assert resolve_sender(tid, conn=conn).phone_number == _PIN


def test_customer_send_with_no_tenant_is_refused(substrate):
    from orchestrator.integrations.sender_resolution import SenderUnresolvable, resolve_sender

    with pytest.raises(SenderUnresolvable):
        resolve_sender(None, require_own_waba=True)


def test_malformed_live_waba_number_is_refused_as_a_sender(substrate):
    """A live WABA carrying a non-E.164 number: refused outright for a customer send, and never
    used as a sender for an owner send either. Migration 069 put no CHECK on that column, and the
    harness itself wrote `+1555<uuid-hex>` into it for months."""
    from orchestrator.integrations.sender_resolution import (
        KIND_DEFAULT_SHARED,
        SenderUnresolvable,
        resolve_sender,
    )

    # Unique per run: mig 207's live-phone UNIQUE is real, and a constant here collided with the
    # ACTUAL leftover harness row on dev (`+1555470fea5` — the one non-E.164 live number of 134).
    # The 'e' form is the VT-487 float-corruption artifact, so this is malformed by construction.
    malformed = f"+1555{uuid4().int % 10**6:06d}e11"
    with psycopg.connect(substrate.dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn)
        _waba(conn, tid, status="live", number=malformed)

        with pytest.raises(SenderUnresolvable):
            resolve_sender(tid, conn=conn, require_own_waba=True)

        owner = resolve_sender(tid, conn=conn)

    assert owner.phone_number == _DEFAULT_SHARED
    assert owner.kind == KIND_DEFAULT_SHARED, "a malformed WABA number is never dispatched"


def test_unresolvable_when_the_default_sender_is_absent(substrate, monkeypatch):
    from orchestrator.integrations.sender_resolution import SenderUnresolvable, resolve_sender

    monkeypatch.delenv("TEAM_TWILIO_FROM_NUMBER", raising=False)
    with psycopg.connect(substrate.dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn)
        with pytest.raises(SenderUnresolvable):
            resolve_sender(tid, conn=conn)


def test_unresolvable_when_the_default_sender_is_malformed(substrate, monkeypatch):
    from orchestrator.integrations.sender_resolution import SenderUnresolvable, resolve_sender

    monkeypatch.setenv("TEAM_TWILIO_FROM_NUMBER", "+91998886e+11")
    with psycopg.connect(substrate.dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn)
        with pytest.raises(SenderUnresolvable):
            resolve_sender(tid, conn=conn)


def test_resolve_sender_reads_a_tuple_row_connection_too(substrate):
    """``wa_send_allowed`` accepts either row factory and so must this — a caller's connection is
    not ours to assume. Without the positional branch this returns the shared default and the
    tenant's own WABA silently disappears, which is the exact class of bug this row fixes."""
    from orchestrator.integrations.sender_resolution import KIND_OWN_WABA, resolve_sender

    own = _unique_number()
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:  # default: tuple rows
        tid = _tenant(conn)
        _waba(conn, tid, status="live", number=own)

        sender = resolve_sender(tid, conn=conn)

    assert sender.phone_number == own
    assert sender.kind == KIND_OWN_WABA


# --- the RLS-scoped path ------------------------------------------------------------------


def test_own_waba_resolves_through_tenant_connection(substrate):
    """The no-conn path (``tenant_connection``, SET ROLE app_role + tenant GUC) must see both
    sources. Not vacuous: the assertion is the seeded number, so an RLS-blocked read returns the
    shared default and fails."""
    from orchestrator import graph as graphmod
    from orchestrator.integrations.sender_resolution import KIND_OWN_WABA, resolve_sender

    graphmod.init_substrate(substrate.dsn)
    own = _unique_number()
    with psycopg.connect(substrate.dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn)
        _waba(conn, tid, status="live", number=own)

    sender = resolve_sender(tid)

    assert sender.phone_number == own
    assert sender.kind == KIND_OWN_WABA


# --- the template send site ---------------------------------------------------------------


def test_template_customer_send_without_own_waba_returns_a_failed_result(substrate, monkeypatch):
    """``send_template_message`` reports an unresolvable sender; it does not raise.

    It is a ``@DBOS.step``, so a raise would be RETRIED — and a sender does not become resolvable on
    a retry. The refusal here is real, not stubbed: a live tenant with no WABA row, sent as a
    customer send, which is precisely the case that used to leave from the shared number and produce
    a message the customer could not answer.
    """
    from types import SimpleNamespace as NS

    from orchestrator import graph as graphmod
    from orchestrator.utils import twilio_send

    graphmod.init_substrate(substrate.dsn)
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "vt742-substrate-salt")
    monkeypatch.setattr(
        twilio_send,
        "_registry_resolve",
        lambda name, language="en": NS(content_sid="HXvt742", variables=(), audience="customer"),
    )
    sent: list[dict] = []
    monkeypatch.setattr(
        twilio_send,
        "_client",
        lambda: NS(messages=NS(create=lambda **kw: sent.append(kw))),
    )

    with psycopg.connect(substrate.dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        tid = _tenant(conn)

    with twilio_send.customer_send_context():
        result = twilio_send.send_template_message(
            tid,
            "team_test_vt742",
            {},
            recipient_phone=_unique_number(),
            is_customer_send=True,
        )

    assert result.success is False
    assert result.error_code == twilio_send.SENDER_UNRESOLVABLE
    assert sent == [], "fail-closed: nothing may reach messages.create"


# --- migration 207's invariants -----------------------------------------------------------


def test_migration_207_rejects_a_malformed_pin(substrate):
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            _tenant(conn, pin="+1555470fea5")


def test_migration_207_forbids_two_tenants_claiming_one_live_number(substrate):
    """The `LIMIT 1` in `_lookup_customer_inbound_tenant` turns a duplicate live number into one
    tenant silently receiving another tenant's customer replies."""
    shared = _unique_number()
    with psycopg.connect(substrate.dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        a = _tenant(conn)
        b = _tenant(conn)
        _waba(conn, a, status="live", number=shared)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _waba(conn, b, status="live", number=shared)


def test_migration_207_allows_a_duplicate_number_on_non_live_rows(substrate):
    """Partial index: a superseded/parked row keeping a historical number is legitimate, and the
    routing query only ever reads live rows."""
    historical = _unique_number()
    with psycopg.connect(substrate.dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        a = _tenant(conn)
        b = _tenant(conn)
        _waba(conn, a, status="pending", number=historical)
        _waba(conn, b, status="pending", number=historical)
