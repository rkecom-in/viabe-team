"""VT-369 Gap-5 PR-1 — behavioral tests for ``orchestrator.agents.coordinator``.

No live LLM anywhere (the coordinator is deterministic by design; the specialist is a spy).
``DBOS.start_workflow`` is monkeypatched to a spy, so the sweep's dispatch contract is pinned
without DBOS workflow plumbing; ``agent_dispatch_workflow`` is exercised directly as the plain
function it is pre-registration.

Covered behaviours:
  - registry validation fail-loud: unknown keys / 'unassigned' / instance-name mismatch raise;
    the module-level ``_REGISTRY_SPEC`` keys are OWNING_AGENTS members (the import-time check);
  - sweep dispatches exactly ONE item for a tenant with accepted roadmap items (at-most-1 per
    tenant per sweep), INSERTs the ``agent_work_items`` row, advances the item
    ``accepted → in_progress`` via the Gap-4 seam, and starts the dispatch workflow with IDs only;
  - dedupe: an OPEN work item (the migration-125 partial unique) blocks a second dispatch;
  - CL-425 gate 3.5: ``owner_inputs`` absent → no dispatch AND no status write (item stays
    ``accepted``, no new business_plan version);
  - gate 3.7: any open ``pending_approvals`` row defers the tenant (``skipped_open_approval``);
  - ``AGENT_AUTONOMY_GLOBAL_FREEZE`` blocks all dispatch;
  - the dispatch workflow re-checks the CL-425 gate FAIL-CLOSED (cancels the work item, never
    invokes the agent), records a ``pipeline_runs`` row + the result status on success, and is
    fail-soft (an agent exception → work item 'failed', the workflow never raises).

DB substrate mirrors ``tests/orchestrator/business_plan/test_generator.py``: migrations applied
once, DBOS launched so the ``tenant_connection`` pool exists, tenants/plans seeded via the real
store + a direct service-role psycopg connection.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("dbos")

import psycopg  # noqa: E402 — after dependency skip guards

from orchestrator.agents import coordinator  # noqa: E402
from orchestrator.business_plan import store  # noqa: E402

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-369 coordinator substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so ``tenant_connection`` resolves a pool. Mirrors
    tests/orchestrator/business_plan/test_generator.py."""
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


def _new_tenant(dsn: str, *, owner_inputs: bool = True, name: str = "VT-369 coord test") -> UUID:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "business_type, whatsapp_number, owner_inputs) "
            "VALUES (%s, 'founding', 'trial', now(), 'restaurant', %s, %s) RETURNING id",
            (name, f"+9198{uuid4().int % 10**8:08d}", owner_inputs),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _seed_plan(tenant: UUID, owning_agents: list[str]) -> list[str]:
    """Seed v1 through the REAL store with one ``accepted`` item per entry. Returns item_ids."""
    roadmap = [
        {
            "item_id": str(uuid4()),
            "seq": i,
            "month": 1,
            "objective": f"Test objective {i}",
            "why": "test",
            "cited_facts": [],
            "owning_agent": agent,
            "owner_action_needed": False,
            "owner_action": None,
            "owner_action_hi": None,
            "status": "accepted",
            "provenance": {"origin": "llm_v1"},
        }
        for i, agent in enumerate(owning_agents, start=1)
    ]
    v = store.write_new_version(
        tenant,
        summary={"text": "s", "text_hi": "s", "cited_facts": [], "headline_metrics": {}},
        roadmap=roadmap,
        fact_bundle={},
        generated_by="test_seed",
    )
    assert v == 1
    return [r["item_id"] for r in roadmap]


