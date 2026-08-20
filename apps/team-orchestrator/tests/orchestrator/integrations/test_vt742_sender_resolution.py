"""VT-742 §1 — resolve_sender against real Postgres, plus migration 207's two invariants.

**Fazal 2026-08-17: everything sends from the Viabe number for now.** Owner-owned WABA needs owner
action, so it is a BLOCKER, not a mitigation — the 2026-08-10 ruling and VT-742's own Boundaries line
(*zero owner action required for anything in this row*). `CUSTOMER_SENDS_USE_OWN_WABA` is therefore
False in production and BOTH audiences resolve within the shared estate.

The own-WABA branch stays built and tested behind that flag, so the day owner-owned WABA is
deliverable the flip is the whole change. Tests that exercise it say so by flipping the flag; the
default-path tests assert what actually ships.

Exit gate (b) wants "a tenant with a provisioned WABA demonstrably sends from their own number and
another tenant does not" proven rather than asserted; gate (c) wants the unresolvable case forced;
gate (g) wants the pin provable with ONE number in the estate. All three are here.

Most reads run on an injected connection (mirroring `wa_send_allowed`'s VT-460 contract), EXCEPT
`test_own_waba_resolves_through_tenant_connection`, which deliberately takes the no-conn path so the
RLS-scoped read is exercised too — an injected superuser connection would let a broken RLS path pass.
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
_SECOND_SHARED = "+910000000009"
_PIN = _SECOND_SHARED


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    yield SimpleNamespace(dsn=dsn)


@pytest.fixture(autouse=True)
def _shared_env(monkeypatch):
    """A ONE-number estate, which is production today."""
    monkeypatch.setenv("TEAM_TWILIO_FROM_NUMBER", _DEFAULT_SHARED)
    monkeypatch.delenv("TEAM_TWILIO_SHARED_SENDER_NUMBERS", raising=False)


@pytest.fixture
def own_waba_enabled(monkeypatch):
    """Flip the Fazal-2026-08-17 flag so the own-WABA branch is reachable. Nothing in production
    runs with this on — these tests keep the branch honest for the day it does."""
    from orchestrator.integrations import sender_resolution

    monkeypatch.setattr(sender_resolution, "CUSTOMER_SENDS_USE_OWN_WABA", True)


@pytest.fixture
def two_number_estate(monkeypatch):
    """Declare a second shared number, which is what activates the pin."""
    monkeypatch.setenv("TEAM_TWILIO_SHARED_SENDER_NUMBERS", _SECOND_SHARED)


def _unique_number(prefix: str = "+9199") -> str:
    return f"{prefix}{uuid4().int % 10**8:08d}"


def _conn(dsn):
    return psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row)


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


# --- what SHIPS: every send leaves from the Viabe number ------------------------------------


def test_a_customer_send_uses_the_shared_viabe_number_even_when_the_tenant_has_a_live_waba(
    substrate,
):
    """Fazal 2026-08-17. The tenant HAS a usable live WABA here and the send must still leave from
    the Viabe number: requiring theirs would make a customer send depend on owner action, which is
    the blocker the ruling excludes."""
    from orchestrator.integrations.sender_resolution import KIND_DEFAULT_SHARED, resolve_sender

    own = _unique_number()
    with _conn(substrate.dsn) as conn:
        tid = _tenant(conn)
        _waba(conn, tid, status="live", number=own)

        sender = resolve_sender(tid, conn=conn, audience="customer")

    assert sender.phone_number == _DEFAULT_SHARED
    assert sender.kind == KIND_DEFAULT_SHARED
    assert sender.phone_number != own


def test_a_customer_send_needs_no_waba_at_all_today(substrate):
    """A tenant with NO WABA row can still be sent to — nothing about a customer send depends on
    owner action while the flag is off."""
    from orchestrator.integrations.sender_resolution import KIND_DEFAULT_SHARED, resolve_sender

    with _conn(substrate.dsn) as conn:
        tid = _tenant(conn)
        sender = resolve_sender(tid, conn=conn, audience="customer")

    assert sender.kind == KIND_DEFAULT_SHARED


def test_the_shipped_default_is_flag_off():
    """If this ever reads True on main, a customer send has silently acquired an owner-action
    dependency."""
    from orchestrator.integrations.sender_resolution import CUSTOMER_SENDS_USE_OWN_WABA

    assert CUSTOMER_SENDS_USE_OWN_WABA is False


# --- behind the flag: the tenant's own number, or nothing ----------------------------------


def test_a_customer_send_leaves_from_the_tenants_own_live_waba(substrate, own_waba_enabled):
    from orchestrator.integrations.sender_resolution import KIND_OWN_WABA, resolve_sender

    own = _unique_number()
    with _conn(substrate.dsn) as conn:
        tid = _tenant(conn, pin=_PIN)
        _waba(conn, tid, status="live", number=own)

        sender = resolve_sender(tid, conn=conn, audience="customer")

    assert sender.phone_number == own, "the tenant's own live WABA, not the shared estate"
    assert sender.kind == KIND_OWN_WABA
    assert sender.is_shared is False


def test_gate_b_two_tenants_are_distinguishable(substrate, own_waba_enabled):
    """Gate (b): one tenant demonstrably sends from their own number and another does not.

    The second half is the point — 'another tenant does not' has to be checked, or the gate passes on
    a resolver that returns the same number for everyone.
    """
    from orchestrator.integrations.sender_resolution import SenderUnresolvable, resolve_sender

    own = _unique_number()
    with _conn(substrate.dsn) as conn:
        with_waba = _tenant(conn)
        _waba(conn, with_waba, status="live", number=own)
        without_waba = _tenant(conn)

        a = resolve_sender(with_waba, conn=conn, audience="customer")
        assert a.phone_number == own

        with pytest.raises(SenderUnresolvable):
            resolve_sender(without_waba, conn=conn, audience="customer")


def test_a_non_live_waba_is_not_a_sender(substrate, own_waba_enabled):
    from orchestrator.integrations.sender_resolution import SenderUnresolvable, resolve_sender

    with _conn(substrate.dsn) as conn:
        tid = _tenant(conn)
        _waba(conn, tid, status="verifying", number=_unique_number())

        with pytest.raises(SenderUnresolvable):
            resolve_sender(tid, conn=conn, audience="customer")


def test_a_live_waba_with_a_null_number_is_refused_for_a_customer_send(substrate, own_waba_enabled):
    """`wa_send_allowed` checks only the STATUS, so a live row with NO number passes the send gate.
    The resolver must not treat that as a sender."""
    from orchestrator.integrations.sender_resolution import SenderUnresolvable, resolve_sender

    with _conn(substrate.dsn) as conn:
        tid = _tenant(conn)
        _waba(conn, tid, status="live", number=None)

        with pytest.raises(SenderUnresolvable):
            resolve_sender(tid, conn=conn, audience="customer")


def test_a_malformed_live_waba_number_is_refused_not_downgraded(substrate, own_waba_enabled):
    """Migration 069 put no CHECK on that column and the harness itself wrote `+1555<uuid-hex>` into
    it for months. Falling through to shared would look like a tenant who never onboarded, which is
    how VT-286's output stayed invisible for two months."""
    from orchestrator.integrations.sender_resolution import SenderUnresolvable, resolve_sender

    # Unique per run: mig 207's live-phone UNIQUE is real, and a constant here collided with the
    # ACTUAL leftover harness row on dev (`+1555470fea5` — the one non-E.164 live number of 134).
    # The 'e' form is the VT-487 float-corruption artifact, so this is malformed by construction.
    malformed = f"+1555{uuid4().int % 10**6:06d}e11"
    with _conn(substrate.dsn) as conn:
        tid = _tenant(conn)
        _waba(conn, tid, status="live", number=malformed)

        with pytest.raises(SenderUnresolvable):
            resolve_sender(tid, conn=conn, audience="customer")


