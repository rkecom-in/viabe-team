"""VT-369 Gap-5 PR-1 — behavioral tests for ``orchestrator.agents.customer_send``.

The ONE customer-send choke point: every gate in the stack must fail CLOSED
with its own distinct marker, and the happy path must delegate to the EXISTING
VT-45 send path (``send_whatsapp_template``) exactly once per draft — the
``send_idempotency_keys`` ledger row keyed ``agent:{draft_id}`` makes a
re-attempt a no-send.

No live Twilio anywhere: ``send_fn`` is injected (the conftest autouse stub
guards the default path as well). No LLM anywhere — the module under test is
deterministic by design (Pillar 1).

DB substrate mirrors ``tests/orchestrator/business_plan/test_generator.py``:
importorskip psycopg+dbos, skipif no DATABASE_URL, migrations applied once +
DBOS launched (module-scoped fixture), rows seeded via a direct service-role
psycopg connection, the code under test exercised through ``tenant_connection``
(real RLS path).

Registry coverage (MED-1 — the new ``category``/``money_bearing``/``optout_line``
fields ride the SHARED resolver that the live campaign path uses): every
pre-existing template must still resolve with the new TemplateEntry defaults,
and ``canary_load`` must reject malformed Gap-5 fields.
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

import psycopg  # noqa: E402 — after dependency skip guards
from psycopg.types.json import Jsonb  # noqa: E402

import orchestrator.templates_registry as reg  # noqa: E402
from orchestrator.agents import customer_send  # noqa: E402
from orchestrator.db import tenant_connection  # noqa: E402
from orchestrator.templates_registry import (  # noqa: E402
    TemplateRegistryError,
    canary_load,
    resolve,
)

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-369 customer_send substrate tests skipped",
)

_REAL_YAML_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "twilio_templates.yaml"
)

_FAKE_SID = "HX" + "0123456789abcdef" * 2  # matches ^HX[0-9a-f]{32}$
_TEST_TEMPLATE = "team_winback_itest"  # injected registry-only; never in the yaml
_TEST_PARAMS = {"customer_name": "Ravi", "business_name": "Test Cafe"}
_GAP5_NAMES = (
    "team_winback_simple",
    "team_winback_offer",
    "team_agent_draft_approval",
    "team_l3_presend_notice",
    "team_autonomy_offer",
)


@pytest.fixture(autouse=True)
def _fresh_registry_cache():
    """The registry TTL cache is module-global — keep tests order-independent."""
    reg._invalidate_cache()
    yield
    reg._invalidate_cache()


# ---------------------------------------------------------------------------
# Registry: new fields parse, existing entries unbroken (MED-1) — no DB needed
# ---------------------------------------------------------------------------


def test_every_existing_template_still_resolves_with_new_field_defaults() -> None:
    """The category/money_bearing/optout_line fields are ADDITIVE: every entry
    in the real yaml — pre-existing and Gap-5 — resolves for every declared
    language, and entries without the fields get the safe defaults (which can
    never pass the customer_marketing send gate)."""
    raw = reg._load_raw(_REAL_YAML_PATH)
    assert raw, "real yaml is empty?"
    for name, entry in raw.items():
        assert isinstance(entry, dict)
        for lang in entry.get("languages") or {}:
            resolved = resolve(name, lang, _path=_REAL_YAML_PATH)
            assert resolved.template_name == name
            if "category" not in entry:
                assert resolved.category == ""
                assert resolved.money_bearing is False
                assert resolved.optout_line is False


def test_gap5_entries_parse_into_template_entry_fields() -> None:
    import re

    for name in _GAP5_NAMES:
        entry = resolve(name, "en", _path=_REAL_YAML_PATH)
        # VT-383 (F1 armed): the five entries carry real Content SIDs + sha pins now.
        assert entry.content_sid and re.fullmatch(r"HX[0-9a-f]{32}", entry.content_sid), (
            f"{name} must carry a real Content SID post-F1"
        )
        assert entry.body_sha256 and re.fullmatch(r"[0-9a-f]{64}", entry.body_sha256), (
            f"{name} must pin body_sha256 post-F1"
        )
        assert entry.category in reg.TEMPLATE_CATEGORIES
    for name in ("team_winback_simple", "team_winback_offer"):
        entry = resolve(name, "en", _path=_REAL_YAML_PATH)
        assert entry.category == "customer_marketing"
        assert entry.optout_line is True, f"{name} must pin the STOP line"
    assert resolve("team_winback_offer", "en", _path=_REAL_YAML_PATH).money_bearing is True
    assert resolve("team_winback_simple", "en", _path=_REAL_YAML_PATH).money_bearing is False
    for name in ("team_agent_draft_approval", "team_l3_presend_notice", "team_autonomy_offer"):
        assert resolve(name, "en", _path=_REAL_YAML_PATH).category == "owner_notification"


def test_canary_load_passes_on_real_yaml() -> None:
    canary_load(_REAL_YAML_PATH)


def _write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "templates.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_canary_rejects_customer_marketing_without_optout_line(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        "bad_marketing:\n"
        "  audience: customer\n"
        "  category: customer_marketing\n"
        "  variables: [customer_name]\n"
        "  languages:\n    en: null\n",
    )
    with pytest.raises(TemplateRegistryError, match="optout_line"):
        canary_load(p)


def test_canary_rejects_unknown_category(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        "bad_category:\n"
        "  audience: customer\n"
        "  category: spam\n"
        "  variables: [customer_name]\n"
        "  languages:\n    en: null\n",
    )
    with pytest.raises(TemplateRegistryError, match="category"):
        canary_load(p)


def test_canary_rejects_non_bool_flags(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        "bad_flag:\n"
        "  audience: customer\n"
        "  money_bearing: 'yes'\n"
        "  variables: [customer_name]\n"
        "  languages:\n    en: null\n",
    )
    with pytest.raises(TemplateRegistryError, match="money_bearing"):
        canary_load(p)


# ---------------------------------------------------------------------------
# DB substrate (mirrors business_plan/test_generator.py)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so the tenant_connection pool exists."""
    import apply_migrations

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


