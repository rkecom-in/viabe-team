"""VT-476 — adversarial-verify for the dev transport send-guard (SAFETY-CRITICAL).

The guard is the OUTER gate that stops dev from sending real WhatsApp to real
numbers. The acceptance bar (must be airtight):

  1. dev + EMPTY allowlist → a send to Fazal's real number is MOCKED: the inner
     Twilio client's ``messages.create`` is NEVER invoked, a mock-success result is
     returned, the call site does not crash.
  2. dev + ALLOWLISTED number → real path reached (inner client IS called).
  3. EXPECTED_ENV=prod → guard inert: real send path regardless of allowlist.
  4. EVERY send path funnels through the guard: the freeform owner-send
     (send_freeform_message), the template send (send_template_message), AND the
     customer-send (via send_template_message) all hit the guard on dev — none
     bypasses. This is the core safety property; the breach was a bypassing path.

These tests do NOT use the package ``twilio_create`` autouse fixture's monkeypatch
of ``_client`` (which would bypass the guard). They install the guard over a SPY
inner client so we can assert whether the REAL transport (inner.messages.create)
was reached.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

pytest.importorskip("twilio")

from orchestrator.utils import twilio_send  # noqa: E402
from orchestrator.utils.dev_send_guard import (  # noqa: E402
    DevSendGuardClient,
    _allowlist,
    _normalize_number,
    is_prod_env,
    maybe_wrap_for_dev,
)

# Fazal's real number — the number the breach actually messaged. The guard MUST
# mock a dev send to it when it is not explicitly allowlisted.
FAZAL_NUMBER = "+919321553267"


@pytest.fixture
def spy_inner():
    """A spy standing in for the REAL Twilio client.

    ``inner.messages.create`` returns a real-shaped Message (``SM…`` sid) and
    records every call, so a test can assert whether the REAL transport was hit.
    """
    inner = MagicMock()
    inner.messages.create = MagicMock(
        return_value=SimpleNamespace(sid="SM" + "1" * 32, status="queued")
    )
    return inner


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test owns EXPECTED_ENV + DEV_SEND_ALLOWLIST explicitly; start unset."""
    monkeypatch.delenv("EXPECTED_ENV", raising=False)
    monkeypatch.delenv("DEV_SEND_ALLOWLIST", raising=False)
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "devsalt")
    monkeypatch.setenv("TEAM_TWILIO_FROM_NUMBER", "+910000000000")
    monkeypatch.setenv("TEAM_TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TEAM_TWILIO_AUTH_TOKEN", "test-token")


# --------------------------------------------------------------------------- #
# Normalization — the comparison must survive whatsapp:/+/spaces variants.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "+919321553267",
        "919321553267",
        "whatsapp:+919321553267",
        "whatsapp:919321553267",
        " +91 93215 53267 ",
        "whatsapp:+91 93215 53267",
    ],
)
def test_normalize_collapses_to_canonical(raw):
    assert _normalize_number(raw) == "919321553267"


def test_normalize_missing_is_empty():
    assert _normalize_number(None) == ""
    assert _normalize_number("") == ""


def test_allowlist_empty_by_default(monkeypatch):
    monkeypatch.delenv("DEV_SEND_ALLOWLIST", raising=False)
    assert _allowlist() == set()


def test_allowlist_parses_and_normalizes(monkeypatch):
    monkeypatch.setenv("DEV_SEND_ALLOWLIST", "whatsapp:+919321553267, +91 99999 00000 ,")
    assert _allowlist() == {"919321553267", "919999900000"}


# --------------------------------------------------------------------------- #
# 1. dev + EMPTY allowlist → Fazal's number is MOCKED (no real Twilio call).
# --------------------------------------------------------------------------- #


