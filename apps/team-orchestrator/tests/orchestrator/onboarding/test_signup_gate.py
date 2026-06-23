"""VT-408 — the GSTIN hard-gate at signup (verify-then-create), DB-LESS unit tests.

The invariant: NO tenant reaches trial OR active without a confirmed ACTIVE GSTIN. These tests
drive the gate with an INJECTED GSTIN search fn (no live Sandbox creds — the real vendor 500s
right now) and an INJECTED create fn (no DB), so the whole verify-then-create + kick-conditioning
matrix runs in unit time. The live Sandbox canary (Rule #15) is run separately by CC.

Coverage (the §9 invariant matrix):
- verify_gstin_for_signup branches: gstin_verified / vendor_down / invalid_gstin / empty.
- run_signup: empty + invalid → SignupGateError, NO create, NO welcome/discovery/journey kick.
- run_signup: vendor_down → SignupGateError(retryable=True), NO create, NO kicks.
- run_signup: a client-claimed-verified body field is IGNORED — the gate reads the server verify
  (IDOR posture: there is no client 'verified' field; the GSTIN is re-verified server-side).
- run_signup: green verify → create fires, the verified gstin+name flow into create, kicks fire.
- bilingual copy: reject / vendor_down / inbound_directive resolve EN + HI; generic reject (no
  enumeration oracle — identical message regardless of inactive-vs-not-found).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

pytest.importorskip("pydantic")

from orchestrator.onboarding.signup import (  # noqa: E402
    SignupGateError,
    SignupInput,
    SignupResult,
    run_signup,
)
from orchestrator.onboarding.signup_gate import (  # noqa: E402
    GSTIN_VERIFIED,
    INVALID_GSTIN,
    VENDOR_DOWN,
    gate_copy,
    verify_gstin_for_signup,
)

_ACTIVE_GSTIN = "27AAKCR3738B1ZE"  # Fazal's consented test GSTIN (also the live-canary input)


@dataclass(frozen=True)
class _FakeLookup:
    """A GstinLookup-shaped stand-in: ok / is_active() / authoritative_name()."""

    ok: bool
    status: str | None = None
    name: str | None = None

    def is_active(self) -> bool:
        return self.ok and (self.status or "").strip().lower() == "active"

    def authoritative_name(self) -> str | None:
        return self.name


def _active(name: str = "Asha Traders"):
    return lambda g: _FakeLookup(ok=True, status="Active", name=name)


def _inactive():
    return lambda g: _FakeLookup(ok=True, status="Cancelled", name="Stale Co")


def _not_found():
    return lambda g: _FakeLookup(ok=True, status=None, name=None)


def _vendor_down():
    return lambda g: _FakeLookup(ok=False)


def _valid_input(**over) -> SignupInput:
    base = dict(
        business_name="Asha Traders",
        owner_name="Asha",
        whatsapp_number="+919812345678",
        preferred_language="en",
        city="Mumbai",
        business_type="kirana",
        consent_dpdpa=True,
        consent_residency=True,
        gstin=_ACTIVE_GSTIN,
    )
    base.update(over)
    return SignupInput(**base)


# --------------------------------------------------------------------------- #
# 1. verify_gstin_for_signup — the three outcomes (+ empty).
# --------------------------------------------------------------------------- #


def test_verify_active_gstin_is_verified():
    r = verify_gstin_for_signup(_ACTIVE_GSTIN, search_fn=_active("Asha Traders"))
    assert r.ok is True and r.outcome == GSTIN_VERIFIED
    assert r.retryable is False
    assert r.verified_name == "Asha Traders" and r.gstin == _ACTIVE_GSTIN


def test_verify_inactive_gstin_is_invalid_reject():
    r = verify_gstin_for_signup(_ACTIVE_GSTIN, search_fn=_inactive())
    assert r.ok is False and r.outcome == INVALID_GSTIN and r.retryable is False


def test_verify_not_found_gstin_is_invalid_reject():
    r = verify_gstin_for_signup(_ACTIVE_GSTIN, search_fn=_not_found())
    assert r.ok is False and r.outcome == INVALID_GSTIN and r.retryable is False


def test_verify_vendor_down_is_retryable_hold_not_reject():
    r = verify_gstin_for_signup(_ACTIVE_GSTIN, search_fn=_vendor_down())
    assert r.ok is False and r.outcome == VENDOR_DOWN
    assert r.retryable is True  # HOLD, not a reject — an outage must not turn away a GST business


def test_verify_empty_gstin_is_reject_not_hold():
    r = verify_gstin_for_signup("", search_fn=_active())
    assert r.ok is False and r.outcome == INVALID_GSTIN and r.retryable is False
    r2 = verify_gstin_for_signup("   ", search_fn=_active())
    assert r2.outcome == INVALID_GSTIN


# --------------------------------------------------------------------------- #
# 2. run_signup gate — verify-then-create + kick conditioning (DB-less via injected create).
# A spy create fn records calls; the welcome/discovery/journey kicks are recorded so the tests
# can assert the invariant: NO product kick fires on a non-verified path.
# --------------------------------------------------------------------------- #


@pytest.fixture
def spy_create(monkeypatch):
    """Patch create_signup_tenant (DB-less). Returns a recorder dict for create + welcome.

    create returns a SignupResult(created=True) so the green path proceeds to the welcome kick;
    welcome is injected via welcome_send_fn (already a run_signup param). At this commit the only
    post-create product kick in run_signup is the welcome send — the invariant we assert is that
    NEITHER create NOR welcome fires on a non-verified path (the gate is upstream of both)."""
    import uuid

    rec: dict[str, list] = {"create": [], "welcome": []}

    def fake_create(**kw):
        rec["create"].append(kw)
        return SignupResult(
            tenant_id=uuid.uuid4(), created=True, plan_tier="founding", city_tier="tier_1"
        )

    monkeypatch.setattr(
        "orchestrator.onboarding.signup.create_signup_tenant", fake_create
    )
    return rec


def _welcome_fn(rec):
    def _w(*a, **k):
        rec["welcome"].append(a)
        return True

    return _w


def test_run_signup_green_creates_and_fires_all_kicks(spy_create):
    out = run_signup(
        _valid_input(),
        welcome_send_fn=_welcome_fn(spy_create),
        verify_search_fn=_active("Asha Traders"),
    )
    # Created (verified path) — and the verified gstin + authoritative name flowed into create.
    assert len(spy_create["create"]) == 1
    assert spy_create["create"][0]["verified_gstin"] == _ACTIVE_GSTIN
    assert spy_create["create"][0]["verified_business_name"] == "Asha Traders"
    # The welcome kick fired exactly on the verified path (the only post-create kick at this
    # commit; any later kick — discovery / journey — sits BELOW the gate, so it inherits the
    # same "verified path only" guarantee structurally).
    assert len(spy_create["welcome"]) == 1
    assert out.plan_tier == "founding"


def test_run_signup_invalid_gstin_no_tenant_no_kicks(spy_create):
    with pytest.raises(SignupGateError) as ei:
        run_signup(
            _valid_input(),
            welcome_send_fn=_welcome_fn(spy_create),
            verify_search_fn=_inactive(),
        )
    assert ei.value.outcome == INVALID_GSTIN and ei.value.retryable is False
    # The whole point: NO create and NO welcome kick on a rejected (non-verified) path.
    assert spy_create["create"] == []
    assert spy_create["welcome"] == []


def test_run_signup_vendor_down_holds_no_tenant_no_kicks(spy_create):
    with pytest.raises(SignupGateError) as ei:
        run_signup(
            _valid_input(),
            welcome_send_fn=_welcome_fn(spy_create),
            verify_search_fn=_vendor_down(),
        )
    assert ei.value.outcome == VENDOR_DOWN and ei.value.retryable is True  # HOLD, not reject
    assert spy_create["create"] == []
    assert spy_create["welcome"] == []


def test_run_signup_empty_gstin_rejected_no_tenant(spy_create):
    with pytest.raises(SignupGateError) as ei:
        run_signup(
            _valid_input(gstin=""),
            welcome_send_fn=_welcome_fn(spy_create),
            verify_search_fn=_active(),
        )
    assert ei.value.outcome == INVALID_GSTIN and ei.value.retryable is False
    assert spy_create["create"] == []


def test_run_signup_gate_reads_server_verify_not_a_client_field(spy_create):
    """IDOR posture: there is no client 'verified' boolean — the gate ALWAYS re-verifies the
    GSTIN server-side. Even a 'looks legit' GSTIN string is rejected if the server verify
    (here vendor says inactive) does not return ACTIVE. No body field can shortcut the gate."""
    with pytest.raises(SignupGateError):
        run_signup(
            _valid_input(gstin="27AAAAA0000A1Z5"),  # well-formed but server says inactive
            welcome_send_fn=_welcome_fn(spy_create),
            verify_search_fn=_inactive(),
        )
    assert spy_create["create"] == []


# --------------------------------------------------------------------------- #
# 3. Bilingual copy — reject / vendor_down / inbound_directive, EN + HI, no enumeration oracle.
# --------------------------------------------------------------------------- #


def test_reject_copy_is_bilingual_and_generic():
    en = gate_copy("reject", "en")
    hi = gate_copy("reject", "hi")
    assert "GST-registered" in en
    assert en != hi and len(hi) > 0
    # No enumeration oracle: the reject copy must NOT reveal whether the GSTIN was inactive or
    # not-found — it is one generic message.
    low = en.lower()
    assert "inactive" not in low and "not found" not in low and "not-found" not in low


def test_vendor_down_copy_distinct_from_reject():
    assert gate_copy("vendor_down", "en") != gate_copy("reject", "en")
    assert "on our side" in gate_copy("vendor_down", "en").lower()


def test_inbound_directive_copy_points_to_signup():
    en = gate_copy("inbound_directive", "en")
    assert "verify" in en.lower()
    assert gate_copy("inbound_directive", "hi") != en


def test_unknown_language_falls_back_to_en():
    assert gate_copy("reject", "ta") == gate_copy("reject", "en")

# --------------------------------------------------------------------------- #
# 4. Invariant: paths that must NOT create a tenant below the verified floor.
#    DB-less source-level assertions (ruling #10 + the §3 invariant matrix).
# --------------------------------------------------------------------------- #


def test_vt405_vtr_confirm_path_never_creates_a_tenant():
    """VT-405's confirm path is run_vtr_override (verification.py) — an UPGRADE of an EXISTING
    tenant to vtr_verified, never a create. It can only ever satisfy the invariant from above
    (it operates on a tenant that already passed the signup gate), never breach it from below.
    Asserted at the source: run_vtr_override contains NO 'INSERT INTO tenants' — only an UPDATE
    guarded by 'SELECT 1 FROM tenants WHERE id' (tenant must already exist)."""
    import inspect

    from orchestrator.onboarding import verification

    src = inspect.getsource(verification.run_vtr_override)
    assert "INSERT INTO tenants" not in src
    assert "UPDATE tenants SET verification_status = 'vtr_verified'" in src
    # It verifies the row exists first (no create-on-missing).
    assert "SELECT 1 FROM tenants WHERE id" in src


def test_tenant_provision_new_branch_is_verified_gated():
    """VT-408: create_tenant_if_unknown refuses an UNKNOWN number unless verified=True, and the
    refusal returns provisioned=False with NO tenant_id (no row, no PII). Asserted at the source
    so the DB-less suite covers the backdoor closure; the live merge/refuse behaviour is in the
    DATABASE_URL-gated test_tenant_provision.py matrix."""
    import inspect

    from orchestrator.onboarding import tenant_provision

    src = inspect.getsource(tenant_provision.create_tenant_if_unknown)
    # Unknown + unverified short-circuits to a refusal BEFORE the INSERT.
    assert "if not verified:" in src
    assert "provisioned=False" in src
    # The refusal returns no tenant.
    assert "tenant_id=None" in src

