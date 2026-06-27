"""VT-421 — the registry-driven agent ACTIVATION gate (real Postgres).

Fazal HALT + PIN + EXPAND (2026-06-25): agent execution (for SR: detect → approve → win-back SEND)
runs ONLY for a tenant that has crossed that agent's activation bar. No out-of-track communication.
The held send to +919321553267 stays HELD until this lands AND a real activated Sundaram tenant
exists.

Fazal-pinned model:
  - The bar is **journey-complete, NOT paid-active.** Gate on ``onboarding_journey.status='complete'``
    (admits BOTH trial AND paid — the 1-month free trial is DELIBERATELY UNRESTRICTED).
  - The bar is DECLARATIVE per-agent in ``activation_registry.REGISTRY``; the gate READS it.

THE GATE (orchestrator.agents.onboarding_gate.is_agent_eligible) is enforced at TWO sites:
  - Call site A (DETECT, optimization): ``SalesRecoveryAgent.execute_item`` entry → a non-activated
    tenant returns ``cancelled`` + ``skipped_not_onboarded`` BEFORE detection runs.
  - Call site B (SEND, THE load-bearing boundary — Gate 0): ``customer_send.agent_send_draft`` →
    a non-activated tenant's draft is SKIPPED (``SKIP_NOT_ONBOARDED``) before any Twilio call. ONE
    edit covers BOTH L2 (l2_send) and L3 (l3_hold) — they converge on this single choke point.

CANARY (Rule #15), BOTH directions:
  1. Journey-incomplete tenant NO-OPs on DETECT (0 candidates) AND on SEND (SKIP_NOT_ONBOARDED,
     0 Twilio calls).
  2. Fully-activated tenant (journey-complete + gstin_verified + enabled+ok connector + ≥1 customer)
     PASSES Gate 0 — execute_item proceeds past the gate (reaches detection), and agent_send_draft
     passes Gate 0 (then meets the normal downstream gates).
  3. A TRIAL tenant with the full activation set (journey-complete + verified + connector + customers)
     is ADMITTED — proving the free trial is unrestricted (journey-complete, not paid).
  4. The +919321553267-style non-eligible tenant is BLOCKED on the SEND regardless of trigger
     (L2 approved batch / L3 auto_send_pending) — Gate 0 short-circuits both.
  5. The REGISTRY: SR's prereqs evaluated correctly; ``unmet_prerequisites`` returns the right
     reasons; a SECOND (stub) agent with different prereqs evaluates INDEPENDENTLY (extensibility).
  6. Fail-closed unit pins: unknown agent / missing journey / NULL / missing connector / 0 customers
     / verified-below / forced read error → ineligible.

HARNESS — house realdb conventions (mirrors test_vt418_l2_send_driver_realdb.py): importorskip
psycopg+dbos, skipif no DATABASE_URL, migrations applied through the UNGUARDED ``apply(dsn=...)``
path, rows seeded through a direct service-role psycopg connection, the code under test exercised
through ``tenant_connection`` (the real RLS path). Unique tenants/customers per test (uuid-suffixed)
so a recycled DB never collides (CL-422 synthetic only; CL-390 no PII). NO real Twilio anywhere —
``send_fn`` is injected and records every would-be send.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after the dependency skip guards
from psycopg.types.json import Jsonb  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-421 activation-gate realdb suite skipped",
)

# Modules under test. importorskip keeps collection fresh-DB-safe before they land.
onboarding_gate = pytest.importorskip(
    "orchestrator.agents.onboarding_gate",
    reason="VT-421 onboarding_gate module not yet in tree — integrator re-runs",
)
activation_registry = pytest.importorskip(
    "orchestrator.agents.activation_registry",
    reason="VT-421 activation_registry module not yet in tree — integrator re-runs",
)

from orchestrator.agents import customer_send  # noqa: E402
from orchestrator.agents import sales_recovery_executor as sr  # noqa: E402
from orchestrator.agents.activation_registry import AgentPrerequisites  # noqa: E402
from orchestrator.agents.coordinator import AgentItemContext  # noqa: E402
from orchestrator.db import tenant_connection  # noqa: E402
import orchestrator.templates_registry as reg  # noqa: E402

_AGENT = "sales_recovery"
_FAKE_SID = "HX" + "0123456789abcdef" * 2  # matches ^HX[0-9a-f]{32}$
_TEST_TEMPLATE = "team_winback_vt421_itest"  # injected registry-only; never in the yaml


# ---------------------------------------------------------------------------
# Substrate — migrations (UNGUARDED) + DBOS launch so tenant_connection exists.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations through the unguarded ``apply(dsn=...)`` path (expected_env=None) + launch
    DBOS so ``tenant_connection`` exists."""
    import apply_migrations

    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "local-test-salt-not-secret")
    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


