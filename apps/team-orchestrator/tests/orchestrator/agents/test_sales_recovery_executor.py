"""VT-369 Gap-5 PR-1 — behavioral tests for ``orchestrator.agents.sales_recovery_executor``.

No live LLM anywhere: the drafting call is injected. No live sends anywhere (the executor holds
no sender — that is itself under test).

Covered behaviours:
  - the shipped ``MARKETING_CONSENT_VERSIONS`` allowlist is EMPTY and detection is structurally
    fail-closed: zero candidates ALWAYS, even over a fully eligible seeded customer base
    (plan §2.1 / C2; risk #5 — the empty set is deliberate);
  - with the allowlist monkeypatched to a test version: p75-recency + p50-spend thresholds
    honoured; opted-out, complaint-open, consent-missing, consent-opted-out, wrong-consent-
    version, and recently-agent-contacted customers excluded; richest-first ordering + limit;
  - the in-SQL phone-token expression matches ``utils.phone_token.hash_phone`` byte-for-byte
    (the drift pin for the consent join);
  - the fact-bundle numbers are computed in Python from raw ledger rows;
  - ``execute_item`` with an injected LLM persists ``agent_draft_batches(awaiting_approval)`` +
    ``agent_drafts(drafted)``, drops the ungrounded draft (counted), and arms the Pillar-7
    approval through the injected arm fn (spy) with IDs + counters only;
  - arm refusal cancels the batch fail-closed (drafts halted);
  - the CL-425 ``owner_inputs`` gate trips BEFORE any LLM call;
  - CRITICAL-2 structural no-sender posture: the module's tool surface is the pinned empty
    tuple run through ``assert_agent_tools_safe`` at import, the module source contains no
    forbidden sender-capability name, and a fresh import pulls no sender/Twilio module.

DB substrate mirrors ``tests/orchestrator/business_plan/test_generator.py``: migrations applied
once, DBOS launched so the ``tenant_connection`` pool exists, rows seeded via a direct
service-role psycopg connection (consent seeded through the REAL ``privacy.consent`` writer so
the token join is pinned end-to-end).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after dependency skip guards

from orchestrator.agent.tool_guardrail import FORBIDDEN_CAPABILITY_SUBSTRINGS  # noqa: E402
from orchestrator.agents import sales_recovery_executor as sre  # noqa: E402
from orchestrator.agents.coordinator import AgentItemContext  # noqa: E402
from orchestrator.db import tenant_connection  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-369 sales_recovery executor substrate tests skipped",
)

_SRC_DIR = Path(sre.__file__).resolve().parents[2]
_TEST_CONSENT_VERSION = "vt369-test-v1"
_WRONG_CONSENT_VERSION = "vt369-transactional-v0"


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so the ``tenant_connection`` pool exists. Mirrors
    tests/orchestrator/business_plan/test_generator.py."""
    import apply_migrations

    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt369-test-salt")
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


def _new_tenant(dsn: str, *, name: str, owner_inputs: bool = True) -> UUID:
    # VT-421: execute_item now has a DETECT-side ACTIVATION gate (tenant_is_sr_eligible →
    # is_agent_eligible) right after the owner_inputs check. For these end-to-end SR tests to reach
    # detection/drafting, the tenant must be fully activated: journey-complete + gstin_verified + ≥1
    # enabled+ok connector (the per-test _seed_customer satisfies the ≥1-customer leg). owner_inputs=
    # False tests still trip the owner_inputs gate first (it runs above the activation gate). The bar
    # is now journey-complete (onboarding_journey.status='complete'), NOT paid-active — so seed it.
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            # VT-522/VT-517: ownership_verified is a universal execution bar (VTR-human review).
            # Seed it verified so these end-to-end SR tests reach detection/drafting past the gate.
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, whatsapp_number, owner_inputs, verification_status, "
            "ownership_verified, ownership_status) "
            "VALUES (%s, 'founding', 'paid_active', now(), 'restaurant', %s, %s, 'gstin_verified', "
            "TRUE, 'verified') "
            "RETURNING id",
            (name, f"+9198{uuid4().int % 10**8:08d}", owner_inputs),
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
    return tenant