def test_a_customer_send_with_no_tenant_is_refused(own_waba_enabled):
    """The production customer-inbound path was calling the transport with no tenant at all. A
    customer send that cannot name whose customer it is has no sender."""
    from orchestrator.integrations.sender_resolution import SenderUnresolvable, resolve_sender

    with pytest.raises(SenderUnresolvable):
        resolve_sender(None, audience="customer")


def test_an_unknown_audience_is_refused():
    from orchestrator.integrations.sender_resolution import SenderUnresolvable, resolve_sender

    with pytest.raises(SenderUnresolvable):
        resolve_sender(uuid4(), audience="marketing")


# --- the owner audience: the shared estate, and today no DB read at all -------------------


def test_an_owner_send_uses_the_shared_default_and_never_the_tenants_waba(substrate):
    """The tenant HAS a live WABA here and the owner send must still not use it: the owner's
    relationship is with Viabe, and moving owner messages onto the shop's own number is Fazal's
    product call."""
    from orchestrator.integrations.sender_resolution import KIND_DEFAULT_SHARED, resolve_sender

    own = _unique_number()
    with _conn(substrate.dsn) as conn:
        tid = _tenant(conn)
        _waba(conn, tid, status="live", number=own)

        sender = resolve_sender(tid, conn=conn)

    assert sender.phone_number == _DEFAULT_SHARED
    assert sender.kind == KIND_DEFAULT_SHARED
    assert sender.phone_number != own


def test_an_owner_send_reads_no_database_while_the_estate_holds_one_number():
    """With one shared number a pin can only point at that same number, so the read cannot change
    the answer — and paying a per-send tenant query to confirm a column that is NULL everywhere is
    cost for nothing. The connection here RAISES if touched, which is the only way to prove a read
    did not happen."""
    from orchestrator.integrations.sender_resolution import KIND_DEFAULT_SHARED, resolve_sender

    class _Forbidden:
        def execute(self, *a, **k):
            raise AssertionError("an owner send must not query the tenant with a 1-number estate")

    sender = resolve_sender(uuid4(), conn=_Forbidden())

    assert sender.kind == KIND_DEFAULT_SHARED
    assert sender.phone_number == _DEFAULT_SHARED