@pytest.fixture(autouse=True)
def _fresh_caches():
    reg._invalidate_cache()
    yield
    reg._invalidate_cache()


@pytest.fixture()
def armed_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Real yaml + one fully-sendable customer_marketing entry on the (customer_name,
    business_name) signature — a template that WOULD send if every gate let it."""
    yaml_path = Path(__file__).resolve().parents[2] / "config" / "twilio_templates.yaml"
    data = dict(reg._load_raw(yaml_path))
    data[_TEST_TEMPLATE] = {
        "audience": "customer",
        "category": "customer_marketing",
        "optout_line": True,
        "variables": ["customer_name", "business_name"],
        "languages": {"en": _FAKE_SID},
    }
    monkeypatch.setattr(reg, "_get_cached", lambda path=None: data)
    return data


# ---------------------------------------------------------------------------
# Recording transport — make EVERY would-be customer send observable. NEVER network.
# ---------------------------------------------------------------------------


class _RecordingCustomerSend:
    """Records every customer-send transport call; mimics send_template_message's SendResult.
    ``calls`` is the audit the zero-send proof asserts on."""

    def __init__(self, *, success: bool = True) -> None:
        self.success = success
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(
        self, tenant_id: Any, template_name: str, params: dict[str, Any],
        *, recipient_phone: str | None = None,
    ) -> SimpleNamespace:
        self.calls.append((str(tenant_id), template_name, dict(params)))
        if self.success:
            return SimpleNamespace(
                success=True, message_sid="SM" + uuid4().hex[:30],
                error_code=None, error_message=None,
            )
        return SimpleNamespace(
            success=False, message_sid=None,
            error_code="21211", error_message="simulated permanent failure",
        )


# ---------------------------------------------------------------------------
# Seed helpers (direct service-role — RLS bypassed at seed only).
# ---------------------------------------------------------------------------


def _new_tenant(
    dsn: str,
    *,
    phase: str = "trial",
    verification_status: str = "gstin_verified",
    owner_inputs: bool = True,
) -> UUID:
    """A tenants row. ``phase`` is now IRRELEVANT to eligibility (the gate keys on the journey row,
    not phase) — defaulted to 'trial' precisely to prove journey-complete, not paid, is the bar."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, owner_inputs, verification_status, whatsapp_number) "
            "VALUES (%s, 'founding', %s, now(), 'restaurant', %s, %s, %s) RETURNING id",
            (
                f"VT421 {uuid4().hex[:8]}", phase, owner_inputs, verification_status,
                f"+9198{uuid4().int % 10**8:08d}",
            ),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_journey(dsn: str, tenant: UUID, *, status: str = "complete") -> None:
    """The VT-367 onboarding_journey row at a given status. 'complete' = the activation signal."""
    completed = "now()" if status == "complete" else "NULL"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO onboarding_journey (tenant_id, status, completed_at) "
            f"VALUES (%s, %s, {completed})",
            (str(tenant), status),
        )


def _seed_connector(
    dsn: str, tenant: UUID, *, enabled: bool = True, last_status: str = "ok",
    connector_id: str | None = None,
) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_connector_status (tenant_id, connector_id, enabled, last_status, "
            "last_ingested_date) VALUES (%s, %s, %s, %s, CURRENT_DATE)",
            (str(tenant), connector_id or f"conn-{uuid4().hex[:8]}", enabled, last_status),
        )