# --- seeding helpers (direct service-role connection — RLS bypassed at seed) ---


def _new_tenant(dsn: str, *, name: str = "VT-369 customer_send test") -> UUID:
    # VT-421: the send choke point now has a Gate-0 ACTIVATION gate at the TOP of the stack
    # (is_agent_eligible). For these gate-1..6 tests to reach the gate they actually assert, the
    # tenant must be fully activated: journey-complete + gstin_verified + ≥1 enabled+ok connector
    # (the per-test _seed_customer satisfies the ≥1-customer leg). A non-activated tenant would
    # short-circuit on SKIP_NOT_ONBOARDED before any of these gates. The bar is now journey-complete
    # (onboarding_journey.status='complete'), NOT paid-active — so seed that row.
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, verification_status, whatsapp_number) "
            "VALUES (%s, 'founding', 'paid_active', now(), 'restaurant', 'gstin_verified', %s) "
            "RETURNING id",
            (name, f"+9198{uuid4().int % 10**8:08d}"),
        ).fetchone()
        assert row is not None
        tenant = UUID(str(row[0]))
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
        # VT-460 Gate-0b: agent_send_draft now also passes the universal WABA-live pre-gate
        # (wa_send_allowed). For the gate-1..6 tests to reach the gate they assert, seed a 'live'
        # WABA — a not-live tenant would short-circuit on SKIP_WABA_NOT_LIVE before those gates.
        conn.execute(
            "INSERT INTO tenant_whatsapp_accounts (tenant_id, status, phone_number) "
            "VALUES (%s, 'live', %s)",
            (str(tenant), f"+9180{uuid4().int % 10**8:08d}"),
        )
    return tenant


def _seed_customer(
    dsn: str,
    tenant: UUID,
    *,
    opt_out_status: str = "subscribed",
    complaint_status: str = "none",
    phone: str | None = None,
) -> tuple[UUID, str]:
    phone = phone or f"+9197{uuid4().int % 10**8:08d}"
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