def _q(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


def _item_statuses(tenant: UUID) -> dict[str, str]:
    plan = store.get_active_plan(tenant)
    assert plan is not None
    return {raw["item_id"]: raw["status"] for raw in plan.roadmap}


def _seed_open_approval(dsn: str, tenant: UUID) -> None:
    run_id = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'paused')",
            (run_id, str(tenant)),
        )
        conn.execute(
            "INSERT INTO pending_approvals (tenant_id, run_id, approval_type, summary, "
            "timeout_at) VALUES (%s, %s, 'other', 'test open approval', now() + interval '1h')",
            (str(tenant), run_id),
        )


# --- fakes / spies -------------------------------------------------------------


class _FakeAgent:
    """SpecialistAgent spy — records every ctx, replays a canned result or raises."""

    name = "sales_recovery"

    def __init__(
        self,
        result: coordinator.ItemExecutionResult | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.calls: list[coordinator.AgentItemContext] = []
        self.result = result or coordinator.ItemExecutionResult(
            work_item_status="awaiting_approval", batch_id=None, counters={"drafts": 2}
        )
        self.exc = exc

    def execute_item(self, ctx: coordinator.AgentItemContext) -> coordinator.ItemExecutionResult:
        self.calls.append(ctx)
        if self.exc is not None:
            raise self.exc
        return self.result


@pytest.fixture(autouse=True)
def _no_global_freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leaked freeze env must not silently no-op every other test."""
    monkeypatch.delenv(coordinator.GLOBAL_FREEZE_ENV, raising=False)


@pytest.fixture(autouse=True)
def log_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Spy on the coordinator's log_event binding (the real writer dispatches async — racy)."""
    calls: list[dict[str, Any]] = []

    def _spy(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(coordinator, "log_event", _spy)
    return calls


@pytest.fixture()
def wf_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, tuple[Any, ...]]]:
    """Replace DBOS.start_workflow with a spy — the sweep's dispatch is pinned without DBOS."""
    calls: list[tuple[Any, tuple[Any, ...]]] = []

    def _spy(fn: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append((fn, args))
        return SimpleNamespace(workflow_id=f"spy-{len(calls)}")

    monkeypatch.setattr(coordinator.DBOS, "start_workflow", _spy)
    return calls


# --- registry validation (no DB) ------------------------------------------------


def test_registry_key_validation_fail_loud() -> None:
    """Unknown keys and the 'unassigned' marker must die loudly, never dispatch."""
    coordinator._validate_registry_keys({"sales_recovery"})  # all real owners pass
    coordinator._validate_registry_keys(store.OWNING_AGENTS - {"unassigned"})
    with pytest.raises(RuntimeError, match="bogus_agent"):
        coordinator._validate_registry_keys({"bogus_agent"})
    with pytest.raises(RuntimeError, match="unassigned"):
        coordinator._validate_registry_keys({"unassigned"})


def test_registry_instance_name_mismatch_fail_loud() -> None:
    """A registered instance whose ``name`` differs from its key is a mis-wiring — fail-loud."""
    mismatched = _FakeAgent()
    mismatched.name = "reputation"  # registered under sales_recovery below
    with pytest.raises(RuntimeError, match="must match"):
        coordinator._validate_registry({"sales_recovery": mismatched})
    ok = _FakeAgent()
    coordinator._validate_registry({"sales_recovery": ok})  # aligned key/name passes


def test_registry_spec_keys_are_owning_agents() -> None:
    """The import-time check: every static spec key is a dispatchable OWNING_AGENTS member."""
    assert set(coordinator._REGISTRY_SPEC) <= store.OWNING_AGENTS - {"unassigned"}
    assert "sales_recovery" in coordinator._REGISTRY_SPEC


# --- sweep behaviours (DB substrate) ---------------------------------------------


@requires_db
def test_sweep_dispatches_one_item_and_advances_status(substrate, wf_spy):  # type: ignore[no-untyped-def]
    """A tenant with TWO accepted sales_recovery items → exactly ONE dispatch (the at-most-1
    rule), one agent_work_items row (status 'dispatched'), the dispatched item advanced to
    in_progress via the Gap-4 seam, the sibling untouched, and start_workflow called with IDs."""
    fake = _FakeAgent()
    tenant = _new_tenant(substrate.dsn, name="sweep dispatches one")
    item1, item2 = _seed_plan(tenant, ["sales_recovery", "sales_recovery"])

    summary = coordinator.run_coordinator_sweep_body(registry={"sales_recovery": fake})

    assert summary.global_freeze is False
    assert summary.dispatched >= 1
    mine = [c for c in wf_spy if c[1][0] == str(tenant)]
    assert len(mine) == 1, f"expected exactly one dispatch for this tenant; got {wf_spy}"
    fn, args = mine[0]
    assert fn is coordinator.agent_dispatch_workflow
    tid_arg, item_arg, agent_arg, work_item_arg = args
    assert (tid_arg, item_arg, agent_arg) == (str(tenant), item1, "sales_recovery")

    rows = _q(
        substrate.dsn,
        "SELECT id::text, item_id, agent, status FROM agent_work_items WHERE tenant_id = %s",
        (str(tenant),),
    )
    assert len(rows) == 1
    assert rows[0][0] == work_item_arg
    assert (rows[0][1], rows[0][2], rows[0][3]) == (item1, "sales_recovery", "dispatched")

    statuses = _item_statuses(tenant)
    assert statuses[item1] == "in_progress", "dispatch advances accepted → in_progress (seam)"
    assert statuses[item2] == "accepted", "the at-most-1 rule leaves the sibling untouched"
    assert fake.calls == [], "the sweep itself never executes the agent (LLM lives downstream)"


@requires_db
def test_dedupe_open_work_item_blocks_second_dispatch(substrate, wf_spy):  # type: ignore[no-untyped-def]
    """An OPEN work item (partial unique, migration 125) → the next sweep skips, no 2nd row."""
    fake = _FakeAgent()
    tenant = _new_tenant(substrate.dsn, name="dedupe open work item")
    _seed_plan(tenant, ["sales_recovery"])

    first = coordinator.kick_coordinator(tenant, registry={"sales_recovery": fake})
    assert first.dispatched == 1
    second = coordinator.kick_coordinator(tenant, registry={"sales_recovery": fake})

    assert second.dispatched == 0
    assert second.skipped_open_work_item == 1
    assert len(wf_spy) == 1, "no second start_workflow for a deduped item"
    rows = _q(
        substrate.dsn,
        "SELECT count(*) FROM agent_work_items WHERE tenant_id = %s",
        (str(tenant),),
    )
    assert rows[0][0] == 1


@requires_db
def test_skip_no_owner_inputs_no_dispatch_no_status_write(substrate, wf_spy):  # type: ignore[no-untyped-def]
    """CL-425 gate 3.5: owner_inputs=false → counted skip, NO dispatch, NO status write (the
    item stays accepted and no new business_plan version is minted)."""
    fake = _FakeAgent()
    tenant = _new_tenant(substrate.dsn, owner_inputs=False, name="no owner_inputs")
    (item,) = _seed_plan(tenant, ["sales_recovery"])

    summary = coordinator.kick_coordinator(tenant, registry={"sales_recovery": fake})

    assert summary.skipped_no_owner_inputs == 1
    assert summary.dispatched == 0
    assert wf_spy == []
    assert _item_statuses(tenant)[item] == "accepted"
    versions = _q(
        substrate.dsn, "SELECT count(*) FROM business_plan WHERE tenant_id = %s", (str(tenant),)
    )
    assert versions[0][0] == 1, "a consent skip must mint NO new plan version (no status write)"
    rows = _q(
        substrate.dsn,
        "SELECT count(*) FROM agent_work_items WHERE tenant_id = %s",
        (str(tenant),),
    )
    assert rows[0][0] == 0


@requires_db
def test_skip_open_approval_defers_tenant(substrate, wf_spy):  # type: ignore[no-untyped-def]
    """Gate 3.7 (queue serialization, plan §4.1): ANY open pending_approvals row defers the
    tenant to the next sweep — no dispatch, no status write."""
    fake = _FakeAgent()
    tenant = _new_tenant(substrate.dsn, name="open approval defers")
    (item,) = _seed_plan(tenant, ["sales_recovery"])
    _seed_open_approval(substrate.dsn, tenant)

    summary = coordinator.kick_coordinator(tenant, registry={"sales_recovery": fake})

    assert summary.skipped_open_approval == 1
    assert summary.dispatched == 0
    assert wf_spy == []
    assert _item_statuses(tenant)[item] == "accepted"


@requires_db
def test_global_freeze_env_blocks_all_dispatch(substrate, wf_spy, monkeypatch):  # type: ignore[no-untyped-def]
    """AGENT_AUTONOMY_GLOBAL_FREEZE set → the sweep and the kick both dispatch nothing."""
    fake = _FakeAgent()
    tenant = _new_tenant(substrate.dsn, name="global freeze")
    _seed_plan(tenant, ["sales_recovery"])
    monkeypatch.setenv(coordinator.GLOBAL_FREEZE_ENV, "1")

    kicked = coordinator.kick_coordinator(tenant, registry={"sales_recovery": fake})
    swept = coordinator.run_coordinator_sweep_body(registry={"sales_recovery": fake})

    assert kicked.global_freeze is True and kicked.dispatched == 0
    assert swept.global_freeze is True and swept.dispatched == 0
    assert swept.tenants_scanned == 0, "the freeze short-circuits before any tenant scan"
    assert wf_spy == []
    rows = _q(
        substrate.dsn,
        "SELECT count(*) FROM agent_work_items WHERE tenant_id = %s",
        (str(tenant),),
    )
    assert rows[0][0] == 0


@requires_db
def test_unregistered_owner_counted_never_dispatched(substrate, wf_spy):  # type: ignore[no-untyped-def]
    """Items owned by an agent with no registry entry → counted skipped_no_agent, no error,
    no status write (plan §1.2)."""
    fake = _FakeAgent()
    tenant = _new_tenant(substrate.dsn, name="unregistered owner")
    (item,) = _seed_plan(tenant, ["reputation"])  # real owner, not in the registry

    summary = coordinator.kick_coordinator(tenant, registry={"sales_recovery": fake})

    assert summary.skipped_no_agent == 1
    assert summary.dispatched == 0
    assert wf_spy == []
    assert _item_statuses(tenant)[item] == "accepted"


# --- agent_dispatch_workflow behaviours (DB substrate) ----------------------------


def _dispatch(substrate: Any, wf_spy: list[Any], fake: _FakeAgent) -> tuple[UUID, str, str]:
    """Seed + kick once; return (tenant, item_id, work_item_id) from the spied dispatch."""
    tenant = _new_tenant(substrate.dsn, name="workflow test")
    (item,) = _seed_plan(tenant, ["sales_recovery"])
    summary = coordinator.kick_coordinator(tenant, registry={"sales_recovery": fake})
    assert summary.dispatched == 1
    (_, args) = wf_spy[-1]
    return tenant, item, args[3]


@requires_db
def test_workflow_rechecks_owner_inputs_fail_closed(substrate, wf_spy, monkeypatch):  # type: ignore[no-untyped-def]
    """Consent revoked between sweep and workflow → the re-check cancels the work item and the
    agent is NEVER invoked (fail-closed; frees the partial unique for a future re-dispatch)."""
    fake = _FakeAgent()
    tenant, item, work_item_id = _dispatch(substrate, wf_spy, fake)
    with psycopg.connect(substrate.dsn, autocommit=True) as conn:
        conn.execute("UPDATE tenants SET owner_inputs = false WHERE id = %s", (str(tenant),))
    monkeypatch.setattr(coordinator, "get_registry", lambda: {"sales_recovery": fake})

    result = coordinator.agent_dispatch_workflow(str(tenant), item, "sales_recovery", work_item_id)

    assert result["status"] == "skipped_no_owner_inputs"
    assert fake.calls == [], "the agent must never run without the CL-425 basis"
    rows = _q(
        substrate.dsn,
        "SELECT status FROM agent_work_items WHERE tenant_id = %s AND id = %s",
        (str(tenant), work_item_id),
    )
    assert rows[0][0] == "cancelled"


@requires_db
def test_workflow_executes_item_and_records(substrate, wf_spy, monkeypatch):  # type: ignore[no-untyped-def]
    """Happy path: the registry agent runs ONCE with an IDs-only ctx; the work item carries the
    reported status + run_id; a pipeline_runs row exists and closes; the output is IDs+counters."""
    fake = _FakeAgent()
    tenant, item, work_item_id = _dispatch(substrate, wf_spy, fake)
    monkeypatch.setattr(coordinator, "get_registry", lambda: {"sales_recovery": fake})

    result = coordinator.agent_dispatch_workflow(str(tenant), item, "sales_recovery", work_item_id)

    assert result["status"] == "awaiting_approval"
    assert result["counters"] == {"drafts": 2}
    assert set(result) == {"status", "work_item_id", "item_id", "run_id", "batch_id", "counters"}
    assert len(fake.calls) == 1
    ctx = fake.calls[0]
    assert (ctx.tenant_id, ctx.item_id, ctx.agent, ctx.work_item_id) == (
        str(tenant),
        item,
        "sales_recovery",
        work_item_id,
    )
    assert ctx.run_id == result["run_id"]

    rows = _q(
        substrate.dsn,
        "SELECT status, run_id::text FROM agent_work_items WHERE tenant_id = %s AND id = %s",
        (str(tenant), work_item_id),
    )
    assert rows[0] == ("awaiting_approval", result["run_id"])
    runs = _q(
        substrate.dsn,
        "SELECT run_type, status FROM pipeline_runs WHERE id = %s AND tenant_id = %s",
        (result["run_id"], str(tenant)),
    )
    assert runs[0] == ("agent_dispatch", "completed")


@requires_db
def test_workflow_fail_soft_marks_failed_never_raises(substrate, wf_spy, monkeypatch):  # type: ignore[no-untyped-def]
    """An executor exception → the work item lands 'failed' and the workflow RETURNS (one item's
    failure must not poison the queue). An invalid reported status is also recorded 'failed'."""
    boom = _FakeAgent(exc=RuntimeError("executor exploded"))
    tenant, item, work_item_id = _dispatch(substrate, wf_spy, boom)
    monkeypatch.setattr(coordinator, "get_registry", lambda: {"sales_recovery": boom})

    result = coordinator.agent_dispatch_workflow(str(tenant), item, "sales_recovery", work_item_id)

    assert result["status"] == "failed"
    rows = _q(
        substrate.dsn,
        "SELECT status FROM agent_work_items WHERE tenant_id = %s AND id = %s",
        (str(tenant), work_item_id),
    )
    assert rows[0][0] == "failed"

    # invalid reported status → recorded 'failed', not persisted verbatim
    bogus = _FakeAgent(
        result=coordinator.ItemExecutionResult(work_item_status="not_a_status")
    )
    tenant2, item2, work_item_id2 = _dispatch(substrate, wf_spy, bogus)
    monkeypatch.setattr(coordinator, "get_registry", lambda: {"sales_recovery": bogus})
    result2 = coordinator.agent_dispatch_workflow(
        str(tenant2), item2, "sales_recovery", work_item_id2
    )
    assert result2["status"] == "failed"
    rows2 = _q(
        substrate.dsn,
        "SELECT status FROM agent_work_items WHERE tenant_id = %s AND id = %s",
        (str(tenant2), work_item_id2),
    )
    assert rows2[0][0] == "failed"
