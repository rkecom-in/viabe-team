"""VT-460 — consolidated adversarial NON-BYPASSABILITY proof for the customer-send rail.

This is the FOUNDATION deliverable of the rail harness (rail-harness-findings.md): a single module
that adversarially PROVES the existing rails cannot be bypassed. It REUSES the production guard +
gate functions (it does NOT re-implement them) — every test exercises the REAL code path and proves
a side-effect is structurally impossible.

It maps to the test-matrix "set D" (scenarios 19-25, the deterministic safety rails that must stay
100% per design §6 — consent allowlist + opt-out, caps, onboarded-gate, WABA-live, the transport
choke). Two layers, mirroring the rail's two structural boundaries:

  A. CAPABILITY rail (the brain holds NO side-effect tool) — the VT-268 guard. Adversarial:
     attempt to build the agent with a send/write tool → MUST raise at graph-build.
       D19  brain cannot HOLD a direct customer-send tool (build raises).
       D20  brain cannot HOLD an accounts-book / ledger-write tool (build raises).
       D21  the pinned agent allowlist exposes no send/write capability (defense in depth).

  B. SEND-CHOKE rail (every customer send routes a deterministic gate stack) — agent_send_draft +
     the unified pre-gate + the transport choke. Adversarial (DB-backed):
       D22  empty MARKETING_CONSENT_VERSIONS ⇒ ZERO marketing sends (the C2 structural stop).
       D23  an opted-out customer ⇒ blocked (no transport call).
       D24  a non-onboarded tenant ⇒ Gate-0 blocks (no transport call).
       D25  the transport itself fails CLOSED for an un-gated customer send (structural choke).

DB substrate mirrors tests/orchestrator/agents/test_customer_send.py (importorskip psycopg+dbos,
skipif no DATABASE_URL, migrations applied once + DBOS launched module-scoped, rows seeded via a
direct service-role connection, the code exercised through tenant_connection — the real RLS path).
No live Twilio (injected send_fn); no LLM (the path under test is deterministic by design).
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

# ===========================================================================
# Layer A — the CAPABILITY rail (no DB): the brain holds NO side-effect tool
# ===========================================================================

# langchain is required to build an agent surface; skip the capability layer without it.
_HAS_LANGCHAIN = True
try:  # pragma: no cover - import probe
    import langchain  # noqa: F401
except Exception:  # noqa: BLE001
    _HAS_LANGCHAIN = False

requires_langchain = pytest.mark.skipif(
    not _HAS_LANGCHAIN, reason="langchain not installed — capability-rail proofs skipped"
)


@requires_langchain
@pytest.mark.parametrize(
    "evil_tool_name",
    [
        # D19 — direct customer-send capabilities (every FORBIDDEN send substring).
        "send_whatsapp_message",
        "send_whatsapp_template",
        "send_template_message",
        "send_freeform_message",
        "send_to_customer_now",
        # D20 — accounts-book / ledger writes.
        "append_to_sheet",
        "write_accounts_book",
        "record_ledger_entries",
        "write_ledger_entry",
    ],
)
def test_D19_D20_brain_cannot_hold_a_send_or_write_tool(evil_tool_name: str) -> None:
    """D19/D20: handing the agent builder ANY send/write-capable tool RAISES at build — the brain
    can never structurally HOLD a side-effect tool (the VT-268 fail-closed guard, proven against the
    real build_orchestrator_agent, not a mock)."""
    from langchain_core.tools import tool

    from orchestrator.agent.orchestrator_agent import _MODEL, build_orchestrator_agent
    from orchestrator.agent.tool_guardrail import ToolGuardrailViolation

    # Build a real langchain tool whose NAME matches a forbidden capability substring. langchain
    # requires a docstring (the description), so provide one — the guard keys on the NAME, and the
    # guard MUST fire regardless of how innocuous the description reads.
    def _impl(x: str) -> str:
        """A would-be side-effect tool that must never reach the agent surface."""
        return x

    evil = tool(evil_tool_name)(_impl)  # name = evil_tool_name

    with pytest.raises(ToolGuardrailViolation):
        build_orchestrator_agent(_MODEL, extra_tools=[evil])


@requires_langchain
def test_D21_pinned_agent_surface_exposes_no_side_effect_capability() -> None:
    """D21 (defense in depth): the REAL pinned agent + handoff surface passes the guard AND
    contains none of the forbidden capability substrings. Proves the shipped surface is safe, not
    just that the guard would catch a bad addition."""
    from orchestrator.agent.integration_agent import INTEGRATION_AGENT_TOOLS
    from orchestrator.agent.orchestrator_agent import ORCHESTRATOR_AGENT_TOOLS
    from orchestrator.agent.tool_guardrail import (
        FORBIDDEN_CAPABILITY_SUBSTRINGS,
        assert_agent_tools_safe,
        find_forbidden_tools,
    )
    from orchestrator.handoffs import spawn_integration, spawn_sales_recovery

    full_surface = [
        *ORCHESTRATOR_AGENT_TOOLS,
        *INTEGRATION_AGENT_TOOLS,
        spawn_sales_recovery,
        spawn_integration,
    ]
    # No raise on the real surface.
    assert_agent_tools_safe(full_surface, surface="rail_harness_full_surface")
    # And no tool name carries a forbidden substring (the allowlist is genuinely side-effect-free).
    assert find_forbidden_tools(full_surface) == []
    # The guard's forbidden set still covers BOTH classes (send + write) — a regression that emptied
    # it would silently open the boundary.
    joined = " ".join(FORBIDDEN_CAPABILITY_SUBSTRINGS)
    assert "send" in joined and ("ledger" in joined or "sheet" in joined)


# ===========================================================================
# Layer B — the SEND-CHOKE rail (DB-backed): every customer send is gated
# ===========================================================================

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after the dependency skip guards
from psycopg.types.json import Jsonb  # noqa: E402

import orchestrator.templates_registry as reg  # noqa: E402
from orchestrator.agents import customer_send  # noqa: E402
from orchestrator.db import tenant_connection  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-460 send-choke proof tests skipped",
)

_REAL_YAML_PATH = Path(__file__).resolve().parents[2] / "config" / "twilio_templates.yaml"
_FAKE_SID = "HX" + "0123456789abcdef" * 2  # matches ^HX[0-9a-f]{32}$
_TEST_TEMPLATE = "team_winback_railproof"
_TEST_PARAMS = {"customer_name": "Ravi", "business_name": "Test Cafe"}


@pytest.fixture(autouse=True)
def _fresh_registry_cache():  # type: ignore[no-untyped-def]
    reg._invalidate_cache()
    yield
    reg._invalidate_cache()


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so the tenant_connection pool exists (mirrors test_customer_send)."""
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt460-railproof-salt")
    if not os.environ.get("TEAM_PHONE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        os.environ["TEAM_PHONE_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


@pytest.fixture()
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Real yaml + one fully-sendable customer_marketing test entry (SID + optout line)."""
    data = dict(reg._load_raw(_REAL_YAML_PATH))
    data[_TEST_TEMPLATE] = {
        "audience": "customer",
        "category": "customer_marketing",
        "optout_line": True,
        "variables": ["customer_name", "business_name"],
        "languages": {"en": _FAKE_SID},
    }
    monkeypatch.setattr(reg, "_get_cached", lambda path=None: data)
    return data


@pytest.fixture()
def allow_test_consent_version(monkeypatch: pytest.MonkeyPatch) -> str:
    """Patch the C2 allowlist to a test version so a path CAN pass the marketing-consent gate."""
    monkeypatch.setattr(
        customer_send, "_marketing_consent_versions", lambda: frozenset({"railproof-v1"})
    )
    return "railproof-v1"


# --- seeding helpers (direct service-role connection — RLS bypassed at seed) ---


def _new_tenant(dsn: str, *, onboarded: bool = True, wa_live: bool = True) -> UUID:
    """Seed a tenant. ``onboarded`` satisfies the activation bar (journey-complete + gstin_verified
    + ≥1 enabled connector; ≥1 customer is the per-test _seed_customer). ``wa_live`` seeds a live
    WABA so the universal WABA pre-gate passes (the agent path's Gate-0b + the campaign/inbound
    pre-gate)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        verification = "gstin_verified" if onboarded else "unverified"
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, verification_status, whatsapp_number) "
            "VALUES (%s, 'founding', 'paid_active', now(), 'restaurant', %s, %s) RETURNING id",
            ("VT-460 railproof", verification, f"+9198{uuid4().int % 10**8:08d}"),
        ).fetchone()
        assert row is not None
        tenant = UUID(str(row[0]))
        if onboarded:
            conn.execute(
                "INSERT INTO tenant_connector_status (tenant_id, connector_id, enabled, last_status, "
                "last_ingested_date) VALUES (%s, %s, TRUE, 'ok', CURRENT_DATE)",
                (str(tenant), f"conn-{uuid4().hex[:8]}"),
            )
            conn.execute(
                "INSERT INTO onboarding_journey (tenant_id, status, completed_at) "
                "VALUES (%s, 'complete', now())",
                (str(tenant),),
            )
        if wa_live:
            conn.execute(
                "INSERT INTO tenant_whatsapp_accounts (tenant_id, status, phone_number) "
                "VALUES (%s, 'live', %s)",
                (str(tenant), f"+9180{uuid4().int % 10**8:08d}"),
            )
    return tenant


