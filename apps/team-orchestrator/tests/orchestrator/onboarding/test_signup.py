"""VT-82 — create_signup_tenant canary (Rule #15, real PG, CL-422 synthetic).

The atomic service_role create: tenant row + owner consent_records + trial init in one
txn. Plus the duplicate-whatsapp_number (→ created=False, endpoint 409) and the
consent-false (Pillar-7 reject, no tenant) negatives. Mock connections hide RLS +
the ON CONFLICT, so this runs on a live DB.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-82 signup canary skipped",
)


@pytest.fixture(scope="module")
def pool():
    import apply_migrations
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from orchestrator import graph as graph_mod

    dsn = os.environ["DATABASE_URL"]
    assert not apply_migrations.apply(dsn=dsn)["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    prev = graph_mod._pool
    graph_mod._pool = ConnectionPool(
        dsn, min_size=1, max_size=4,
        kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
    )
    try:
        yield graph_mod._pool
    finally:
        graph_mod._pool.close()
        graph_mod._pool = prev


def _wa() -> str:
    return "+91" + str(uuid.uuid4().int)[:10]


def test_create_signup_tenant_atomic(pool):
    from orchestrator.onboarding.signup import create_signup_tenant

    wa = _wa()
    res = create_signup_tenant(
        business_name="Asha Kirana", whatsapp_number=wa,
        preferred_language="hi", owner_name="Owner X",  consent_dpdpa=True, consent_residency=True,
    )
    assert res.created is True
    assert res.plan_tier == "founding"
    # 2026-08-21: city is not a signup input, so the tier is DEFERRED (NULL) rather than
    # coarsened at create. auto_discovery + the journey set it once the city is actually known.
    assert res.city_tier is None

    with pool.connection() as c:
        t = c.execute(
            "SELECT phase, plan_tier, preferred_language, language_preference, "
            "trial_started_at, signed_up_at, created_via, business_type "
            "FROM tenants WHERE id = %s",
            (str(res.tenant_id),),
        ).fetchone()
        assert t["phase"] == "onboarding"
        assert t["plan_tier"] == "founding"
        # VT-677 D3 held while the form toggle was a display-language PROXY: it seeded only the
        # OBSERVED column and preferred_language stayed NULL until the owner actually chose.
        # 2026-08-21 (Fazal): the form now ASKS which language we communicate in (en|hinglish|hi),
        # which IS the owner choosing — so the answer lands in BOTH the explicit and observed
        # columns. Explicit wins the read COALESCE; observed is seeded with the same best evidence
        # and refined later by per-turn inference.
        assert t["language_preference"] == "hi"
        assert t["preferred_language"] == "hi"
        assert t["trial_started_at"] is not None
        assert t["created_via"] == "web"
        assert t["business_type"] is None  # auto-detected later, never asked for at signup

        cr = c.execute(
            "SELECT consent_dpdpa, consent_residency, dpdpa_version, residency_version "
            "FROM consent_records WHERE tenant_id = %s", (str(res.tenant_id),),
        ).fetchone()
        assert cr["consent_dpdpa"] and cr["consent_residency"]
        assert cr["dpdpa_version"] == "dpdpa_v1_2026-06"
        assert cr["residency_version"] == "residency_v1_2026-06"


def test_duplicate_whatsapp_number_not_created(pool):
    from orchestrator.onboarding.signup import create_signup_tenant

    wa = _wa()
    r1 = create_signup_tenant(
        business_name="Branch One", whatsapp_number=wa,
        preferred_language="en", owner_name="Owner X",  consent_dpdpa=True, consent_residency=True,
    )
    r2 = create_signup_tenant(
        business_name="Branch One Again", whatsapp_number=wa,
        preferred_language="en", owner_name="Owner X",  consent_dpdpa=True, consent_residency=True,
    )
    assert r1.created is True
    assert r2.created is False  # endpoint maps this → 409
    assert r2.tenant_id == r1.tenant_id  # same identity, no new row
    assert r2.plan_tier is None
    # exactly one consent_records row for the identity (no duplicate proof).
    with pool.connection() as c:
        n = c.execute(
            "SELECT count(*) AS n FROM consent_records WHERE tenant_id = %s",
            (str(r1.tenant_id),),
        ).fetchone()["n"]
    assert n == 1


def test_consent_false_rejected(pool):
    from orchestrator.onboarding.signup import create_signup_tenant

    with pytest.raises(ValueError):
        create_signup_tenant(
            business_name="No Consent Co", whatsapp_number=_wa(),
            preferred_language="en", owner_name="Owner X",  consent_dpdpa=True, consent_residency=False,
        )


def test_bad_business_type_rejected(pool):
    from orchestrator.onboarding.signup import create_signup_tenant

    # 2026-08-21: create_signup_tenant no longer ACCEPTS a business_type at all (auto-detected),
    # so there is no out-of-taxonomy value to reject here. Passing one is now a TypeError — which is
    # the stronger guarantee: the argument cannot be supplied, not merely validated.
    with pytest.raises(TypeError):
        create_signup_tenant(
            business_name="Mystery Co", owner_name="X", whatsapp_number=_wa(),
            preferred_language="en", business_type="not_a_real_type",
            consent_dpdpa=True, consent_residency=True,
        )


def _valid_input(**over):
    from orchestrator.onboarding.signup import SignupInput

    base = dict(
        business_name="Asha Kirana", owner_name="Asha Devi", whatsapp_number=_wa_91(),
        preferred_language="hi", 
        consent_dpdpa=True, consent_residency=True,
        gstin="27AAKCR3738B1ZE",  # VT-408: a GSTIN is now mandatory at signup (verify-then-create)
    )
    base.update(over)
    return SignupInput(**base)


def _active_search(_gstin):
    """Injectable verify_search_fn → an ACTIVE GSTIN (VT-408 gate green path, no live creds).
    Returns the REAL production GstinLookup type so the gate exercises the real is_active() /
    authoritative_name() contract — only an ACTIVE status with a name earns gstin_verified."""
    from orchestrator.integrations.methods.sandbox_kyc import GstinLookup

    return GstinLookup(ok=True, legal_name="Asha Kirana", status="Active")


def _wa_91() -> str:
    # +91 + 10-digit mobile starting 6-9, unique per call.
    import random
    return "+919" + "".join(str(random.randint(0, 9)) for _ in range(9))


def test_run_signup_full(pool):
    from orchestrator.onboarding.signup import run_signup

    calls = []
    out = run_signup(
        _valid_input(),
        welcome_send_fn=lambda *a, **k: calls.append(a) or True,
        verify_search_fn=_active_search,  # VT-408: green GSTIN verify (no live Sandbox)
    )
    assert out.plan_tier == "founding"
    assert out.city_tier is None  # deferred until discovery finds the city
    assert out.welcome_sent is True
    assert len(calls) == 1  # welcome invoked once

    with pool.connection() as c:
        t = c.execute(
            "SELECT business_type, city_tier, preferred_language FROM tenants WHERE id = %s",
            (str(out.tenant_id),),
        ).fetchone()
        # Both are AUTO-DETECTED and not signup inputs, so the row starts honest-empty and the
        # journey fills it. VT-317's point stands — the raw city is never persisted — but there is
        # now no city at signup to coarsen in the first place.
        assert not t["business_type"]
        assert t["city_tier"] is None
        cr = c.execute(
            "SELECT count(*) AS n FROM consent_records WHERE tenant_id = %s",
            (str(out.tenant_id),),
        ).fetchone()["n"]
        assert cr == 1
    # owner_name merged into business_profile (where the brain reads it).
    from orchestrator.db import tenant_connection
    with tenant_connection(out.tenant_id) as conn:
        bp = conn.execute(
            "SELECT attributes FROM l1_entities WHERE entity_type = 'business_profile'"
        ).fetchone()
    attrs = bp["attributes"] if isinstance(bp, dict) else bp[0]
    assert attrs.get("owner_name") == "Asha Devi"


def test_run_signup_reconciliation_anchors_verified_entity(pool, monkeypatch):
    """VT-406 reconciliation (verify-then-create completion): a verified signup persists the entity
    anchor on the NEW tenant from the GATE's SERVER-verified gstin/name (never a client value) AND
    seeds auto-discovery with the VERIFIED entity (name + gstin) — NOT the raw typed business_name."""
    import dbos

    from orchestrator.onboarding.signup import run_signup

    seeds: list = []
    monkeypatch.setattr(
        dbos.DBOS, "start_workflow",
        staticmethod(lambda _wf, _tid, seed: seeds.append(seed)), raising=False,
    )
    # Typed "Sundaram Book Store"; the verifier returns the AUTHORITATIVE "Sundaram Multi Pap Limited"
    # — differs from the typed name but shares the distinctive 'sundaram' token, so it passes the VT-448
    # name-match while still proving discovery anchors the VERIFIED name, not the typed one.
    def _active_sundaram(_gstin):
        from orchestrator.integrations.methods.sandbox_kyc import GstinLookup
        return GstinLookup(ok=True, legal_name="Sundaram Multi Pap Limited", status="Active")

    out = run_signup(
        _valid_input(business_name="Sundaram Book Store"),
        welcome_send_fn=lambda *a, **k: True,
        verify_search_fn=_active_sundaram,
    )
    # Discovery anchored on the VERIFIED entity (name + gstin), not the typed name (the Sundaram fix).
    assert len(seeds) == 1
    assert seeds[0]["business_name"] == "Sundaram Multi Pap Limited"
    assert seeds[0]["gstin"] == "27AAKCR3738B1ZE"

    # The entity anchor is persisted on the business_profile entity from the server-verified result.
    from orchestrator.db import tenant_connection
    with tenant_connection(out.tenant_id) as conn:
        bp = conn.execute(
            "SELECT attributes FROM l1_entities WHERE entity_type = 'business_profile'"
        ).fetchone()
    attrs = bp["attributes"] if isinstance(bp, dict) else bp[0]
    anchor = attrs.get("business_entity_anchor")
    assert anchor is not None
    assert anchor["gstin"] == "27AAKCR3738B1ZE"
    assert anchor["trade_name"] == "Sundaram Multi Pap Limited"
    assert anchor["source"] == "sandbox" and anchor["verified"] is True


def test_run_signup_rejects_name_mismatch_unrelated_gstin(pool):
    """VT-448 name-match security: a valid+ACTIVE GSTIN whose authoritative registry name is a DIFFERENT
    business is REJECTED (a valid GSTIN alone is not enough) — SignupGateError, the generic invalid_gstin
    outcome (no enumeration oracle), and NO tenant is created."""
    from orchestrator.onboarding.signup import SignupGateError, run_signup

    def _active_unrelated(_gstin):
        from orchestrator.integrations.methods.sandbox_kyc import GstinLookup
        return GstinLookup(ok=True, legal_name="Shubham Telecom Services", status="Active")

    with pytest.raises(SignupGateError) as ei:
        run_signup(
            _valid_input(business_name="RKeCom Services Pvt Ltd"),
            welcome_send_fn=lambda *a, **k: True,
            verify_search_fn=_active_unrelated,
        )
    assert ei.value.outcome == "invalid_gstin"  # generic reject — no enumeration oracle


def test_run_signup_mca_enrich_canonical_name_match_and_persist(pool, monkeypatch):
    """VT-449 WIRING: a signup with a CIN fetches MCA → the name-match uses the MCA CANONICAL name (so a
    typed name that would FAIL still passes when the GSTIN matches the registry canonical) AND the MCA
    company data is persisted (encrypted) post-create. Proves the enrich is on the critical path, not dormant."""
    monkeypatch.setenv("ENABLE_SANDBOX_MCA", "true")  # the ENABLED path (MCA parked OFF by default now)
    from orchestrator.integrations.methods import mca
    from orchestrator.onboarding import mca_store
    from orchestrator.onboarding.signup import run_signup

    cmd = mca.CompanyMasterData(ok=True, cin="U52609MH2020OPC344309",
                                company_name="RKECOM SERVICES OPC PRIVATE LIMITED")
    monkeypatch.setattr(mca, "company_master_data", lambda _cin, *, reason, request_fn=None: cmd)
    stored = {"n": 0}
    monkeypatch.setattr(mca_store, "store_company_master_data", lambda _t, _c: stored.update(n=stored["n"] + 1))

    def _active(_g):
        from orchestrator.integrations.methods.sandbox_kyc import GstinLookup
        return GstinLookup(ok=True, legal_name="RKECOM SERVICES OPC PRIVATE LIMITED", status="Active")

    # Typed "My Shop" shares NO distinctive token with the GSTIN's verified name → would REJECT without
    # MCA; the MCA canonical "RKECOM…" matches the verified name → the create SUCCEEDS, proving the
    # MCA canonical anchor (not the typed name) gates the name-match.
    out = run_signup(
        _valid_input(business_name="My Shop", cin="U52609MH2020OPC344309"),
        welcome_send_fn=lambda *a, **k: True,
        verify_search_fn=_active,
    )
    assert out.tenant_id is not None
    assert stored["n"] == 1  # MCA company data persisted post-create (enrich is wired, not dormant)


def test_run_signup_parked_mca_makes_zero_calls_when_flag_off(pool, monkeypatch):
    """VT-449 PARK (Fazal 2026-06-27): with ENABLE_SANDBOX_MCA OFF (default), a signup carrying a CIN makes
    ZERO MCA calls — company_master_data is never invoked, the name-match falls back to the typed name, and
    the tenant is still created (gstin_verified). The clean parked-flow guarantee."""
    monkeypatch.delenv("ENABLE_SANDBOX_MCA", raising=False)  # default OFF
    from orchestrator.integrations.methods import mca
    from orchestrator.onboarding.signup import run_signup

    def _must_not_call(*_a, **_k):
        raise AssertionError("MCA company_master_data must NOT be called when parked (flag off)")

    monkeypatch.setattr(mca, "company_master_data", _must_not_call)

    def _active(_g):
        from orchestrator.integrations.methods.sandbox_kyc import GstinLookup
        return GstinLookup(ok=True, legal_name="Asha Kirana", status="Active")

    # CIN supplied, but parked → no MCA call; the name-match anchors on the typed "Asha Kirana".
    out = run_signup(
        _valid_input(business_name="Asha Kirana", cin="U52609MH2020OPC344309"),
        welcome_send_fn=lambda *a, **k: True,
        verify_search_fn=_active,
    )
    assert out.tenant_id is not None  # tenant created via GST-verify alone, zero MCA calls


def test_run_signup_discovery_kick_failure_non_blocking(pool, monkeypatch):
    """VT-366: a failing Auto-Discovery kick (post-commit, best-effort) must NEVER 500 the signup —
    the tenant is already committed; discovery is fire-and-forget."""
    import dbos

    from orchestrator.onboarding.signup import run_signup

    def _boom(*a, **k):
        raise RuntimeError("discovery kick exploded")

    monkeypatch.setattr(dbos.DBOS, "start_workflow", staticmethod(_boom), raising=False)

    out = run_signup(
        _valid_input(whatsapp_number="+919900000366"),
        welcome_send_fn=lambda *a, **k: True,
        verify_search_fn=_active_search,  # VT-408: green GSTIN verify (no live Sandbox)
    )
    # Signup still succeeds despite the kick raising.
    assert out.tenant_id is not None
    assert out.welcome_sent is True


def _send_result(*, success: bool, error_code: str | None = None):
    """Build a PII-safe SendResult for injecting into _default_welcome's send seam."""
    from datetime import datetime, timezone

    from orchestrator.utils.twilio_send import SendResult

    return SendResult(
        success=success,
        message_sid=("SM" + "0" * 32) if success else None,
        error_code=error_code,
        error_message=None if success else "injected",
        attempted_at=datetime.now(timezone.utc),
        template_name="team_welcome",
        recipient_phone_token="phone_tok_test",
    )