def test_the_pin_activates_when_a_second_number_is_declared(substrate, two_number_estate):
    """Gate (g): the pin is provable with the ONE number already owned — declaring a second in
    TEAM_TWILIO_SHARED_SENDER_NUMBERS is what switches the read on, with no code change at that
    moment. The design never required buying a number to be testable."""
    from orchestrator.integrations.sender_resolution import KIND_PINNED_SHARED, resolve_sender

    with _conn(substrate.dsn) as conn:
        tid = _tenant(conn, pin=_PIN)

        sender = resolve_sender(tid, conn=conn)

    assert sender.phone_number == _PIN
    assert sender.kind == KIND_PINNED_SHARED
    assert sender.is_shared is True


def test_an_unpinned_tenant_falls_to_the_default_even_with_a_larger_estate(
    substrate, two_number_estate
):
    from orchestrator.integrations.sender_resolution import KIND_DEFAULT_SHARED, resolve_sender

    with _conn(substrate.dsn) as conn:
        tid = _tenant(conn)
        sender = resolve_sender(tid, conn=conn)

    assert sender.kind == KIND_DEFAULT_SHARED
    assert sender.phone_number == _DEFAULT_SHARED


def test_owner_send_with_no_tenant_is_legal():
    """Some owner-facing helpers genuinely have no tenant in hand — they are addressed by phone."""
    from orchestrator.integrations.sender_resolution import KIND_DEFAULT_SHARED, resolve_sender

    assert resolve_sender(None).kind == KIND_DEFAULT_SHARED


# --- fail-closed on the estate itself (gate (c)) -------------------------------------------


def test_unresolvable_when_the_default_sender_is_absent(monkeypatch):
    from orchestrator.integrations.sender_resolution import SenderUnresolvable, resolve_sender

    monkeypatch.delenv("TEAM_TWILIO_FROM_NUMBER", raising=False)
    with pytest.raises(SenderUnresolvable):
        resolve_sender(uuid4())


def test_unresolvable_when_the_default_sender_is_malformed(monkeypatch):
    from orchestrator.integrations.sender_resolution import SenderUnresolvable, resolve_sender

    monkeypatch.setenv("TEAM_TWILIO_FROM_NUMBER", "+91998886e+11")
    with pytest.raises(SenderUnresolvable):
        resolve_sender(uuid4())


# --- the RLS-scoped path and the other row factory ----------------------------------------


def test_own_waba_resolves_through_tenant_connection(substrate, own_waba_enabled):
    """The no-conn path (`tenant_connection`, SET ROLE app_role + tenant GUC) must see the WABA row.
    Not vacuous: the assertion is the seeded number, so an RLS-blocked read raises instead."""
    from orchestrator import graph as graphmod
    from orchestrator.integrations.sender_resolution import KIND_OWN_WABA, resolve_sender

    graphmod.init_substrate(substrate.dsn)
    own = _unique_number()
    with _conn(substrate.dsn) as conn:
        tid = _tenant(conn)
        _waba(conn, tid, status="live", number=own)

    sender = resolve_sender(tid, audience="customer")

    assert sender.phone_number == own
    assert sender.kind == KIND_OWN_WABA


def test_resolve_sender_reads_a_tuple_row_connection_too(substrate, own_waba_enabled):
    """`wa_send_allowed` accepts either row factory and so must this — a caller's connection is not
    ours to assume. Without the positional branch this would refuse a tenant that HAS a live WABA."""
    from orchestrator.integrations.sender_resolution import KIND_OWN_WABA, resolve_sender

    own = _unique_number()
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:  # default: tuple rows
        tid = _tenant(conn)
        _waba(conn, tid, status="live", number=own)

        sender = resolve_sender(tid, conn=conn, audience="customer")

    assert sender.phone_number == own
    assert sender.kind == KIND_OWN_WABA


# --- the template send site ----------------------------------------------------------------


def test_template_customer_send_without_own_waba_returns_a_failed_result(
    substrate, monkeypatch, own_waba_enabled
):
    """`send_template_message` REPORTS an unresolvable sender; it does not raise.

    It is a `@DBOS.step`, so a raise would be RETRIED — and a sender does not become resolvable on a
    retry. The refusal here is real, not stubbed: a live tenant with no WABA row, sent as a customer
    send, which is precisely the case that used to leave from the shared number and produce a message
    the customer could not answer.
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
        twilio_send, "_client", lambda: NS(messages=NS(create=lambda **kw: sent.append(kw)))
    )

    with _conn(substrate.dsn) as conn:
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
    with _conn(substrate.dsn) as conn:
        a = _tenant(conn)
        b = _tenant(conn)
        _waba(conn, a, status="live", number=shared)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _waba(conn, b, status="live", number=shared)


def test_migration_207_allows_a_duplicate_number_on_non_live_rows(substrate):
    """Partial index: a superseded/parked row keeping a historical number is legitimate, and the
    routing query only ever reads live rows."""
    historical = _unique_number()
    with _conn(substrate.dsn) as conn:
        a = _tenant(conn)
        b = _tenant(conn)
        _waba(conn, a, status="pending", number=historical)
        _waba(conn, b, status="pending", number=historical)
