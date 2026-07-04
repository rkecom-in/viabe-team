"""VT-598 addendum — the dev-only /api/orchestrator/dev-test/consent-seed endpoint's guard.

LIVE FINDING this endpoint fixes: canaries/convo_harness.py's --seed-lapsed-customers previously
called orchestrator.privacy.consent.record_consent() directly in the HARNESS's own process (via
`railway run`, which does not inject the sealed TEAM_PHONE_HASH_SALT) — tokenising with a throwaway
salt that never matches what the DEPLOYED service computes server-side, so a seeded consent row
could never join against the real sales_recovery detection query (a seeded cohort always read as
empty). The fix moves tokenisation into the deployed process itself via this new endpoint.

Mirrors test_convo_harness_ingress_gate.py's pattern: pure guard tests calling the route function
DIRECTLY (no TestClient, no DB) since `_verify_internal_secret` is checked BEFORE `record_consent`
is ever touched — a rejected request never reaches the DB. The guard-passes paths monkeypatch
`record_consent` to a stub (never touches a DB), keeping this whole file DB-free like its sibling.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

pytest.importorskip("dbos")
pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402 — after the dependency skip guards

import orchestrator.api.twilio_ingress as ti  # noqa: E402
from orchestrator.privacy.consent import ConsentRecord  # noqa: E402

_PROD_SECRET = "prod-internal-secret-value"
_DEV_SECRET = "harness-dev-ingress-secret-value"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Same known-env baseline as test_convo_harness_ingress_gate.py: both secrets set,
    EXPECTED_ENV unset. Each test overrides what it needs."""
    monkeypatch.setenv("INTERNAL_API_SECRET", _PROD_SECRET)
    monkeypatch.setenv("DEV_TEST_INGRESS_SECRET", _DEV_SECRET)
    monkeypatch.delenv("EXPECTED_ENV", raising=False)
    yield


def _body(**overrides) -> ti.ConsentSeedBody:
    defaults = {
        "tenant_id": str(uuid4()),
        "phone_e164": "+15550123456",
        "consent_text_version": "dev-test-v0",
    }
    defaults.update(overrides)
    return ti.ConsentSeedBody(**defaults)


def _fake_record(**overrides) -> ConsentRecord:
    defaults: dict = {
        "tenant_id": uuid4(),
        "phone_token": "abcdef0123456789fedcba",
        "consent_text_version": "dev-test-v0",
        "consent_method": "dev_test_seed",
        "active": True,
    }
    defaults.update(overrides)
    return ConsentRecord(**defaults)


# --- guard: wrong / missing secret ---------------------------------------------------------------


def test_wrong_secret_rejected_on_dev(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    with pytest.raises(HTTPException) as exc_info:
        ti.dev_test_consent_seed(_body(), x_internal_secret="totally-wrong")
    assert exc_info.value.status_code == 403


def test_missing_secret_rejected(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    with pytest.raises(HTTPException) as exc_info:
        ti.dev_test_consent_seed(_body(), x_internal_secret=None)
    assert exc_info.value.status_code == 403


# --- guard: DEV_TEST_INGRESS_SECRET is env-gated (CL-431 fail-closed, mirrors twilio-ingress) -----


def test_dev_secret_rejected_on_prod(monkeypatch):
    # Tightened 2026-07-04: off-dev the route answers 404 for EVERYTHING —
    # indistinguishable from route-absent (no 403 oracle that a guard exists).
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    with pytest.raises(HTTPException) as exc_info:
        ti.dev_test_consent_seed(_body(), x_internal_secret=_DEV_SECRET)
    assert exc_info.value.status_code == 404


def test_dev_secret_rejected_when_env_unset(monkeypatch):
    monkeypatch.delenv("EXPECTED_ENV", raising=False)
    with pytest.raises(HTTPException) as exc_info:
        ti.dev_test_consent_seed(_body(), x_internal_secret=_DEV_SECRET)
    assert exc_info.value.status_code == 404


def test_dev_secret_rejected_on_garbage_env(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "not-a-real-env")
    with pytest.raises(HTTPException) as exc_info:
        ti.dev_test_consent_seed(_body(), x_internal_secret=_DEV_SECRET)
    assert exc_info.value.status_code == 404


def test_dev_secret_rejected_on_development_typo_variant_is_still_accepted(monkeypatch):
    # "development" IS one of the two accepted spellings (mirrors _dev_ingress_enabled's matrix) —
    # not a rejection case, asserted here for contrast with the rejections above.
    monkeypatch.setenv("EXPECTED_ENV", "development")
    stub = MagicMock(return_value=_fake_record())
    monkeypatch.setattr(ti, "record_consent", stub)
    result = ti.dev_test_consent_seed(_body(), x_internal_secret=_DEV_SECRET)
    assert result.recorded is True


# --- guard passes: ONLY on a positively-dev EXPECTED_ENV (tightened 2026-07-04) -------------------
# record_consent is monkeypatched to a stub in every passing case — never touches a DB.


def test_dev_secret_accepted_on_dev_reaches_record_consent(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    stub = MagicMock(return_value=_fake_record(phone_token="abcdef0123456789fedcba", active=True))
    monkeypatch.setattr(ti, "record_consent", stub)

    result = ti.dev_test_consent_seed(_body(), x_internal_secret=_DEV_SECRET)

    assert stub.called
    assert result.recorded is True
    assert result.active is True
    assert result.phone_token_prefix == "abcdef012345"  # first 12 chars only — never the full token


def test_prod_secret_rejected_off_dev(monkeypatch):
    # Tightened 2026-07-04 (review decision): unlike /consent/capture, even the prod
    # INTERNAL_API_SECRET cannot open this route off-dev — a dev-test seeding surface
    # has no legitimate prod use, so off-dev it does not exist (404).
    monkeypatch.delenv("EXPECTED_ENV", raising=False)  # no dev env at all
    stub = MagicMock(return_value=_fake_record())
    monkeypatch.setattr(ti, "record_consent", stub)

    with pytest.raises(HTTPException) as exc_info:
        ti.dev_test_consent_seed(_body(), x_internal_secret=_PROD_SECRET)

    assert exc_info.value.status_code == 404
    stub.assert_not_called()


def test_prod_secret_accepted_on_dev(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    stub = MagicMock(return_value=_fake_record())
    monkeypatch.setattr(ti, "record_consent", stub)

    result = ti.dev_test_consent_seed(_body(), x_internal_secret=_PROD_SECRET)

    assert stub.called
    assert result.recorded is True


def test_record_consent_called_with_the_given_fields(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    stub = MagicMock(return_value=_fake_record())
    monkeypatch.setattr(ti, "record_consent", stub)
    tenant_id = str(uuid4())

    ti.dev_test_consent_seed(
        _body(tenant_id=tenant_id, phone_e164="+15550999999", consent_text_version="v7"),
        x_internal_secret=_DEV_SECRET,
    )

    args, kwargs = stub.call_args
    assert str(args[0]) == tenant_id
    assert args[1] == "+15550999999"
    assert kwargs["consent_text_version"] == "v7"


# --- invalid tenant_id: rejected BEFORE record_consent is ever called -----------------------------


def test_invalid_tenant_id_rejected_before_record_consent(monkeypatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    stub = MagicMock()
    monkeypatch.setattr(ti, "record_consent", stub)

    with pytest.raises(HTTPException) as exc_info:
        ti.dev_test_consent_seed(_body(tenant_id="not-a-uuid"), x_internal_secret=_DEV_SECRET)

    assert exc_info.value.status_code == 400
    stub.assert_not_called()