def test_default_welcome_calls_send_owner_template_correctly(pool, monkeypatch):
    """VT-393/VT-404/VT-520: the un-injected default _default_welcome sends the real team_welcome3
    template via the owner_send seam with the owner's language + {owner_name,
    trial_end_date}, and welcome_sent MIRRORS SendResult.success (here: True)."""
    from datetime import datetime, timezone

    from orchestrator.onboarding import signup as signup_mod

    captured: dict = {}

    def _spy(tenant_id, template_name, language, params, *, recipient_phone):
        captured.update(
            tenant_id=tenant_id, template_name=template_name, language=language,
            params=params, recipient_phone=recipient_phone,
        )
        return _send_result(success=True)

    # Patch the seam where _default_welcome imports it (module-local import).
    monkeypatch.setattr(
        "orchestrator.owner_surface.owner_send.send_owner_template", _spy
    )

    tid = uuid.uuid4()
    trial_end = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
    sent = signup_mod._default_welcome(
        tid, "+919812300013", "hi", "Asha Devi", trial_end,
    )
    assert sent is True  # mirrors SendResult.success
    assert captured["template_name"] == "team_welcome4"  # VT-555: UTILITY quick-reply welcome (team_welcome3 → MARKETING)
    assert captured["language"] == "hi"  # honors the owner's preferred_language
    assert captured["recipient_phone"] == "+919812300013"  # signup number, NOT owner_phone
    assert captured["tenant_id"] == tid
    # VT-555: name-only params — the trial-date var was dropped (no trial/free wording → UTILITY).
    assert captured["params"] == {"owner_name": "Asha Devi"}
    assert "trial_end_date" not in captured["params"]  # dropped var must not leak back in


