"""VT-332 — mint_trial_end_token canary (orchestrator self-mint). PyJWT round-trip + the
audience-isolation line. No DB (pure mint)."""

from __future__ import annotations

import pytest

# trial_end_token imports PyJWT (`jwt`) → skip in the dep-less smoke; runs in the full suite.
pytest.importorskip("jwt")
# Importing orchestrator.billing.* pulls billing/__init__ → attribution_close, which imports psycopg
# at module level → skip dep-less (psycopg absent); runs in the full orchestrator job.
pytest.importorskip("psycopg")

import jwt as pyjwt  # noqa: E402

from orchestrator.billing.trial_end_token import (  # noqa: E402
    TrialEndSecretMissing,
    build_subscribe_deep_link,
    mint_trial_end_token,
)

_SECRET = "vt332-test-owner-jwt-secret"


def test_mint_round_trip(monkeypatch):
    monkeypatch.setenv("OWNER_JWT_SECRET", _SECRET)
    token, jti = mint_trial_end_token("tenant-abc")
    payload = pyjwt.decode(token, _SECRET, algorithms=["HS256"], audience="trial-end-subscribe")
    assert payload["tenant_id"] == "tenant-abc"
    assert "plan_tier" not in payload  # VT-332 F3: no tier claim (pure auth; tier is owner-choice)
    assert payload["aud"] == "trial-end-subscribe"
    assert payload["jti"] == jti  # the returned jti is the token's jti (the single-use key)
    assert payload["exp"] > payload["iat"]


def test_mint_unique_jti(monkeypatch):
    monkeypatch.setenv("OWNER_JWT_SECRET", _SECRET)
    _, jti1 = mint_trial_end_token("t")
    _, jti2 = mint_trial_end_token("t")
    assert jti1 != jti2  # each mint → a fresh single-use key


def test_mint_requires_secret(monkeypatch):
    monkeypatch.delenv("OWNER_JWT_SECRET", raising=False)
    with pytest.raises(TrialEndSecretMissing):
        mint_trial_end_token("t")  # fail closed — never mint a blank-key token


def test_audience_isolation(monkeypatch):
    """A trial-end token must NOT verify as another audience (a leaked deep-link token can't be
    replayed as an owner session)."""
    monkeypatch.setenv("OWNER_JWT_SECRET", _SECRET)
    token, _ = mint_trial_end_token("t")
    with pytest.raises(pyjwt.InvalidAudienceError):
        pyjwt.decode(token, _SECRET, algorithms=["HS256"], audience="viabe-owner-session")


def test_deep_link_builder(monkeypatch):
    monkeypatch.setenv("OWNER_JWT_SECRET", _SECRET)
    token, _ = mint_trial_end_token("t")
    link = build_subscribe_deep_link("https://viabe.ai/", "founding", token)
    assert link == f"https://viabe.ai/team/subscribe?plan=founding&token={token}"