def _seed_draft(
    dsn: str,
    tenant: UUID,
    batch: UUID,
    customer: UUID,
    *,
    template_name: str = _TEST_TEMPLATE,
    params: dict[str, Any] | None = None,
) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_drafts (tenant_id, batch_id, customer_id, template_name, params) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (
                str(tenant), str(batch), str(customer), template_name,
                Jsonb(params if params is not None else dict(_TEST_PARAMS)),
            ),
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


def _seed_contact(dsn: str, tenant: UUID, customer: UUID, *, days_ago: int = 0) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO agent_customer_contacts "
            "(tenant_id, customer_id, agent, template_name, sent_at) "
            "VALUES (%s, %s, 'sales_recovery', %s, now() - make_interval(days => %s))",
            (str(tenant), str(customer), _TEST_TEMPLATE, days_ago),
        )


def _draft_row(dsn: str, tenant: UUID, draft: UUID) -> tuple[str, str | None, str | None]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, skip_reason, message_sid FROM agent_drafts "
            "WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(draft)),
        ).fetchone()
    assert row is not None
    return str(row[0]), row[1], row[2]


def _batch_status(dsn: str, tenant: UUID, batch: UUID) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status FROM agent_draft_batches WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch)),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _work_item_status(dsn: str, tenant: UUID, work_item: UUID) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status FROM agent_work_items WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(work_item)),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _contact_count(dsn: str, tenant: UUID, draft: UUID) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM agent_customer_contacts WHERE tenant_id = %s AND draft_id = %s",
            (str(tenant), str(draft)),
        ).fetchone()
    assert row is not None
    return int(row[0])


# --- fakes -------------------------------------------------------------------


class _FakeSendFn:
    """Records every transport call; mimics twilio_send.send_template_message's
    SendResult contract. NEVER touches the network."""

    def __init__(self, *, success: bool = True):
        self.success = success
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(
        self, tenant_id: Any, template_name: str, params: dict[str, Any],
        *, recipient_phone: str | None = None,
    ) -> SimpleNamespace:
        # CL-390 hygiene even in a test double: never retain the raw phone.
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


@pytest.fixture()
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Real yaml + injected test entries (one fully-sendable customer_marketing
    entry with a SID, one wrong-category, one missing-optout-line). Patches the
    single load path (_get_cached) so BOTH customer_send's gate and the VT-45
    delegate resolve identically."""
    data = dict(reg._load_raw(_REAL_YAML_PATH))
    data[_TEST_TEMPLATE] = {
        "audience": "customer",
        "category": "customer_marketing",
        "optout_line": True,
        "variables": ["customer_name", "business_name"],
        "languages": {"en": _FAKE_SID},
    }
    data["team_owner_ops_itest"] = {
        "audience": "owner",
        "category": "owner_notification",
        "variables": ["customer_name", "business_name"],
        "languages": {"en": _FAKE_SID},
    }
    data["team_no_optout_itest"] = {
        "audience": "customer",
        "category": "customer_marketing",
        "optout_line": False,  # registry drift simulation — gate must fail closed
        "variables": ["customer_name", "business_name"],
        "languages": {"en": _FAKE_SID},
    }
    monkeypatch.setattr(reg, "_get_cached", lambda path=None: data)
    return data


@pytest.fixture()
def allow_test_consent_version(monkeypatch: pytest.MonkeyPatch) -> str:
    """Patch the C2 allowlist (EMPTY in prod until counsel rules) to a test
    version so the happy path can pass gate #4."""
    monkeypatch.setattr(
        customer_send, "_marketing_consent_versions", lambda: frozenset({"test-v1"})
    )
    return "test-v1"


def _send(
    tenant: UUID, draft: UUID, send_fn: Any, **kw: Any
) -> customer_send.AgentSendResult:
    with tenant_connection(tenant) as conn:
        return customer_send.agent_send_draft(tenant, draft, conn=conn, send_fn=send_fn, **kw)


def _stack(
    dsn: str, *, batch_status: str = "approved", template_name: str = _TEST_TEMPLATE,
    opt_out_status: str = "subscribed", complaint_status: str = "none",
) -> SimpleNamespace:
    tenant = _new_tenant(dsn)
    customer, phone = _seed_customer(
        dsn, tenant, opt_out_status=opt_out_status, complaint_status=complaint_status
    )
    work_item = _seed_work_item(dsn, tenant)
    batch = _seed_batch(dsn, tenant, work_item, status=batch_status)
    draft = _seed_draft(dsn, tenant, batch, customer, template_name=template_name)
    return SimpleNamespace(
        tenant=tenant, customer=customer, phone=phone,
        work_item=work_item, batch=batch, draft=draft,
    )