def _seed_customer(dsn: str, tenant: UUID, *, phone: str | None = None) -> tuple[UUID, str]:
    phone = phone or f"+9197{uuid4().int % 10**8:08d}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO customers (tenant_id, display_name, phone_e164, opt_out_status, "
            "complaint_status) VALUES (%s, 'Ravi', %s, 'subscribed', 'none') RETURNING id",
            (str(tenant), phone),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0])), phone


def _seed_work_item(dsn: str, tenant: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
            "VALUES (%s, %s, %s, 'approved') RETURNING id",
            (str(tenant), f"item-{uuid4().hex[:12]}", _AGENT),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_batch(dsn: str, tenant: UUID, work_item: UUID, *, status: str = "approved") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_draft_batches (tenant_id, work_item_id, agent, status) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (str(tenant), str(work_item), _AGENT, status),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_draft(
    dsn: str, tenant: UUID, batch: UUID, customer: UUID, *,
    template_name: str = _TEST_TEMPLATE, status: str = "drafted",
) -> UUID:
    body = {"customer_name": "Ravi", "business_name": "Test Cafe"}
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_drafts (tenant_id, batch_id, customer_id, template_name, "
            "params, status) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (str(tenant), str(batch), str(customer), template_name, Jsonb(body), status),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_consent(dsn: str, tenant: UUID, phone: str, *, version: str) -> None:
    from orchestrator.utils.phone_token import hash_phone

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO record_of_consent (tenant_id, phone_token, consent_text_version) "
            "VALUES (%s, %s, %s)",
            (str(tenant), hash_phone(phone), version),
        )


# --- readback helpers --------------------------------------------------------


def _draft_row(dsn: str, tenant: UUID, draft: UUID) -> tuple[str, str | None]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, skip_reason FROM agent_drafts WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(draft)),
        ).fetchone()
    assert row is not None
    return str(row[0]), row[1]


def _count_drafts(dsn: str, tenant: UUID) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM agent_drafts WHERE tenant_id = %s", (str(tenant),)
            ).fetchone()[0]
        )


def _customer_contacts(dsn: str, tenant: UUID) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM agent_customer_contacts WHERE tenant_id = %s",
                (str(tenant),),
            ).fetchone()[0]
        )


def _ctx(tenant: UUID, work_item: UUID) -> AgentItemContext:
    return AgentItemContext(
        tenant_id=str(tenant),
        item_id=f"item-{uuid4().hex[:8]}",
        agent=_AGENT,
        work_item_id=str(work_item),
        run_id=str(uuid4()),
    )


def _seed_live_waba(dsn: str, tenant: UUID) -> None:
    """A 'live' WABA so the VT-460 Gate-0b WABA pre-gate passes on the send path. Harmless for the
    gate-0-only (is_agent_eligible) tests, which never read the WABA."""
    from uuid import uuid4

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_whatsapp_accounts (tenant_id, status, phone_number) "
            "VALUES (%s, 'live', %s)",
            (str(tenant), f"+9180{uuid4().int % 10**8:08d}"),
        )


def _activated_tenant(dsn: str, *, phase: str = "trial") -> SimpleNamespace:
    """A fully-ACTIVATED tenant: journey-complete + gstin_verified + enabled+ok connector +
    ≥1 customer + owner_inputs + a 'live' WABA (VT-460 Gate-0b). Defaults to phase='trial' to prove
    journey-complete (not paid) is the bar."""
    tenant = _new_tenant(dsn, phase=phase, verification_status="gstin_verified")
    _seed_journey(dsn, tenant, status="complete")
    _seed_connector(dsn, tenant)
    customer, phone = _seed_customer(dsn, tenant)
    _seed_live_waba(dsn, tenant)
    return SimpleNamespace(tenant=tenant, customer=customer, phone=phone)


# ===========================================================================
# 0. The eligibility helper directly (fail-closed unit pins, journey-complete model).
# ===========================================================================


