"""VT-384 — the L3 autonomous-send wire (Act mode), C2-gated (real Postgres).

The deferred Gap-5 PR-3 wire: an L3-granted agent batch arms ``auto_send_pending`` +
a delivery-anchored hold; silence proceeds to the wake-side send (every gate re-evaluated
at send time); any owner inbound during the hold demotes to L2 (CAS, race-proof); the owner
kill keyword freezes + cancels in-flight holds same-txn. The defining acceptance is the
**C2 full-wire proof**: L3 granted + everything armed + the EMPTY
``MARKETING_CONSENT_VERSIONS`` frozenset ⇒ ZERO customer sends end-to-end. The wire is
proven AGAINST the stop, never opened (C2 stays empty until counsel — VT-384, CL-438).

Cowork ruling 20260612T140000Z conditions tested here:
  (C-a) hold/demote durations CONFIG-DRIVEN (config/l3_autonomy.yaml) — fail-closed to the
        safe defaults (hold_hours: 2, no_delivery_demote_minutes: 30);
  (C-b) the RULE-ORDER PIN — see test_vt384_pre_filter_rule_order.py (structural);
  (C-c) demote-to-awaiting_approval handles an already-open approval explicitly (queue,
        NEVER two open — the mig-128 backstop asserted);
  (C-d) a late-delivered callback after a demote is a no-op (the two-sided CAS).

HARNESS — house realdb conventions (mirrors test_run_control_realdb.py +
test_vt382_outbox_redaction_realdb.py): importorskip psycopg+dbos, skipif no DATABASE_URL,
migrations applied through the module-scoped ``substrate`` fixture via the UNGUARDED
``apply(dsn=...)`` path (the VT-379 lesson — NEVER assume the app_environment sentinel; a
fresh CI DB with no sentinel must pass), rows seeded through a direct service-role psycopg
connection, the code under test exercised through ``tenant_connection`` (the real RLS path).
Unique tenants/customers per test (uuid-suffixed) so a recycled DB never collides (CL-422
synthetic data only; CL-390 no PII in logs/asserts).

This suite targets the B1 module's ACTUAL public API (``orchestrator.agents.l3_hold`` —
``enter_l3_hold`` / ``stamp_delivery_anchor`` / ``demote_auto_send_pending`` /
``hold_hours`` / ``no_delivery_demote_minutes`` / ``l3_hold_workflow`` / ``start_l3_hold``)
+ the customer_send L3 arm (``agent_send_draft(autonomy_level='L3')`` + the
``SKIP_CAP_L3_DAILY`` / ``SKIP_SIGNATURE_MISMATCH`` markers + ``assert_winback_signature``).
``importorskip`` on the B1 module keeps collection fresh-DB-safe before it lands.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
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
    reason="DATABASE_URL not set — VT-384 L3-wire realdb suite skipped",
)

# The B1 wire module under test. Imported lazily-safe: if B1 has not landed the WHOLE suite
# skips (the integrator re-runs once B1 + mig-136 are in the tree) rather than erroring
# collection — fresh-DB-collection-safe (VT-379 spirit; same pattern as the VT-382 B2
# importorskip of outbox_redaction).
l3_hold = pytest.importorskip(
    "orchestrator.agents.l3_hold",
    reason="VT-384 B1 module (l3_hold) not yet in tree — integrator re-runs",
)

from orchestrator.agents import autonomy as autonomy_mod  # noqa: E402
from orchestrator.agents import customer_send  # noqa: E402
import orchestrator.templates_registry as reg  # noqa: E402
from orchestrator.db import tenant_connection  # noqa: E402

_AGENT = l3_hold.AGENT_NAME  # 'sales_recovery'
_FAKE_SID = "HX" + "0123456789abcdef" * 2  # matches ^HX[0-9a-f]{32}$
_TEST_TEMPLATE = "team_winback_vt384_itest"  # injected registry-only; never in the yaml

_WIRE_WORKER = Path(__file__).parent / "_vt384_hold_worker.py"


# ---------------------------------------------------------------------------
# Substrate — migrations (UNGUARDED, no sentinel assumption) + DBOS launch
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations through the unguarded ``apply(dsn=...)`` path (expected_env=None —
    never stamps the app_environment sentinel; the VT-379 fresh-DB lesson made structural)
    + launch DBOS so ``tenant_connection`` / the hold workflow exist. Registers the L3 hold
    workflow BEFORE launch (the house register-before-launch pattern)."""
    import apply_migrations

    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "local-test-salt-not-secret")
    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    # Register the L3 hold workflow into the DBOS registry before launch (so the in-process
    # legs that start it resolve; the subprocess worker registers in its own process).
    if hasattr(l3_hold, "register_l3_hold"):
        try:
            l3_hold.register_l3_hold()
        except Exception:  # noqa: BLE001 — already registered / launch-order tolerant
            pass
    launch_dbos()
    try:
        yield SimpleNamespace(dsn=dsn)
    finally:
        shutdown_dbos()


@pytest.fixture(autouse=True)
def _fresh_caches():
    reg._invalidate_cache()
    if hasattr(l3_hold, "_invalidate_config_cache"):
        l3_hold._invalidate_config_cache()
    yield
    reg._invalidate_cache()
    if hasattr(l3_hold, "_invalidate_config_cache"):
        l3_hold._invalidate_config_cache()


@pytest.fixture()
def armed_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Real yaml + one fully-sendable customer_marketing entry on the (customer_name,
    business_name) signature (the team_winback_simple canon). This is the 'everything armed'
    half of the C2 centerpiece: a template that WOULD send if the consent gate let it."""
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
# Recording transports — make EVERY would-be send observable end-to-end.
# Two shapes: the CUSTOMER send (send_whatsapp_template's send_fn:
# (tenant_id, template_name, params, *, recipient_phone)) and the owner NOTICE
# (l3_hold's notice send_fn: (tenant_id, params)). NEVER touch the network.
# ---------------------------------------------------------------------------


class _RecordingCustomerSend:
    """Records every customer-send transport call; mimics send_template_message's SendResult.
    ``calls`` is the audit the C2 proof asserts is EMPTY."""

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


class _RecordingNotice:
    """Records every owner presend-notice send; matches l3_hold's notice send_fn contract
    ``(tenant_id, params) -> SendResult-ish``."""

    def __init__(self, *, success: bool = True) -> None:
        self.success = success
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, tenant_id: Any, params: dict[str, Any]) -> SimpleNamespace:
        self.calls.append((str(tenant_id), dict(params)))
        return SimpleNamespace(
            success=self.success,
            message_sid=("SM" + uuid4().hex[:30]) if self.success else None,
            error_code=None if self.success else "21211",
            error_message=None if self.success else "simulated failure",
        )


# ---------------------------------------------------------------------------
# Seed helpers (direct service-role — RLS bypassed at seed only)
# ---------------------------------------------------------------------------


def _new_tenant(dsn: str) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, owner_inputs, whatsapp_number) "
            "VALUES (%s, 'founding', 'paid_active', now(), 'restaurant', true, %s) RETURNING id",
            (f"VT384 {uuid4().hex[:8]}", f"+9198{uuid4().int % 10**8:08d}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


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


def _seed_batch(
    dsn: str, tenant: UUID, work_item: UUID, *, status: str = "auto_send_pending",
    presend_notice_sid: str | None = None,
) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_draft_batches (tenant_id, work_item_id, agent, status, "
            "presend_notice_sid) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (str(tenant), str(work_item), _AGENT, status, presend_notice_sid),
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


def _grant_l3(dsn: str, tenant: UUID, *, approval_id: UUID | None = None) -> UUID:
    """Put the tenant_agent_autonomy row at L3 directly (service-role write — the granting
    flow is B2; here we need the GRANTED state to prove the wire fires under it)."""
    approval_id = approval_id or uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_agent_autonomy (tenant_id, agent, level, "
            "clean_approval_streak, l3_granted_at, l3_grant_approval_id) "
            "VALUES (%s, %s, 'L3', %s, now(), %s) "
            "ON CONFLICT (tenant_id, agent) DO UPDATE SET level='L3', "
            "clean_approval_streak=EXCLUDED.clean_approval_streak, l3_granted_at=now(), "
            "l3_grant_approval_id=EXCLUDED.l3_grant_approval_id, frozen=false",
            (str(tenant), _AGENT, autonomy_mod.L3_CLEAN_STREAK_THRESHOLD, str(approval_id)),
        )
    return approval_id


def _seed_sale_ledger(
    dsn: str, tenant: UUID, customer: UUID, *, days_ago: int = 120, amount_paise: int = 50000
) -> None:
    """One 'sale' customer_ledger_entries row so build_customer_fact_bundle resolves (detection
    guarantees a sale history in prod). Carries the NOT-NULL acquired_via/source_confidence/entry_key
    columns the executor's fact-bundle read needs."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO customer_ledger_entries (tenant_id, customer_id, amount_paise, entry_type, "
            "entry_date, acquired_via, source_confidence, entry_key) "
            "VALUES (%s, %s, %s, 'sale', now() - make_interval(days => %s), 'owner_typed', 1.0, %s)",
            (str(tenant), str(customer), amount_paise, days_ago, uuid4().hex),
        )


def _seed_consent(dsn: str, tenant: UUID, phone: str, *, version: str) -> None:
    from orchestrator.utils.phone_token import hash_phone

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO record_of_consent (tenant_id, phone_token, consent_text_version) "
            "VALUES (%s, %s, %s)",
            (str(tenant), hash_phone(phone), version),
        )


def _seed_open_approval(dsn: str, tenant: UUID, batch: UUID) -> UUID:
    """An OPEN agent_customer_send approval linked to the batch — the C-c collision
    substrate (a demote must not arm a SECOND open approval over this one)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        run = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, status) VALUES (%s, 'running') RETURNING id",
            (str(tenant),),
        ).fetchone()[0]
        row = conn.execute(
            "INSERT INTO pending_approvals (tenant_id, run_id, approval_type, summary, "
            "details, draft_batch_id, timeout_at) "
            "VALUES (%s, %s, 'agent_customer_send', %s, %s, %s, now() + interval '1 hour') "
            "RETURNING id",
            (str(tenant), str(run), f"Batch {batch} — approve to send?",
             Jsonb({"draft_batch_id": str(batch)}), str(batch)),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_dispatch_run(dsn: str, tenant: UUID, work_item: UUID) -> str:
    """Open the pipeline_runs row the dispatch workflow opens for a work item — at the SAME
    deterministic uuid5 id the coordinator derives (``_agent_run_id``). This is the run the demote
    arm + the re-arm leg resolve from the batch's work_item_id; without it the FK to pipeline_runs
    cannot be satisfied (the stranding gap's root). Mirrors coordinator._open_agent_run."""
    from orchestrator.agents.coordinator import _agent_run_id

    run_id = _agent_run_id(str(work_item))
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'agent_dispatch', 'running') ON CONFLICT (id) DO NOTHING",
            (run_id, str(tenant)),
        )
    return run_id