def test_run_signup_unapproved_sid_reports_not_sent_but_signup_succeeds(pool, monkeypatch):
    """VT-390/VT-393 honesty: an unapproved SID → SendResult(success=False,
    error_code='template_not_yet_approved') → welcome_sent=False, and the committed
    signup STILL succeeds (the welcome is best-effort, non-terminal)."""
    def _unapproved(*a, **k):
        return _send_result(success=False, error_code="template_not_yet_approved")

    monkeypatch.setattr(
        "orchestrator.owner_surface.owner_send.send_owner_template", _unapproved
    )

    from orchestrator.onboarding.signup import run_signup

    out = run_signup(
        _valid_input(whatsapp_number=f"+9199{uuid.uuid4().int % 10**8:08d}"),
        verify_search_fn=_active_search,  # VT-408: green GSTIN verify (no live Sandbox)
    )
    assert out.tenant_id is not None, "signup must still succeed when the send is unapproved"
    assert out.welcome_sent is False, (
        "an unapproved SID sends nothing — welcome_sent must report False (no faked delivery)"
    )


def test_run_signup_raising_welcome_send_is_non_terminal(pool, monkeypatch):
    """A welcome send that RAISES (e.g. a 5xx re-raise for DBOS retry) must NEVER 500
    the signup — the tenant is already committed; the run_signup try/except swallows it
    and welcome_sent reports False."""
    def _boom(*a, **k):
        raise RuntimeError("twilio 5xx re-raised")

    monkeypatch.setattr(
        "orchestrator.owner_surface.owner_send.send_owner_template", _boom
    )

    from orchestrator.onboarding.signup import run_signup

    out = run_signup(
        _valid_input(whatsapp_number=f"+9199{uuid.uuid4().int % 10**8:08d}"),
        verify_search_fn=_active_search,  # VT-408: green GSTIN verify (no live Sandbox)
    )
    assert out.tenant_id is not None, "a raising welcome send must not fail a committed signup"
    assert out.welcome_sent is False