def test_activated_tenant_returns_true(substrate):  # type: ignore[no-untyped-def]
    s = _activated_tenant(substrate.dsn)
    with tenant_connection(s.tenant) as conn:
        assert onboarding_gate.is_agent_eligible(s.tenant, _AGENT, conn=conn) is True
        # backward-compat alias agrees.
        assert onboarding_gate.tenant_is_sr_eligible(s.tenant, conn=conn) is True


def test_trial_tenant_with_full_set_is_admitted(substrate):  # type: ignore[no-untyped-def]
    """Fazal-pinned: the 1-month free trial is UNRESTRICTED. A phase='trial' tenant that is
    journey-complete + verified + connector + customers is ELIGIBLE — the bar is journey-complete,
    NOT paid-active. (The old model EXCLUDED trial; this is the deliberate flip.)"""
    s = _activated_tenant(substrate.dsn, phase="trial")
    with tenant_connection(s.tenant) as conn:
        assert onboarding_gate.is_agent_eligible(s.tenant, _AGENT, conn=conn) is True
        assert onboarding_gate.unmet_prerequisites(s.tenant, _AGENT, conn=conn) == []


def test_journey_incomplete_returns_false(substrate):  # type: ignore[no-untyped-def]
    """An onboarding (journey status='active') tenant — connector + customers + verified present —
    is ineligible: the journey-complete bar trips."""
    tenant = _new_tenant(substrate.dsn, phase="trial", verification_status="gstin_verified")
    _seed_journey(substrate.dsn, tenant, status="active")
    _seed_connector(substrate.dsn, tenant)
    _seed_customer(substrate.dsn, tenant)
    with tenant_connection(tenant) as conn:
        assert onboarding_gate.is_agent_eligible(tenant, _AGENT, conn=conn) is False
        assert "onboarding not complete" in onboarding_gate.unmet_prerequisites(
            tenant, _AGENT, conn=conn
        )


def test_no_journey_row_returns_false(substrate):  # type: ignore[no-untyped-def]
    """A tenant with NO onboarding_journey row at all → journey-complete unmet → ineligible."""
    tenant = _new_tenant(substrate.dsn, phase="paid_active", verification_status="gstin_verified")
    _seed_connector(substrate.dsn, tenant)
    _seed_customer(substrate.dsn, tenant)  # no journey row
    with tenant_connection(tenant) as conn:
        assert onboarding_gate.is_agent_eligible(tenant, _AGENT, conn=conn) is False


def test_journey_complete_but_unverified_returns_false(substrate):  # type: ignore[no-untyped-def]
    """journey-complete but verification_status='unverified' → False. Verification is asserted
    DIRECTLY, not inferred from phase or journey."""
    tenant = _new_tenant(substrate.dsn, phase="paid_active", verification_status="unverified")
    _seed_journey(substrate.dsn, tenant, status="complete")
    _seed_connector(substrate.dsn, tenant)
    _seed_customer(substrate.dsn, tenant)
    with tenant_connection(tenant) as conn:
        assert onboarding_gate.is_agent_eligible(tenant, _AGENT, conn=conn) is False
        assert "GSTIN not verified" in onboarding_gate.unmet_prerequisites(tenant, _AGENT, conn=conn)


def test_no_connector_returns_false(substrate):  # type: ignore[no-untyped-def]
    """journey-complete + verified + customers, but NO connector row → False with the right reason."""
    tenant = _new_tenant(substrate.dsn, verification_status="gstin_verified")
    _seed_journey(substrate.dsn, tenant, status="complete")
    _seed_customer(substrate.dsn, tenant)  # no connector
    with tenant_connection(tenant) as conn:
        assert onboarding_gate.is_agent_eligible(tenant, _AGENT, conn=conn) is False
        assert onboarding_gate.unmet_prerequisites(tenant, _AGENT, conn=conn) == [
            "no connected customer-data source"
        ]


def test_disabled_connector_returns_false(substrate):  # type: ignore[no-untyped-def]
    """A connector that exists but is enabled=FALSE does NOT count as connected → False."""
    tenant = _new_tenant(substrate.dsn, verification_status="gstin_verified")
    _seed_journey(substrate.dsn, tenant, status="complete")
    _seed_connector(substrate.dsn, tenant, enabled=False, last_status="ok")
    _seed_customer(substrate.dsn, tenant)
    with tenant_connection(tenant) as conn:
        assert onboarding_gate.is_agent_eligible(tenant, _AGENT, conn=conn) is False