def _resolve_approval(dsn: str, tenant: UUID, approval_id: UUID) -> None:
    """Resolve an open approval (the trigger that frees the tenant's one-open slot for the re-arm)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE pending_approvals SET resolved_at = now(), decision = 'approved' "
            "WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(approval_id)),
        )


def _open_approval_for_batch(dsn: str, tenant: UUID, batch: UUID) -> int:
    """Count OPEN approvals referencing a specific batch (draft_batch_id) — the re-arm assertion
    that a queued batch ends up with its OWN open approval row (FK-satisfied)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM pending_approvals "
                "WHERE tenant_id = %s AND draft_batch_id = %s AND resolved_at IS NULL",
                (str(tenant), str(batch)),
            ).fetchone()[0]
        )


# --- readback helpers --------------------------------------------------------


def _batch_status(dsn: str, tenant: UUID, batch: UUID) -> str:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status FROM agent_draft_batches WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch)),
        ).fetchone()
    assert row is not None, "batch row vanished"
    return str(row[0])


def _draft_row(dsn: str, tenant: UUID, draft: UUID) -> tuple[str, str | None]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT status, skip_reason FROM agent_drafts WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(draft)),
        ).fetchone()
    assert row is not None
    return str(row[0]), row[1]


def _send_not_before(dsn: str, tenant: UUID, batch: UUID):  # type: ignore[no-untyped-def]
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "SELECT send_not_before FROM agent_draft_batches WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch)),
        ).fetchone()[0]


def _open_approval_count(dsn: str, tenant: UUID) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM pending_approvals "
                "WHERE tenant_id = %s AND resolved_at IS NULL",
                (str(tenant),),
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


def _now(dsn: str):  # type: ignore[no-untyped-def]
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute("SELECT now()").fetchone()[0]


def _defeat_always_confirm_floor(
    dsn: str, tenant: UUID, customer: UUID, *, template_name: str = _TEST_TEMPLATE
) -> None:
    """Seed a PRIOR agent_customer_contacts row for (tenant, customer, template) dated >30d
    ago so the is_always_confirm floor does NOT trip on first_contact / novel_template (the
    customer has been contacted before AND this tenant has sent this template before). Dated
    >30d so the 30d-recontact-suppression cap does not pre-empt a later real send. The
    money/bulk floor is defeated by using a non-money template + a single-customer batch."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO agent_customer_contacts (tenant_id, customer_id, agent, draft_id, "
            "batch_id, template_name, autonomy_level, message_sid, sent_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'L2', %s, now() - interval '60 days')",
            (str(tenant), str(customer), _AGENT, str(uuid4()), str(uuid4()),
             template_name, "SM" + uuid4().hex[:30]),
        )


def _sendable_l3_stack(dsn: str, *, with_consent_version: str | None = None) -> SimpleNamespace:
    """An L3-granted tenant with an ``auto_send_pending`` batch + one drafted customer —
    fully armed EXCEPT the C2 consent gate. The batch carries a presend_notice_sid + a
    DELIVERED anchor + a PAST send_not_before so the wake-side send path is reachable
    directly (the hold has 'elapsed'). With ``with_consent_version`` a matching
    record_of_consent row is seeded (the negative-control's 'open the gate' half)."""
    tenant = _new_tenant(dsn)
    customer, phone = _seed_customer(dsn, tenant)
    work_item = _seed_work_item(dsn, tenant)
    notice_sid = "SM" + uuid4().hex[:30]
    batch = _seed_batch(dsn, tenant, work_item, status="auto_send_pending",
                        presend_notice_sid=notice_sid)
    draft = _seed_draft(dsn, tenant, batch, customer)
    _grant_l3(dsn, tenant)
    # Stamp a DELIVERED anchor + a PAST window so the hold is "due" (wake-side is reachable).
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE agent_draft_batches SET presend_notice_delivered_at = now() - interval '3 hours', "
            "send_not_before = now() - interval '1 hour' WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch)),
        )
    if with_consent_version is not None:
        _seed_consent(dsn, tenant, phone, version=with_consent_version)
    return SimpleNamespace(
        tenant=tenant, customer=customer, phone=phone,
        work_item=work_item, batch=batch, draft=draft, notice_sid=notice_sid,
    )


def _send_l3_draft(tenant: UUID, draft: UUID, send_fn: Any) -> Any:
    """The wake-side per-draft send the hold leg makes: agent_send_draft(autonomy_level='L3')
    with an injected recording transport (the exact call l3_hold._hold_send_step_body makes,
    made observable)."""
    return customer_send.agent_send_draft(tenant, draft, autonomy_level="L3", send_fn=send_fn)


# ===========================================================================
# C2 CENTERPIECE — the row's defining test. L3 granted + everything armed +
# the EMPTY frozenset ⇒ ZERO customer sends end-to-end through the FULL wire.
# Negative control proves the test CAN see a send (so zero is meaningful).
# ===========================================================================


@pytest.mark.usefixtures("armed_registry")
def test_c2_empty_frozenset_yields_zero_sends_end_to_end(substrate):  # type: ignore[no-untyped-def]
    """THE defining acceptance. The wake-side send runs the per-draft
    ``agent_send_draft(autonomy_level='L3')`` with ALL gates re-evaluated.
    ``MARKETING_CONSENT_VERSIONS`` is EMPTY (the frozen C2 stop), so Gate 4 fail-closes for
    EVERY draft: the draft goes 'skipped' with SKIP_CONSENT, the transport is NEVER called,
    ZERO agent_customer_contacts rows. The wire is proven AGAINST the stop."""
    s = _sendable_l3_stack(substrate.dsn)  # NO consent row, empty frozenset (untouched)
    assert customer_send._marketing_consent_versions() == frozenset(), (
        "C2 must stay EMPTY — the wire is proven against the stop, not by opening it"
    )
    send_fn = _RecordingCustomerSend()

    result = _send_l3_draft(s.tenant, s.draft, send_fn)

    # ZERO sends: transport never called, no contact ledger rows, the draft fail-closed.
    assert send_fn.calls == [], (
        f"C2 STOP BREACHED — the wire produced {len(send_fn.calls)} customer send(s) "
        "with an EMPTY consent frozenset"
    )
    assert _customer_contacts(substrate.dsn, s.tenant) == 0
    status, skip = _draft_row(substrate.dsn, s.tenant, s.draft)
    assert status == "skipped"
    assert skip == customer_send.SKIP_CONSENT, f"expected consent skip, got {skip!r}"
    assert result.status == "skipped"


@pytest.mark.usefixtures("armed_registry")
def test_c2_full_wake_side_wire_leg_yields_zero_sends(substrate):  # type: ignore[no-untyped-def]
    """C2, the FULLEST-wire form: drive the ACTUAL hold-wake send leg
    (``l3_hold._hold_send_step_body``) — the checkpointed @DBOS.step the durable hold runs on
    silent expiry — end to end, default transport. With the EMPTY frozenset every draft
    fail-closes at the consent gate: the leg reports ``sent: 0``, the drafts are 'skipped'
    (skipped_consent), ZERO agent_customer_contacts. This is the wire-faithful proof (the
    per-draft test above is the observable-transport companion); together they pin both the
    leg's accounting AND the at-rest effect."""
    s = _sendable_l3_stack(substrate.dsn)  # NO consent row, empty frozenset
    assert customer_send._marketing_consent_versions() == frozenset()

    out = l3_hold._hold_send_step_body(str(s.tenant), str(s.batch))

    assert out.get("sent", 0) == 0, f"C2 STOP BREACHED — the wake-side leg sent {out}"
    assert _customer_contacts(substrate.dsn, s.tenant) == 0
    status, skip = _draft_row(substrate.dsn, s.tenant, s.draft)
    assert status == "skipped" and skip == customer_send.SKIP_CONSENT


@pytest.mark.usefixtures("armed_registry")
def test_c2_negative_control_nonempty_frozenset_DOES_send(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """NEGATIVE CONTROL (contract: prove the test CAN see sends). Monkeypatch the consent
    allowlist to a non-empty version AND seed a matching record_of_consent row — now the
    SAME wire DOES reach the transport for the SAME draft. This is what makes the zero-send
    assertion above non-vacuous: a wire that skips the consent gate would send here too, so
    the empty-set zero-send is a real property of the gate, not of a dead wire. We
    monkeypatch customer_send._marketing_consent_versions — NEVER MARKETING_CONSENT_VERSIONS
    itself; C2 stays an empty frozenset at rest."""
    s = _sendable_l3_stack(substrate.dsn, with_consent_version="vt384-control-v1")
    monkeypatch.setattr(
        customer_send, "_marketing_consent_versions",
        lambda: frozenset({"vt384-control-v1"}),
    )
    from orchestrator.agents import sales_recovery_executor as ex
    assert ex.MARKETING_CONSENT_VERSIONS == frozenset(), "C2 constant must stay empty"

    send_fn = _RecordingCustomerSend()
    result = _send_l3_draft(s.tenant, s.draft, send_fn)

    assert len(send_fn.calls) == 1, (
        "negative control: with the gate open the wire MUST reach the transport once — "
        "if this is 0 the C2 zero-send test is vacuous (the wire is dead, not gated)"
    )
    assert send_fn.calls[0][1] == _TEST_TEMPLATE
    assert result.status == "sent"
    assert _draft_row(substrate.dsn, s.tenant, s.draft)[0] == "sent"
    assert _customer_contacts(substrate.dsn, s.tenant) == 1


# ===========================================================================
# THE ARM + DELIVERY ANCHOR — enter_l3_hold flips auto_send_pending + sends the
# presend notice; the delivery callback stamps the anchor + sets send_not_before
# = delivered_at + hold_hours (config). C-d: a late callback post-demote no-ops.
# ===========================================================================


@pytest.mark.usefixtures("armed_registry")
def test_enter_l3_hold_arms_auto_send_pending_and_sends_notice(substrate):  # type: ignore[no-untyped-def]
    """The L3 arm: an L3-eligible drafted batch flips to ``auto_send_pending`` and the
    owner-facing ``team_l3_presend_notice`` is sent (registry-resolved), its SID recorded as
    the delivery anchor. is_always_confirm is checked BEFORE the arm (we seed a prior contact
    so the first_contact/novel floor does not trip — that floor is its own test below). The
    notice is owner-facing (NOT a customer send) — zero customer-contact rows beyond the seed."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="awaiting_approval")
    _seed_draft(substrate.dsn, tenant, batch, customer)
    _grant_l3(substrate.dsn, tenant)
    _defeat_always_confirm_floor(substrate.dsn, tenant, customer)

    notice_fn = _RecordingNotice()
    with tenant_connection(tenant) as conn:
        armed = l3_hold.enter_l3_hold(tenant, batch, conn=conn, send_fn=notice_fn)

    assert armed.armed is True, f"arm refused: {armed.reason}"
    assert _batch_status(substrate.dsn, tenant, batch) == "auto_send_pending"
    assert len(notice_fn.calls) == 1, "the presend notice must be sent to the owner"
    assert armed.presend_notice_sid is not None
    # The notice is OWNER-facing — it writes NO new agent_customer_contacts row (only the
    # one floor-defeat seed exists; the arm itself adds nothing).
    assert _customer_contacts(substrate.dsn, tenant) == 1
    # No send_not_before yet — it is DELIVERY-anchored (set by the callback, not the arm).
    assert _send_not_before(substrate.dsn, tenant, batch) is None


@pytest.mark.usefixtures("armed_registry")
def test_delivery_callback_stamps_anchor_and_sets_send_not_before(substrate):  # type: ignore[no-untyped-def]
    """F6 anchor: the presend notice's ``delivered`` status callback (matched by the recorded
    notice SID) stamps presend_notice_delivered_at + sets send_not_before = delivered_at +
    hold_hours (config). The clock starts at DELIVERY, not send."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="awaiting_approval")
    _seed_draft(substrate.dsn, tenant, batch, customer)
    _grant_l3(substrate.dsn, tenant)
    _defeat_always_confirm_floor(substrate.dsn, tenant, customer)

    notice_fn = _RecordingNotice()
    with tenant_connection(tenant) as conn:
        armed = l3_hold.enter_l3_hold(tenant, batch, conn=conn, send_fn=notice_fn)
    notice_sid = armed.presend_notice_sid
    assert _send_not_before(substrate.dsn, tenant, batch) is None

    with tenant_connection(tenant) as conn:
        stamped = l3_hold.stamp_delivery_anchor(tenant, notice_sid, conn=conn)
    assert stamped == str(batch), "the delivered callback must stamp the matching-SID anchor"

    snb = _send_not_before(substrate.dsn, tenant, batch)
    assert snb is not None, "delivery must set send_not_before = delivered_at + hold_hours"
    delta = (snb - _now(substrate.dsn)).total_seconds()
    assert delta > 0, "send_not_before must be in the FUTURE (the hold has not elapsed)"
    # Config-driven: ~hold_hours into the future (default 2h). Generous bound so the 2h
    # config passes and a stray 0/None does not.
    assert delta <= l3_hold.hold_hours() * 3600 + 120


