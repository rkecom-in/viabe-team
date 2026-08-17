"""VT-742 §1 — every send site takes its ``from_`` from ``resolve_sender``, and a shared number
never resolves a customer inbound.

These are the tests that would have caught the original defect. For two months
``tenant_whatsapp_accounts.phone_number`` was populated per tenant (138 live rows on dev) while all
three ``messages.create`` sites read ``os.environ["TEAM_TWILIO_FROM_NUMBER"]`` — so the assertions
here deliberately set the env to a DIFFERENT number than the resolver returns. A regression to an
env read reads as the env value and fails, instead of coincidentally matching.

No DB: ``resolve_sender(None)`` and a monkeypatched resolver cover the send-site wiring. The
precedence itself is proven against real Postgres in
``tests/orchestrator/integrations/test_vt742_sender_resolution.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("twilio")
pytest.importorskip("dbos")

from orchestrator.integrations.sender_resolution import (  # noqa: E402
    KIND_OWN_WABA,
    Sender,
    SenderUnresolvable,
)
from orchestrator.utils import twilio_send  # noqa: E402

_ENV_SENDER = "+910000000000"
_OWN_WABA = "+919900000123"


class _SpyMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(sid="SM" + "0" * 32)


class _SpyClient:
    def __init__(self):
        self.messages = _SpyMessages()


@pytest.fixture
def spy(monkeypatch):
    client = _SpyClient()
    monkeypatch.setattr(twilio_send, "_client", lambda: client)
    monkeypatch.setenv("TEAM_TWILIO_FROM_NUMBER", _ENV_SENDER)
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "vt742-test-salt")
    return client


def _resolver_spy(monkeypatch, sender: Sender | None = None, raises: bool = False):
    """Replace twilio_send's bound resolve_sender and record how each site called it."""
    calls: list[dict] = []

    def _fake(tenant_id, *, audience="owner", conn=None):
        calls.append({"tenant_id": tenant_id, "audience": audience})
        if raises:
            raise SenderUnresolvable("no sender (test)")
        return sender or Sender(_OWN_WABA, KIND_OWN_WABA, str(tenant_id))

    monkeypatch.setattr(twilio_send, "resolve_sender", _fake)
    return calls


def test_freeform_from_is_the_resolved_sender_not_the_env(spy, monkeypatch):
    tid = uuid4()
    calls = _resolver_spy(monkeypatch)

    twilio_send.send_freeform_message(
        "Aaj ka update ready hai.", "+919321553267", tenant_id=tid, record_turn=False
    )

    call = spy.messages.calls[0]
    assert call["from_"] == f"whatsapp:{_OWN_WABA}", (
        "the freeform site must send from the RESOLVED sender; "
        f"got {call['from_']!r} (the env sender is {_ENV_SENDER})"
    )
    assert calls == [{"tenant_id": tid, "audience": "owner"}], (
        "an owner freeform send resolves within the shared Viabe estate, not the tenant's WABA"
    )


def test_interactive_from_is_the_resolved_sender_not_the_env(spy, monkeypatch):
    tid = uuid4()
    calls = _resolver_spy(monkeypatch)

    twilio_send.send_interactive_message(
        "HX60ace8008b02439ca0db444dee6327d2",
        "+919321553267",
        content_variables={"1": "Local services — right?"},
        tenant_id=tid,
        record_turn=False,
    )

    assert spy.messages.calls[0]["from_"] == f"whatsapp:{_OWN_WABA}"
    assert calls[0]["audience"] == "owner"


def test_customer_session_send_requires_the_tenants_own_waba(spy, monkeypatch):
    """A customer-facing send must demand the tenant's own live WABA.

    Not stylistic: customer inbound resolves the tenant by the number the customer messaged TO
    (`_lookup_customer_inbound_tenant`), so a message sent from the shared number cannot be replied
    to at all. VT-742 finding 2 — the two halves of the conversation were built against different
    senders.
    """
    tid = uuid4()
    calls = _resolver_spy(monkeypatch)

    with twilio_send.customer_send_context():
        twilio_send.send_freeform_message(
            "Namaste! Aapke liye ek offer hai.",
            "+919321553267",
            is_customer_session=True,
            tenant_id=tid,
        )

    assert calls == [{"tenant_id": tid, "audience": "customer"}], (
        "is_customer_session=True must resolve with audience=customer"
    )
    assert spy.messages.calls[0]["from_"] == f"whatsapp:{_OWN_WABA}"


def test_freeform_unresolvable_sender_raises_and_never_dispatches(spy, monkeypatch):
    _resolver_spy(monkeypatch, raises=True)

    with pytest.raises(SenderUnresolvable):
        twilio_send.send_freeform_message(
            "should never go out", "+919321553267", tenant_id=uuid4(), record_turn=False
        )

    assert spy.messages.calls == [], "fail-closed: nothing may reach messages.create"


# The template site's fail-closed behaviour needs a real tenant connection (it resolves the
# recipient and the sender on ONE connection), so it is proven in
# tests/orchestrator/integrations/test_vt742_sender_resolution.py against real Postgres — where the
# refusal comes from an actual tenant with no live WABA rather than a stubbed raise.


# --- the inbound half -----------------------------------------------------------------------


def test_customer_inbound_refuses_a_shared_number_without_querying(monkeypatch):
    """A customer inbound addressed to a SHARED number resolves to no tenant, and does not even ask.

    If the shared number were ever written into some tenant's ``tenant_whatsapp_accounts`` row, the
    ``WHERE phone_number = %s AND status='live'`` lookup would hand EVERY tenant's customer replies
    to that one tenant. ``get_pool`` is replaced with a raiser, so a query would fail the test —
    the guard must precede the lookup, not filter its result.
    """
    from orchestrator.api import twilio_ingress

    monkeypatch.setenv("TEAM_TWILIO_FROM_NUMBER", _ENV_SENDER)
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "vt742-test-salt")

    def _no_db():
        raise AssertionError("the shared-number guard must return before touching the DB")

    monkeypatch.setattr(twilio_ingress, "get_pool", _no_db)

    assert twilio_ingress._lookup_customer_inbound_tenant(_ENV_SENDER) is None


def test_customer_inbound_refuses_every_number_in_the_shared_estate(monkeypatch):
    """The estate CSV exists so the guard keeps holding when a second shared number is bought —
    without a code change at that moment."""
    from orchestrator.api import twilio_ingress

    second = "+910000000001"
    monkeypatch.setenv("TEAM_TWILIO_FROM_NUMBER", _ENV_SENDER)
    monkeypatch.setenv("TEAM_TWILIO_SHARED_SENDER_NUMBERS", f"{second}, +910000000002")
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "vt742-test-salt")
    monkeypatch.setattr(
        twilio_ingress,
        "get_pool",
        lambda: (_ for _ in ()).throw(AssertionError("guard must precede the DB")),
    )

    assert twilio_ingress._lookup_customer_inbound_tenant(second) is None
    assert twilio_ingress._lookup_customer_inbound_tenant("+910000000002") is None