def test_generalized_data_source_google_sheet(substrate):  # type: ignore[no-untyped-def]
    """The data-source prereq is GENERALIZED beyond shopify: a 'google_sheet' connector (enabled+ok)
    satisfies it just like any ingest connector."""
    tenant = _new_tenant(substrate.dsn, verification_status="gstin_verified")
    _seed_journey(substrate.dsn, tenant, status="complete")
    _seed_connector(substrate.dsn, tenant, connector_id="google_sheet")
    _seed_customer(substrate.dsn, tenant)
    with tenant_connection(tenant) as conn:
        assert onboarding_gate.is_agent_eligible(tenant, _AGENT, conn=conn) is True


def test_zero_customers_returns_false(substrate):  # type: ignore[no-untyped-def]
    """journey-complete + verified + connector, but 0 ingested customers → False."""
    tenant = _new_tenant(substrate.dsn, verification_status="gstin_verified")
    _seed_journey(substrate.dsn, tenant, status="complete")
    _seed_connector(substrate.dsn, tenant)  # no customers
    with tenant_connection(tenant) as conn:
        assert onboarding_gate.is_agent_eligible(tenant, _AGENT, conn=conn) is False
        assert onboarding_gate.unmet_prerequisites(tenant, _AGENT, conn=conn) == [
            "no customers ingested"
        ]


def test_unmet_lists_all_reasons_for_bare_tenant(substrate):  # type: ignore[no-untyped-def]
    """A tenant with NOTHING (no journey, unverified, no connector, no customers) reports ALL four
    unmet reasons — the portal sees the full picture, not just the first failure."""
    tenant = _new_tenant(substrate.dsn, verification_status="unverified")
    with tenant_connection(tenant) as conn:
        reasons = onboarding_gate.unmet_prerequisites(tenant, _AGENT, conn=conn)
    assert set(reasons) == {
        "onboarding not complete",
        "GSTIN not verified",
        "no connected customer-data source",
        "no customers ingested",
    }


def test_missing_tenant_row_returns_false(substrate):  # type: ignore[no-untyped-def]
    """A tenant_id with no tenants row → False (the RLS conn sees nothing)."""
    ghost = uuid4()
    with tenant_connection(ghost) as conn:
        assert onboarding_gate.is_agent_eligible(ghost, _AGENT, conn=conn) is False


def test_unknown_agent_fails_closed(substrate):  # type: ignore[no-untyped-def]
    """An agent NOT in the registry → ineligible (KeyError caught → fail-closed). The gate never
    silently admits an agent we never declared a bar for. unmet returns a sentinel reason."""
    s = _activated_tenant(substrate.dsn)
    with tenant_connection(s.tenant) as conn:
        assert onboarding_gate.is_agent_eligible(s.tenant, "no_such_agent", conn=conn) is False
        assert onboarding_gate.unmet_prerequisites(s.tenant, "no_such_agent", conn=conn) != []


def test_read_error_fails_closed(substrate):  # type: ignore[no-untyped-def]
    """A forced conn.execute exception → False (the except-returns-False path)."""
    s = _activated_tenant(substrate.dsn)

    class _Boom:
        def execute(self, *_a, **_k):  # noqa: ANN002, ANN003, ANN201
            raise RuntimeError("simulated DB read failure")

    assert onboarding_gate.is_agent_eligible(s.tenant, _AGENT, conn=_Boom()) is False
    # unmet is fail-closed too (sentinel reason, not an empty/"eligible" list).
    assert onboarding_gate.unmet_prerequisites(s.tenant, _AGENT, conn=_Boom()) != []


# ===========================================================================
# 1. The REGISTRY itself — structure, SR's entry, and EXTENSIBILITY (a stub agent).
# ===========================================================================


