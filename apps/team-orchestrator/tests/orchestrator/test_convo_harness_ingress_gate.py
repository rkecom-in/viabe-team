"""VT-582 — the DEV-ONLY ingress-secret gate on twilio_ingress._verify_internal_secret.

The conversation harness authenticates to the DEPLOYED dev orchestrator with DEV_TEST_INGRESS_SECRET
instead of the real INTERNAL_API_SECRET. That secret MUST be accepted ONLY on a positively-dev env
(EXPECTED_ENV in {dev,development}) and NEVER on prod / unset / garbage (the CL-431 prod gate,
fail-closed). The prod INTERNAL_API_SECRET path must be unchanged on every env.

Pure env-function tests — no DB, no network. Importing the module pulls dbos/fastapi (the module
carries the DBOS ingress workflow), so we skip if those aren't installed, exactly like the sibling
test_twilio_ingress.py.
"""

from __future__ import annotations

import pytest

pytest.importorskip("dbos")
pytest.importorskip("fastapi")

import orchestrator.api.twilio_ingress as ti  # noqa: E402 — after the dependency skip guards

_PROD_SECRET = "prod-internal-secret-value"
_DEV_SECRET = "harness-dev-ingress-secret-value"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start each test from a known env: both secrets set, EXPECTED_ENV unset. Each test overrides
    what it needs so no ambient CI value leaks in."""
    monkeypatch.setenv("INTERNAL_API_SECRET", _PROD_SECRET)
    monkeypatch.setenv("DEV_TEST_INGRESS_SECRET", _DEV_SECRET)
    monkeypatch.delenv("EXPECTED_ENV", raising=False)
    yield


# --- _dev_ingress_enabled: the positively-dev gate --------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("dev", True),
        ("development", True),
        ("DEV", True),          # case-insensitive
        ("  dev  ", True),      # trimmed
        ("prod", False),
        ("production", False),
        ("staging", False),
        ("", False),            # unset-equivalent
        ("garbage", False),
    ],
)
def test_dev_ingress_enabled_matrix(monkeypatch, value, expected):
    monkeypatch.setenv("EXPECTED_ENV", value)
    assert ti._dev_ingress_enabled() is expected


def test_dev_ingress_enabled_unset_is_false(monkeypatch):
    monkeypatch.delenv("EXPECTED_ENV", raising=False)
    assert ti._dev_ingress_enabled() is False


# --- prod INTERNAL_API_SECRET: unchanged on every env -----------------------------------------


@pytest.mark.parametrize("env", ["prod", "dev", "development", "garbage", None])
def test_prod_secret_accepted_on_every_env(monkeypatch, env):
    if env is None:
        monkeypatch.delenv("EXPECTED_ENV", raising=False)
    else:
        monkeypatch.setenv("EXPECTED_ENV", env)
    assert ti._verify_internal_secret(_PROD_SECRET) is True


# --- DEV_TEST_INGRESS_SECRET: dev-only, fail-closed off dev -----------------------------------


def test_dev_secret_accepted_on_dev(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    assert ti._verify_internal_secret(_DEV_SECRET) is True


def test_dev_secret_accepted_on_development(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "development")
    assert ti._verify_internal_secret(_DEV_SECRET) is True


def test_dev_secret_rejected_on_prod(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    assert ti._verify_internal_secret(_DEV_SECRET) is False


def test_dev_secret_rejected_when_env_unset(monkeypatch):
    monkeypatch.delenv("EXPECTED_ENV", raising=False)
    assert ti._verify_internal_secret(_DEV_SECRET) is False


def test_dev_secret_rejected_on_garbage_env(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "not-a-real-env")
    assert ti._verify_internal_secret(_DEV_SECRET) is False


# --- rejection cases --------------------------------------------------------------------------


def test_wrong_value_rejected_on_dev(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    assert ti._verify_internal_secret("neither-secret") is False


def test_empty_provided_rejected(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    assert ti._verify_internal_secret("") is False
    assert ti._verify_internal_secret(None) is False


def test_dev_path_inert_when_dev_secret_unset(monkeypatch):
    """On dev with NO DEV_TEST_INGRESS_SECRET configured, the dev path can authenticate nothing —
    a provided value that isn't the prod secret is rejected (an empty configured secret never
    matches)."""
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.delenv("DEV_TEST_INGRESS_SECRET", raising=False)
    assert ti._verify_internal_secret("anything") is False
    # the prod secret still works
    assert ti._verify_internal_secret(_PROD_SECRET) is True


def test_prod_secret_unset_and_dev_secret_off_dev_rejects_all(monkeypatch):
    """No prod secret configured + off-dev: nothing authenticates (no accidental open door)."""
    monkeypatch.delenv("INTERNAL_API_SECRET", raising=False)
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    assert ti._verify_internal_secret(_DEV_SECRET) is False
    assert ti._verify_internal_secret(_PROD_SECRET) is False