@pytest.mark.usefixtures("armed_registry")
def test_late_delivered_callback_after_demote_is_noop(substrate):  # type: ignore[no-untyped-def]
    """C-d (explicit acceptance leg): a presend-notice ``delivered`` callback that arrives
    AFTER the batch has already been demoted is a NO-OP — it must NOT stamp the anchor or set
    send_not_before on a batch that is no longer auto_send_pending. The CAS is guarded on the
    batch still being auto_send_pending."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="awaiting_approval")
    _seed_draft(substrate.dsn, tenant, batch, customer)
    _grant_l3(substrate.dsn, tenant)
    _defeat_always_confirm_floor(substrate.dsn, tenant, customer)

    notice_fn = _RecordingNotice()
    with tenant_connection(tenant) as conn:
        armed = l3_hold.enter_l3_hold(tenant, batch, conn=conn, send_fn=notice_fn)
    notice_sid = armed.presend_notice_sid

    # Owner objects BEFORE the delivery callback → demote.
    with tenant_connection(tenant) as conn:
        l3_hold.demote_auto_send_pending(tenant, conn=conn, agent=_AGENT)
    assert _batch_status(substrate.dsn, tenant, batch) == "awaiting_approval"

    # The late delivery callback arrives — must be a NO-OP.
    with tenant_connection(tenant) as conn:
        stamped = l3_hold.stamp_delivery_anchor(tenant, notice_sid, conn=conn)
    assert stamped is None, "a late callback after demote must NOT stamp the anchor"
    assert _batch_status(substrate.dsn, tenant, batch) == "awaiting_approval"
    assert _send_not_before(substrate.dsn, tenant, batch) is None, (
        "the late callback must NOT set send_not_before on a demoted batch"
    )


# ===========================================================================
# THE HOLD — parks on the run-control poll idiom, survives a restart (the N2
# subprocess kill-recover harness from test_run_control_realdb). The hold parks
# on a FAR-future send_not_before so it never fires during the test.
# ===========================================================================


def test_hold_parks_survives_restart(substrate):  # type: ignore[no-untyped-def]
    """N2 kill-and-recover (the test_run_control_realdb webhook-pause pattern): the hold is a
    CHECKPOINTED durable wait (run-control poll idiom — DBOS.sleep loop). A worker enters the
    hold on an auto_send_pending batch whose send_not_before is FAR future; it is SIGKILLed
    mid-park; the workflow is observed PENDING; a second launch lets DBOS recovery re-enter
    the body and resume the hold. We assert the PARK SURVIVED the restart (durability), not
    the eventual send. The batch carries the C2 stop (empty frozenset) so the hold can never
    accidentally send even if the window elapsed."""
    from dbos import DBOSClient

    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="auto_send_pending",
                        presend_notice_sid="SM" + uuid4().hex[:30])
    _seed_draft(substrate.dsn, tenant, batch, customer)
    _grant_l3(substrate.dsn, tenant)
    # FAR-future delivered anchor + window so the hold parks (never fires during the test).
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE agent_draft_batches SET presend_notice_delivered_at = now(), "
            "send_not_before = now() + interval '24 hours' WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch)),
        )

    workflow_id = f"l3_hold_{batch}"  # the production workflow_id keying (start_l3_hold)
    argv = [sys.executable, str(_WIRE_WORKER), substrate.dsn, workflow_id, str(tenant), str(batch)]

    proc1 = subprocess.Popen(argv)
    try:
        assert _wait_pending(substrate.dsn, workflow_id, timeout=60), (
            "hold workflow never reached PENDING (the durable park)"
        )
    finally:
        proc1.kill()
        proc1.wait(timeout=15)

    pending = {str(w.workflow_id) for w in DBOSClient(substrate.dsn).list_workflows(status="PENDING")}
    assert workflow_id in pending, "the parked hold was not left PENDING by the crash"

    proc2 = subprocess.Popen(argv)  # DBOS recovery re-enters the held workflow
    try:
        time.sleep(8)
        pending2 = {
            str(w.workflow_id)
            for w in DBOSClient(substrate.dsn).list_workflows(status="PENDING")
        }
        assert workflow_id in pending2, "recovered hold did not resume the park — durability lost"
        # Never sent: still auto_send_pending, no contacts.
        assert _batch_status(substrate.dsn, tenant, batch) == "auto_send_pending"
        assert _customer_contacts(substrate.dsn, tenant) == 0
    finally:
        proc2.kill()
        proc2.wait(timeout=15)


def _wait_pending(dsn: str, workflow_id: str, timeout: float) -> bool:
    from dbos import DBOSClient

    deadline = time.time() + timeout
    while time.time() < deadline:
        ids = {str(w.workflow_id) for w in DBOSClient(dsn).list_workflows(status="PENDING")}
        if workflow_id in ids:
            return True
        time.sleep(1.0)
    return False


# ===========================================================================
# NO-DELIVERY DEMOTE — an undelivered notice = no informed silence ⇒ demote at
# the config window (C-a). Config-driven, NOT a module constant (VT-381 lesson).
# ===========================================================================


def test_no_delivery_demote_window_is_config_driven_default_30(substrate):  # type: ignore[no-untyped-def]
    """C-a structural: the demote window comes from config/l3_autonomy.yaml (the trial.yaml
    pattern, fail-closed), NOT a hard-coded module constant — a tuning change is a config
    edit, not a code change (the VT-381 TTL lesson). The safe default is 30 min, the hold
    2h. Both are read through the config functions, not inlined."""
    assert l3_hold.no_delivery_demote_minutes() == 30, "safe default demote window = 30 min"
    assert l3_hold.hold_hours() == 2, "safe default hold = 2h"


def test_config_loader_fails_closed_to_safe_defaults(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """C-a: the config loader is cached AND fail-closed — a missing/malformed config yields
    the SAFE defaults (hold_hours 2, no_delivery_demote_minutes 30), never a permissive zero
    (which would skip the hold). Point the loader at a non-existent path and assert the safe
    defaults, not a crash or a permissive value."""
    monkeypatch.setattr(l3_hold, "_CONFIG", Path("/nonexistent/vt384/l3_autonomy.yaml"))
    l3_hold._invalidate_config_cache()
    assert l3_hold.hold_hours() == 2, "missing config must fail CLOSED to the 2h default"
    assert l3_hold.no_delivery_demote_minutes() == 30, (
        "missing config must fail CLOSED to the 30-min default, never a permissive 0"
    )
    assert l3_hold.hold_hours() > 0 and l3_hold.no_delivery_demote_minutes() > 0


# ===========================================================================
# OWNER-INBOUND DEMOTE CAS — any owner non-STOP/DSR inbound during the hold
# demotes auto_send_pending → awaiting_approval (CAS). Race-tested BOTH orderings.
# ===========================================================================


def test_owner_inbound_demotes_auto_send_pending_cas(substrate):  # type: ignore[no-untyped-def]
    """The demote CAS (contract item 2): an owner inbound while a tenant has an
    auto_send_pending batch atomically CAS's it to awaiting_approval + records the regression.
    The batch re-enters the approval path (nothing lost — the ruling's demote-to-L2 choice).
    ZERO customer sends. The autonomy streak resets (owner engagement = 'eyes on this')."""
    s = _sendable_l3_stack(substrate.dsn)
    with tenant_connection(s.tenant) as conn:
        out = l3_hold.demote_auto_send_pending(s.tenant, conn=conn, agent=_AGENT)
    assert any(r.demoted for r in out), f"no batch demoted: {out}"
    assert _batch_status(substrate.dsn, s.tenant, s.batch) == "awaiting_approval"
    assert _customer_contacts(substrate.dsn, s.tenant) == 0
    assert autonomy_mod.get_autonomy(s.tenant, _AGENT).clean_approval_streak == 0


def test_demote_vs_window_expiry_race_no_send_over_objection(substrate):  # type: ignore[no-untyped-def]
    """The two-sided CAS race (BOTH orderings, barrier): the owner-inbound demote and the
    hold's wake-side send-attempt race the SAME batch row. Whichever wins the row lock, a
    window-expiry send can NEVER fire over an in-flight objection — either the send wins (and
    the demote no-ops) OR the demote wins (and the wake-side CAS re-check finds
    awaiting_approval and refuses). Invariant across every interleaving: if the batch ended
    awaiting_approval, ZERO sends; the consent stop (empty frozenset) means even a 'winning'
    send fail-closes — the race proves the CAS ordering, the consent stop proves no leak."""
    for _ in range(6):
        s = _sendable_l3_stack(substrate.dsn)  # empty frozenset (C2 stop active)
        send_fn = _RecordingCustomerSend()
        barrier = threading.Barrier(2)
        results: dict[str, Any] = {}

        def _demote() -> None:
            try:
                barrier.wait(timeout=10)
                with tenant_connection(s.tenant) as conn:
                    results["demote"] = l3_hold.demote_auto_send_pending(
                        s.tenant, conn=conn, agent=_AGENT
                    )
            except Exception as exc:  # noqa: BLE001 — surface in the assert
                results["demote"] = exc

        def _wake_send() -> None:
            try:
                barrier.wait(timeout=10)
                results["send"] = _send_l3_draft(s.tenant, s.draft, send_fn)
            except Exception as exc:  # noqa: BLE001
                results["send"] = exc

        threads = [threading.Thread(target=_demote), threading.Thread(target=_wake_send)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert set(results) == {"demote", "send"}, f"a racer never returned: {results}"
        assert not any(isinstance(v, Exception) for v in results.values()), results

        # C2 stop: a customer send NEVER fires, either ordering.
        assert send_fn.calls == [], "a customer send fired during the demote race (C2 breach)"
        assert _customer_contacts(substrate.dsn, s.tenant) == 0
        final = _batch_status(substrate.dsn, s.tenant, s.batch)
        assert final in ("awaiting_approval", "auto_send_pending", "sent"), (
            f"batch left in an inconsistent state after the race: {final!r}"
        )


# ===========================================================================
# DEMOTE COLLISION (C-c) — a demote when an open approval ALREADY exists QUEUES
# the batch (awaiting_approval WITHOUT arming a SECOND approval); NEVER two open.
# ===========================================================================


def test_demote_collision_open_approval_queues_never_two_open(substrate):  # type: ignore[no-untyped-def]
    """C-c: the demote target handles an already-open approval EXPLICITLY (queue, never two
    open). We seed an OPEN agent_customer_send approval for the tenant, then a SECOND
    auto_send_pending batch demotes. It moves to awaiting_approval (queued) but must NOT arm a
    second open approval — the open count stays exactly 1 (DemoteResult.queued True). The
    mig-128 backstop is asserted directly: a second open INSERT is rejected by the partial
    unique."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)

    # Batch 1 already has an OPEN approval (the collision substrate).
    batch1 = _seed_batch(substrate.dsn, tenant, work_item, status="awaiting_approval")
    _seed_draft(substrate.dsn, tenant, batch1, customer)
    _seed_open_approval(substrate.dsn, tenant, batch1)
    assert _open_approval_count(substrate.dsn, tenant) == 1

    # Batch 2 is auto_send_pending under L3; an owner inbound demotes it.
    batch2 = _seed_batch(substrate.dsn, tenant, work_item, status="auto_send_pending")
    _seed_draft(substrate.dsn, tenant, batch2, customer)
    _grant_l3(substrate.dsn, tenant)

    with tenant_connection(tenant) as conn:
        out = l3_hold.demote_auto_send_pending(tenant, conn=conn, agent=_AGENT)

    # Batch 2 queued (awaiting_approval) but NO second open approval armed.
    b2 = next((r for r in out if r.batch_id == str(batch2)), None)
    assert b2 is not None and b2.demoted is True
    assert b2.queued is True, "C-c: the collision demote must QUEUE (not arm a second approval)"
    assert _batch_status(substrate.dsn, tenant, batch2) == "awaiting_approval"
    assert _open_approval_count(substrate.dsn, tenant) == 1, (
        "C-c violated: a second open approval arose from the collision demote"
    )

    # mig-128 backstop: a direct attempt to open a SECOND approval is rejected by the partial
    # unique (proves the never-two-open guarantee is STRUCTURAL, not just logic).
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        run = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, status) VALUES (%s, 'running') RETURNING id",
            (str(tenant),),
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO pending_approvals (tenant_id, run_id, approval_type, summary, "
                "draft_batch_id, timeout_at) VALUES (%s, %s, 'agent_customer_send', %s, %s, "
                "now() + interval '1 hour')",
                (str(tenant), str(run), "second open — must be rejected", str(batch2)),
            )