def test_sr_registry_entry_shape():  # type: ignore[no-untyped-def]
    """SR's declared bar is journey-complete + verified + data-source + ≥1 customer."""
    sr_prereqs = activation_registry.get_prerequisites("sales_recovery")
    assert sr_prereqs.requires_journey_complete is True
    assert sr_prereqs.requires_verification is True
    assert sr_prereqs.requires_enabled_data_source is True
    assert sr_prereqs.min_customers == 1


def test_unknown_agent_raises_keyerror():  # type: ignore[no-untyped-def]
    with pytest.raises(KeyError):
        activation_registry.get_prerequisites("not_a_registered_agent")


def test_second_agent_evaluates_independently(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """EXTENSIBILITY proof: register a SECOND agent whose bar is journey-complete + verified ONLY
    (no data-source, no customers). For a journey-complete + verified tenant with NO connector and
    NO customers, SR is INELIGIBLE (its bar requires those) but the stub agent is ELIGIBLE — the
    two evaluate independently off the SAME registry, with ZERO gate-logic change."""
    stub = AgentPrerequisites(
        agent="stub_agent",
        requires_journey_complete=True,
        requires_verification=True,
        requires_enabled_data_source=False,
        min_customers=0,
    )
    monkeypatch.setitem(activation_registry.REGISTRY, "stub_agent", stub)

    tenant = _new_tenant(substrate.dsn, verification_status="gstin_verified")
    _seed_journey(substrate.dsn, tenant, status="complete")  # no connector, no customers
    with tenant_connection(tenant) as conn:
        assert onboarding_gate.is_agent_eligible(tenant, "sales_recovery", conn=conn) is False
        assert onboarding_gate.is_agent_eligible(tenant, "stub_agent", conn=conn) is True
        assert onboarding_gate.unmet_prerequisites(tenant, "stub_agent", conn=conn) == []


# ===========================================================================
# 2. DETECT side (call site A) — non-activated tenant NO-OPs; activated passes the gate.
# ===========================================================================


def test_detect_noop_for_journey_incomplete(substrate):  # type: ignore[no-untyped-def]
    """A journey-incomplete tenant (connector + customers present) → execute_item returns
    cancelled + skipped_not_onboarded, and detection NEVER runs (0 drafts persisted)."""
    tenant = _new_tenant(substrate.dsn, verification_status="gstin_verified")
    _seed_journey(substrate.dsn, tenant, status="active")
    _seed_connector(substrate.dsn, tenant)
    _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)

    out = sr.SalesRecoveryAgent().execute_item(_ctx(tenant, work_item))

    assert out.work_item_status == "cancelled"
    assert out.counters == {"skipped_not_onboarded": 1}
    assert _count_drafts(substrate.dsn, tenant) == 0  # detection / drafting never reached


def test_detect_passes_gate_for_activated_tenant(substrate):  # type: ignore[no-untyped-def]
    """A fully-activated tenant PASSES Gate 0 — execute_item does NOT short-circuit on
    skipped_not_onboarded; it proceeds INTO detection (which with the empty C2 allowlist returns
    skipped_no_candidates — proving the gate let it through to the detect phase)."""
    s = _activated_tenant(substrate.dsn)
    work_item = _seed_work_item(substrate.dsn, s.tenant)

    # C2 stays EMPTY at rest — detection returns [] structurally, so the activated tenant reaches
    # the detect phase and reports skipped_no_candidates (NOT skipped_not_onboarded). That is the
    # proof Gate 0 passed.
    assert sr.MARKETING_CONSENT_VERSIONS == frozenset()
    out = sr.SalesRecoveryAgent().execute_item(_ctx(s.tenant, work_item))

    assert out.work_item_status == "cancelled"
    assert out.counters == {"skipped_no_candidates": 1}
    assert "skipped_not_onboarded" not in out.counters


# ===========================================================================
# 3. SEND side (call site B = Gate 0) — the LOAD-BEARING safety boundary, BOTH L2 and L3.
# ===========================================================================


