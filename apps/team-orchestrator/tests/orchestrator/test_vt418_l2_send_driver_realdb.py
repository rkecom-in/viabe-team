"""VT-418 — the L2 owner-approve→send DRIVER (real Postgres).

The connector-audit dead end this row fixes: approval flips a batch to ``'approved'`` but NOTHING
selected the approved batch and called ``agent_send_draft`` — the batch just SAT. VT-418 is the
missing driver (the L2 sibling of ``l3_hold``): ``l2_send_workflow`` (durable, keyed
``l2_send_{batch_id}``, exactly-once start) selects the batch's ``drafted`` drafts under an
``approved`` batch → ``agent_send_draft(autonomy_level='L2')`` per draft → the EXISTING gate stack
→ a real ``team_winback_simple`` send. A reconciler sweep heals the crash-between-commit-and-start
residual.

The LOAD-BEARING acceptance (money-send): the driver selects + invokes EXACTLY ONCE; re-running the
driver / re-firing the workflow → NO second send. The proof is at the LEDGER layer — the existing
``agent:{draft_id}`` dedup in ``send_idempotency_keys`` (``'sent'`` permanent hit, ``'error'``
excluded — VT-387/410). A send-transport SPY makes every would-be send observable; the assertion is
that across N invocations the transport is reached AT MOST ONCE per draft.

C2 floor (plan §4): with ``MARKETING_CONSENT_VERSIONS`` the EMPTY frozenset, Gate 4 fail-closes →
ZERO sends end-to-end even on a fully-approved L2 batch. The no-double-send proof opens the gate the
SAME way the VT-384 negative control does — monkeypatch ``customer_send._marketing_consent_versions``
(NEVER ``MARKETING_CONSENT_VERSIONS`` itself; C2 stays empty at rest) + seed a matching
``record_of_consent`` — so a send CAN happen, making "exactly once" a real property of the
idempotency ledger, not of a dead/gated wire. NO real Twilio anywhere: ``send_fn`` is injected.

HARNESS — house realdb conventions (mirrors test_vt384_l3_wire_realdb.py): importorskip psycopg+dbos,
skipif no DATABASE_URL, migrations applied through the module-scoped substrate via the UNGUARDED
``apply(dsn=...)`` path, rows seeded through a direct service-role psycopg connection, the code under
test exercised through ``tenant_connection`` (the real RLS path). Unique tenants/customers per test
(uuid-suffixed) so a recycled DB never collides (CL-422 synthetic only; CL-390 no PII).
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
    reason="DATABASE_URL not set — VT-418 L2-send-driver realdb suite skipped",
)

# The driver module under test. importorskip keeps collection fresh-DB-safe before it lands.
l2_send = pytest.importorskip(
    "orchestrator.agents.l2_send",
    reason="VT-418 driver module (l2_send) not yet in tree — integrator re-runs",
)

from orchestrator.agents import customer_send  # noqa: E402
import orchestrator.templates_registry as reg  # noqa: E402

_AGENT = "sales_recovery"
_FAKE_SID = "HX" + "0123456789abcdef" * 2  # matches ^HX[0-9a-f]{32}$
_TEST_TEMPLATE = "team_winback_vt418_itest"  # injected registry-only; never in the yaml


# ---------------------------------------------------------------------------
# Substrate — migrations (UNGUARDED) + DBOS launch + register the workflow.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations through the unguarded ``apply(dsn=...)`` path (expected_env=None) + launch
    DBOS so ``tenant_connection`` exists. Registers the L2 send workflow BEFORE launch (the house
    register-before-launch pattern) so a direct ``start_l2_send`` resolves."""
    import apply_migrations

    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "local-test-salt-not-secret")
    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    try:
        l2_send.register_l2_send()
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
    yield
    reg._invalidate_cache()


@pytest.fixture()
def armed_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Real yaml + one fully-sendable customer_marketing entry on the (customer_name,
    business_name) signature (the team_winback_simple canon) — a template that WOULD send if the
    consent gate let it."""
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
    ``calls`` is the audit the exactly-once proof asserts on."""

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