# ---------------------------------------------------------------------------
# Gate stack — each fails CLOSED with its distinct marker
# ---------------------------------------------------------------------------


@requires_db
def test_unapproved_batch_skips_without_poisoning_the_draft(substrate, fake_registry):  # type: ignore[no-untyped-def]
    s = _stack(substrate.dsn, batch_status="awaiting_approval")
    send_fn = _FakeSendFn()

    result = _send(s.tenant, s.draft, send_fn)

    assert result.status == "skipped"
    assert result.skip_reason == customer_send.SKIP_BATCH_NOT_APPROVED
    assert send_fn.calls == []
    # NOT persisted: the batch may legitimately be approved later.
    assert _draft_row(substrate.dsn, s.tenant, s.draft) == ("drafted", None, None)
    assert _batch_status(substrate.dsn, s.tenant, s.batch) == "awaiting_approval"


@requires_db
def test_l3_arm_is_wired_not_a_stub(substrate, fake_registry):  # type: ignore[no-untyped-def]
    """VT-384: the two PR-1 NotImplementedError stub arms are now the real L3 wire. The L3 arm no
    longer raises — it runs the full gate stack. With C2 empty (MARKETING_CONSENT_VERSIONS), the
    consent gate makes an L3 send on a fully-armed auto_send_pending batch ZERO sends end-to-end
    (skipped_consent), NOT a NotImplementedError. The unknown-level ValueError stays loud."""
    # (1) L3 on a non-auto_send_pending (approved) batch: Gate 1 rejects (L3 expects the hold
    # state) — skipped batch_not_approved, fail-closed, no send.
    s = _stack(substrate.dsn)
    send_fn = _FakeSendFn()
    r1 = _send(s.tenant, s.draft, send_fn, autonomy_level="L3")
    assert r1.status == "skipped"
    assert r1.skip_reason == customer_send.SKIP_BATCH_NOT_APPROVED
    assert send_fn.calls == []

    # (2) L3 on an auto_send_pending batch (the hold-wake path): runs the gate stack; the C2 empty
    # frozenset trips the consent gate → ZERO sends end-to-end (the centerpiece proof), NOT a raise.
    s2 = _stack(substrate.dsn, batch_status="auto_send_pending")
    send_fn2 = _FakeSendFn()
    r2 = _send(s2.tenant, s2.draft, send_fn2, autonomy_level="L3")
    assert r2.status == "skipped"
    assert r2.skip_reason == customer_send.SKIP_CONSENT  # C2 empty ⇒ no version resolves ⇒ no send
    assert send_fn2.calls == []

    # (3) L2 on an auto_send_pending batch: the explicit fail-LOUD guard — an L2 caller must never
    # send over an in-flight L3 hold. Skipped batch_not_approved, not persisted, no send.
    s3 = _stack(substrate.dsn, batch_status="auto_send_pending")
    send_fn3 = _FakeSendFn()
    r3 = _send(s3.tenant, s3.draft, send_fn3)  # default autonomy_level='L2'
    assert r3.status == "skipped"
    assert r3.skip_reason == customer_send.SKIP_BATCH_NOT_APPROVED
    assert send_fn3.calls == []

    # (4) The unknown-autonomy-level guard stays a loud ValueError.
    with pytest.raises(ValueError, match="autonomy_level"):
        _send(s.tenant, s.draft, _FakeSendFn(), autonomy_level="L9")


