"""VT-476 — dep-less unit tests for the dev send-guard wrapper logic.

These exercise ``orchestrator.utils.dev_send_guard`` in ISOLATION — no DB, no DBOS,
no twilio (the module is stdlib-only) — so they run in the lightweight CI ``test``
job + the pre-push dep-less smoke, where twilio/dbos are NOT installed. The live
transport wiring (every send funnels through ``twilio_send._client()``, which wraps
via the guard) is asserted by ``test_dev_send_guard_transport.py`` (the
twilio/DBOS-requiring funnel proof).

Core breach-stopping property under test: on dev, a send to a non-allowlisted real
number is MOCKED — the wrapped (real) client's ``messages.create`` is NEVER called.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.utils.dev_send_guard import (
    DevSendGuardClient,
    _allowlist,
    _normalize_number,
    is_prod_env,
    maybe_wrap_for_dev,
)

# Fazal's real number — the number the breach actually messaged.
FAZAL_NUMBER = "+919321553267"


class _RecordingMessages:
    """Fake inner Twilio ``.messages`` that RECORDS every real create(). A recorded
    call here means the guard FAILED to block — the breach."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(sid="SMREAL" + "0" * 26, status="queued")


class _RecordingClient:
    def __init__(self) -> None:
        self.messages = _RecordingMessages()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("EXPECTED_ENV", raising=False)
    monkeypatch.delenv("DEV_SEND_ALLOWLIST", raising=False)


# --- normalization: whatsapp:/+/spaces all compare equal ---


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


def test_allowlist_empty_by_default():
    assert _allowlist() == set()


def test_allowlist_parses_and_normalizes(monkeypatch):
    monkeypatch.setenv("DEV_SEND_ALLOWLIST", "whatsapp:+919321553267, +91 99999 00000 ,")
    assert _allowlist() == {"919321553267", "919999900000"}


# --- THE breach-stopping property: non-allowlisted dev send NEVER hits real Twilio ---


def test_dev_empty_allowlist_mocks_all(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    inner = _RecordingClient()
    guarded = maybe_wrap_for_dev(inner)
    assert isinstance(guarded, DevSendGuardClient)
    msg = guarded.messages.create(to=f"whatsapp:{FAZAL_NUMBER}", body="onboarding q")
    assert inner.messages.calls == [], "BREACH: a real Twilio send escaped the dev guard"
    assert msg.sid.startswith("MKDEV")


def test_dev_default_env_is_dev_mocks(monkeypatch):
    """EXPECTED_ENV UNSET defaults to dev (never silently prod) → still mocks."""
    inner = _RecordingClient()
    guarded = maybe_wrap_for_dev(inner)
    msg = guarded.messages.create(to=FAZAL_NUMBER, body="x")
    assert inner.messages.calls == []
    assert msg.sid.startswith("MKDEV")


def test_dev_non_allowlisted_mocked_when_other_number_allowed(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setenv("DEV_SEND_ALLOWLIST", FAZAL_NUMBER)
    inner = _RecordingClient()
    guarded = maybe_wrap_for_dev(inner)
    guarded.messages.create(to="whatsapp:+447911123456", body="x")  # different number
    assert inner.messages.calls == [], "non-allowlisted dev send must be mocked"


def test_missing_destination_mocked(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setenv("DEV_SEND_ALLOWLIST", FAZAL_NUMBER)
    inner = _RecordingClient()
    guarded = maybe_wrap_for_dev(inner)
    msg = guarded.messages.create(to=None, body="x")
    assert inner.messages.calls == []
    assert msg.sid.startswith("MKDEV")


# --- the allowlist DOES let an explicit number through (so Fazal can test) ---


def test_dev_allowlisted_number_real_send(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setenv("DEV_SEND_ALLOWLIST", FAZAL_NUMBER)
    inner = _RecordingClient()
    guarded = maybe_wrap_for_dev(inner)
    msg = guarded.messages.create(to=f"whatsapp:{FAZAL_NUMBER}", body="x")
    assert len(inner.messages.calls) == 1, "allowlisted dev send must reach real Twilio"
    assert msg.sid.startswith("SMREAL")


@pytest.mark.parametrize(
    "allowlist_spec,to_value",
    [
        ("+919321553267", "whatsapp:+919321553267"),
        ("919321553267", "+919321553267"),
        ("+91 93215 53267", "whatsapp:+919321553267"),
        ("+10000000000,+919321553267", "+919321553267"),
    ],
)
def test_allowlist_normalization_matches(monkeypatch, allowlist_spec, to_value):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setenv("DEV_SEND_ALLOWLIST", allowlist_spec)
    inner = _RecordingClient()
    guarded = maybe_wrap_for_dev(inner)
    guarded.messages.create(to=to_value, body="x")
    assert len(inner.messages.calls) == 1, f"{allowlist_spec!r} should match {to_value!r}"


# --- prod is UNAFFECTED: guard inert, real sends as today ---


def test_prod_env_guard_inert(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    inner = _RecordingClient()
    out = maybe_wrap_for_dev(inner)
    assert out is inner, "prod must get the unwrapped real client (guard inert)"
    out.messages.create(to=FAZAL_NUMBER, body="x")  # allowlist ignored
    assert len(inner.messages.calls) == 1


def test_is_prod_env_positive_only(monkeypatch):
    monkeypatch.delenv("EXPECTED_ENV", raising=False)
    assert is_prod_env() is False
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    assert is_prod_env() is False
    monkeypatch.setenv("EXPECTED_ENV", "staging")
    assert is_prod_env() is False  # only a positive 'prod' reads prod
    monkeypatch.setenv("EXPECTED_ENV", "PROD")
    assert is_prod_env() is True  # case-insensitive
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    assert is_prod_env() is True


# --- structural: the transport client installs the guard (no send path escapes) ---


def test_client_installs_guard_at_source():
    """``twilio_send._client()`` must wrap its resolved client via ``maybe_wrap_for_dev``,
    and BOTH send functions must obtain their transport from ``_client()`` — so EVERY
    send path is guarded. Asserted from the module SOURCE (immune to the conftest autouse
    stub that rebinds the ``_client`` attribute at runtime)."""
    import inspect

    # twilio_send imports twilio/dbos at module top; skip in the dep-less smoke job.
    pytest.importorskip("twilio")
    pytest.importorskip("dbos")
    import orchestrator.utils.twilio_send as ts

    source = inspect.getsource(ts)
    start = source.index("def _client(")
    nxt = source.index("\ndef ", start + 1)
    assert "maybe_wrap_for_dev" in source[start:nxt], (
        "twilio_send._client() must wrap its client via maybe_wrap_for_dev — "
        "otherwise a send path can reach real Twilio on dev"
    )
    for fn in ("send_template_message", "send_freeform_message"):
        fstart = source.index(f"def {fn}(")
        rest = source[fstart + 1:]
        fend = source.index("\ndef ", fstart + 1) if "\ndef " in rest else len(source)
        assert "_client()" in source[fstart:fend], (
            f"{fn} must send via _client() (the guarded transport), not a raw Client()"
        )
