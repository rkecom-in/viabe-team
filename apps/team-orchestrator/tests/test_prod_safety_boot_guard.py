"""VT-434 — fail-closed boot guard: dangerous test/mock bypass flags impossible in prod.

Proves the boot assertion (``orchestrator.auth.prod_safety.assert_prod_safe_flags``)
REFUSES to boot under ``EXPECTED_ENV=prod`` when a dangerous test/mock auth-bypass /
send-mock flag is on, and stays a no-op on dev (the flags remain usable as the test /
canary convenience).

Dep-less: the guard only reads ``os.environ``. No network, no DB, no twilio import —
so this collects + runs under the dep-less CI ``test`` job and the pre-push smoke. Placed
at the tests/ top level (NOT tests/orchestrator/) so the orchestrator package's autouse
twilio_send stub fixture does not apply.

Cases:
  - prod + TEAM_TWILIO_VERIFY_MOCK_MODE on  → REFUSES (ProdSafetyError)
  - prod + TEAM_TWILIO_MOCK_MODE on         → REFUSES (ProdSafetyError)
  - prod + flags off                        → boots fine (no raise)
  - dev  + either flag on                   → boots fine (dev convenience preserved)
  - prod is detected ONLY by a positive EXPECTED_ENV=prod (default dev; unset ≠ prod)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator.auth.prod_safety import (  # noqa: E402
    _PROD_FORBIDDEN_FLAGS,
    ProdSafetyError,
    assert_prod_safe_flags,
)

# The flags the guard must cover. Kept here as an explicit, independent list so the
# test fails loudly if the guarded set drifts from VT-434's intent.
_DANGEROUS_FLAGS = ("TEAM_TWILIO_VERIFY_MOCK_MODE", "TEAM_TWILIO_MOCK_MODE")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """Start every case from a clean slate — no EXPECTED_ENV, no bypass flags set."""
    monkeypatch.delenv("EXPECTED_ENV", raising=False)
    for flag in _DANGEROUS_FLAGS:
        monkeypatch.delenv(flag, raising=False)
    yield


def test_guard_covers_exactly_the_dangerous_flags():
    """The guarded set is precisely the two dangerous flags (no drift, no over-block)."""
    assert set(_PROD_FORBIDDEN_FLAGS) == set(_DANGEROUS_FLAGS)


# --- prod + a dangerous flag ON → REFUSE -------------------------------------
@pytest.mark.parametrize("flag", _DANGEROUS_FLAGS)
def test_prod_with_bypass_flag_on_refuses(flag: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    monkeypatch.setenv(flag, "1")
    with pytest.raises(ProdSafetyError, match=flag):
        assert_prod_safe_flags()


def test_prod_with_multiple_flags_on_lists_all(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    for flag in _DANGEROUS_FLAGS:
        monkeypatch.setenv(flag, "1")
    with pytest.raises(ProdSafetyError) as exc:
        assert_prod_safe_flags()
    for flag in _DANGEROUS_FLAGS:
        assert flag in str(exc.value)


def test_prod_is_case_insensitive(monkeypatch: pytest.MonkeyPatch):
    """EXPECTED_ENV=PROD (any case) is still prod → guard fires."""
    monkeypatch.setenv("EXPECTED_ENV", "PROD")
    monkeypatch.setenv("TEAM_TWILIO_VERIFY_MOCK_MODE", "1")
    with pytest.raises(ProdSafetyError):
        assert_prod_safe_flags()


# --- prod + flags OFF → boots fine -------------------------------------------
def test_prod_with_flags_off_boots(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    # No bypass flag set (autouse fixture cleared them) → must not raise.
    assert assert_prod_safe_flags() is None


def test_prod_with_flag_explicitly_zero_boots(monkeypatch: pytest.MonkeyPatch):
    """A flag set to the OFF literal '0' is not 'on' → no refusal."""
    monkeypatch.setenv("EXPECTED_ENV", "prod")
    for flag in _DANGEROUS_FLAGS:
        monkeypatch.setenv(flag, "0")
    assert assert_prod_safe_flags() is None


# --- dev + a dangerous flag ON → boots fine (dev convenience preserved) -------
@pytest.mark.parametrize("flag", _DANGEROUS_FLAGS)
def test_dev_with_bypass_flag_on_boots(flag: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXPECTED_ENV", "dev")
    monkeypatch.setenv(flag, "1")
    assert assert_prod_safe_flags() is None


@pytest.mark.parametrize("flag", _DANGEROUS_FLAGS)
def test_unset_env_with_bypass_flag_on_boots(flag: str, monkeypatch: pytest.MonkeyPatch):
    """EXPECTED_ENV unset defaults to dev (VT-362 posture) → unset is NOT prod, no refusal."""
    monkeypatch.delenv("EXPECTED_ENV", raising=False)
    monkeypatch.setenv(flag, "1")
    assert assert_prod_safe_flags() is None


def test_nonprod_unknown_env_with_flag_on_boots(monkeypatch: pytest.MonkeyPatch):
    """An unrecognized EXPECTED_ENV value is treated as non-prod (default-dev posture)."""
    monkeypatch.setenv("EXPECTED_ENV", "staging")
    monkeypatch.setenv("TEAM_TWILIO_VERIFY_MOCK_MODE", "1")
    assert assert_prod_safe_flags() is None