def _seed_customer(
    dsn: str,
    tenant_id: UUID,
    *,
    display_name: str | None,
    phone: str,
    opt_out_status: str = "subscribed",
    complaint_status: str = "none",
) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO customers "
            "(tenant_id, display_name, phone_e164, opt_out_status, complaint_status) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (str(tenant_id), display_name, phone, opt_out_status, complaint_status),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_sales(
    dsn: str, tenant_id: UUID, customer_id: UUID, sales: list[tuple[int, int]]
) -> None:
    """``sales`` = [(days_ago, amount_paise), ...] — 'sale' ledger entries."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        for days_ago, amount_paise in sales:
            conn.execute(
                "INSERT INTO customer_ledger_entries "
                "(tenant_id, customer_id, amount_paise, entry_type, entry_date, "
                " acquired_via, source_confidence, entry_key) "
                "VALUES (%s, %s, %s, 'sale', %s, 'owner_typed', 1.0, %s)",
                (
                    str(tenant_id),
                    str(customer_id),
                    amount_paise,
                    date.today() - timedelta(days=days_ago),
                    uuid4().hex,
                ),
            )


def _seed_agent_contact(dsn: str, tenant_id: UUID, customer_id: UUID, *, days_ago: int) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO agent_customer_contacts "
            "(tenant_id, customer_id, agent, template_name, autonomy_level, sent_at) "
            "VALUES (%s, %s, 'sales_recovery', %s, 'L2', now() - make_interval(days => %s))",
            (str(tenant_id), str(customer_id), sre.WINBACK_TEMPLATE_NAME, days_ago),
        )


def _seed_consent(tenant_id: UUID, phone: str, version: str) -> None:
    """Through the REAL privacy.consent writer — pins the token join end-to-end."""
    from orchestrator.privacy import consent

    consent.record_consent(tenant_id, phone, consent_text_version=version)


def _seed_work_item(dsn: str, tenant_id: UUID) -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
            "VALUES (%s, %s, 'sales_recovery', 'drafting') RETURNING id",
            (str(tenant_id), f"item-{uuid4()}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _phone(i: int) -> str:
    return f"+9197{uuid4().int % 10**6:06d}{i:02d}"


def _seed_lapsed_scenario(dsn: str) -> SimpleNamespace:
    """One tenant, ten customers — exactly TWO eligible candidates once the consent allowlist
    admits ``_TEST_CONSENT_VERSION``.

    Population metrics (all ten have sales, so all ten shape the percentiles):
      recency sorted [5, 100×8, 150] → p75 = 100; spend sorted [1000, 50000×8, 80000] → p50 =
      50000. Eligible: a2 (150d, 80000) and a (100d, 50000). Every other customer fails exactly
      one gate."""
    t = _new_tenant(dsn, name="VT-369 sre detection")
    mk = _seed_customer

    a2 = mk(dsn, t, display_name="Asha", phone=_phone(1))
    _seed_sales(dsn, t, a2, [(200, 50000), (150, 30000)])  # last sale 150d ago; lifetime 80000
    a = mk(dsn, t, display_name="Vikram", phone=_phone(2))
    _seed_sales(dsn, t, a, [(100, 50000)])
    recent = mk(dsn, t, display_name="Recent", phone=_phone(3))
    _seed_sales(dsn, t, recent, [(5, 50000)])  # fails p75 recency
    low_spend = mk(dsn, t, display_name="LowSpend", phone=_phone(4))
    _seed_sales(dsn, t, low_spend, [(100, 1000)])  # fails p50 spend
    opted_out = mk(dsn, t, display_name="OptedOut", phone=_phone(5), opt_out_status="opted_out")
    _seed_sales(dsn, t, opted_out, [(100, 50000)])
    complaint = mk(dsn, t, display_name="Complaint", phone=_phone(6), complaint_status="open")
    _seed_sales(dsn, t, complaint, [(100, 50000)])
    contacted = mk(dsn, t, display_name="Contacted", phone=_phone(7))
    _seed_sales(dsn, t, contacted, [(100, 50000)])
    _seed_agent_contact(dsn, t, contacted, days_ago=10)  # inside the 30d suppression
    no_consent = mk(dsn, t, display_name="NoConsent", phone=_phone(8))
    _seed_sales(dsn, t, no_consent, [(100, 50000)])
    wrong_version = mk(dsn, t, display_name="WrongVersion", phone=_phone(9))
    _seed_sales(dsn, t, wrong_version, [(100, 50000)])
    consent_optout = mk(dsn, t, display_name="ConsentOptOut", phone=_phone(10))
    _seed_sales(dsn, t, consent_optout, [(100, 50000)])

    phones: dict[UUID, str] = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        for row in conn.execute(
            "SELECT id, phone_e164 FROM customers WHERE tenant_id = %s", (str(t),)
        ).fetchall():
            phones[UUID(str(row[0]))] = str(row[1])

    # Consent: the eligible pair + the per-gate exclusion fixtures. ``opted_out``/``complaint``/
    # ``contacted`` hold VALID consent so each exclusion is attributable to its own gate.
    for cid in (a2, a, opted_out, complaint, contacted, consent_optout):
        _seed_consent(t, phones[cid], _TEST_CONSENT_VERSION)
    _seed_consent(t, phones[wrong_version], _WRONG_CONSENT_VERSION)

    from orchestrator.privacy import consent

    assert consent.opt_out_for_phone(t, phones[consent_optout])  # opted_out_at stamped

    return SimpleNamespace(
        tenant=t,
        a2=a2,
        a=a,
        excluded=[recent, low_spend, opted_out, complaint, contacted, no_consent,
                  wrong_version, consent_optout],
    )


# --- fakes -------------------------------------------------------------------


class _EchoLLM:
    """Injectable LLM double: replays the ``<allowed_params>`` JSON verbatim (a perfectly
    grounded model); calls listed in ``corrupt_calls`` (1-based) fabricate the name instead."""

    def __init__(self, corrupt_calls: set[int] | None = None):
        self.calls: list[tuple[str, str]] = []
        self.corrupt_calls = corrupt_calls or set()

    def __call__(self, prompt: str, model: str) -> str:
        self.calls.append((prompt, model))
        match = re.search(r"<allowed_params>\s*(\{.*?\})\s*</allowed_params>", prompt, re.S)
        assert match is not None, "the drafting prompt must carry <allowed_params>"
        params = json.loads(match.group(1))
        if len(self.calls) in self.corrupt_calls:
            params["customer_name"] = "Fabricated Name"  # ungrounded — must be dropped
        return json.dumps(params, ensure_ascii=False)


def _forbidden_llm(prompt: str, model: str) -> str:
    raise AssertionError("the LLM must not be called on this path")


def _forbidden_arm(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("the approval arm fn must not be called on this path")


class _ArmSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, tenant_id: str, run_id: str, batch_id: str, counts: dict) -> str:
        self.calls.append((tenant_id, run_id, batch_id, counts))
        return str(uuid4())


def _read_batches(dsn: str, tenant_id: UUID) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT id, work_item_id, agent, status FROM agent_draft_batches "
            "WHERE tenant_id = %s",
            (str(tenant_id),),
        ).fetchall()
    return [
        {"id": UUID(str(r[0])), "work_item_id": UUID(str(r[1])), "agent": r[2], "status": r[3]}
        for r in rows
    ]


def _read_drafts(dsn: str, tenant_id: UUID, batch_id: UUID) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT customer_id, template_name, params, status, skip_reason FROM agent_drafts "
            "WHERE tenant_id = %s AND batch_id = %s",
            (str(tenant_id), str(batch_id)),
        ).fetchall()
    return [
        {
            "customer_id": UUID(str(r[0])),
            "template_name": r[1],
            "params": r[2],
            "status": r[3],
            "skip_reason": r[4],
        }
        for r in rows
    ]


# --- the C2 allowlist: empty + structurally fail-closed ------------------------


def test_shipped_consent_allowlist_is_empty_and_short_circuits() -> None:
    """The SHIPPED allowlist is frozenset() (C2 unresolved) and detection returns [] before
    touching the connection — zero candidates ALWAYS, structurally (conn=None proves no SQL)."""
    assert sre.MARKETING_CONSENT_VERSIONS == frozenset()
    assert sre.detect_lapsed_customers(uuid4(), conn=None) == []


@requires_db
def test_empty_allowlist_zero_candidates_over_eligible_base(substrate):  # type: ignore[no-untyped-def]
    """A fully eligible seeded base STILL yields zero candidates while the allowlist is empty
    (the fail-closed property holds against real data, not just against no data)."""
    scenario = _seed_lapsed_scenario(substrate.dsn)
    with tenant_connection(scenario.tenant) as conn:
        assert sre.detect_lapsed_customers(scenario.tenant, conn=conn) == []


# --- detection (allowlist patched to the test version) -------------------------


@requires_db
def test_detection_thresholds_exclusions_and_ordering(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """Eligible = exactly [a2, a], richest-first; every exclusion gate (recency p75, spend p50,
    opt-out, complaint, 30d agent contact, no consent, wrong consent version, consent opted out)
    removes exactly its fixture; candidate metrics are correct."""
    monkeypatch.setattr(sre, "MARKETING_CONSENT_VERSIONS", frozenset({_TEST_CONSENT_VERSION}))
    scenario = _seed_lapsed_scenario(substrate.dsn)

    with tenant_connection(scenario.tenant) as conn:
        candidates = sre.detect_lapsed_customers(scenario.tenant, conn=conn)

    assert [c.customer_id for c in candidates] == [scenario.a2, scenario.a], (
        "expected exactly [a2, a] ordered by lifetime spend DESC"
    )
    a2 = candidates[0]
    assert a2.days_since_last_sale == 150
    assert a2.lifetime_spend_paise == 80000
    assert a2.last_sale_date == date.today() - timedelta(days=150)
    a = candidates[1]
    assert a.days_since_last_sale == 100
    assert a.lifetime_spend_paise == 50000
    excluded_hits = {c.customer_id for c in candidates} & set(scenario.excluded)
    assert not excluded_hits, f"excluded customers leaked into detection: {excluded_hits}"


@requires_db
def test_detection_limit_caps_richest_first(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(sre, "MARKETING_CONSENT_VERSIONS", frozenset({_TEST_CONSENT_VERSION}))
    scenario = _seed_lapsed_scenario(substrate.dsn)
    with tenant_connection(scenario.tenant) as conn:
        candidates = sre.detect_lapsed_customers(scenario.tenant, conn=conn, limit=1)
    assert [c.customer_id for c in candidates] == [scenario.a2]


@requires_db
def test_sql_token_expression_matches_hash_phone(substrate):  # type: ignore[no-untyped-def]
    """The drift pin: the in-SQL consent-join token must equal utils.phone_token.hash_phone
    byte-for-byte. If VT-122 changes the tokenisation, THIS fails loudly (detection itself
    would drift fail-closed)."""
    from orchestrator.utils.phone_token import hash_phone

    salt = os.environ["TEAM_PHONE_HASH_SALT"]
    phone = "+919876501234"
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT 'phone_tok_' || encode(sha256(convert_to(%s || ':' || %s, 'UTF8')), 'hex')",
            (salt, phone),
        ).fetchone()
    assert row is not None
    assert row[0] == hash_phone(phone)


# --- fact bundle ----------------------------------------------------------------


@requires_db
def test_bundle_numbers_computed_in_python(substrate):  # type: ignore[no-untyped-def]
    """days_since_last_sale / last_sale_amount_paise / lifetime_spend_paise derived from the
    raw ledger rows: latest sale's amount (not the max, not the sum) is the last amount."""
    dsn = substrate.dsn
    t = _new_tenant(dsn, name="VT-369 sre bundle")
    cid = _seed_customer(dsn, t, display_name="Asha", phone=_phone(1))
    _seed_sales(dsn, t, cid, [(200, 50000), (150, 30000)])

    with tenant_connection(t) as conn:
        bundle = sre.build_customer_fact_bundle(t, cid, conn=conn)

    assert bundle.customer_id == cid
    assert bundle.display_name == "Asha"
    assert bundle.days_since_last_sale == 150
    assert bundle.last_sale_amount_paise == 30000
    assert bundle.lifetime_spend_paise == 80000

    with tenant_connection(t) as conn, pytest.raises(LookupError):
        sre.build_customer_fact_bundle(t, uuid4(), conn=conn)