@pytest.mark.usefixtures("armed_registry")
def test_send_blocked_for_journey_incomplete_l2(substrate):  # type: ignore[no-untyped-def]
    """The +919321553267-style block, L2 trigger: a journey-INCOMPLETE tenant with an APPROVED L2
    batch + a drafted draft → agent_send_draft returns skipped (SKIP_NOT_ONBOARDED), ZERO Twilio
    calls. Gate 0 short-circuits even with an otherwise fully-sendable approved batch."""
    tenant = _new_tenant(substrate.dsn, verification_status="gstin_verified")
    _seed_journey(substrate.dsn, tenant, status="active")
    _seed_connector(substrate.dsn, tenant)
    customer, phone = _seed_customer(substrate.dsn, tenant, phone="+919321553267")
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="approved")
    draft = _seed_draft(substrate.dsn, tenant, batch, customer)
    send_fn = _RecordingCustomerSend()

    out = customer_send.agent_send_draft(
        tenant, draft, autonomy_level="L2", send_fn=send_fn
    )

    assert out.status == "skipped"
    assert out.skip_reason == customer_send.SKIP_NOT_ONBOARDED
    assert send_fn.calls == [], "Gate 0 must block before any Twilio send"
    assert _customer_contacts(substrate.dsn, tenant) == 0
    assert _draft_row(substrate.dsn, tenant, draft) == ("skipped", customer_send.SKIP_NOT_ONBOARDED)


@pytest.mark.usefixtures("armed_registry")
def test_send_blocked_for_journey_incomplete_l3(substrate):  # type: ignore[no-untyped-def]
    """Same block, L3 trigger (the L3-wake / auto_send_pending path): Gate 0 sits ABOVE the L3
    batch-state gate, so a journey-incomplete tenant's auto_send_pending batch is blocked too — ONE
    Gate 0 covers BOTH send paths. ZERO Twilio calls."""
    tenant = _new_tenant(substrate.dsn, verification_status="gstin_verified")
    _seed_journey(substrate.dsn, tenant, status="active")
    _seed_connector(substrate.dsn, tenant)
    customer, phone = _seed_customer(substrate.dsn, tenant, phone="+919321553267")
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="auto_send_pending")
    draft = _seed_draft(substrate.dsn, tenant, batch, customer)
    send_fn = _RecordingCustomerSend()

    out = customer_send.agent_send_draft(
        tenant, draft, autonomy_level="L3", send_fn=send_fn
    )

    assert out.status == "skipped"
    assert out.skip_reason == customer_send.SKIP_NOT_ONBOARDED
    assert send_fn.calls == [], "Gate 0 must block the L3 path before any Twilio send"
    assert _customer_contacts(substrate.dsn, tenant) == 0


@pytest.mark.usefixtures("armed_registry")
def test_send_passes_gate0_for_activated_trial_tenant(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """A fully-activated TRIAL tenant PASSES Gate 0 on the SEND side: with the C2 gate opened
    (matching consent + patched allowlist — the L2-test pattern), agent_send_draft proceeds THROUGH
    Gate 0 and the downstream gates to a real (injected) send. This proves the journey-complete bar
    ADMITS a trial tenant on the load-bearing send path — it is NOT a paid-only block."""
    s = _activated_tenant(substrate.dsn, phase="trial")
    work_item = _seed_work_item(substrate.dsn, s.tenant)
    batch = _seed_batch(substrate.dsn, s.tenant, work_item, status="approved")
    draft = _seed_draft(substrate.dsn, s.tenant, batch, s.customer)
    _seed_consent(substrate.dsn, s.tenant, s.phone, version="vt421-gate0-v1")
    monkeypatch.setattr(
        customer_send, "_marketing_consent_versions", lambda: frozenset({"vt421-gate0-v1"})
    )
    send_fn = _RecordingCustomerSend()

    out = customer_send.agent_send_draft(
        s.tenant, draft, autonomy_level="L2", send_fn=send_fn
    )

    # Gate 0 passed (no SKIP_NOT_ONBOARDED); the send reached the transport exactly once.
    assert out.skip_reason != customer_send.SKIP_NOT_ONBOARDED
    assert out.status == "sent"
    assert len(send_fn.calls) == 1
    assert _draft_row(substrate.dsn, s.tenant, draft)[0] == "sent"