class _RaisingCustomerSend:
    """A transport that RAISES on the first call (a 5xx-equivalent → twilio re-raises) then
    succeeds — the transient-retry substrate (VT-410: 'error' is NOT a ledger hit, so the next
    run re-sends, exactly once on delivery)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self, tenant_id: Any, template_name: str, params: dict[str, Any],
        *, recipient_phone: str | None = None,
    ) -> SimpleNamespace:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated transient 5xx")
        return SimpleNamespace(
            success=True, message_sid="SM" + uuid4().hex[:30],
            error_code=None, error_message=None,
        )


# ---------------------------------------------------------------------------
# Seed helpers (direct service-role — RLS bypassed at seed only).
# ---------------------------------------------------------------------------


def _new_tenant(dsn: str, *, ownership_verified: bool = True) -> UUID:
    # VT-421: agent_send_draft now has a Gate-0 ACTIVATION gate. This driver's batches must reach the
    # send, so the tenant is fully activated: journey-complete + gstin_verified + ≥1 enabled+ok
    # connector (the per-test _seed_customer satisfies the ≥1-customer leg). The bar is now
    # journey-complete (onboarding_journey.status='complete'), NOT paid-active — so seed that row.
    # VT-517: ownership_verified (renamed from owner_channel_verified) is now required by the
    # universal Gate-0 for sales_recovery; default True so all eligible-path seeds clear the gate.
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, owner_inputs, verification_status, whatsapp_number, ownership_verified) "
            "VALUES (%s, 'founding', 'paid_active', now(), 'restaurant', true, 'gstin_verified', %s, %s) "
            "RETURNING id",
            (f"VT418 {uuid4().hex[:8]}", f"+9198{uuid4().int % 10**8:08d}", ownership_verified),
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
        # VT-460 Gate-0b: agent_send_draft now also passes the universal WABA-live pre-gate. Seed a
        # 'live' WABA so the driver's batches reach the send (a not-live tenant short-circuits on
        # SKIP_WABA_NOT_LIVE before the gate stack).
        conn.execute(
            "INSERT INTO tenant_whatsapp_accounts (tenant_id, status, phone_number) "
            "VALUES (%s, 'live', %s)",
            (str(tenant), f"+9180{uuid4().int % 10**8:08d}"),
        )
    return tenant


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


def _age_batch(dsn: str, tenant: UUID, batch: UUID, *, minutes: int) -> None:
    """Backdate the batch's updated_at so the reconciler's staleness grace selects it."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE agent_draft_batches SET updated_at = now() - make_interval(mins => %s) "
            "WHERE tenant_id = %s AND id = %s",
            (minutes, str(tenant), str(batch)),
        )


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


def _customer_contacts(dsn: str, tenant: UUID) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM agent_customer_contacts WHERE tenant_id = %s",
                (str(tenant),),
            ).fetchone()[0]
        )


def _idempotency_rows(dsn: str, tenant: UUID, draft: UUID) -> list[tuple[str, str | None]]:
    """(send_status, message_sid) rows for the draft's agent:{draft_id} key."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT send_status, message_sid FROM send_idempotency_keys "
            "WHERE tenant_id = %s AND idempotency_key = %s",
            (str(tenant), f"agent:{draft}"),
        ).fetchall()
    return [(str(r[0]), r[1]) for r in rows]


def _approved_l2_stack(dsn: str, *, with_consent_version: str | None = None) -> SimpleNamespace:
    """An ``approved`` L2 batch + one ``drafted`` customer — fully sendable EXCEPT the C2 gate.
    With ``with_consent_version`` a matching record_of_consent row is seeded (opens Gate 4)."""
    tenant = _new_tenant(dsn)
    customer, phone = _seed_customer(dsn, tenant)
    work_item = _seed_work_item(dsn, tenant)
    batch = _seed_batch(dsn, tenant, work_item, status="approved")
    draft = _seed_draft(dsn, tenant, batch, customer)
    if with_consent_version is not None:
        _seed_consent(dsn, tenant, phone, version=with_consent_version)
    return SimpleNamespace(
        tenant=tenant, customer=customer, phone=phone,
        work_item=work_item, batch=batch, draft=draft,
    )


# ===========================================================================
# 1. SELECTION — the step drives only 'approved' batches and only 'drafted' drafts.
# ===========================================================================


@pytest.mark.usefixtures("armed_registry")
def test_step_skips_non_approved_batch(substrate):  # type: ignore[no-untyped-def]
    """A batch NOT in 'approved' (e.g. 'awaiting_approval') is a no-op — the step's re-confirm
    guard returns raced_out, no draft is selected, the transport is never reached."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="awaiting_approval")
    _seed_draft(substrate.dsn, tenant, batch, customer)

    out = l2_send._l2_send_step_body(str(tenant), str(batch))

    assert out.get("raced_out") == 1
    assert out["sent"] == 0 and out["skipped"] == 0 and out["failed"] == 0
    assert _customer_contacts(substrate.dsn, tenant) == 0