# ===========================================================================
# KILL KEYWORD — record_regression_event(owner_keyword) freezes + cancels
# in-flight holds same-txn (the existing autonomy freeze path; it MUST cover
# auto_send_pending).
# ===========================================================================


def test_kill_keyword_freezes_and_cancels_in_flight_hold_same_txn(substrate):  # type: ignore[no-untyped-def]
    """Contract item 3: the kill keyword fires record_regression_event(owner_keyword) — the
    EXISTING freeze path cancels open batches + in-flight holds ATOMICALLY in-txn. We verify
    it covers an ``auto_send_pending`` batch (recon: _OPEN_BATCH_STATUSES includes it). After
    the freeze: the L3 batch is cancelled, its drafts halted, the agent is frozen + demoted to
    L2. ZERO sends — the kill is absolute."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="auto_send_pending")
    draft = _seed_draft(substrate.dsn, tenant, batch, customer)
    _grant_l3(substrate.dsn, tenant)

    with tenant_connection(tenant) as conn:
        autonomy_mod.record_regression_event(tenant, _AGENT, "owner_keyword", conn=conn)

    assert _batch_status(substrate.dsn, tenant, batch) == "cancelled", (
        "the kill keyword must cancel an in-flight auto_send_pending hold "
        "(_OPEN_BATCH_STATUSES must include it)"
    )
    assert _draft_row(substrate.dsn, tenant, draft)[0] == "halted"
    st = autonomy_mod.get_autonomy(tenant, _AGENT)
    assert st.frozen is True, "the kill keyword must FREEZE the agent"
    assert st.level == "L2", "an L3 agent that gets the kill keyword is revoked to L2"
    assert _customer_contacts(substrate.dsn, tenant) == 0


def test_open_batch_statuses_includes_auto_send_pending():  # type: ignore[no-untyped-def]
    """Structural backstop for the leg above: the freeze/cancel substrate's
    _OPEN_BATCH_STATUSES MUST include 'auto_send_pending', else a kill keyword would leave an
    L3 hold ticking (the binding rule: a kill switch never leaves armed batches ticking).
    Pinned here so a future edit to that tuple fails THIS test, not production."""
    assert "auto_send_pending" in autonomy_mod._OPEN_BATCH_STATUSES, (
        "auto_send_pending must be in _OPEN_BATCH_STATUSES — a kill must cancel L3 holds"
    )


# ===========================================================================
# F2 — DETERMINISTIC send-HOLDS-lock-FIRST ordering. The barrier race above
# covers demote-first nondeterministically; this leg pins the OTHER interleave
# deterministically: the wake-side send holds the batch FOR UPDATE lock, and a
# concurrent demote must SERIALIZE behind it (block until the lock releases).
# ===========================================================================


def test_demote_serializes_behind_send_held_batch_lock(substrate):  # type: ignore[no-untyped-def]
    """F2 (gate-bounce): the deterministic 'send acquires the row lock FIRST' interleave. We hold
    the SAME ``SELECT ... FOR UPDATE`` batch-row lock the wake-side send takes (customer_send L3
    gate-6) in a control transaction, then fire ``demote_auto_send_pending`` from another thread.
    The demote MUST block on that lock (it cannot flip the row out from under an in-flight send) —
    we assert it does NOT complete while the lock is held, then RELEASE the lock and assert the
    demote then completes. This proves both lock-acquisition orderings serialize on the row (the
    barrier race proves demote-first; this proves send-first), so a window-expiry send can never
    race a demote into a double-decision. No transport is ever touched (no send is performed here —
    we only hold the lock the send WOULD hold)."""
    s = _sendable_l3_stack(substrate.dsn)

    lock_acquired = threading.Event()
    release_lock = threading.Event()
    demote_done = threading.Event()
    demote_result: dict[str, Any] = {}

    def _hold_send_lock() -> None:
        # Mimic the wake-side send's gate-6 lock: take FOR UPDATE on the batch and HOLD it open
        # until released — the exact lock customer_send.agent_send_draft(L3) holds across its send.
        with tenant_connection(s.tenant) as conn, conn.transaction():
            conn.execute(
                "SELECT status FROM agent_draft_batches WHERE tenant_id = %s AND id = %s "
                "FOR UPDATE",
                (str(s.tenant), str(s.batch)),
            ).fetchone()
            lock_acquired.set()
            # Hold the lock until the main thread tells us to release (after proving the demote
            # blocked). Bounded so a bug can't hang the suite.
            release_lock.wait(timeout=15)

    def _attempt_demote() -> None:
        try:
            with tenant_connection(s.tenant) as conn:
                demote_result["out"] = l3_hold.demote_auto_send_pending(
                    s.tenant, conn=conn, agent=_AGENT
                )
        except Exception as exc:  # noqa: BLE001 — surface in the assert
            demote_result["out"] = exc
        finally:
            demote_done.set()

    holder = threading.Thread(target=_hold_send_lock)
    holder.start()
    assert lock_acquired.wait(timeout=10), "the send-side lock was never acquired"

    demoter = threading.Thread(target=_attempt_demote)
    demoter.start()

    # While the send holds the row lock the demote MUST block — it cannot complete. Give it a
    # generous window to (wrongly) finish; it must NOT.
    assert not demote_done.wait(timeout=2.0), (
        "F2 VIOLATION: the demote completed while the send held the batch FOR UPDATE lock — "
        "the two are NOT serializing on the row"
    )
    assert _batch_status(substrate.dsn, s.tenant, s.batch) == "auto_send_pending", (
        "the batch must stay auto_send_pending while the send holds the lock (no demote slipped in)"
    )

    # Release the send's lock — the demote now serializes through and completes.
    release_lock.set()
    holder.join(timeout=10)
    assert demote_done.wait(timeout=10), "the demote never completed after the lock released"
    demoter.join(timeout=10)

    out = demote_result["out"]
    assert not isinstance(out, Exception), f"the serialized demote errored: {out!r}"
    assert any(r.demoted for r in out), f"the demote should land once the lock frees: {out}"
    assert _batch_status(substrate.dsn, s.tenant, s.batch) == "awaiting_approval", (
        "after the lock releases the serialized demote must flip the batch to awaiting_approval"
    )


# ===========================================================================
# F1 — THE STRONG ARM: owner STOP/DSR during an armed hold FREEZES + cancels it
# (strictly stronger than the kill keyword) ⇒ ZERO send at expiry. The Hinglish
# "auto band karo" phrasing routes to opt_out AND still kills the hold.
# ===========================================================================


@pytest.mark.usefixtures("armed_registry")
def test_owner_stop_during_armed_hold_freezes_and_zero_send_at_expiry(substrate):  # type: ignore[no-untyped-def]
    """F1 PINNING TEST (gate-bounce blocker): an owner STOP while an L3 auto_send_pending hold is
    armed must FREEZE + cancel the hold (opt-out is strictly stronger than the kill keyword), so
    when the hold is driven to expiry NO customer send fires and the regression is recorded.

    Flow: armed L3 hold (delivered anchor, past send_not_before — the wake leg is reachable) →
    owner sends STOP → opt_out_handler runs the freeze leg → assert the batch is cancelled + the
    agent is frozen + the tenant opt_out flag set. THEN drive the hold's wake-side send leg to
    'expiry' and assert it sends ZERO (the batch is gone from auto_send_pending — no transport
    call), and that the autonomy regression (owner_keyword freeze) landed."""
    from orchestrator.direct_handlers import HANDLERS
    from orchestrator.state import new_subscriber_state
    from orchestrator.types import WebhookEvent

    s = _sendable_l3_stack(substrate.dsn)  # empty frozenset (C2) + armed, anchored, past-due hold
    # Pre-condition: the hold is armed and reachable (auto_send_pending, the wake leg would fire).
    assert _batch_status(substrate.dsn, s.tenant, s.batch) == "auto_send_pending"

    # The owner sends STOP — opt_out_handler now runs the FREEZE leg (F1).
    state = new_subscriber_state(s.tenant)
    event = WebhookEvent(body="STOP", sender_phone=s.phone, message_type="inbound_message")
    outcome = HANDLERS["opt_out_handler"](event, state)
    assert outcome["opt_out_set"] is True
    assert outcome["autonomy_frozen"] is True, "the opt-out must run the freeze leg (F1 strong arm)"

    # The armed hold is CANCELLED (the freeze cancels open batches incl. auto_send_pending) and the
    # agent is FROZEN + revoked to L2 — strictly stronger than a demote.
    assert _batch_status(substrate.dsn, s.tenant, s.batch) == "cancelled", (
        "the owner STOP must CANCEL the in-flight auto_send_pending hold (F1)"
    )
    assert _draft_row(substrate.dsn, s.tenant, s.draft)[0] == "halted"
    st = autonomy_mod.get_autonomy(s.tenant, _AGENT)
    assert st.frozen is True, "the owner STOP must FREEZE the agent (strictly stronger than kill)"
    assert st.level == "L2", "the freeze revokes an L3 agent to L2"
    # The opt_out flag is set on the tenant (the DPDP compliance leg — committed first).
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        opt_out = conn.execute(
            "SELECT opt_out FROM tenants WHERE id = %s", (str(s.tenant),)
        ).fetchone()[0]
    assert opt_out is True, "the opt-out flag must be set (compliance priority lands)"

    # NOW drive the hold's wake-side send leg to 'expiry' AFTER the STOP — it must send ZERO: the
    # batch left auto_send_pending (cancelled), so the wake leg's CAS re-check finds nothing.
    send_fn = _RecordingCustomerSend()
    # The wake leg re-confirms auto_send_pending; the cancelled batch raced out → no send attempted.
    out = l3_hold._hold_send_step_body(str(s.tenant), str(s.batch))
    assert out.get("sent", 0) == 0, f"a send fired at expiry AFTER the owner STOP (F1 breach): {out}"
    # Belt-and-braces: a direct wake-side per-draft send also sends ZERO (the draft is halted /
    # batch cancelled → gate-1 fail-closed), proving no transport over the objection.
    direct = _send_l3_draft(s.tenant, s.draft, send_fn)
    assert send_fn.calls == [], (
        f"the transport was called at expiry after the owner STOP (F1 breach): {send_fn.calls}"
    )
    assert direct.status in ("skipped", "already_sent") or direct.status != "sent", (
        f"a draft sent after the owner STOP froze the hold (F1 breach): {direct.status}"
    )
    assert _customer_contacts(substrate.dsn, s.tenant) == 0, "ZERO customer contacts after STOP"


def test_hinglish_auto_band_karo_routes_to_opt_out_and_kills_hold(substrate):  # type: ignore[no-untyped-def]
    """F1 phrasing leg: the NATURAL Hinglish "auto band karo" (the body the Meta-approved offer
    invites — "say STOP to turn this off") contains the opt-out keyword "band karo", so the
    pre_filter gate routes it to opt_out_handler (NOT the autonomy_kill branch), and that handler
    STILL kills the armed hold (the F1 freeze). This pins both halves: the routing (opt-out wins
    the tie, the RULE-ORDER discipline) AND the kill (the freeze cancels the auto_send_pending
    batch). Without F1 this body hit opt_out_handler — which did nothing to the hold."""
    from orchestrator.pre_filter_gate import matches_opt_out_or_dsr
    from orchestrator.state import new_subscriber_state
    from orchestrator.types import WebhookEvent
    from orchestrator.direct_handlers import HANDLERS
    from orchestrator import pre_filter_gate

    body = "auto band karo"
    # Routing: the Hinglish kill-intent body carries an opt-out keyword → opt-out path (authoritative).
    assert matches_opt_out_or_dsr(body), "'auto band karo' must match the opt-out keyword set"
    s = _sendable_l3_stack(substrate.dsn)
    state = new_subscriber_state(s.tenant)
    event = WebhookEvent(body=body, sender_phone=s.phone, message_type="inbound_message")
    route = pre_filter_gate.pre_filter(event, state)
    assert getattr(route, "handler_name", None) == "opt_out_handler", (
        f"'auto band karo' must route to opt_out_handler (authoritative-first), got {route!r}"
    )

    # The kill: opt_out_handler freezes + cancels the armed hold (F1).
    outcome = HANDLERS["opt_out_handler"](event, state)
    assert outcome["autonomy_frozen"] is True
    assert _batch_status(substrate.dsn, s.tenant, s.batch) == "cancelled", (
        "the Hinglish opt-out 'auto band karo' must STILL kill the auto_send_pending hold (F1)"
    )
    assert autonomy_mod.get_autonomy(s.tenant, _AGENT).frozen is True


# ===========================================================================
# 50/DAY CAP — L3_DAILY_AUTO_SEND_CAP enforcement (per-agent 24h L3 count +
# SKIP_CAP_L3_DAILY marker) in check_agent_send_caps, L3 path only.
# ===========================================================================


@pytest.mark.usefixtures("armed_registry")
def test_l3_daily_auto_send_cap_skip_marker(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """Contract item: the 50/day L3 auto-send cap (defined-not-enforced pre-PR-3) is now
    enforced per-agent over a 24h L3 count with the SKIP_CAP_L3_DAILY marker. We seed the
    agent at the cap (50 L3 contacts in the last 24h) and assert the next L3 auto-send skips
    with that marker — NOT the generic tenant-daily marker. Negative control open so the ONLY
    thing stopping the send is the L3 cap, not C2."""
    s = _sendable_l3_stack(substrate.dsn, with_consent_version="vt384-cap-v1")
    monkeypatch.setattr(
        customer_send, "_marketing_consent_versions", lambda: frozenset({"vt384-cap-v1"})
    )
    _seed_n_l3_contacts(substrate.dsn, s.tenant, n=customer_send.L3_DAILY_AUTO_SEND_CAP)

    send_fn = _RecordingCustomerSend()
    result = _send_l3_draft(s.tenant, s.draft, send_fn)

    assert send_fn.calls == [], "the 50/day L3 cap must block the auto-send"
    status, skip = _draft_row(substrate.dsn, s.tenant, s.draft)
    assert status == "skipped"
    assert skip == customer_send.SKIP_CAP_L3_DAILY, (
        f"expected the L3-daily-cap marker, got {skip!r}"
    )
    assert result.status == "skipped"


def _seed_n_l3_contacts(dsn: str, tenant: UUID, *, n: int) -> None:
    """Seed N L3 agent_customer_contacts rows in the last 24h for the (tenant, agent) — the
    L3-daily-cap counter substrate. Distinct synthetic customers so per-customer caps do not
    pre-empt the per-agent daily cap under test."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        for _ in range(n):
            cust = conn.execute(
                "INSERT INTO customers (tenant_id, display_name, phone_e164, opt_out_status) "
                "VALUES (%s, 'Capfill', %s, 'subscribed') RETURNING id",
                (str(tenant), f"+9196{uuid4().int % 10**8:08d}"),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO agent_customer_contacts (tenant_id, customer_id, agent, "
                "draft_id, batch_id, template_name, autonomy_level, message_sid, sent_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'L3', %s, now() - interval '1 hour')",
                (str(tenant), str(cust), _AGENT, str(uuid4()), str(uuid4()),
                 _TEST_TEMPLATE, "SM" + uuid4().hex[:30]),
            )


# ===========================================================================
# MONEY-BEARING FLOOR — winback_offer (money_bearing) NEVER reaches
# auto_send_pending: is_always_confirm trips BEFORE the arm (CL-438 non-bypassable).
# ===========================================================================


def test_money_bearing_winback_offer_never_reaches_auto_send_pending(substrate):  # type: ignore[no-untyped-def]
    """Contract item: a money-bearing template (team_winback_offer) trips the always-confirm
    floor BEFORE the L3 arm — it can NEVER flip to auto_send_pending, so it NEVER auto-sends
    (CL-438 non-bypassable). The arm is refused; the batch stays in its approval-bound state.
    The draft uses team_winback_offer (money_bearing in the real registry yaml — no
    monkeypatch needed)."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="awaiting_approval")
    _seed_draft(substrate.dsn, tenant, batch, customer, template_name="team_winback_offer")
    _grant_l3(substrate.dsn, tenant)

    with tenant_connection(tenant) as conn:
        armed = l3_hold.enter_l3_hold(tenant, batch, conn=conn, send_fn=_RecordingNotice())

    assert armed.armed is False, "a money-bearing batch must REFUSE the L3 arm (floor)"
    assert (armed.reason or "").startswith("always_confirm"), (
        f"expected an always-confirm refusal, got {armed.reason!r}"
    )
    assert _batch_status(substrate.dsn, tenant, batch) != "auto_send_pending", (
        "money-bearing winback_offer must NEVER reach auto_send_pending"
    )
    assert _customer_contacts(substrate.dsn, tenant) == 0


# ===========================================================================
# SIGNATURE CROSS-CHECK — registry variables vs the executor constant: the
# import-time assert + the per-send Gate-2b refuse on a MUTATED registry.
# ===========================================================================


def test_winback_signature_conforms_to_registry_canon():  # type: ignore[no-untyped-def]
    """Contract item 5 (the conformance half, registry-as-canon — ruling 1): the executor
    WINBACK_TEMPLATE_PARAMS now equals the armed registry's team_winback_simple variables
    (customer_name, business_name) — the old days_since_last_visit mismatch is CLOSED here.
    assert_winback_signature() passes against the real registry (the import-time lock)."""
    from orchestrator.agents import sales_recovery_executor as ex

    assert set(ex.WINBACK_TEMPLATE_PARAMS) == {"customer_name", "business_name"}, (
        "WINBACK_TEMPLATE_PARAMS must conform to the registry canon (customer_name, business_name)"
    )
    customer_send.assert_winback_signature()  # the import-time lock — must not raise on the real yaml


@pytest.mark.usefixtures("armed_registry")
def test_signature_cross_check_refuses_mutated_registry(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """Contract item 5 (the refuse half): the per-send Gate-2b cross-check hard-REFUSES a
    MUTATED registry whose variable signature drifts from the executor constant — fail-closed
    SKIP (SKIP_SIGNATURE_MISMATCH), never a signature-mismatched send. We register the
    injected template under the WINBACK_TEMPLATE_NAME with a DRIFTED variable list so the
    cross-check fires for it; the negative control is open so the ONLY block is the
    signature mismatch, not C2."""
    from orchestrator.agents import sales_recovery_executor as ex

    winback_name = ex.WINBACK_TEMPLATE_NAME

    s = _sendable_l3_stack(substrate.dsn, with_consent_version="vt384-sig-v1")
    monkeypatch.setattr(
        customer_send, "_marketing_consent_versions", lambda: frozenset({"vt384-sig-v1"})
    )
    # Point the draft at the winback template, and arm the registry for that name with a
    # DRIFTED variable signature (the OLD wrong shape) so Gate-2b cross-check refuses.
    yaml_path = Path(__file__).resolve().parents[2] / "config" / "twilio_templates.yaml"
    data = dict(reg._load_raw(yaml_path))
    data[winback_name] = {
        "audience": "customer", "category": "customer_marketing", "optout_line": True,
        "variables": ["customer_name", "days_since_last_visit"],  # drift from the executor canon
        "languages": {"en": _FAKE_SID},
    }
    monkeypatch.setattr(reg, "_get_cached", lambda path=None: data)

    # Re-point the draft at the mutated-signature template.
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE agent_drafts SET template_name = %s, params = %s "
            "WHERE tenant_id = %s AND id = %s",
            (winback_name, Jsonb({"customer_name": "Ravi", "business_name": "Cafe"}),
             str(s.tenant), str(s.draft)),
        )

    send_fn = _RecordingCustomerSend()
    result = _send_l3_draft(s.tenant, s.draft, send_fn)

    assert send_fn.calls == [], "a signature-mismatched template must NEVER reach the transport"
    status, skip = _draft_row(substrate.dsn, s.tenant, s.draft)
    assert status == "skipped"
    assert skip == customer_send.SKIP_SIGNATURE_MISMATCH, (
        f"expected the signature-mismatch marker, got {skip!r}"
    )
    assert result.status == "skipped"


# ===========================================================================
# ENABLE GRANT — grant_l3(approval_id) writes l3_grant_approval_id (the C3
# evidence row). The B2 ENABLE handler calls exactly this on a deterministic match.
# ===========================================================================


def test_enable_grant_writes_l3_grant_approval_id(substrate):  # type: ignore[no-untyped-def]
    """Contract item 4: a deterministic ENABLE match resolves the armed autonomy_upgrade
    approval + grant_l3(approval_id). The grant writes l3_grant_approval_id = the approval row
    id — that row IS the C3 consent evidence. We exercise grant_l3 from a clean 20-streak L2
    row (the grant's in-txn revalidation requires it) and assert the evidence id is persisted
    + the level is L3."""
    tenant = _new_tenant(substrate.dsn)
    approval_id = uuid4()
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_agent_autonomy (tenant_id, agent, level, clean_approval_streak) "
            "VALUES (%s, %s, 'L2', %s)",
            (str(tenant), _AGENT, autonomy_mod.L3_CLEAN_STREAK_THRESHOLD),
        )

    with tenant_connection(tenant) as conn:
        st = autonomy_mod.grant_l3(tenant, _AGENT, approval_id, conn=conn)
    assert st.level == "L3"
    assert st.l3_grant_approval_id == str(approval_id), (
        "the grant must persist the approval row id as the C3 consent evidence"
    )


# ===========================================================================
# SILENT-NOTICE COUNTER — each silent presend-notice expiry increments
# consecutive_silent_l3_notices (column exists; the wire bumps it).
# ===========================================================================


@pytest.mark.usefixtures("armed_registry")
def test_silent_notice_counter_increments_on_silent_proceed(substrate):  # type: ignore[no-untyped-def]
    """Contract item 4 (counter): a presend notice that elapses with NO owner reply (silent
    proceed to the wake-side send) increments consecutive_silent_l3_notices on the autonomy
    row — the owner-disengagement OBSERVABILITY substrate (VT-384 gate-bounce F4: the counter is
    KEPT as observability + a VT-385 design input; the auto-demote THRESHOLD path was dropped).
    The hold-wake send leg (``_hold_send_step_body``) bumps it. The C2 empty frozenset → still
    zero customer sends, but the silence is informed by the DELIVERED notice (independent of the
    consent gate), so the counter still bumps.

    F4 HARD ASSERT — the prior regression-masking pytest.skip legs are removed: the counter bump
    is wired into ``_hold_send_step_body`` (kept by F4), so this test pins the column behavior
    directly. A future edit that drops the bump fails HERE, not silently in production."""
    proceed = l3_hold._hold_send_step_body  # F4: directly-callable wake-side send leg (no skip)

    s = _sendable_l3_stack(substrate.dsn)
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        c0 = conn.execute(
            "SELECT consecutive_silent_l3_notices FROM tenant_agent_autonomy "
            "WHERE tenant_id = %s AND agent = %s",
            (str(s.tenant), _AGENT),
        ).fetchone()[0]

    proceed(str(s.tenant), str(s.batch))  # the silent-proceed wake leg

    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        c1 = conn.execute(
            "SELECT consecutive_silent_l3_notices FROM tenant_agent_autonomy "
            "WHERE tenant_id = %s AND agent = %s",
            (str(s.tenant), _AGENT),
        ).fetchone()[0]
    # F4 hard assert: a silent proceed MUST increment the counter (no skip escape hatch).
    assert c1 == c0 + 1, "a silent proceed must increment consecutive_silent_l3_notices by exactly 1"


# ===========================================================================
# HANDLER REGISTRATION (verifier BLOCKER) — pre_filter routes kill/enable to
# direct_handlers.HANDLERS; an unregistered name KeyErrors in runner dispatch.
# Pin the registration AND drive both through the dispatch path end-to-end.
# ===========================================================================


def test_pre_filter_handler_names_all_registered():  # type: ignore[no-untyped-def]
    """Structural registration pin (the BLOCKER guard): EVERY handler_name the pre_filter gate can
    emit MUST be a key in direct_handlers.HANDLERS, else runner.py's HANDLERS[name] dispatch raises
    KeyError on a live owner message. We scan the pre_filter source for every
    ``handler_name="..."`` literal and assert each is registered — so a future route added without
    a registration fails THIS test, not production."""
    import re as _re

    from orchestrator import pre_filter_gate
    from orchestrator.direct_handlers import HANDLERS

    src = Path(pre_filter_gate.__file__).read_text(encoding="utf-8")
    emitted = set(_re.findall(r'handler_name=["\']([a-z_]+)["\']', src))
    assert {"autonomy_kill_handler", "autonomy_enable_handler"} <= emitted, (
        "this test must see the VT-384 routes — the regex drifted"
    )
    missing = emitted - set(HANDLERS)
    assert not missing, f"pre_filter routes to UNREGISTERED handlers (KeyError in prod): {missing}"


def test_kill_keyword_through_runner_dispatch_freezes(substrate):  # type: ignore[no-untyped-def]
    """The BLOCKER end-to-end leg: a kill keyword driven THROUGH the pre_filter → HANDLERS dispatch
    path (the exact runner.py:webhook_pipeline_run call) freezes + cancels the in-flight L3 hold —
    no KeyError. We resolve the handler by the name pre_filter emits, then invoke it (the runner's
    ``HANDLERS[name](event, state)`` line), and assert the freeze actually fired."""
    from orchestrator.direct_handlers import HANDLERS
    from orchestrator.pre_filter_gate import pre_filter
    from orchestrator.state import new_subscriber_state
    from orchestrator.types import WebhookEvent

    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="auto_send_pending")
    _seed_draft(substrate.dsn, tenant, batch, customer)
    _grant_l3(substrate.dsn, tenant)

    event = WebhookEvent(
        body="please turn off automatic sending", sender_phone="+919812345678",
        message_type="inbound_message", twilio_message_sid="SM" + uuid4().hex[:30],
        dupe_status=False, num_media=0,
    )
    state = new_subscriber_state(tenant, uuid4())
    result = pre_filter(event, state)
    assert result.kind == "direct_handler"
    assert result.handler_name == "autonomy_kill_handler"
    # The runner's dispatch line — must NOT KeyError (the registration is the fix under test).
    out = HANDLERS[result.handler_name](event, state)
    assert out.get("autonomy_killed") is True

    assert _batch_status(substrate.dsn, tenant, batch) == "cancelled", (
        "the kill keyword via the runner dispatch path must cancel the in-flight L3 hold"
    )
    st = autonomy_mod.get_autonomy(tenant, _AGENT)
    assert st.frozen is True and st.level == "L2"
    assert _customer_contacts(substrate.dsn, tenant) == 0


def test_enable_keyword_through_runner_dispatch_grants_l3(substrate):  # type: ignore[no-untyped-def]
    """The BLOCKER end-to-end leg (grant side): an ENABLE reply driven THROUGH pre_filter → HANDLERS
    grants L3 — no KeyError. We arm an open autonomy_upgrade approval for a clean-streak L2 tenant,
    then drive 'ENABLE' through the dispatch path and assert the grant wrote l3_grant_approval_id."""
    from orchestrator.direct_handlers import HANDLERS
    from orchestrator.pre_filter_gate import pre_filter
    from orchestrator.state import new_subscriber_state
    from orchestrator.types import WebhookEvent

    tenant = _new_tenant(substrate.dsn)
    approval_id = uuid4()
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenant_agent_autonomy (tenant_id, agent, level, clean_approval_streak) "
            "VALUES (%s, %s, 'L2', %s)",
            (str(tenant), _AGENT, autonomy_mod.L3_CLEAN_STREAK_THRESHOLD),
        )
        run = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, status) VALUES (%s, 'running') RETURNING id",
            (str(tenant),),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO pending_approvals (tenant_id, run_id, approval_type, summary, details, "
            "timeout_at) VALUES (%s, %s, 'autonomy_upgrade', %s, %s, now() + interval '1 hour')",
            (str(tenant), str(run), "Enable automatic sending?",
             Jsonb({"agent": _AGENT, "approval_id_hint": str(approval_id)})),
        )

    event = WebhookEvent(
        body="ENABLE", sender_phone="+919812345678", message_type="inbound_message",
        twilio_message_sid="SM" + uuid4().hex[:30], dupe_status=False, num_media=0,
    )
    state = new_subscriber_state(tenant, uuid4())
    result = pre_filter(event, state)
    assert result.kind == "direct_handler"
    assert result.handler_name == "autonomy_enable_handler"
    out = HANDLERS[result.handler_name](event, state)  # the runner dispatch line — no KeyError
    assert out.get("l3_granted") is True, f"ENABLE via dispatch must grant L3: {out}"

    st = autonomy_mod.get_autonomy(tenant, _AGENT)
    assert st.level == "L3"
    assert st.l3_grant_approval_id is not None


# ===========================================================================
# L3 ARM WIRED INTO execute_item (verifier BLOCKER) — an L3-granted tenant's
# execute_item lands the batch in auto_send_pending (presend notice sent); an
# L2 tenant still L2-arms. Proven through the PRODUCTION execute_item path.
# ===========================================================================


def _make_execute_ctx(tenant: UUID, work_item: UUID):  # type: ignore[no-untyped-def]
    """A minimal AgentItemContext for execute_item (the coordinator builds the real one)."""
    from orchestrator.agents.coordinator import AgentItemContext

    return AgentItemContext(
        tenant_id=str(tenant),
        item_id=f"item-{uuid4().hex[:8]}",
        agent=_AGENT,
        work_item_id=str(work_item),
        run_id=str(uuid4()),
    )


@pytest.mark.usefixtures("armed_registry")
def test_execute_item_l3_grant_lands_auto_send_pending(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """The BLOCKER wire leg: a GRANTED-L3 tenant's execute_item routes the drafted batch into the
    hold (auto_send_pending) + sends the presend notice, INSTEAD of the L2 approval arm. We drive
    the real SalesRecoveryAgent.execute_item with a stub LLM (grounded params) + a stub presend
    notice transport, and assert the batch ends auto_send_pending with a recorded notice SID — the
    orphaned arm is connected."""
    from orchestrator.agents import l3_hold as l3
    from orchestrator.agents import sales_recovery_executor as ex

    tenant = _new_tenant(substrate.dsn)
    customer, phone = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    _grant_l3(substrate.dsn, tenant)
    _defeat_always_confirm_floor(substrate.dsn, tenant, customer, template_name=ex.WINBACK_TEMPLATE_NAME)
    # A sale ledger row so build_customer_fact_bundle resolves (detection guarantees it in prod).
    _seed_sale_ledger(substrate.dsn, tenant, customer)

    # Detection is structurally empty (C2) — inject the candidate + bundle directly so execute_item
    # reaches the draft+arm phase. Stub the LLM to echo the grounded params; stub the notice send.
    monkeypatch.setattr(
        ex, "detect_lapsed_customers",
        lambda tid, *, conn, limit: [
            ex.LapsedCandidate(customer_id=customer, days_since_last_sale=120,
                               last_sale_date=__import__("datetime").date(2025, 1, 1),
                               lifetime_spend_paise=50000)
        ],
    )
    notice_fn = _RecordingNotice()
    monkeypatch.setattr(l3, "_default_notice_sender", notice_fn)
    # Make start_l3_hold a no-op (the durable workflow start is exercised by the restart test) so
    # this leg isolates the ARM decision, not DBOS workflow plumbing.
    monkeypatch.setattr(l3, "start_l3_hold", lambda tid, bid: None)

    def _stub_llm(prompt: str, model: str) -> str:
        import json as _json
        biz = _json.loads(prompt.split("<allowed_params>")[1].split("</allowed_params>")[0])
        return _json.dumps(biz)

    agent = ex.SalesRecoveryAgent(llm=_stub_llm)
    ctx = _make_execute_ctx(tenant, work_item)
    result = agent.execute_item(ctx)

    assert result.batch_id is not None, f"no batch persisted: {result.counters}"
    assert result.counters.get("l3_armed") == 1, (
        f"the L3 arm did not fire through execute_item: {result.counters}"
    )
    assert _batch_status(substrate.dsn, tenant, UUID(result.batch_id)) == "auto_send_pending", (
        "an L3-granted tenant's execute_item must land the batch in auto_send_pending"
    )
    assert len(notice_fn.calls) == 1, "the presend notice must be sent on the L3 arm"


@pytest.mark.usefixtures("armed_registry")
def test_execute_item_l2_tenant_still_l2_arms(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """The BLOCKER wire leg (L2 unchanged): a NON-L3 (L2) tenant's execute_item still takes the L2
    approval arm — the batch is awaiting_approval, NOT auto_send_pending, and an approval is armed.
    Proves the L3 branch is conditional on the grant, not a blanket behavior change."""
    from orchestrator.agents import sales_recovery_executor as ex

    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    # No _grant_l3 → the tenant is L2 by default.
    _seed_sale_ledger(substrate.dsn, tenant, customer)

    monkeypatch.setattr(
        ex, "detect_lapsed_customers",
        lambda tid, *, conn, limit: [
            ex.LapsedCandidate(customer_id=customer, days_since_last_sale=120,
                               last_sale_date=__import__("datetime").date(2025, 1, 1),
                               lifetime_spend_paise=50000)
        ],
    )

    def _stub_llm(prompt: str, model: str) -> str:
        import json as _json
        biz = _json.loads(prompt.split("<allowed_params>")[1].split("</allowed_params>")[0])
        return _json.dumps(biz)

    armed_batches: list[str] = []

    def _stub_arm(tid: str, rid: str, bid: str, counts: dict) -> None:
        armed_batches.append(bid)

    agent = ex.SalesRecoveryAgent(llm=_stub_llm, arm_fn=_stub_arm)
    ctx = _make_execute_ctx(tenant, work_item)
    result = agent.execute_item(ctx)

    assert result.work_item_status == "awaiting_approval"
    assert result.counters.get("l3_armed") is None, "an L2 tenant must NOT take the L3 arm"
    assert result.batch_id is not None and result.batch_id in armed_batches, (
        "the L2 tenant must take the L2 approval arm"
    )
    assert _batch_status(substrate.dsn, tenant, UUID(result.batch_id)) == "awaiting_approval"


# ===========================================================================
# CAS WITH THE CONSENT GATE OPEN (verifier MAJOR) — the demote-vs-wake-send race
# with C2 OPENED (non-vacuous): a demote interleaving the gate1→send window must
# yield ZERO sends-after-demote. The send only fires if the demote lost the lock.
# ===========================================================================


@pytest.mark.usefixtures("armed_registry")
def test_demote_vs_wake_send_race_consent_open_no_send_after_demote(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """The MAJOR fix's behavioral test (NON-VACUOUS): open the consent gate (monkeypatch
    _marketing_consent_versions + seed a matching consent row) so the wake-side send WOULD fire,
    then barrier-race it against a demote on the SAME batch. The FOR UPDATE CAS serializes them:
    the invariant is no-send-AFTER-demote — if the batch ended awaiting_approval (the demote won the
    lock), ZERO customer contacts; if it ended sent (the send won the lock first), the demote
    no-oped. NEVER both a send AND a demote-won-final.

    Non-vacuity is proven SEPARATELY + deterministically (the NEGATIVE CONTROL leg at the end): with
    the SAME open gate and NO demote racing, the wake-side send DOES reach the transport once. So a
    zero-send under a winning demote is a real CAS property, not a dead wire — even though, in
    practice, the demote (which goes straight to FOR UPDATE) almost always wins the lock over the
    send (which runs gates 2-5 first)."""
    version = "vt384-cas-open-v1"
    monkeypatch.setattr(
        customer_send, "_marketing_consent_versions", lambda: frozenset({version})
    )
    demote_won_count = 0
    send_won_count = 0
    for _ in range(8):
        s = _sendable_l3_stack(substrate.dsn, with_consent_version=version)
        send_fn = _RecordingCustomerSend()
        barrier = threading.Barrier(2)
        results: dict[str, Any] = {}

        def _demote(_s=s, _results=results, _barrier=barrier) -> None:
            try:
                _barrier.wait(timeout=10)
                with tenant_connection(_s.tenant) as conn:
                    _results["demote"] = l3_hold.demote_auto_send_pending(
                        _s.tenant, conn=conn, agent=_AGENT
                    )
            except Exception as exc:  # noqa: BLE001
                _results["demote"] = exc

        def _wake_send(_s=s, _results=results, _barrier=barrier, _send_fn=send_fn) -> None:
            try:
                _barrier.wait(timeout=10)
                _results["send"] = _send_l3_draft(_s.tenant, _s.draft, _send_fn)
            except Exception as exc:  # noqa: BLE001
                _results["send"] = exc

        threads = [threading.Thread(target=_demote), threading.Thread(target=_wake_send)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert set(results) == {"demote", "send"}, f"a racer never returned: {results}"
        assert not any(isinstance(v, Exception) for v in results.values()), results

        final = _batch_status(substrate.dsn, s.tenant, s.batch)
        contacts = _customer_contacts(substrate.dsn, s.tenant)
        if final == "awaiting_approval":
            # The demote won the lock — there must be NO send over the objection.
            assert send_fn.calls == [], "a send fired AFTER the demote won the CAS (objection breach)"
            assert contacts == 0, "a contact row landed after the demote won — send-over-objection"
            demote_won_count += 1
        else:
            # The send won the lock first (batch sent); the demote no-oped (1 send, 1 contact).
            assert final == "sent", f"unexpected terminal state: {final!r}"
            assert len(send_fn.calls) == 1 and contacts == 1
            send_won_count += 1

    # At least one ordering must have exercised the demote-won branch (the safety property).
    assert demote_won_count >= 1, "no ordering exercised the demote-won-the-lock branch"

    # NON-VACUITY (deterministic): the SAME open gate, NO demote → the send DOES reach the transport.
    # This is what makes every demote-won zero-send above a real CAS property, not a dead wire.
    s2 = _sendable_l3_stack(substrate.dsn, with_consent_version=version)
    send_fn2 = _RecordingCustomerSend()
    result2 = _send_l3_draft(s2.tenant, s2.draft, send_fn2)
    assert result2.status == "sent" and len(send_fn2.calls) == 1, (
        "consent gate open + no demote: the wake-side send MUST reach the transport once "
        "(else the race test is vacuous — a dead wire)"
    )
    assert _batch_status(substrate.dsn, s2.tenant, s2.batch) == "sent"


# ===========================================================================
# NO-DELIVERY DEMOTE ANCHORED ON HOLD ENTRY (verifier MAJOR) — a batch with a
# backdated created_at that JUST entered the hold must NOT insta-demote; the
# window is measured from auto_send_pending_at (hold entry), not created_at.
# ===========================================================================


@pytest.mark.usefixtures("armed_registry")
def test_no_delivery_window_anchors_on_hold_entry_not_created_at(substrate):  # type: ignore[no-untyped-def]
    """The MAJOR fix's behavioral test: a batch CREATED long ago (created_at backdated well past
    the no-delivery window) that ENTERED the hold just now must NOT demote on its first poll — the
    window is anchored on auto_send_pending_at (hold entry), not created_at. We arm via enter_l3_hold
    (which stamps auto_send_pending_at = now()), backdate created_at far past the window, and assert
    _hold_state_body returns 'wait' (no delivery anchor yet, but the hold just began)."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="awaiting_approval")
    _seed_draft(substrate.dsn, tenant, batch, customer)
    _grant_l3(substrate.dsn, tenant)
    _defeat_always_confirm_floor(substrate.dsn, tenant, customer)

    with tenant_connection(tenant) as conn:
        armed = l3_hold.enter_l3_hold(tenant, batch, conn=conn, send_fn=_RecordingNotice())
    assert armed.armed is True, f"arm refused: {armed.reason}"

    # Backdate created_at far past the no-delivery window (the bug: would insta-demote off created_at).
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE agent_draft_batches SET created_at = now() - interval '10 hours' "
            "WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch)),
        )

    # No delivery anchor yet, hold JUST entered → the no-delivery window has NOT elapsed (anchored
    # on auto_send_pending_at = now()), so the state body must say 'wait', NOT 'demote'.
    decision = l3_hold._hold_state_body(str(tenant), str(batch))
    assert decision == "wait", (
        f"a just-armed batch with a stale created_at must NOT insta-demote (got {decision!r}) — "
        "the window must anchor on hold entry, not created_at"
    )

    # And the demote DOES fire once the window elapses from hold entry: backdate auto_send_pending_at.
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE agent_draft_batches SET auto_send_pending_at = now() - interval '40 minutes' "
            "WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch)),
        )
    assert l3_hold._hold_state_body(str(tenant), str(batch)) == "demote", (
        "once the no-delivery window elapses from hold entry, the batch must demote"
    )