def test_run_signup_duplicate_409(pool):
    from orchestrator.onboarding.signup import SignupError, run_signup

    wa = _wa_91()
    run_signup(
        _valid_input(whatsapp_number=wa),
        welcome_send_fn=lambda *a, **k: True, verify_search_fn=_active_search,
    )
    with pytest.raises(SignupError) as e:
        run_signup(
            _valid_input(whatsapp_number=wa),
            welcome_send_fn=lambda *a, **k: True, verify_search_fn=_active_search,
        )
    assert e.value.code == "duplicate"


def test_run_signup_consent_false_no_tenant(pool):
    from orchestrator.onboarding.signup import SignupError, run_signup

    wa = _wa_91()
    with pytest.raises(SignupError) as e:
        run_signup(_valid_input(whatsapp_number=wa, consent_residency=False))
    assert e.value.code == "consent"
    # NO tenant created.
    with pool.connection() as c:
        n = c.execute(
            "SELECT count(*) AS n FROM tenants WHERE whatsapp_number = %s", (wa,)
        ).fetchone()["n"]
    assert n == 0


def test_absent_city_defers_the_tier_instead_of_asserting_tier_3(pool):
    """2026-08-21 — city and business_type are AUTO-DETECTED, so the web form stopped asking.

    The trap this pins: ``coarsen_city("")`` returns ``'tier_3'``. Dropping the field without this
    guard would have tiered EVERY web signup as tier_3 and fed that into the L3 cohort key
    (business_type x city_tier) — asserting a tier nobody knows, with the same confidence as a real
    one. An absent city must persist NULL and let discovery fill it later.
    """
    from orchestrator.db import tenant_connection
    from orchestrator.onboarding.signup import run_signup

    res = run_signup(
        _valid_input(),
        welcome_send_fn=lambda *a, **k: True,
        verify_search_fn=_active_search,
    )
    assert res.tenant_id is not None
    with tenant_connection(res.tenant_id) as conn:
        row = conn.execute(
            "SELECT city_tier, business_type FROM tenants WHERE id = %s", (str(res.tenant_id),)
        ).fetchone()
    tier = row["city_tier"] if isinstance(row, dict) else row[0]
    btype = row["business_type"] if isinstance(row, dict) else row[1]
    assert tier is None, f"an absent city must defer the tier, not coarsen '' to {tier!r}"
    assert not btype, "an absent business_type must stay absent, not be guessed"