@pytest.mark.usefixtures("armed_registry")
def test_step_selects_only_drafted_drafts(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """Only ``status='drafted'`` drafts are attempted: a same-batch 'sent' draft is NOT
    re-selected. With the gate open the ONE drafted draft sends; the pre-sent draft is untouched
    (no transport call for it)."""
    s = _approved_l2_stack(substrate.dsn, with_consent_version="vt418-sel-v1")
    # A sibling draft already 'sent' (different customer) must NOT be re-selected.
    other_customer, _ = _seed_customer(substrate.dsn, s.tenant)
    already = _seed_draft(substrate.dsn, s.tenant, s.batch, other_customer, status="sent")
    monkeypatch.setattr(
        customer_send, "_marketing_consent_versions", lambda: frozenset({"vt418-sel-v1"})
    )
    send_fn = _RecordingCustomerSend()

    out = _drive(s, send_fn)

    assert len(send_fn.calls) == 1, "only the 'drafted' draft should reach the transport"
    assert out["sent"] == 1
    assert _draft_row(substrate.dsn, s.tenant, s.draft)[0] == "sent"
    assert _draft_row(substrate.dsn, s.tenant, already)[0] == "sent"  # untouched


def _drive(s: SimpleNamespace, send_fn: Any) -> dict[str, Any]:
    """Invoke the driver's send step on the approved batch, with the recording transport injected
    into each per-draft agent_send_draft (the exact call the step makes, made observable).

    The step calls ``agent_send_draft(tid, did, autonomy_level='L2')`` with NO send_fn (the prod
    contract — the live Twilio transport). To make the would-be send observable WITHOUT touching the
    network, patch ``customer_send.agent_send_draft`` to forward to the REAL one WITH the injected
    recording ``send_fn`` (the step imports it from the module each call, so the patch is seen)."""
    import unittest.mock as _mock

    import orchestrator.agents.customer_send as cs

    real = cs.agent_send_draft

    def _patched(tenant_id, draft_id, *, autonomy_level="L2", conn=None, send_fn=None):  # noqa: ANN001
        return real(tenant_id, draft_id, autonomy_level=autonomy_level, conn=conn, send_fn=send_fn or _spy)

    _spy = send_fn
    with _mock.patch.object(cs, "agent_send_draft", _patched):
        return l2_send._l2_send_step_body(str(s.tenant), str(s.batch))


# ===========================================================================
# 2. C2 FAIL-CLOSED — empty frozenset ⇒ ZERO sends end-to-end (plan §4).
# ===========================================================================


@pytest.mark.usefixtures("armed_registry")
def test_consent_empty_fails_closed_zero_sends(substrate):  # type: ignore[no-untyped-def]
    """The C2 stop. The driver selects the drafted draft and invokes agent_send_draft(L2), but
    Gate 4 fail-closes on the EMPTY MARKETING_CONSENT_VERSIONS frozenset: the draft goes 'skipped'
    (SKIP_CONSENT), the transport is NEVER reached, ZERO agent_customer_contacts. The driver is
    proven AGAINST the stop."""
    s = _approved_l2_stack(substrate.dsn)  # NO consent row, empty frozenset (untouched)
    assert customer_send._marketing_consent_versions() == frozenset(), (
        "C2 must stay EMPTY — the driver is proven against the stop, not by opening it"
    )
    send_fn = _RecordingCustomerSend()

    out = _drive(s, send_fn)

    assert send_fn.calls == [], (
        f"C2 STOP BREACHED — the driver produced {len(send_fn.calls)} send(s) "
        "with an EMPTY consent frozenset"
    )
    assert out["sent"] == 0 and out["skipped"] == 1
    assert _customer_contacts(substrate.dsn, s.tenant) == 0
    status, skip = _draft_row(substrate.dsn, s.tenant, s.draft)
    assert status == "skipped"
    assert skip == customer_send.SKIP_CONSENT, f"expected consent skip, got {skip!r}"


# ===========================================================================
# 3. THE LOAD-BEARING PROOF — exactly-once + no-double-send (money-send).
# ===========================================================================


@pytest.mark.usefixtures("armed_registry")
def test_driver_sends_exactly_once_and_re_run_no_double_send(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """THE money-send acceptance. Open the gate (negative-control style: monkeypatch
    _marketing_consent_versions + seed consent), drive the send ONCE → the transport is reached
    EXACTLY ONCE, the draft flips 'sent', the batch closes to 'sent', ONE agent_customer_contacts
    row, ONE send_idempotency_keys row at agent:{draft_id} with send_status='sent'. Then RE-RUN the
    SAME driver step → NO second send: the transport count is UNCHANGED (the in-module 'already_sent'
    short-circuit + the ledger 'sent' hit). This is the exactly-once + ledger-idempotent guarantee."""
    s = _approved_l2_stack(substrate.dsn, with_consent_version="vt418-money-v1")
    monkeypatch.setattr(
        customer_send, "_marketing_consent_versions", lambda: frozenset({"vt418-money-v1"})
    )

    send_fn = _RecordingCustomerSend()

    # --- First drive: exactly one real send.
    out1 = _drive(s, send_fn)
    assert out1["sent"] == 1, f"first drive must send exactly once: {out1}"
    assert len(send_fn.calls) == 1
    assert send_fn.calls[0][1] == _TEST_TEMPLATE
    assert _draft_row(substrate.dsn, s.tenant, s.draft)[0] == "sent"
    assert _batch_status(substrate.dsn, s.tenant, s.batch) == "sent"
    assert _customer_contacts(substrate.dsn, s.tenant) == 1
    ledger = _idempotency_rows(substrate.dsn, s.tenant, s.draft)
    assert len(ledger) == 1 and ledger[0][0] == "sent", f"expected one 'sent' ledger row: {ledger}"

    # --- Re-run the SAME driver step: NO second send (the batch is no longer 'approved' AND the
    # ledger 'sent' hit short-circuits; the step re-confirm guard alone returns raced_out, but even
    # if it ran the loop the ledger would block the send).
    out2 = _drive(s, send_fn)
    assert len(send_fn.calls) == 1, (
        f"DOUBLE-SEND — re-running the driver produced a second transport call: {send_fn.calls}"
    )
    assert _customer_contacts(substrate.dsn, s.tenant) == 1, "no second contact ledger row"
    assert len(_idempotency_rows(substrate.dsn, s.tenant, s.draft)) == 1, "ledger row count unchanged"
    # raced_out because the batch already closed to 'sent' (not 'approved').
    assert out2.get("raced_out") == 1


class _RaiseAtContactsInsert:
    """A connection PROXY that simulates a crash AT the ``agent_customer_contacts`` INSERT (the last
    statement inside the L2 draft->'sent' txn), delegating every other statement to the real
    connection. VT-644: because the INSERT now runs INSIDE the flip txn, this failure rolls the flip
    BACK — so ``draft_status`` stays 'drafted' (not 'sent'), the re-drive's draft_status early-out
    (customer_send.py:510) does NOT fire, and the idempotent re-drive completes the contact-ledger row
    with NO second wire send. (Pre-fix the INSERT ran as a separate autocommit AFTER the txn: the flip
    committed 'sent', the INSERT crashed, and the re-drive early-out then lost the row permanently.)"""

    _CONTACTS_INSERT = "INSERT INTO agent_customer_contacts"

    def __init__(self, real: Any) -> None:
        self._real = real

    def execute(self, query: str, *args: Any, **kwargs: Any) -> Any:
        if self._CONTACTS_INSERT in query:
            raise RuntimeError("simulated crash at agent_customer_contacts INSERT")
        return self._real.execute(query, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:  # delegate transaction()/cursor()/commit()/… to the real conn
        return getattr(self._real, name)


@pytest.mark.usefixtures("armed_registry")
def test_crash_at_contacts_insert_rolls_back_flip_then_redrive_completes(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """VT-644 crash-injection — THE atomicity regression guard for the frequency-cap suppression row.

    A crash AT the ``agent_customer_contacts`` INSERT must roll the draft->'sent' flip BACK, so the
    suppression-ledger row is never permanently lost. Then a clean re-drive (the DBOS step retry)
    completes flip+contacts with NO second wire send (send_whatsapp_template's ledger idempotency).
    If the INSERT is ever moved back OUTSIDE the flip txn, the first assertion (draft still 'drafted')
    fails — the draft would be 'sent' with the ledger row gone, and the re-drive early-out at
    customer_send.py:510 would strand it."""
    s = _approved_l2_stack(substrate.dsn, with_consent_version="vt644-crash-v1")
    monkeypatch.setattr(
        customer_send, "_marketing_consent_versions", lambda: frozenset({"vt644-crash-v1"})
    )
    send_fn = _RecordingCustomerSend()

    import contextlib

    import orchestrator.agents.customer_send as cs

    real_tc = cs.tenant_connection

    @contextlib.contextmanager
    def _wrapping_tc(tenant_id, *a, **k):  # noqa: ANN001, ANN202
        with real_tc(tenant_id, *a, **k) as real_conn:
            yield _RaiseAtContactsInsert(real_conn)

    # --- First drive: the wire send fires, then the contacts INSERT crashes inside the flip txn.
    monkeypatch.setattr(cs, "tenant_connection", _wrapping_tc)
    with pytest.raises(RuntimeError, match="crash at agent_customer_contacts"):
        _drive(s, send_fn)

    assert len(send_fn.calls) == 1, "the wire send must have fired exactly once before the crash"
    assert _draft_row(substrate.dsn, s.tenant, s.draft)[0] == "drafted", (
        "ATOMICITY BREACH — the draft is 'sent' but the contact-ledger INSERT never committed; the "
        "flip must roll back WITH the INSERT (VT-644) so the re-drive is not short-circuited"
    )
    assert _customer_contacts(substrate.dsn, s.tenant) == 0
    # The idempotency ledger IS 'sent' — it committed in the wire-send txn, upstream of the flip txn.
    ledger = _idempotency_rows(substrate.dsn, s.tenant, s.draft)
    assert len(ledger) == 1 and ledger[0][0] == "sent", f"expected one 'sent' ledger row: {ledger}"

    # --- Re-drive with the crash cleared (the DBOS step retry): completes flip+contacts, NO 2nd send.
    monkeypatch.setattr(cs, "tenant_connection", real_tc)
    _drive(s, send_fn)
    assert len(send_fn.calls) == 1, (
        f"DOUBLE-SEND on recovery — the re-drive re-hit the transport: {send_fn.calls}"
    )
    assert _draft_row(substrate.dsn, s.tenant, s.draft)[0] == "sent"
    assert _customer_contacts(substrate.dsn, s.tenant) == 1, (
        "the suppression-ledger row must be written on the idempotent re-drive (VT-644 recovery)"
    )
    assert len(_idempotency_rows(substrate.dsn, s.tenant, s.draft)) == 1, "no duplicate ledger row"


@pytest.mark.usefixtures("armed_registry")
def test_re_select_drafted_draft_hits_ledger_no_double_send(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """The harder no-double-send case: the batch is STILL 'approved' on re-run AND the draft is
    adversarially re-presented as 'drafted' with the ledger 'sent' row INTACT. The send MUST NOT
    reach the transport a SECOND time. Defense-in-depth: BOTH the per-customer recontact cap (Gate
    5 — the fresh agent_customer_contacts row from the first send) AND the ledger 'sent' hit (Gate
    6) independently block the second send. Either way the load-bearing property holds: NO second
    transport call, NO second contact ledger row, NO new send_idempotency_keys row. (We deliberately
    do NOT defeat the recontact cap here — its job is exactly to stop a re-contact, and a real
    re-select would hit it first; the assertion is on the no-double-send invariant, not on which gate
    catches it.)"""
    s = _approved_l2_stack(substrate.dsn, with_consent_version="vt418-resel-v1")
    monkeypatch.setattr(
        customer_send, "_marketing_consent_versions", lambda: frozenset({"vt418-resel-v1"})
    )
    send_fn = _RecordingCustomerSend()

    out1 = _drive(s, send_fn)
    assert out1["sent"] == 1 and len(send_fn.calls) == 1
    assert _idempotency_rows(substrate.dsn, s.tenant, s.draft)[0][0] == "sent"

    # Force the adversarial re-select: draft back to 'drafted', batch back to 'approved', ledger
    # 'sent' row + the contact row INTACT. The second send MUST NOT fire.
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE agent_drafts SET status = 'drafted', message_sid = NULL "
            "WHERE tenant_id = %s AND id = %s", (str(s.tenant), str(s.draft)),
        )
        conn.execute(
            "UPDATE agent_draft_batches SET status = 'approved' WHERE tenant_id = %s AND id = %s",
            (str(s.tenant), str(s.batch)),
        )

    out2 = _drive(s, send_fn)
    # THE invariant: no second transport call, no second contact, no new ledger row.
    assert len(send_fn.calls) == 1, (
        f"DOUBLE-SEND on forced re-select — a second transport call fired: {send_fn.calls}"
    )
    assert _customer_contacts(substrate.dsn, s.tenant) == 1, "no second contact ledger row"
    assert len(_idempotency_rows(substrate.dsn, s.tenant, s.draft)) == 1, "no new ledger row"
    # The draft did NOT send again (it was blocked — skipped by the recontact cap and/or the
    # ledger). It is NOT a delivered second send.
    assert out2["sent"] == 0, "the re-selected draft must NOT count as a fresh send"


@pytest.mark.usefixtures("armed_registry")
def test_ledger_sent_hit_alone_blocks_resend_with_caps_defeated(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """ISOLATE the load-bearing LEDGER guard. Re-drive the SAME draft with the recontact caps
    DEFEATED (backdate the first send's contact row past every cap window), the draft reset to
    'drafted', the batch back to 'approved', and the 'sent' ledger row at agent:{draft_id} INTACT.
    Now the ONLY thing that can stop a second transport call is the ledger 'sent' hit — and it MUST:
    agent_send_draft reaches Gate 6, send_whatsapp_template's _check_idempotency finds the 'sent'
    row, short-circuits, returns the cached SID, and NEVER calls the transport again. This is the
    money-send 'sent is a permanent hit' assertion in isolation (plan §3)."""
    s = _approved_l2_stack(substrate.dsn, with_consent_version="vt418-ledger-v1")
    monkeypatch.setattr(
        customer_send, "_marketing_consent_versions", lambda: frozenset({"vt418-ledger-v1"})
    )
    send_fn = _RecordingCustomerSend()

    out1 = _drive(s, send_fn)
    assert out1["sent"] == 1 and len(send_fn.calls) == 1
    assert _idempotency_rows(substrate.dsn, s.tenant, s.draft)[0][0] == "sent"

    # Defeat every recontact cap window: backdate the contact row to >90d ago. Reset the draft +
    # batch so the gate stack runs to Gate 6 — the ledger 'sent' hit is the SOLE remaining guard.
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE agent_customer_contacts SET sent_at = now() - interval '120 days' "
            "WHERE tenant_id = %s AND customer_id = %s", (str(s.tenant), str(s.customer)),
        )
        conn.execute(
            "UPDATE agent_drafts SET status = 'drafted', message_sid = NULL "
            "WHERE tenant_id = %s AND id = %s", (str(s.tenant), str(s.draft)),
        )
        conn.execute(
            "UPDATE agent_draft_batches SET status = 'approved' WHERE tenant_id = %s AND id = %s",
            (str(s.tenant), str(s.batch)),
        )

    out2 = _drive(s, send_fn)
    # The LEDGER blocked the transport (caps were defeated) — exactly the 'sent permanent hit'.
    assert len(send_fn.calls) == 1, (
        f"LEDGER FAILED — with caps defeated, the 'sent' idempotency hit did NOT block the second "
        f"transport call: {send_fn.calls}"
    )
    # The cached 'sent' hit returns status='sent'; the draft re-flips to sent (contact already
    # exists → already_sent) → the driver counts it as a (non-new) send, NOT a fresh transport call.
    assert out2["sent"] == 1, "the ledger hit returns the cached 'sent' (already_sent), not a skip"
    assert len(_idempotency_rows(substrate.dsn, s.tenant, s.draft)) == 1, "no new ledger row written"
    assert _customer_contacts(substrate.dsn, s.tenant) == 1, "no second contact row"


# ===========================================================================
# 4. TRANSIENT RETRY — 'error' is NOT a ledger hit, so a re-run re-sends (VT-410).
# ===========================================================================


@pytest.mark.usefixtures("armed_registry")
def test_transient_error_is_retryable_then_sends_exactly_once(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """VT-410 semantics through the driver. The first attempt's transport RAISES (5xx-equivalent)
    → the draft stays 'drafted', the ledger row is 'error' (NOT a hit). A re-run on the still-
    'approved' batch re-selects the drafted draft and NOW sends exactly once (the 'error' row did
    not suppress it). Total real sends across both runs: exactly ONE."""
    s = _approved_l2_stack(substrate.dsn, with_consent_version="vt418-tx-v1")
    monkeypatch.setattr(
        customer_send, "_marketing_consent_versions", lambda: frozenset({"vt418-tx-v1"})
    )
    raising = _RaisingCustomerSend()

    # First drive: transport raises → draft stays drafted, ledger 'error', batch stays approved.
    out1 = _drive(s, raising)
    assert out1["failed"] == 1, f"first drive should record a failed send: {out1}"
    assert raising.calls == 1
    assert _draft_row(substrate.dsn, s.tenant, s.draft)[0] == "drafted", "draft must stay drafted"
    assert _batch_status(substrate.dsn, s.tenant, s.batch) in ("approved", "sending"), (
        "an errored send must not close the batch"
    )
    err_ledger = _idempotency_rows(substrate.dsn, s.tenant, s.draft)
    assert any(st == "error" for st, _ in err_ledger), f"expected an 'error' ledger row: {err_ledger}"

    # Re-confirm the batch back to 'approved' if the CAS left it in 'sending' (the L2 flip happens
    # before the transport raises) so the driver re-selects it — the recovery path.
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE agent_draft_batches SET status = 'approved' "
            "WHERE tenant_id = %s AND id = %s AND status = 'sending'",
            (str(s.tenant), str(s.batch)),
        )

    # Re-run: now the SAME draft sends exactly once (the 'error' row was not a hit).
    out2 = _drive(s, raising)
    assert out2["sent"] == 1, f"re-run must re-send the transiently-failed draft: {out2}"
    assert raising.calls == 2, "exactly one more transport call (the successful retry)"
    assert _draft_row(substrate.dsn, s.tenant, s.draft)[0] == "sent"
    assert _customer_contacts(substrate.dsn, s.tenant) == 1, "exactly one delivered contact total"
    # Ledger nuance (load-bearing for the money audit): _write_idempotency_ledger is
    # INSERT ... ON CONFLICT (tenant_id, idempotency_key) DO NOTHING. The first attempt wrote the
    # 'error' row; the successful retry's 'sent' write CONFLICTS and is a no-op, so the ledger row
    # STAYS 'error' (still NOT a hit). That does NOT re-open a double-send: a THIRD driver run would
    # not re-select this draft at all — it is now status='sent', excluded by the step's
    # 'status=drafted' selection AND the in-module already_sent short-circuit. So the no-double-send
    # invariant after an error→success transition is carried by the DRAFT STATUS, not the ledger.
    # The single row remains; exactly ONE message was delivered.
    final = _idempotency_rows(substrate.dsn, s.tenant, s.draft)
    assert len(final) == 1, f"exactly one idempotency row (ON CONFLICT DO NOTHING): {final}"
    # Prove the no-third-send invariant explicitly: re-drive once more — the draft is now 'sent',
    # never re-selected, transport count unchanged.
    out3 = _drive(s, raising)
    assert raising.calls == 2, "no THIRD transport call — the sent draft is never re-selected"
    assert out3.get("raced_out") == 1 or out3["sent"] == 0, "the closed/sent batch drives nothing"


# ===========================================================================
# 5. RECONCILER — heals a missed post-commit start (recovery-only, plan §1B).
# ===========================================================================


@pytest.mark.usefixtures("armed_registry")
def test_reconciler_selects_only_stuck_approved_l2_batches_past_grace(substrate):  # type: ignore[no-untyped-def]
    """The reconciler's SELECTION: it picks an 'approved' L2 batch with a 'drafted' draft aged past
    the staleness grace, and IGNORES (a) a fresh 'approved' batch within the grace, (b) a non-
    approved batch, (c) an approved batch with no drafted drafts. Cross-tenant service-role scan."""
    # (a) stuck-past-grace approved L2 batch with a drafted draft → SELECTED.
    stuck = _approved_l2_stack(substrate.dsn)
    _age_batch(substrate.dsn, stuck.tenant, stuck.batch, minutes=30)
    # (b) fresh approved batch within grace → NOT selected.
    fresh = _approved_l2_stack(substrate.dsn)  # updated_at = now()
    # (c) non-approved batch → NOT selected.
    other = _new_tenant(substrate.dsn)
    oc, _ = _seed_customer(substrate.dsn, other)
    owi = _seed_work_item(substrate.dsn, other)
    ob = _seed_batch(substrate.dsn, other, owi, status="awaiting_approval")
    _seed_draft(substrate.dsn, other, ob, oc)
    _age_batch(substrate.dsn, other, ob, minutes=30)
    # (d) approved-but-no-drafted-drafts → NOT selected (its only draft is 'sent').
    nodraft = _new_tenant(substrate.dsn)
    ndc, _ = _seed_customer(substrate.dsn, nodraft)
    ndwi = _seed_work_item(substrate.dsn, nodraft)
    ndb = _seed_batch(substrate.dsn, nodraft, ndwi, status="approved")
    _seed_draft(substrate.dsn, nodraft, ndb, ndc, status="sent")
    _age_batch(substrate.dsn, nodraft, ndb, minutes=30)

    from datetime import datetime, timezone

    rows = l2_send._scan_stuck_approved_l2_batches(datetime.now(timezone.utc))
    selected = {r["batch_id"] for r in rows}

    assert str(stuck.batch) in selected, "stuck-past-grace approved L2 batch must be selected"
    assert str(fresh.batch) not in selected, "fresh-within-grace batch must NOT be selected"
    assert str(ob) not in selected, "non-approved batch must NOT be selected"
    assert str(ndb) not in selected, "approved-but-no-drafted-drafts batch must NOT be selected"


@pytest.mark.usefixtures("armed_registry")
def test_reconciler_heals_missed_start_and_sends(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """End-to-end recovery: the post-commit start never ran (the batch just sits in 'approved'),
    so the reconciler body re-drives it via ``start_l2_send`` → the durable workflow runs its send
    step → the stuck batch sends exactly once.

    The reconciler starts the REAL durable workflow (the substrate fixture launched DBOS + registered
    it). The workflow's send step runs in a DBOS worker thread and resolves the LIVE
    ``send_template_message`` — which the orchestrator-package autouse ``_autostub_twilio`` fixture
    has stubbed (``twilio_send._client`` → a fake client returning a SID), so NO real Twilio call is
    made. The consent gate is opened process-wide via ``monkeypatch.setattr`` on the shared
    ``customer_send`` module object (visible cross-thread — only thread-locals would not be). We wait
    on the workflow handle for its terminal result and assert the stuck batch closed to 'sent'
    exactly once."""
    s = _approved_l2_stack(substrate.dsn, with_consent_version="vt418-recon-v1")
    _age_batch(substrate.dsn, s.tenant, s.batch, minutes=30)
    monkeypatch.setattr(
        customer_send, "_marketing_consent_versions", lambda: frozenset({"vt418-recon-v1"})
    )

    from datetime import datetime, timezone

    from dbos import DBOS

    # The reconciler SELECTS + STARTS the stuck batch's durable workflow.
    started = l2_send.run_l2_approved_send_sweep_body(now=datetime.now(timezone.utc))
    assert str(s.batch) in started, "reconciler must (re)start the stuck-approved batch"

    # Wait on the workflow the reconciler started for its terminal result.
    handle = DBOS.retrieve_workflow(f"l2_send_{s.batch}")
    result = handle.get_result()
    assert result["batch_id"] == str(s.batch)
    # The workflow drove the batch to a terminal close (sent), via the real gate stack (autouse-
    # stubbed twilio transport) on the dev synthetic with the gate opened.
    assert _batch_status(substrate.dsn, s.tenant, s.batch) == "sent"
    assert _customer_contacts(substrate.dsn, s.tenant) == 1, "exactly one delivered contact"


# ===========================================================================
# 6. EXACTLY-ONCE START — the l2_send_{batch_id} workflow-id makes a double start a no-op.
# ===========================================================================


def _seed_resolved_agent_approval(
    dsn: str, tenant: UUID, batch: UUID, *, decision: str = "approved"
) -> UUID:
    """A RESOLVED agent_customer_send approval linked to the batch — the runner post-commit seam
    substrate (the approval has been marked resolved + the batch flipped in the same txn; the seam
    helper then starts the send)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        run = conn.execute(
            "INSERT INTO pipeline_runs (tenant_id, status) VALUES (%s, 'running') RETURNING id",
            (str(tenant),),
        ).fetchone()[0]
        row = conn.execute(
            "INSERT INTO pending_approvals (tenant_id, run_id, approval_type, summary, details, "
            "draft_batch_id, timeout_at, status, decision, resolved_at) "
            "VALUES (%s, %s, 'agent_customer_send', %s, %s, %s, now() + interval '1 hour', "
            "%s, %s, now()) RETURNING id",
            (str(tenant), str(run), f"Batch {batch} — approve to send?",
             Jsonb({"draft_batch_id": str(batch)}), str(batch),
             "approved" if decision == "approved" else "rejected", decision),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


# ===========================================================================
# 7. THE RUNNER POST-COMMIT ARM SEAM — start_l2_send_for_resolved_approval.
# ===========================================================================


@pytest.mark.usefixtures("armed_registry")
def test_arm_seam_starts_send_for_approved_batch(substrate):  # type: ignore[no-untyped-def]
    """The runner post-commit seam helper. Given a RESOLVED agent_customer_send approval whose
    linked batch is NOW 'approved' (the resolution txn already committed the flip), the helper
    looks up the batch + starts the durable send workflow. It returns the started batch id; the
    workflow id is l2_send_{batch_id} (idempotent). C2 stays empty → zero real sends (the seam is
    proven against the stop)."""
    from dbos import DBOS

    s = _approved_l2_stack(substrate.dsn)  # batch is 'approved'
    approval = _seed_resolved_agent_approval(substrate.dsn, s.tenant, s.batch, decision="approved")

    out = l2_send.start_l2_send_for_resolved_approval(str(s.tenant), str(approval))
    assert out == str(s.batch), "the seam must start the send for the approved batch"

    handle = DBOS.retrieve_workflow(f"l2_send_{s.batch}")
    result = handle.get_result()
    assert result["batch_id"] == str(s.batch)
    assert _customer_contacts(substrate.dsn, s.tenant) == 0  # C2 empty → zero sends


@pytest.mark.usefixtures("armed_registry")
def test_arm_seam_noops_when_batch_not_approved(substrate):  # type: ignore[no-untyped-def]
    """The seam's 'status=approved' guard. If the resolution flipped the batch to a NON-approved
    state (e.g. cancelled / rejected / edit_requested), the helper is a SAFE no-op — it never starts
    a send over a batch that did not reach approved. Returns None; no workflow, no send."""
    tenant = _new_tenant(substrate.dsn)
    customer, _ = _seed_customer(substrate.dsn, tenant)
    work_item = _seed_work_item(substrate.dsn, tenant)
    # The batch resolved to 'cancelled' (a timeout/defer), NOT 'approved'.
    batch = _seed_batch(substrate.dsn, tenant, work_item, status="cancelled")
    _seed_draft(substrate.dsn, tenant, batch, customer)
    approval = _seed_resolved_agent_approval(substrate.dsn, tenant, batch, decision="rejected")

    out = l2_send.start_l2_send_for_resolved_approval(str(tenant), str(approval))
    assert out is None, "a non-approved batch must be a safe no-op (no send started)"
    assert _customer_contacts(substrate.dsn, tenant) == 0


@pytest.mark.usefixtures("armed_registry")
def test_double_start_is_one_workflow_run(substrate):  # type: ignore[no-untyped-def]
    """The exactly-once START guarantee: start_l2_send twice for the SAME batch resolves to ONE
    workflow run (DBOS.start_workflow no-ops on the known l2_send_{batch_id} id). A redelivered
    owner-reply / a sweep re-select after the primary cannot spawn a second send driver."""
    from dbos import DBOS

    s = _approved_l2_stack(substrate.dsn)  # gate stays empty → the run will skip the send (fine —
    # this test asserts on the START identity, not the send outcome)

    l2_send.start_l2_send(str(s.tenant), str(s.batch))
    l2_send.start_l2_send(str(s.tenant), str(s.batch))

    handle = DBOS.retrieve_workflow(f"l2_send_{s.batch}")
    result = handle.get_result()
    assert result["batch_id"] == str(s.batch)
    assert result["outcome"] == "sent"
    # The C2-empty gate means zero real sends even though the workflow ran (proven against the stop).
    assert _customer_contacts(substrate.dsn, s.tenant) == 0