@requires_db
def test_missing_sid_fails_closed_template_not_configured(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """A SID-less registry entry must skip fail-closed. Pre-VT-383 the real yaml's
    Gap-5 stubs exercised this; post-F1 they are armed, so inject a synthetic
    SID-less entry (same variable signature as _TEST_PARAMS)."""
    data = dict(reg._load_raw(_REAL_YAML_PATH))
    data["team_sidless_itest"] = {
        "audience": "customer",
        "category": "customer_marketing",
        "optout_line": True,
        "variables": ["customer_name", "business_name"],
        "languages": {"en": None},  # the pre-F1 shape: declared language, no SID
    }
    monkeypatch.setattr(reg, "_get_cached", lambda path=None: data)
    s = _stack(substrate.dsn, template_name="team_sidless_itest")
    send_fn = _FakeSendFn()

    result = _send(s.tenant, s.draft, send_fn)

    assert result.status == "skipped"
    assert result.skip_reason == customer_send.SKIP_TEMPLATE_NOT_CONFIGURED
    assert send_fn.calls == []
    status, reason, _ = _draft_row(substrate.dsn, s.tenant, s.draft)
    assert (status, reason) == ("skipped", customer_send.SKIP_TEMPLATE_NOT_CONFIGURED)


@requires_db
def test_unknown_template_fails_closed(substrate, fake_registry):  # type: ignore[no-untyped-def]
    s = _stack(substrate.dsn, template_name="team_never_registered")
    result = _send(s.tenant, s.draft, _FakeSendFn())
    assert result.status == "skipped"
    assert result.skip_reason == customer_send.SKIP_TEMPLATE_NOT_CONFIGURED


@requires_db
def test_wrong_category_fails_closed(substrate, fake_registry):  # type: ignore[no-untyped-def]
    """A SID-bearing owner_notification template can structurally never reach a
    customer through the agent send path (plan gate #2)."""
    s = _stack(substrate.dsn, template_name="team_owner_ops_itest")
    send_fn = _FakeSendFn()

    result = _send(s.tenant, s.draft, send_fn)

    assert result.status == "skipped"
    assert result.skip_reason == customer_send.SKIP_WRONG_CATEGORY
    assert send_fn.calls == []


@requires_db
def test_legacy_uncategorised_template_fails_closed_as_wrong_category(substrate, fake_registry):  # type: ignore[no-untyped-def]
    """Pre-Gap-5 entries carry no category — the gate must refuse them too
    (category defaults to '' which is never 'customer_marketing')."""
    s = _stack(substrate.dsn, template_name="team_welcome")
    result = _send(s.tenant, s.draft, _FakeSendFn())
    assert result.status == "skipped"
    assert result.skip_reason == customer_send.SKIP_WRONG_CATEGORY


@requires_db
def test_missing_optout_line_fails_closed(substrate, fake_registry):  # type: ignore[no-untyped-def]
    s = _stack(substrate.dsn, template_name="team_no_optout_itest")
    result = _send(s.tenant, s.draft, _FakeSendFn())
    assert result.status == "skipped"
    assert result.skip_reason == customer_send.SKIP_NO_OPTOUT_LINE


@requires_db
def test_opted_out_customer_fails_closed(substrate, fake_registry, allow_test_consent_version):  # type: ignore[no-untyped-def]
    s = _stack(substrate.dsn, opt_out_status="opted_out")
    _seed_consent(substrate.dsn, s.tenant, s.phone, version="test-v1")
    send_fn = _FakeSendFn()

    result = _send(s.tenant, s.draft, send_fn)

    assert result.status == "skipped"
    assert result.skip_reason == customer_send.SKIP_OPT_OUT
    assert send_fn.calls == []
    status, reason, _ = _draft_row(substrate.dsn, s.tenant, s.draft)
    assert (status, reason) == ("skipped", customer_send.SKIP_OPT_OUT)


@requires_db
def test_open_complaint_fails_closed(substrate, fake_registry, allow_test_consent_version):  # type: ignore[no-untyped-def]
    s = _stack(substrate.dsn, complaint_status="open")
    _seed_consent(substrate.dsn, s.tenant, s.phone, version="test-v1")

    result = _send(s.tenant, s.draft, _FakeSendFn())

    assert result.status == "skipped"
    assert result.skip_reason == customer_send.SKIP_COMPLAINT


@requires_db
def test_no_marketing_consent_fails_closed_on_the_empty_allowlist(substrate, fake_registry):  # type: ignore[no-untyped-def]
    """THE C2 structural pin: a recorded consent whose version is not in
    MARKETING_CONSENT_VERSIONS (EMPTY until counsel rules) never sends. This
    is deliberate fail-closed behaviour, not a bug (plan §9 risk 5)."""
    s = _stack(substrate.dsn)
    _seed_consent(substrate.dsn, s.tenant, s.phone, version="not-allowlisted-v0")
    send_fn = _FakeSendFn()

    result = _send(s.tenant, s.draft, send_fn)

    assert result.status == "skipped"
    assert result.skip_reason == customer_send.SKIP_CONSENT
    assert send_fn.calls == []


@requires_db
def test_no_consent_row_at_all_fails_closed(substrate, fake_registry, allow_test_consent_version):  # type: ignore[no-untyped-def]
    s = _stack(substrate.dsn)  # no record_of_consent row seeded
    result = _send(s.tenant, s.draft, _FakeSendFn())
    assert result.status == "skipped"
    assert result.skip_reason == customer_send.SKIP_CONSENT


# ---------------------------------------------------------------------------
# has_marketing_consent_for_phone — version-aware wrapper semantics
# ---------------------------------------------------------------------------


@requires_db
def test_marketing_consent_is_version_aware_and_fail_closed(substrate):  # type: ignore[no-untyped-def]
    tenant = _new_tenant(substrate.dsn)
    _, phone = _seed_customer(substrate.dsn, tenant)
    _seed_consent(substrate.dsn, tenant, phone, version="qr-2026-01")

    with tenant_connection(tenant) as conn:
        ok = customer_send.has_marketing_consent_for_phone(
            tenant, phone, conn=conn, versions=frozenset({"qr-2026-01"})
        )
        wrong_version = customer_send.has_marketing_consent_for_phone(
            tenant, phone, conn=conn, versions=frozenset({"other-v9"})
        )
        empty_allowlist = customer_send.has_marketing_consent_for_phone(
            tenant, phone, conn=conn, versions=frozenset()
        )
    assert ok is True
    assert wrong_version is False
    assert empty_allowlist is False, "EMPTY allowlist must be structurally fail-closed"

    # Opt-out kills it even with the version allowlisted.
    from orchestrator.utils.phone_token import hash_phone

    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE record_of_consent SET opted_out_at = now() "
            "WHERE tenant_id = %s AND phone_token = %s",
            (str(tenant), hash_phone(phone)),
        )
    with tenant_connection(tenant) as conn:
        assert (
            customer_send.has_marketing_consent_for_phone(
                tenant, phone, conn=conn, versions=frozenset({"qr-2026-01"})
            )
            is False
        )