def test_create_payload_refuses_city_and_business_type(pool, monkeypatch):
    """Fazal 2026-08-21: "The create payload shouldn't be expecting or even accepting those 2 fields."

    Pydantic DROPS unknown keys by default, which would make "not accepted" indistinguishable from
    "accepted and silently discarded". SignupBody sets extra='forbid', so sending either is a 422.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.api.signup import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    base = {
        "business_name": "Asha Kirana",
        "owner_name": "Asha Devi",
        "whatsapp_number": _wa_91(),
        "preferred_language": "hi",
        "consent_dpdpa": True,
        "consent_residency": True,
        "gstin": "27AAKCR3738B1ZE",
    }
    for extra in ({"city": "Mumbai"}, {"business_type": "kirana"}):
        r = client.post("/api/signup", json={**base, **extra})
        assert r.status_code == 422, f"{extra} must be REFUSED, not ignored — got {r.status_code}"
        assert "extra_forbidden" in r.text


def test_hinglish_is_an_accepted_communication_language(pool):
    """2026-08-21 (Fazal): the owner is ASKED which language we message them in — English, Hindi or
    Hinglish. 'hinglish' is a real register in owner_locale's value space (hi-Latn), and it used to
    be rejected twice over: SignupBody capped preferred_language at 2 characters, and signup kept a
    private {en, hi} frozenset separate from SUPPORTED_OWNER_LANGS. Both are fixed; this pins that a
    Hinglish owner can actually sign up and that the choice is stored as EXPLICIT.
    """
    from orchestrator.db import tenant_connection
    from orchestrator.onboarding.signup import run_signup

    res = run_signup(
        _valid_input(preferred_language="hinglish"),
        welcome_send_fn=lambda *a, **k: True,
        verify_search_fn=_active_search,
    )
    with tenant_connection(res.tenant_id) as conn:
        row = conn.execute(
            "SELECT preferred_language, language_preference FROM tenants WHERE id = %s",
            (str(res.tenant_id),),
        ).fetchone()
    explicit = row["preferred_language"] if isinstance(row, dict) else row[0]
    observed = row["language_preference"] if isinstance(row, dict) else row[1]
    assert explicit == "hinglish", "the asked answer is the EXPLICIT choice"
    assert observed == "hinglish", "and seeds the observed column with the same evidence"


def test_run_signup_validation_negatives(pool):
    from orchestrator.onboarding.signup import SignupError, run_signup

    # 2026-08-21: city and business_type are gone from this contract entirely — auto-detected, and
    # SignupInput does not accept them, so there is no "blank city" or "wrong bucket" case to test
    # here any more. Supplying either is a TypeError, which test_create_signup_tenant_rejects_...
    # covers; what remains are the fields the owner really does provide.
    for over, code in [
        ({"whatsapp_number": "+1202555"}, "invalid_phone"),
        ({"preferred_language": "ta"}, "invalid_language"),
        ({"business_name": "viabe team"}, "invalid_name"),  # blocklist
    ]:
        with pytest.raises(SignupError) as e:
            run_signup(_valid_input(**over))
        assert e.value.code == code, f"{over} → expected {code}, got {e.value.code}"


def test_signup_route_status_mapping(pool, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.api.signup import router

    monkeypatch.setenv("INTERNAL_API_SECRET", "vt326-test-secret")
    hdr = {"X-Internal-Secret": "vt326-test-secret"}

    # VT-408: the route verifies the GSTIN before create — monkeypatch the Sandbox search to
    # ACTIVE so the happy path reaches create (the HTTP path can't inject a search fn).
    from orchestrator.integrations.methods import sandbox_kyc

    monkeypatch.setattr(
        sandbox_kyc, "search_gstin",
        lambda g, **k: sandbox_kyc.GstinLookup(ok=True, legal_name="Asha Kirana", status="Active"),
    )

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    body = {
        "business_name": "Asha Kirana", "owner_name": "Asha Devi",
        "whatsapp_number": _wa_91(), "preferred_language": "en",
        "consent_dpdpa": True, "consent_residency": True,
        "gstin": "27AAKCR3738B1ZE",
    }
    r = client.post("/api/signup", json=body, headers=hdr)
    assert r.status_code == 201, r.text
    assert r.json()["tenant_id"]
    # 2026-08-21: no city is submitted (auto-detected), so the tier is DEFERRED rather than
    # coarsened. VT-317's guarantee is unchanged — the raw city is never persisted; there is simply
    # no city at signup to coarsen.
    assert r.json()["city_tier"] is None

    # Duplicate → 409.
    r_dup = client.post("/api/signup", json=body, headers=hdr)
    assert r_dup.status_code == 409

    # VT-326 A2: only team-web (holding INTERNAL_API_SECRET) may reach this BYPASSRLS
    # create surface — a missing or wrong secret is 403 (closes flooding at the source).
    assert client.post("/api/signup", json=body).status_code == 403
    assert (
        client.post("/api/signup", json=body, headers={"X-Internal-Secret": "wrong"}).status_code
        == 403
    )
    assert r_dup.json()["detail"]["code"] == "duplicate"

    # Consent false → 400, no tenant.
    r_consent = client.post("/api/signup", json={**body, "whatsapp_number": _wa_91(),
                                                 "consent_residency": False}, headers=hdr)
    assert r_consent.status_code == 400
    assert r_consent.json()["detail"]["code"] == "consent"

    # Bad phone → 400.
    r_phone = client.post("/api/signup", json={**body, "whatsapp_number": "+1202555"}, headers=hdr)
    assert r_phone.status_code == 400
    assert r_phone.json()["detail"]["code"] == "invalid_phone"

    # VT-408: an INACTIVE GSTIN → 422 reject (no tenant), generic "GST-registered" copy. Patch
    # the search to inactive for this one request.
    monkeypatch.setattr(
        sandbox_kyc, "search_gstin",
        lambda g, **k: sandbox_kyc.GstinLookup(ok=True, legal_name="X", status="Cancelled"),
    )
    r_reject = client.post(
        "/api/signup", json={**body, "whatsapp_number": _wa_91()}, headers=hdr
    )
    assert r_reject.status_code == 422
    assert r_reject.json()["detail"]["code"] == "invalid_gstin"
    assert "GST-registered" in r_reject.json()["detail"]["message"]

    # VT-408: a vendor_down → 503 HOLD (retryable), distinct copy.
    monkeypatch.setattr(
        sandbox_kyc, "search_gstin", lambda g, **k: sandbox_kyc.GstinLookup(ok=False)
    )
    r_hold = client.post(
        "/api/signup", json={**body, "whatsapp_number": _wa_91()}, headers=hdr
    )
    assert r_hold.status_code == 503
    assert r_hold.json()["detail"]["code"] == "vendor_down"
    assert r_hold.json()["detail"]["retryable"] is True


def test_signup_kg_event_has_no_business_name_pii(pool):
    """Review/CL-390: the TENANT_CREATED outbox payload (durable, NOT DSR-purged)
    must NOT carry business_name (owner subject data) — only the non-PII business_type."""
    from orchestrator.onboarding.signup import create_signup_tenant

    res = create_signup_tenant(
        business_name="Secret Biz Name", owner_name="X", whatsapp_number=_wa(),
        preferred_language="en", 
        consent_dpdpa=True, consent_residency=True,
    )
    with pool.connection() as c:
        rows = c.execute(
            "SELECT payload FROM kg_events WHERE tenant_id = %s "
            "AND event_type = 'tenant_created'", (str(res.tenant_id),),
        ).fetchall()
    assert rows, "no tenant_created event emitted"
    for r in rows:
        p = r["payload"]
        assert "business_name" not in p, "business_name PII leaked into durable kg_events"
        # business_type is not known at signup any more (auto-detected), so the event carries the
        # honest absence rather than a value the owner never gave.
        assert not p.get("business_type")


def test_consent_records_is_pii_free_schema(pool):
    """Review: consent_records' DSR-retention safety RESTS on it being PII-free.
    Enforce that — only the known booleans/versions/timestamps, no name/phone/email."""
    allowed = {
        "id", "tenant_id", "consent_dpdpa", "consent_residency",
        "dpdpa_version", "residency_version", "signed_up_at", "created_at",
        # VT-715 (mig 185): when the DPDP consent-record email was sent — a timestamp, no PII
        # (the owner_email itself lives on tenants, deliberately NOT here).
        "record_email_sent_at",
    }
    with pool.connection() as c:
        cols = {
            r["column_name"] for r in c.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'consent_records'"
            ).fetchall()
        }
    assert cols <= allowed, f"consent_records has unexpected (possibly-PII) columns: {cols - allowed}"


def test_business_types_endpoint_serves_taxonomy(pool):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.api.signup import router

    app = FastAPI()
    app.include_router(router)
    r = TestClient(app).get("/api/signup/business-types")
    assert r.status_code == 200
    opts = r.json()["business_types"]
    keys = {o["key"] for o in opts}
    assert "kirana" in keys and "other" in keys
    # every option carries both language labels (no PII).
    assert all(o.get("label_en") and o.get("label_hi") for o in opts)


def test_welcome_trial_end_derives_from_trial_yaml(pool):
    """VT-371: the team_welcome {{2}} trial-end date must come from config/trial.yaml trial_days —
    the SAME source the evaluator/sweep read. The stale local _TRIAL_DAYS=14 told every new owner
    their trial ended 16 days early. Asserted against the YAML value (NOT a literal 30 — re-pinning
    a constant would just recreate the drift this fixes)."""
    from datetime import timedelta
    from pathlib import Path

    import yaml

    from orchestrator.onboarding import signup as signup_mod
    from orchestrator.onboarding.signup import run_signup

    yaml_days = int(
        yaml.safe_load(
            (Path(signup_mod.__file__).resolve().parents[3] / "config" / "trial.yaml")
            .read_text(encoding="utf-8")
        )["trial_days"]
    )

    calls: list[tuple] = []
    out = run_signup(
        _valid_input(whatsapp_number=f"+9199{uuid.uuid4().int % 10**8:08d}"),  # unique per run
        welcome_send_fn=lambda *a, **k: calls.append(a) or True,
        verify_search_fn=_active_search,  # VT-408: green GSTIN verify (no live Sandbox)
    )
    assert out.welcome_sent is True and len(calls) == 1
    # _default_welcome signature: (tenant_id, whatsapp_number, preferred_language, owner_name, trial_end)
    trial_end = calls[0][4]
    # run_signup's `now` is internal; derive the expectation from the persisted trial_started_at.
    with pool.connection() as c:
        started = c.execute(
            "SELECT trial_started_at FROM tenants WHERE id = %s", (str(out.tenant_id),)
        ).fetchone()["trial_started_at"]
    assert trial_end == started + timedelta(days=yaml_days), (
        f"welcome trial_end must be trial_started_at + trial.yaml trial_days ({yaml_days})"
    )
    assert not hasattr(signup_mod, "_TRIAL_DAYS"), "the stale constant must be gone (grep-zero)"


# --- VT-691: create_whatsapp_signup_tenant ------------------------------------------------------


def test_create_whatsapp_signup_tenant_atomic(pool):
    """The WhatsApp-initiated variant: tenant + consent proof + trial in one txn, NO OTP,
    created_via='whatsapp', NO fabricated business details (empty name, NULL type/city)."""
    from orchestrator.onboarding.signup import create_whatsapp_signup_tenant

    phone = _wa()
    res = create_whatsapp_signup_tenant(phone)
    assert res.created is True

    with pool.connection() as conn:
        t = conn.execute(
            "SELECT business_name, business_type, city_tier, phase, created_via, "
            "       verification_status, whatsapp_number, language_preference, "
            "       trial_started_at, signed_up_at "
            "FROM tenants WHERE id = %s", (str(res.tenant_id),),
        ).fetchone()
        c = conn.execute(
            "SELECT consent_dpdpa, consent_residency, dpdpa_version, residency_version "
            "FROM consent_records WHERE tenant_id = %s", (str(res.tenant_id),),
        ).fetchone()

    assert t["business_name"] == "" and t["business_type"] is None and t["city_tier"] is None
    assert t["phase"] == "onboarding"
    assert t["created_via"] == "whatsapp"
    assert t["verification_status"] == "unverified"
    assert t["whatsapp_number"] == phone
    assert t["trial_started_at"] is not None and t["signed_up_at"] is not None
    assert c is not None and c["consent_dpdpa"] is True and c["consent_residency"] is True
    assert c["dpdpa_version"] and c["residency_version"], "consent proof must pin versions"


def test_create_whatsapp_signup_tenant_duplicate_returns_existing(pool):
    """A repeated consent reply (or redelivery) returns the SAME tenant, created=False —
    never a second tenant on one number."""
    from orchestrator.onboarding.signup import create_whatsapp_signup_tenant

    phone = _wa()
    first = create_whatsapp_signup_tenant(phone)
    second = create_whatsapp_signup_tenant(phone)
    assert first.created is True and second.created is False
    assert first.tenant_id == second.tenant_id


def test_vt724_web_email_persists_and_invalid_rejects(pool):
    """VT-724 — the web form's optional owner_email: persisted lowercased at create (the
    consent-record hook then fires with an address present); an INVALID non-empty address is a
    loud SignupError('invalid_email'); empty stays legal (email never gates signup)."""
    from orchestrator.onboarding.signup import SignupError, run_signup

    out = run_signup(
        _valid_input(owner_email="Asha.Devi@Example.COM"),
        welcome_send_fn=lambda *a, **k: True,
        verify_search_fn=_active_search,
    )
    assert out.welcome_sent is True
    with pool.connection() as c:
        row = c.execute(
            "SELECT owner_email FROM tenants WHERE id = %s", (str(out.tenant_id),)
        ).fetchone()
    assert row is not None and row["owner_email"] == "asha.devi@example.com"

    with pytest.raises(SignupError) as ei:
        run_signup(
            _valid_input(owner_email="not-an-address", whatsapp_number=_wa_91()),
            welcome_send_fn=lambda *a, **k: True,
            verify_search_fn=_active_search,
        )
    assert ei.value.code == "invalid_email"

    out2 = run_signup(
        _valid_input(owner_email="", whatsapp_number=_wa_91()),
        welcome_send_fn=lambda *a, **k: True,
        verify_search_fn=_active_search,
    )
    assert out2.plan_tier == "founding"  # empty email never gates