# --- grounding validator (pure) ---------------------------------------------------


def test_validate_draft_params_grounding() -> None:
    # VT-384 (registry-as-canon): WINBACK_TEMPLATE_PARAMS = (customer_name, business_name); the old
    # days_since_last_visit param is gone. The grounded literals are display_name + business_name.
    bundle = sre.CustomerFactBundle(
        customer_id=uuid4(),
        display_name="Asha",
        days_since_last_sale=41,
        last_sale_amount_paise=30000,
        lifetime_spend_paise=80000,
        business_name="Test Cafe",
    )
    good = {"customer_name": "Asha", "business_name": "Test Cafe"}
    assert sre.validate_draft_params(good, bundle)
    # Ungrounded name / business — exact-literal rule.
    assert not sre.validate_draft_params({**good, "customer_name": "Priya"}, bundle)
    assert not sre.validate_draft_params({**good, "business_name": "Other Cafe"}, bundle)
    # Key-shape violations.
    assert not sre.validate_draft_params({"customer_name": "Asha"}, bundle)
    assert not sre.validate_draft_params({**good, "extra": "x"}, bundle)
    assert not sre.validate_draft_params("not-a-dict", bundle)
    assert not sre.validate_draft_params({**good, "customer_name": ""}, bundle)
    assert not sre.validate_draft_params({**good, "business_name": 41}, bundle)

    # PII guard outranks grounding: even a LITERALLY-grounded phone-shaped value is rejected.
    phone_bundle = sre.CustomerFactBundle(
        customer_id=uuid4(),
        display_name="+91 98765 43210",
        days_since_last_sale=41,
        last_sale_amount_paise=30000,
        lifetime_spend_paise=80000,
        business_name="Test Cafe",
    )
    assert not sre.validate_draft_params(
        {"customer_name": "+91 98765 43210", "business_name": "Test Cafe"}, phone_bundle
    )