# ---------------------------------------------------------------------------
# check_agent_send_caps — daily / weekly / 30d suppression / 90d ceiling
# ---------------------------------------------------------------------------


@requires_db
def test_cap_tenant_daily(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    tenant = _new_tenant(substrate.dsn)
    other, _ = _seed_customer(substrate.dsn, tenant)
    target, _ = _seed_customer(substrate.dsn, tenant)
    _seed_contact(substrate.dsn, tenant, other, days_ago=0)
    monkeypatch.setattr(customer_send, "AGENT_SEND_DAILY_TENANT_CAP", 1)

    with tenant_connection(tenant) as conn:
        result = customer_send.check_agent_send_caps(tenant, target, conn=conn)
    assert result.allowed is False
    assert result.reason == customer_send.SKIP_CAP_TENANT_DAILY


@requires_db
def test_cap_customer_weekly(substrate):  # type: ignore[no-untyped-def]
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    _seed_contact(substrate.dsn, tenant, customer, days_ago=2)

    with tenant_connection(tenant) as conn:
        result = customer_send.check_agent_send_caps(tenant, customer, conn=conn)
    assert result.allowed is False
    assert result.reason == customer_send.SKIP_CAP_CUSTOMER_WEEKLY


@requires_db
def test_cap_30d_recontact_suppression(substrate):  # type: ignore[no-untyped-def]
    """A contact 10 days ago: outside the weekly window, inside the 30d
    suppression — the send-time re-check the plan added (§2.3)."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    _seed_contact(substrate.dsn, tenant, customer, days_ago=10)

    with tenant_connection(tenant) as conn:
        result = customer_send.check_agent_send_caps(tenant, customer, conn=conn)
    assert result.allowed is False
    assert result.reason == customer_send.SKIP_SUPPRESSION_30D


@requires_db
def test_cap_90d_ceiling(substrate):  # type: ignore[no-untyped-def]
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    _seed_contact(substrate.dsn, tenant, customer, days_ago=40)
    _seed_contact(substrate.dsn, tenant, customer, days_ago=70)

    with tenant_connection(tenant) as conn:
        result = customer_send.check_agent_send_caps(tenant, customer, conn=conn)
    assert result.allowed is False
    assert result.reason == customer_send.SKIP_CAP_90D


@requires_db
def test_clean_history_passes_caps(substrate):  # type: ignore[no-untyped-def]
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    _seed_contact(substrate.dsn, tenant, customer, days_ago=120)  # outside every window

    with tenant_connection(tenant) as conn:
        result = customer_send.check_agent_send_caps(tenant, customer, conn=conn)
    assert result == customer_send.CapCheckResult(allowed=True)


@requires_db
def test_cap_gate_is_wired_into_the_send_stack(substrate, fake_registry, allow_test_consent_version):  # type: ignore[no-untyped-def]
    """Full-stack: a 30d-suppressed customer skips with the marker PERSISTED."""
    s = _stack(substrate.dsn)
    _seed_consent(substrate.dsn, s.tenant, s.phone, version="test-v1")
    _seed_contact(substrate.dsn, s.tenant, s.customer, days_ago=10)
    send_fn = _FakeSendFn()

    result = _send(s.tenant, s.draft, send_fn)

    assert result.status == "skipped"
    assert result.skip_reason == customer_send.SKIP_SUPPRESSION_30D
    assert send_fn.calls == []
    status, reason, _ = _draft_row(substrate.dsn, s.tenant, s.draft)
    assert (status, reason) == ("skipped", customer_send.SKIP_SUPPRESSION_30D)


# ---------------------------------------------------------------------------
# Happy path + idempotency (the VT-45 delegate + 'agent:{draft_id}' ledger)
# ---------------------------------------------------------------------------


@requires_db
def test_happy_path_sends_once_and_writes_the_full_audit_chain(  # type: ignore[no-untyped-def]
    substrate, fake_registry, allow_test_consent_version,
):
    s = _stack(substrate.dsn)
    _seed_consent(substrate.dsn, s.tenant, s.phone, version="test-v1")
    send_fn = _FakeSendFn()

    result = _send(s.tenant, s.draft, send_fn)

    assert result.status == "sent", f"unexpected: {result}"
    assert result.message_sid and result.message_sid.startswith("SM")
    assert len(send_fn.calls) == 1
    _, sent_template, sent_params = send_fn.calls[0]
    assert sent_template == _TEST_TEMPLATE
    # Positional content variables built from the registry signature.
    assert sent_params == {"1": "Ravi", "2": "Test Cafe"}

    status, _, sid = _draft_row(substrate.dsn, s.tenant, s.draft)
    assert status == "sent" and sid == result.message_sid
    assert _contact_count(substrate.dsn, s.tenant, s.draft) == 1
    assert result.batch_status == "sent"
    assert _batch_status(substrate.dsn, s.tenant, s.batch) == "sent"
    assert _work_item_status(substrate.dsn, s.tenant, s.work_item) == "sent"

    # The send ledger carries the agent-namespaced idempotency key.
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT send_status FROM send_idempotency_keys "
            "WHERE tenant_id = %s AND idempotency_key = %s",
            (str(s.tenant), f"agent:{s.draft}"),
        ).fetchone()
    assert row is not None and row[0] == "sent"

    # Second invocation: terminal draft — no second transport call.
    result2 = _send(s.tenant, s.draft, send_fn)
    assert result2.status == "already_sent"
    assert result2.message_sid == result.message_sid
    assert len(send_fn.calls) == 1
    assert _contact_count(substrate.dsn, s.tenant, s.draft) == 1


@requires_db
def test_ledger_idempotency_survives_a_state_reset(  # type: ignore[no-untyped-def]
    substrate, fake_registry, allow_test_consent_version,
):
    """Crash-recovery shape: draft/batch/contact state wiped AFTER a successful
    send (as if we died before the bookkeeping writes). The re-run must NOT
    call the transport again — the 'agent:{draft_id}' row inside the VT-45
    send transaction is the authoritative dedupe — and must repair the state."""
    s = _stack(substrate.dsn)
    _seed_consent(substrate.dsn, s.tenant, s.phone, version="test-v1")
    send_fn = _FakeSendFn()

    first = _send(s.tenant, s.draft, send_fn)
    assert first.status == "sent"
    assert len(send_fn.calls) == 1

    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE agent_drafts SET status = 'drafted', message_sid = NULL "
            "WHERE tenant_id = %s AND id = %s",
            (str(s.tenant), str(s.draft)),
        )
        conn.execute(
            "UPDATE agent_draft_batches SET status = 'approved' "
            "WHERE tenant_id = %s AND id = %s",
            (str(s.tenant), str(s.batch)),
        )
        conn.execute(
            "DELETE FROM agent_customer_contacts WHERE tenant_id = %s AND draft_id = %s",
            (str(s.tenant), str(s.draft)),
        )

    second = _send(s.tenant, s.draft, send_fn)

    assert len(send_fn.calls) == 1, "the ledger row must prevent a second transport call"
    assert second.status == "sent"  # state repaired from the ledger echo
    assert second.message_sid == first.message_sid
    status, _, sid = _draft_row(substrate.dsn, s.tenant, s.draft)
    assert status == "sent" and sid == first.message_sid
    assert _contact_count(substrate.dsn, s.tenant, s.draft) == 1


@requires_db
def test_transport_failure_is_honest_and_not_terminal(  # type: ignore[no-untyped-def]
    substrate, fake_registry, allow_test_consent_version,
):
    s = _stack(substrate.dsn)
    _seed_consent(substrate.dsn, s.tenant, s.phone, version="test-v1")
    send_fn = _FakeSendFn(success=False)

    result = _send(s.tenant, s.draft, send_fn)

    assert result.status == "failed"
    assert result.skip_reason is not None and result.skip_reason.startswith("send_failed:")
    assert len(send_fn.calls) == 1
    status, _, sid = _draft_row(substrate.dsn, s.tenant, s.draft)
    assert status == "drafted" and sid is None, "a failed send must NOT mark the draft terminal"
    assert _contact_count(substrate.dsn, s.tenant, s.draft) == 0
    assert _batch_status(substrate.dsn, s.tenant, s.batch) == "sending"
    assert _work_item_status(substrate.dsn, s.tenant, s.work_item) == "approved"


@requires_db
def test_multi_draft_batch_goes_terminal_only_when_every_draft_is(  # type: ignore[no-untyped-def]
    substrate, fake_registry, allow_test_consent_version,
):
    tenant = _new_tenant(substrate.dsn)
    c1, p1 = _seed_customer(substrate.dsn, tenant)
    c2, p2 = _seed_customer(substrate.dsn, tenant, opt_out_status="opted_out")
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item)
    d1 = _seed_draft(substrate.dsn, tenant, batch, c1)
    d2 = _seed_draft(substrate.dsn, tenant, batch, c2)
    _seed_consent(substrate.dsn, tenant, p1, version="test-v1")
    send_fn = _FakeSendFn()

    r1 = _send(tenant, d1, send_fn)
    assert r1.status == "sent"
    assert r1.batch_status == "sending", "one open draft left — batch must NOT be terminal"
    assert _work_item_status(substrate.dsn, tenant, work_item) == "approved"

    r2 = _send(tenant, d2, send_fn)
    assert r2.status == "skipped"
    assert r2.skip_reason == customer_send.SKIP_OPT_OUT
    assert r2.batch_status == "sent", "all drafts terminal — batch completes"
    assert _batch_status(substrate.dsn, tenant, batch) == "sent"
    assert _work_item_status(substrate.dsn, tenant, work_item) == "sent"
    assert len(send_fn.calls) == 1, "the opted-out draft never reached the transport"