def test_dev_empty_allowlist_mocks_fazal_number_no_real_call(spy_inner, monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.delenv("DEV_SEND_ALLOWLIST", raising=False)  # empty → fail-closed

    guarded = maybe_wrap_for_dev(spy_inner)
    assert isinstance(guarded, DevSendGuardClient)  # wrapped on dev

    result = guarded.messages.create(
        to=f"whatsapp:{FAZAL_NUMBER}", from_="whatsapp:+910000000000", body="hi"
    )

    # CORE SAFETY ASSERTION: the REAL Twilio transport was NEVER invoked.
    spy_inner.messages.create.assert_not_called()
    # A success-shaped mock result is returned (call site proceeds, no crash).
    assert result.sid.startswith("MKDEV")
    assert result.status == "queued"


def test_dev_empty_allowlist_mocks_even_without_plus_or_prefix(spy_inner, monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    guarded = maybe_wrap_for_dev(spy_inner)
    guarded.messages.create(to="919321553267", from_="x", body="hi")
    spy_inner.messages.create.assert_not_called()


# --------------------------------------------------------------------------- #
# 2. dev + ALLOWLISTED number → real path reached (inner client IS called).
# --------------------------------------------------------------------------- #


def test_dev_allowlisted_number_reaches_real_transport(spy_inner, monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setenv("DEV_SEND_ALLOWLIST", FAZAL_NUMBER)

    guarded = maybe_wrap_for_dev(spy_inner)
    result = guarded.messages.create(
        to=f"whatsapp:{FAZAL_NUMBER}", from_="whatsapp:+910000000000", body="hi"
    )

    spy_inner.messages.create.assert_called_once()
    assert result.sid.startswith("SM")  # the REAL client's SID, not a mock


def test_dev_allowlist_matches_across_format_variants(spy_inner, monkeypatch):
    """A bare-digits allowlist entry still matches a whatsapp:+ -prefixed send."""
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setenv("DEV_SEND_ALLOWLIST", "919321553267")  # bare digits
    guarded = maybe_wrap_for_dev(spy_inner)
    guarded.messages.create(to=f"whatsapp:{FAZAL_NUMBER}", from_="x", body="hi")
    spy_inner.messages.create.assert_called_once()


def test_dev_non_allowlisted_still_mocked_when_other_number_allowed(
    spy_inner, monkeypatch
):
    """Only the EXACT allowlisted number gets through; a different dev send is mocked."""
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setenv("DEV_SEND_ALLOWLIST", "+919999900000")  # a different number
    guarded = maybe_wrap_for_dev(spy_inner)
    guarded.messages.create(to=f"whatsapp:{FAZAL_NUMBER}", from_="x", body="hi")
    spy_inner.messages.create.assert_not_called()


# --------------------------------------------------------------------------- #
# 3. EXPECTED_ENV=prod → guard inert: real send regardless of allowlist.
# --------------------------------------------------------------------------- #


def test_prod_guard_is_inert_real_send_empty_allowlist(spy_inner, monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    monkeypatch.delenv("DEV_SEND_ALLOWLIST", raising=False)

    guarded = maybe_wrap_for_dev(spy_inner)
    # On prod the client is returned UNWRAPPED — guard never installed.
    assert guarded is spy_inner
    guarded.messages.create(to=f"whatsapp:{FAZAL_NUMBER}", from_="x", body="hi")
    spy_inner.messages.create.assert_called_once()


def test_prod_ignores_allowlist_entirely(spy_inner, monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    monkeypatch.setenv("DEV_SEND_ALLOWLIST", "+10000000000")  # some other number
    guarded = maybe_wrap_for_dev(spy_inner)
    assert guarded is spy_inner
    guarded.messages.create(to=f"whatsapp:{FAZAL_NUMBER}", from_="x", body="hi")
    spy_inner.messages.create.assert_called_once()


def test_is_prod_env_signal():
    import os

    os.environ["EXPECTED_ENV"] = "prod"
    assert is_prod_env() is True
    os.environ["EXPECTED_ENV"] = "PROD"
    assert is_prod_env() is True  # case-insensitive
    os.environ["EXPECTED_ENV"] = "dev"
    assert is_prod_env() is False
    del os.environ["EXPECTED_ENV"]
    assert is_prod_env() is False  # default dev


# --------------------------------------------------------------------------- #
# 4. EVERY send path funnels through the guard on dev — the core safety property.
#    We install the guard at the real _client() seam (over a spy inner) and drive
#    the actual public send functions: freeform owner-send, template send, and
#    customer-send. NONE may reach the real transport on dev with an empty
#    allowlist.
# --------------------------------------------------------------------------- #


@pytest.fixture
def guarded_client_at_seam(spy_inner, twilio_create, monkeypatch):
    """Install a REAL DevSendGuardClient (wrapping the spy) at twilio_send._client.

    Depends on ``twilio_create`` (the package autouse stub) so this setattr runs
    AFTER it and WINS — replacing the package's bare-MagicMock ``_client`` with a
    genuine guard wrapping our spy. The guard is thus genuinely in the path —
    exactly the production wiring, with only the innermost Twilio network client
    replaced by a spy.
    """
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.delenv("DEV_SEND_ALLOWLIST", raising=False)
    guarded = DevSendGuardClient(spy_inner)
    monkeypatch.setattr(twilio_send, "_client", lambda: guarded)
    return spy_inner


def test_freeform_owner_send_funnels_through_guard(guarded_client_at_seam):
    """send_freeform_message (the BREACH path: onboarding owner-send) is mocked on dev."""
    spy_inner = guarded_client_at_seam
    sid = twilio_send.send_freeform_message(
        "onboarding question 1?", FAZAL_NUMBER
    )
    # NO real Twilio call; a mock SID is returned so the onboarding flow proceeds.
    spy_inner.messages.create.assert_not_called()
    assert sid.startswith("MKDEV")


def test_template_owner_send_funnels_through_guard(guarded_client_at_seam, monkeypatch):
    """send_template_message (owner template) is mocked on dev with empty allowlist."""
    spy_inner = guarded_client_at_seam
    # Resolve to a real-shaped registry entry so we reach the transport call.
    monkeypatch.setattr(
        twilio_send,
        "_registry_resolve",
        lambda name, lang="en": SimpleNamespace(
            content_sid="HXdev", variables=("owner_name",), audience="owner"
        ),
    )
    result = twilio_send.send_template_message(
        uuid4(), "team_welcome", {"owner_name": "Asha"}, recipient_phone=FAZAL_NUMBER
    )
    spy_inner.messages.create.assert_not_called()
    assert result.success is True
    assert result.message_sid.startswith("MKDEV")


def test_customer_send_funnels_through_guard(guarded_client_at_seam, monkeypatch):
    """A CUSTOMER template send (is_customer_send=True, inside the gated context)
    STILL hits the dev guard — the customer compliance rail and the dev guard are
    independent layers; the dev guard mocks the transport regardless."""
    spy_inner = guarded_client_at_seam
    monkeypatch.setattr(
        twilio_send,
        "_registry_resolve",
        lambda name, lang="en": SimpleNamespace(
            content_sid="HXcust", variables=("owner_name",), audience="customer"
        ),
    )
    # Enter the gated customer-send context (the compliance rail) — the dev guard
    # is an ADDITIONAL outer gate, so the send is STILL mocked on dev.
    with twilio_send.customer_send_context():
        result = twilio_send.send_template_message(
            uuid4(),
            "team_winback",
            {"owner_name": "Asha"},
            recipient_phone=FAZAL_NUMBER,
            is_customer_send=True,
        )
    spy_inner.messages.create.assert_not_called()
    assert result.success is True
    assert result.message_sid.startswith("MKDEV")


def test_allowlisted_send_reaches_real_transport_through_public_fn(
    spy_inner, twilio_create, monkeypatch
):
    """End-to-end through the public freeform fn: an ALLOWLISTED dev send DOES reach
    the real transport (the guard is selective, not a blanket block)."""
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setenv("DEV_SEND_ALLOWLIST", FAZAL_NUMBER)
    guarded = DevSendGuardClient(spy_inner)
    monkeypatch.setattr(twilio_send, "_client", lambda: guarded)

    sid = twilio_send.send_freeform_message("hello", FAZAL_NUMBER)
    spy_inner.messages.create.assert_called_once()
    assert sid.startswith("SM")  # the real client's SID


def test_compliance_rail_still_fires_before_dev_guard(monkeypatch):
    """The dev guard does NOT weaken the customer-send compliance rail: a customer
    send OUTSIDE the gated context still raises UngatedCustomerSendError — the rail
    refuses BEFORE the transport (and thus before the dev guard) is reached."""
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setattr(
        twilio_send,
        "_registry_resolve",
        lambda name, lang="en": SimpleNamespace(
            content_sid="HXcust", variables=(), audience="customer"
        ),
    )
    with pytest.raises(twilio_send.UngatedCustomerSendError):
        twilio_send.send_template_message(
            uuid4(),
            "team_winback",
            {},
            recipient_phone=FAZAL_NUMBER,
            is_customer_send=True,  # flagged customer, NOT in customer_send_context
        )