# ===========================================================================
# DEMOTED-BATCH STRANDING FIX (VT-384 follow-up) — the demote arm must use the
# REAL dispatch run id (uuid5 of the batch's work_item_id), NOT a fresh uuid4,
# else pending_approvals.run_id FK-violates and the arm never succeeds → every
# demoted batch strands awaiting_approval with NO approval row. The C-c collision
# case is unstranded by a coordinator sweep re-arm leg after the open approval
# resolves. mig-128 one-open-per-tenant holds across the re-arm.
# ===========================================================================


def test_demote_arms_with_real_run_id_fk_satisfied(substrate):  # type: ignore[no-untyped-def]
    """The stranding fix (case a): a demote with NO open approval ARMS the L2 approval using the
    REAL run id derived from the batch's work_item_id (coordinator._agent_run_id uuid5) — the
    dispatch run row EXISTS, so pending_approvals.run_id FK is satisfied and the arm SUCCEEDS. Before
    the fix the arm passed a fresh uuid4 → FK violation → the arm silently failed and the batch
    stranded awaiting_approval with no approval row. We assert: DemoteResult.queued is False (armed),
    an OPEN pending_approvals row referencing THIS batch exists, and its run_id is the derived run."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="auto_send_pending")
    _seed_draft(substrate.dsn, tenant, batch, customer)
    _grant_l3(substrate.dsn, tenant)
    # The dispatch workflow opened this run for the work item (the FK target the arm must reuse).
    run_id = _seed_dispatch_run(substrate.dsn, tenant, work_item)

    with tenant_connection(tenant) as conn:
        out = l3_hold.demote_auto_send_pending(tenant, conn=conn, agent=_AGENT)

    target = next((r for r in out if r.batch_id == str(batch)), None)
    assert target is not None and target.demoted is True
    assert target.queued is False, (
        "with no open approval the demote MUST arm (not queue) — the real-run-id arm must succeed"
    )
    assert _batch_status(substrate.dsn, tenant, batch) == "awaiting_approval"
    # The approval row exists, references THIS batch, and FK-satisfies on the derived dispatch run.
    assert _open_approval_for_batch(substrate.dsn, tenant, batch) == 1, (
        "the demote arm must create exactly one OPEN approval for the batch (FK satisfied) — "
        "a uuid4 run_id would FK-violate and strand the batch"
    )
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        armed_run = conn.execute(
            "SELECT run_id::text FROM pending_approvals "
            "WHERE tenant_id = %s AND draft_batch_id = %s AND resolved_at IS NULL",
            (str(tenant), str(batch)),
        ).fetchone()[0]
    assert armed_run == run_id, (
        "the armed approval must reference the REAL dispatch run id (uuid5 of work_item_id), "
        f"got {armed_run!r} expected {run_id!r}"
    )
    assert _open_approval_count(substrate.dsn, tenant) == 1


def test_collision_queued_batch_rearmed_by_sweep_after_resolution(substrate):  # type: ignore[no-untyped-def]
    """The stranding fix (case b): a C-c collision demote QUEUES the batch (awaiting_approval, no
    second approval — mig-128). Before the fix nothing ever re-armed it → permanent strand. Now the
    coordinator sweep re-arm leg (l3_hold.rearm_stranded_batch, driven directly) arms it AFTER the
    blocking open approval resolves. We assert: while the other approval is open the re-arm is a
    NO-OP (one-open-per-tenant); after it resolves the re-arm arms the queued batch with the real
    run id (its own open approval row appears, FK satisfied)."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)

    # Batch 1 holds the tenant's single open approval slot (the collision substrate).
    batch1 = _seed_batch(substrate.dsn, tenant, work_item, status="awaiting_approval")
    _seed_draft(substrate.dsn, tenant, batch1, customer)
    blocking_approval = _seed_open_approval(substrate.dsn, tenant, batch1)

    # Batch 2 is auto_send_pending under L3; the dispatch run exists (the FK target).
    work_item2 = _seed_work_item(substrate.dsn, tenant)
    batch2 = _seed_batch(substrate.dsn, tenant, work_item2, status="auto_send_pending")
    _seed_draft(substrate.dsn, tenant, batch2, customer)
    _grant_l3(substrate.dsn, tenant)
    _seed_dispatch_run(substrate.dsn, tenant, work_item2)

    # An owner inbound demotes batch2 — it QUEUES (open approval exists), no second approval armed.
    with tenant_connection(tenant) as conn:
        out = l3_hold.demote_auto_send_pending(tenant, conn=conn, agent=_AGENT, batch_id=batch2)
    b2 = next((r for r in out if r.batch_id == str(batch2)), None)
    assert b2 is not None and b2.demoted is True and b2.queued is True
    assert _batch_status(substrate.dsn, tenant, batch2) == "awaiting_approval"
    assert _open_approval_for_batch(substrate.dsn, tenant, batch2) == 0, "queued: no approval yet"
    assert _open_approval_count(substrate.dsn, tenant) == 1

    # The sweep re-arm leg is a NO-OP while the blocking approval is still open (one-open-per-tenant).
    with tenant_connection(tenant) as conn:
        rearmed = l3_hold.rearm_stranded_batch(tenant, conn=conn, agent=_AGENT)
    assert rearmed is None, "the re-arm must NO-OP while an approval is already open (one-open slot)"
    assert _open_approval_count(substrate.dsn, tenant) == 1

    # The blocking approval resolves → batch1 advances OUT of awaiting_approval (apply_agent_decision
    # moves a resolved batch to approved/rejected — it is no longer strandable). The only remaining
    # awaiting_approval-with-no-open-approval batch is the queued batch2 the demote left behind.
    _resolve_approval(substrate.dsn, tenant, blocking_approval)
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE agent_draft_batches SET status = 'approved', updated_at = now() "
            "WHERE tenant_id = %s AND id = %s",
            (str(tenant), str(batch1)),
        )

    # The next sweep pass re-arms the queued batch.
    with tenant_connection(tenant) as conn:
        rearmed = l3_hold.rearm_stranded_batch(tenant, conn=conn, agent=_AGENT)
    assert rearmed == str(batch2), (
        "after the open approval resolves the sweep MUST re-arm the queued batch — "
        "else it strands awaiting_approval forever (the gap this fixes)"
    )
    assert _open_approval_for_batch(substrate.dsn, tenant, batch2) == 1, (
        "the re-armed batch must now have its OWN open approval (FK satisfied via the real run id)"
    )
    assert _open_approval_count(substrate.dsn, tenant) == 1