# --- execute_item ------------------------------------------------------------------


@requires_db
def test_execute_item_persists_drafts_and_arms(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """The happy path: 2 candidates → 1 grounded draft (the fabricated one is DROPPED and
    counted), batch persisted awaiting_approval, drafts drafted with bundle-literal params,
    the arm spy called once with IDs + counters only."""
    monkeypatch.setattr(sre, "MARKETING_CONSENT_VERSIONS", frozenset({_TEST_CONSENT_VERSION}))
    scenario = _seed_lapsed_scenario(substrate.dsn)
    work_item = _seed_work_item(substrate.dsn, scenario.tenant)

    llm = _EchoLLM(corrupt_calls={2})  # candidate #2 ('Vikram') comes back ungrounded
    arm = _ArmSpy()
    agent = sre.SalesRecoveryAgent(llm=llm, arm_fn=arm)
    ctx = AgentItemContext(
        tenant_id=str(scenario.tenant),
        item_id="roadmap-item-1",
        agent="sales_recovery",
        work_item_id=str(work_item),
        run_id=str(uuid4()),
    )

    result = agent.execute_item(ctx)

    assert result.work_item_status == "awaiting_approval"
    assert result.counters == {"drafted": 1, "dropped_ungrounded": 1}
    assert len(llm.calls) == 2
    assert llm.calls[0][1] == "claude-haiku-4-5", "non-production env must resolve the test slot"

    batches = _read_batches(substrate.dsn, scenario.tenant)
    assert len(batches) == 1
    batch = batches[0]
    assert str(batch["id"]) == result.batch_id
    assert batch["status"] == "awaiting_approval"
    assert batch["work_item_id"] == work_item
    assert batch["agent"] == "sales_recovery"

    drafts = _read_drafts(substrate.dsn, scenario.tenant, batch["id"])
    assert len(drafts) == 1, "the ungrounded draft must be dropped, not persisted"
    draft = drafts[0]
    assert draft["customer_id"] == scenario.a2
    assert draft["template_name"] == sre.WINBACK_TEMPLATE_NAME
    assert draft["status"] == "drafted"
    # VT-384 (registry-as-canon): params now (customer_name, business_name) — business_name is the
    # tenant's business_name (_seed_lapsed_scenario seeds name="VT-369 sre detection").
    assert draft["params"] == {"customer_name": "Asha", "business_name": "VT-369 sre detection"}

    assert arm.calls == [
        (
            str(scenario.tenant),
            ctx.run_id,
            result.batch_id,
            {"drafted": 1, "dropped_ungrounded": 1},
        )
    ]


@requires_db
def test_execute_item_no_candidates(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """Zero DETECTION candidates → cancelled + counted; no LLM call, no arm, no batch row.

    VT-421: the tenant is onboarded-eligible (so it passes the detect-side onboarded gate and
    REACHES detection — the gate-A leg needs ≥1 customer), but its one customer has NO sale ledger
    and no marketing consent, so detection returns [] → skipped_no_candidates (NOT
    skipped_not_onboarded). This proves the path past the gate, into an empty detect."""
    monkeypatch.setattr(sre, "MARKETING_CONSENT_VERSIONS", frozenset({_TEST_CONSENT_VERSION}))
    t = _new_tenant(substrate.dsn, name="VT-369 sre no candidates")
    # One non-qualifying customer (no sales, no consent) — satisfies the onboarded gate's
    # ≥1-customer leg without producing a detection candidate.
    _seed_customer(substrate.dsn, t, display_name="Inactive", phone=f"+9197{uuid4().int % 10**8:08d}")
    agent = sre.SalesRecoveryAgent(llm=_forbidden_llm, arm_fn=_forbidden_arm)
    ctx = AgentItemContext(
        tenant_id=str(t),
        item_id="roadmap-item-1",
        agent="sales_recovery",
        work_item_id=str(uuid4()),
        run_id=str(uuid4()),
    )

    result = agent.execute_item(ctx)

    assert result.work_item_status == "cancelled"
    assert result.counters == {"skipped_no_candidates": 1}
    assert result.batch_id is None
    assert _read_batches(substrate.dsn, t) == []


@requires_db
def test_execute_item_owner_inputs_gate_fail_closed(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """CL-425: tenants.owner_inputs=false → no detection result reaches the LLM, no arm, no
    batch — the gate trips before any PII-bearing transmit."""
    monkeypatch.setattr(sre, "MARKETING_CONSENT_VERSIONS", frozenset({_TEST_CONSENT_VERSION}))
    t = _new_tenant(substrate.dsn, name="VT-369 sre consent gate", owner_inputs=False)
    agent = sre.SalesRecoveryAgent(llm=_forbidden_llm, arm_fn=_forbidden_arm)
    ctx = AgentItemContext(
        tenant_id=str(t),
        item_id="roadmap-item-1",
        agent="sales_recovery",
        work_item_id=str(uuid4()),
        run_id=str(uuid4()),
    )

    result = agent.execute_item(ctx)

    assert result.work_item_status == "cancelled"
    assert result.counters == {"skipped_owner_inputs": 1}
    assert _read_batches(substrate.dsn, t) == []


@requires_db
def test_execute_item_arm_refusal_cancels_batch(substrate, monkeypatch):  # type: ignore[no-untyped-def]
    """Arm refusal (e.g. the one-open-per-tenant mutex) → the batch is cancelled and its drafts
    halted FAIL-CLOSED; the result reports approval_arm_failed (defer-to-next-sweep)."""
    monkeypatch.setattr(sre, "MARKETING_CONSENT_VERSIONS", frozenset({_TEST_CONSENT_VERSION}))
    scenario = _seed_lapsed_scenario(substrate.dsn)
    work_item = _seed_work_item(substrate.dsn, scenario.tenant)

    def _refusing_arm(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("an approval is already open for this tenant")

    agent = sre.SalesRecoveryAgent(llm=_EchoLLM(), arm_fn=_refusing_arm)
    ctx = AgentItemContext(
        tenant_id=str(scenario.tenant),
        item_id="roadmap-item-1",
        agent="sales_recovery",
        work_item_id=str(work_item),
        run_id=str(uuid4()),
    )

    result = agent.execute_item(ctx)

    assert result.work_item_status == "cancelled"
    assert result.counters == {
        "drafted": 2,
        "dropped_ungrounded": 0,
        "approval_arm_failed": 1,
    }
    batches = _read_batches(substrate.dsn, scenario.tenant)
    assert len(batches) == 1
    assert batches[0]["status"] == "cancelled"
    drafts = _read_drafts(substrate.dsn, scenario.tenant, batches[0]["id"])
    assert drafts and all(d["status"] == "halted" for d in drafts)
    assert all(d["skip_reason"] == "approval_arm_failed" for d in drafts)


# --- CRITICAL-2: structural no-sender posture --------------------------------------


def test_no_sender_structural() -> None:
    """The executor is a plain function-call LLM with NO tool surface. Three pins:
    (1) the declared tool surface is the empty tuple and it runs through the guardrail at
    import; (2) the module source names no forbidden sender capability and never touches the
    Twilio util; (3) a FRESH import pulls no sender/Twilio module into sys.modules."""
    assert sre.AGENT_TOOLS == (), "the executor must hold NO tools (CRITICAL-2)"

    source = Path(sre.__file__).read_text(encoding="utf-8")
    assert "assert_agent_tools_safe(AGENT_TOOLS" in source, (
        "the empty tool surface must be pinned through the guardrail at import"
    )
    sender_substrings = [s for s in FORBIDDEN_CAPABILITY_SUBSTRINGS if s.startswith("send_")]
    assert sender_substrings, "guardrail must define sender capability substrings"
    for sub in sender_substrings:
        assert sub not in source, f"executor source references sender capability {sub!r}"
    assert "twilio" not in source.lower(), "executor source must never touch the Twilio util"

    code = (
        "import sys\n"
        "import orchestrator.agents.sales_recovery_executor\n"
        "bad = [m for m in sys.modules\n"
        "       if m.startswith('orchestrator') and ('twilio' in m or '.send_' in m)]\n"
        "assert not bad, f'fresh import pulled sender modules: {bad}'\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=False
    )
    assert proc.returncode == 0, f"fresh-import no-sender guard failed:\n{proc.stderr}"