def _seed_customer(
    dsn: str, tenant: UUID, *, opt_out_status: str = "subscribed", complaint_status: str = "none",
) -> tuple[UUID, str]:
    phone = f"+9197{uuid4().int % 10**8:08d}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO customers (tenant_id, display_name, phone_e164, opt_out_status, "
            "complaint_status) VALUES (%s, 'Ravi', %s, %s, %s) RETURNING id",
            (str(tenant), phone, opt_out_status, complaint_status),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0])), phone


def _seed_work_item(dsn: str, tenant: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
            "VALUES (%s, %s, 'sales_recovery', 'approved') RETURNING id",
            (str(tenant), f"item-{uuid4().hex[:12]}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_batch(dsn: str, tenant: UUID, work_item: UUID, *, status: str = "approved") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_draft_batches (tenant_id, work_item_id, agent, status) "
            "VALUES (%s, %s, 'sales_recovery', %s) RETURNING id",
            (str(tenant), str(work_item), status),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_draft(dsn: str, tenant: UUID, batch: UUID, customer: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_drafts (tenant_id, batch_id, customer_id, template_name, params) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (str(tenant), str(batch), str(customer), _TEST_TEMPLATE, Jsonb(dict(_TEST_PARAMS))),
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


class _FakeSendFn:
    """Records every transport call; mimics twilio_send.send_template_message's SendResult. The
    transport is NEVER reached in these proofs — a recorded call would itself BE the bypass."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(
        self, tenant_id: Any, template_name: str, params: dict[str, Any],
        *, recipient_phone: str | None = None,
    ) -> SimpleNamespace:
        self.calls.append((str(tenant_id), template_name, dict(params)))
        return SimpleNamespace(
            success=True, message_sid="SM" + uuid4().hex[:30], error_code=None, error_message=None,
        )


def _send(tenant: UUID, draft: UUID, send_fn: Any, **kw: Any) -> customer_send.AgentSendResult:
    with tenant_connection(tenant) as conn:
        return customer_send.agent_send_draft(tenant, draft, conn=conn, send_fn=send_fn, **kw)


def _full_stack(dsn: str, *, opt_out_status: str = "subscribed") -> SimpleNamespace:
    tenant = _new_tenant(dsn)
    customer, phone = _seed_customer(dsn, tenant, opt_out_status=opt_out_status)
    work_item = _seed_work_item(dsn, tenant)
    batch = _seed_batch(dsn, tenant, work_item)
    draft = _seed_draft(dsn, tenant, batch, customer)
    return SimpleNamespace(tenant=tenant, customer=customer, phone=phone, draft=draft, batch=batch)


# --- D22: empty MARKETING_CONSENT_VERSIONS ⇒ ZERO marketing sends ---


@requires_db
def test_D22_empty_marketing_consent_versions_yields_zero_sends(substrate, fake_registry):  # type: ignore[no-untyped-def]
    """D22 — THE C2 structural stop. With MARKETING_CONSENT_VERSIONS empty (its production default
    until counsel clears it), a recorded consent of ANY version never resolves → the marketing gate
    fails closed → ZERO marketing sends, even on a fully-onboarded, WABA-live, opted-in customer.
    This proves the empty allowlist is a structural zero-send, not a tunable.

    NOTE: this test does NOT patch _marketing_consent_versions — it asserts the SHIPPED default."""
    # Sanity: the production default really is empty (the structural stop's premise).
    assert customer_send._marketing_consent_versions() == frozenset(), (
        "MARKETING_CONSENT_VERSIONS must be empty by default (C2 not yet cleared) — a non-empty "
        "default would silently arm marketing sends"
    )
    s = _full_stack(substrate.dsn)
    _seed_consent(substrate.dsn, s.tenant, s.phone, version="any-recorded-version")
    send_fn = _FakeSendFn()

    result = _send(s.tenant, s.draft, send_fn)

    assert result.status == "skipped"
    assert result.skip_reason == customer_send.SKIP_CONSENT
    assert send_fn.calls == [], "the transport must NEVER be reached on the empty allowlist"


# --- D23: opted-out customer ⇒ blocked ---


@requires_db
def test_D23_opted_out_customer_is_blocked(substrate, fake_registry, allow_test_consent_version):  # type: ignore[no-untyped-def]
    """D23 — an opted-out customer is blocked at the send-time re-read (Gate-3), NEVER reaching the
    transport, even with consent recorded + the allowlist patched to allow a version. Opt-out wins."""
    s = _full_stack(substrate.dsn, opt_out_status="opted_out")
    _seed_consent(substrate.dsn, s.tenant, s.phone, version="railproof-v1")
    send_fn = _FakeSendFn()

    result = _send(s.tenant, s.draft, send_fn)

    assert result.status == "skipped"
    assert result.skip_reason == customer_send.SKIP_OPT_OUT
    assert send_fn.calls == []


# --- D24: non-onboarded tenant ⇒ Gate-0 blocks ---


@requires_db
def test_D24_non_onboarded_tenant_is_blocked_at_gate0(substrate, fake_registry, allow_test_consent_version):  # type: ignore[no-untyped-def]
    """D24 — a non-onboarded tenant (activation bar not crossed) is blocked at Gate-0 BEFORE any
    other gate runs, regardless of consent / opt-in. Even a perfectly opted-in, allowlisted customer
    on a not-onboarded tenant gets ZERO sends."""
    tenant = _new_tenant(substrate.dsn, onboarded=False)
    customer, phone = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item)
    draft = _seed_draft(substrate.dsn, tenant, batch, customer)
    _seed_consent(substrate.dsn, tenant, phone, version="railproof-v1")
    send_fn = _FakeSendFn()

    result = _send(tenant, draft, send_fn)

    assert result.status == "skipped"
    assert result.skip_reason == customer_send.SKIP_NOT_ONBOARDED
    assert send_fn.calls == []


@requires_db
def test_D24b_not_live_waba_is_blocked_at_gate0b(substrate, fake_registry, allow_test_consent_version):  # type: ignore[no-untyped-def]
    """D24b — VT-460 gap (b): a fully-onboarded tenant whose WABA is NOT live is blocked at the new
    universal WABA pre-gate (Gate-0b), not discovered as a downstream Twilio 4xx. ZERO transport call."""
    tenant = _new_tenant(substrate.dsn, onboarded=True, wa_live=False)
    customer, phone = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item)
    draft = _seed_draft(substrate.dsn, tenant, batch, customer)
    _seed_consent(substrate.dsn, tenant, phone, version="railproof-v1")
    send_fn = _FakeSendFn()

    result = _send(tenant, draft, send_fn)

    assert result.status == "skipped"
    assert result.skip_reason == customer_send.SKIP_WABA_NOT_LIVE
    assert send_fn.calls == []


# --- shared pre-gate: the unified onboarded+WABA choke the campaign + inbound paths use (gap a/b) ---


@requires_db
def test_shared_pregate_blocks_non_onboarded_and_not_live(substrate):  # type: ignore[no-untyped-def]
    """VT-460 gap (a)/(b): the SHARED assert_customer_send_allowed — the SAME deterministic
    onboarded + WABA-live choke now wired into the campaign + inbound paths (closing the
    gate-coverage asymmetry). Proven directly: non-onboarded → SKIP_NOT_ONBOARDED; onboarded but
    WABA-not-live → SKIP_WABA_NOT_LIVE; fully onboarded + live → allowed. Reuses the existing gate
    functions (is_agent_eligible + wa_send_allowed), not a re-implementation."""
    from orchestrator.agents.customer_send_choke import (
        SKIP_NOT_ONBOARDED,
        SKIP_WABA_NOT_LIVE,
        assert_customer_send_allowed,
    )

    # non-onboarded → onboarded gate blocks first.
    t_unonboarded = _new_tenant(substrate.dsn, onboarded=False, wa_live=True)
    with tenant_connection(t_unonboarded) as conn:
        g = assert_customer_send_allowed(t_unonboarded, agent="sales_recovery", conn=conn)
    assert g.allowed is False and g.reason == SKIP_NOT_ONBOARDED

    # onboarded but not-live WABA → WABA pre-gate blocks. (≥1 customer needed for the onboarded leg.)
    t_notlive = _new_tenant(substrate.dsn, onboarded=True, wa_live=False)
    _seed_customer(substrate.dsn, t_notlive)
    with tenant_connection(t_notlive) as conn:
        g = assert_customer_send_allowed(t_notlive, agent="sales_recovery", conn=conn)
    assert g.allowed is False and g.reason == SKIP_WABA_NOT_LIVE

    # fully onboarded + live → allowed.
    t_ok = _new_tenant(substrate.dsn, onboarded=True, wa_live=True)
    _seed_customer(substrate.dsn, t_ok)
    with tenant_connection(t_ok) as conn:
        g = assert_customer_send_allowed(t_ok, agent="sales_recovery", conn=conn)
    assert g.allowed is True and g.reason is None


# --- D25: the transport itself fails CLOSED for an un-gated customer send ---


def test_D25_transport_refuses_ungated_customer_template_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """D25 — the VT-460 gap (c) structural choke, proven at the TRANSPORT (no DB needed). A
    send_template_message flagged is_customer_send=True (set only by the VT-45 tool) OUTSIDE
    customer_send_context() raises UngatedCustomerSendError BEFORE any Twilio call — a future
    un-gated direct caller fails closed. A gated send (inside the context) is admitted. An owner
    send (is_customer_send=False, the default) is exempt — even using a `customer`-audience template,
    because some audience:customer templates (opt-out/status-ping acks) are owner-reply sends."""
    from orchestrator.utils import twilio_send
    from orchestrator.utils.twilio_send import (
        UngatedCustomerSendError,
        customer_send_context,
        send_template_message,
    )

    # A SID-less entry so a gated/owner send stops at the honest no-SID early-out (no Twilio).
    fake_entry = SimpleNamespace(content_sid=None, audience="customer", variables=("x",))
    monkeypatch.setattr(twilio_send, "_registry_resolve", lambda name, lang="en": fake_entry)
    monkeypatch.setattr(twilio_send, "get_tenant_whatsapp_number", lambda tid: "+910000000000")

    tid = UUID(int=1)

    # Un-gated customer send → structural refusal at the transport, before any Twilio dispatch.
    with pytest.raises(UngatedCustomerSendError):
        send_template_message(
            tid, "railproof_tmpl", {"x": "v"}, recipient_phone="+919999999999", is_customer_send=True,
        )

    # Gated customer send → admitted past the choke (then the SID-less honest no-send — the choke
    # let it through; it did NOT raise).
    with customer_send_context():
        out = send_template_message(
            tid, "railproof_tmpl", {"x": "v"}, recipient_phone="+919999999999", is_customer_send=True,
        )
    assert out.success is False and out.error_code == "template_not_yet_approved"

    # Owner send (is_customer_send=False, default) → exempt: no context, no raise — EVEN on a
    # `customer`-audience template (the audience field is NOT the trigger; the explicit flag is).
    out_owner = send_template_message(
        tid, "railproof_tmpl", {"x": "v"}, recipient_phone="+910000000000",
    )
    assert out_owner.success is False and out_owner.error_code == "template_not_yet_approved"


def test_D25b_transport_refuses_ungated_customer_freeform(monkeypatch: pytest.MonkeyPatch) -> None:
    """D25b — the freeform half of the transport choke (the VT-287 inbound session class). A
    customer-session freeform (is_customer_session=True) OUTSIDE the gated context raises; an owner
    freeform (the default) is exempt and proceeds to dispatch."""
    from orchestrator.utils import twilio_send
    from orchestrator.utils.twilio_send import (
        UngatedCustomerSendError,
        customer_send_context,
        send_freeform_message,
    )

    sent: list[str] = []

    class _FakeClient:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kwargs: Any) -> Any:
                sent.append(kwargs.get("to", ""))
                return SimpleNamespace(sid="SMfake")

    monkeypatch.setattr(twilio_send, "_client", lambda: _FakeClient())
    monkeypatch.setenv("TEAM_TWILIO_FROM_NUMBER", "+910000000000")
    monkeypatch.setenv("TEAM_PHONE_HASH_SALT", "vt460-railproof-salt")

    # Un-gated customer-session freeform → structural refusal (no Twilio call recorded).
    with pytest.raises(UngatedCustomerSendError):
        send_freeform_message("intro", "+919999999999", is_customer_session=True)
    assert sent == []

    # Gated customer-session freeform → admitted.
    with customer_send_context():
        send_freeform_message("intro", "+919999999999", is_customer_session=True)
    assert len(sent) == 1

    # Owner freeform (default) → exempt, proceeds with no context.
    send_freeform_message("owner ack", "+910000000000")
    assert len(sent) == 2