def test_rearm_never_opens_two_approvals_mig128_backstop(substrate):  # type: ignore[no-untyped-def]
    """The stranding fix (case c): the never-two-open invariant holds ACROSS the re-arm. After the
    sweep re-arms a queued batch (one open approval now), a SECOND re-arm pass MUST NO-OP (the slot
    is filled), and a direct attempt to open a second open approval is rejected by the mig-128 partial
    unique. Together: the re-arm can never produce two open approvals for a tenant."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)

    # Two queued (stranded) batches, no open approval at all (both awaiting, neither armed).
    wi1 = _seed_work_item(substrate.dsn, tenant)
    batch1 = _seed_batch(substrate.dsn, tenant, wi1, status="awaiting_approval")
    _seed_draft(substrate.dsn, tenant, batch1, customer)
    _seed_dispatch_run(substrate.dsn, tenant, wi1)
    wi2 = _seed_work_item(substrate.dsn, tenant)
    batch2 = _seed_batch(substrate.dsn, tenant, wi2, status="awaiting_approval")
    _seed_draft(substrate.dsn, tenant, batch2, customer)
    _seed_dispatch_run(substrate.dsn, tenant, wi2)
    _grant_l3(substrate.dsn, tenant)
    assert _open_approval_count(substrate.dsn, tenant) == 0

    # First re-arm arms exactly ONE (the oldest) — at most one per sweep per tenant.
    with tenant_connection(tenant) as conn:
        first = l3_hold.rearm_stranded_batch(tenant, conn=conn, agent=_AGENT)
    assert first is not None, "the re-arm must arm one stranded batch when none is open"
    assert _open_approval_count(substrate.dsn, tenant) == 1, "exactly one open approval after re-arm"

    # A SECOND re-arm pass MUST NO-OP — the tenant's single open slot is now filled.
    with tenant_connection(tenant) as conn:
        second = l3_hold.rearm_stranded_batch(tenant, conn=conn, agent=_AGENT)
    assert second is None, "a second re-arm must NO-OP while one approval is open (never two open)"
    assert _open_approval_count(substrate.dsn, tenant) == 1, "still exactly one open approval"

    # mig-128 STRUCTURAL backstop: a direct second open INSERT is rejected by the partial unique —
    # the one-open-per-tenant guarantee is structural, not merely logic in the re-arm path.
    other_batch = batch2 if first == str(batch1) else batch1
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        run = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, status) VALUES (%s, 'running') RETURNING id",
            (str(tenant),),
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO pending_approvals (tenant_id, run_id, approval_type, summary, "
                "draft_batch_id, timeout_at) VALUES (%s, %s, 'agent_customer_send', %s, %s, "
                "now() + interval '1 hour')",
                (str(tenant), str(run), "second open — must be rejected", str(other_batch)),
            )
