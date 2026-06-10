"""VT-368 Gap-4 — behavioral tests for the Gap-5 consume + Gap-6 edit seams
(``orchestrator.business_plan.seams``).

Load-bearing behaviours under test:

  - ``items_for_agent``: latest-plan filter by owning_agent + status, seq-ordered,
    shaped as ``RoadmapItem`` dataclasses; no plan yet → [];
  - ``report_item_status``: an agent advances ITS OWN item → a NEW immutable
    version (siblings copied verbatim, item_id stable, frozen fact_bundle carried
    forward, provenance origin=agent_status) — but PermissionError on another
    agent's item, checked SERVER-SIDE against the STORED owning_agent (the
    VT-293/294 IDOR lesson: never trust the caller's scope);
  - ``edit_roadmap_item``: a single-field VTR edit mints v+1 with diff_from_prev
    + the SAME frozen fact_bundle; an edit injecting an uncited number ("now 4.9
    stars" when the bundle says 4.2) is REJECTED by the re-ground against the
    frozen bundle (a VTR cannot smuggle a hallucination — validation NOT relaxed);
    disallowed patch keys / unknown item_ids are rejected before any write.

Pure contract checks (patch-key whitelist, closed enums) run without a DB — they
are rejected before any connection is opened. The stateful paths require a real
Postgres + the dbos stack and mirror the substrate pattern in
``tests/orchestrator/onboarding/test_journey.py``: migrations applied once, DBOS
launched so the ``tenant_connection`` pool exists, tenants seeded via a direct
service-role (BYPASSRLS) psycopg connection; the seams write through
``tenant_connection`` (the RLS'd app_role path); assertions read back via the
store contract + direct service-role SELECTs.
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

needs_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — VT-368 seams substrate tests skipped",
)


@pytest.fixture(scope="module")
def substrate():  # type: ignore[no-untyped-def]
    """Apply migrations + launch DBOS so the ``tenant_connection`` pool exists.
    Mirrors tests/orchestrator/onboarding/test_journey.py."""
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


# --- Seeding + readback helpers ---------------------------------------------


def _new_tenant(dsn: str, *, name: str = "VT-368 seams test") -> UUID:
    """Seed a tenant via a direct service-role (BYPASSRLS) connection."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, "
            "phase_entered_at, business_type, whatsapp_number) "
            "VALUES (%s, 'founding', 'trial', now(), 'restaurant', %s) RETURNING id",
            (name, f"+9197{uuid4().int % 10**8:08d}"),
        ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _plan_rows(dsn: str, tenant_id: UUID) -> list[dict[str, Any]]:
    """Every business_plan version row via a direct service-role SELECT (the
    append-only audit trail), oldest first. Default row_factory → tuple."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT version, roadmap_json, fact_bundle_json, generated_by "
            "FROM business_plan WHERE tenant_id = %s ORDER BY version",
            (str(tenant_id),),
        ).fetchall()
    return [
        {
            "version": r[0],
            "roadmap": list(r[1] or []),
            "fact_bundle": dict(r[2] or {}),
            "generated_by": r[3],
        }
        for r in rows
    ]


# --- Plan fixtures (grounding-clean against the frozen bundle) ---------------


def _bundle() -> dict[str, Any]:
    """The frozen facts every seeded version grounds on (4.2 is THE rating —
    the hostile-edit test injects an uncited 4.9 against it)."""
    return {
        "F1": {"key": "google_rating", "value": 4.2, "source": "platform_listings.google"},
        "F2": {"key": "review_count", "value": 57, "source": "platform_listings.google"},
        "F3": {"key": "category", "value": "restaurant", "source": "business_profile.category"},
    }


def _summary() -> dict[str, Any]:
    return {
        "text": "Rated 4.2 across 57 reviews on Google.",
        "text_hi": "Google पर 57 समीक्षाओं में 4.2 रेटिंग।",
        "cited_facts": ["F1", "F2"],
        "headline_metrics": {"google_rating": 4.2, "review_count": 57},
    }


def _item(
    seq: int,
    month: int,
    agent: str,
    status: str,
    *,
    item_id: str | None = None,
    objective: str = "Reply to every review this week",
    why: str = "Rating is 4.2 on Google with 57 reviews.",
    cited: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "item_id": item_id or str(uuid4()),
        "seq": seq,
        "month": month,
        "objective": objective,
        "why": why,
        "cited_facts": cited if cited is not None else ["F1", "F2"],
        "owning_agent": agent,
        "owner_action_needed": False,
        "owner_action": None,
        "owner_action_hi": None,
        "status": status,
        "provenance": {
            "origin": "llm_v1",
            "editor": None,
            "prev_version": None,
            "diff_from_prev": None,
        },
    }


def _seed_plan(tenant_id: UUID, roadmap: list[dict[str, Any]]) -> int:
    """Write v1 through the store contract (the RLS'd tenant_connection path)."""
    from orchestrator.business_plan import store

    return store.write_new_version(
        tenant_id,
        summary=_summary(),
        roadmap=roadmap,
        fact_bundle=_bundle(),
        generated_by="gap4_generator",
        model_id="claude-sonnet-4-6",
    )


# --- Pure contract checks (rejected BEFORE any DB connection) ----------------


def test_edit_rejects_disallowed_patch_keys_pure():
    """Identity/grounding/audit fields are NOT editable: item_id, seq,
    cited_facts, provenance (and arbitrary keys) raise ValueError up front."""
    from orchestrator.business_plan import seams

    for bad_key in ("item_id", "seq", "cited_facts", "provenance", "made_up"):
        with pytest.raises(ValueError, match="non-editable patch keys"):
            seams.edit_roadmap_item(
                uuid4(), str(uuid4()), {bad_key: "x"}, vtr_id="ops-1"
            )


def test_edit_rejects_empty_patch_pure():
    from orchestrator.business_plan import seams

    with pytest.raises(ValueError, match="empty patch"):
        seams.edit_roadmap_item(uuid4(), str(uuid4()), {}, vtr_id="ops-1")


def test_edit_rejects_invalid_patch_values_pure():
    """Contract-shape guards: closed enums, month 1..6 (bools excluded),
    objective ≤120 chars, owner_action_needed bool."""
    from orchestrator.business_plan import seams

    bad_patches: list[dict[str, Any]] = [
        {"status": "banana"},
        {"owning_agent": "world_domination"},
        {"month": 9},
        {"month": True},
        {"objective": "x" * 121},
        {"objective": "   "},
        {"owner_action_needed": "yes"},
    ]
    for patch in bad_patches:
        with pytest.raises(ValueError, match="invalid patch values"):
            seams.edit_roadmap_item(uuid4(), str(uuid4()), patch, vtr_id="ops-1")


def test_report_rejects_unknown_status_pure():
    from orchestrator.business_plan import seams

    with pytest.raises(ValueError, match="unknown status"):
        seams.report_item_status(
            uuid4(), str(uuid4()), "obliterated", agent="sales_recovery"
        )


def test_items_for_agent_rejects_off_enum_args_pure():
    from orchestrator.business_plan import seams

    with pytest.raises(ValueError, match="unknown owning_agent"):
        seams.items_for_agent(uuid4(), "not_an_agent")
    with pytest.raises(ValueError, match="unknown statuses"):
        seams.items_for_agent(uuid4(), "reputation", statuses=("accepted", "exploded"))


# --- Gap-5 consume: items_for_agent ------------------------------------------


@needs_db
def test_items_for_agent_filters_and_orders(substrate):  # type: ignore[no-untyped-def]
    """Only the requested agent's items in the requested statuses come back,
    seq-ASCENDING regardless of array order, as RoadmapItem dataclasses."""
    from orchestrator.business_plan import seams
    from orchestrator.business_plan.store import RoadmapItem

    tenant = _new_tenant(substrate.dsn, name="items filter+order")
    # Array deliberately OUT of seq order to prove the seam sorts.
    roadmap = [
        _item(3, 2, "sales_recovery", "accepted"),
        _item(1, 1, "sales_recovery", "in_progress"),
        _item(5, 4, "sales_recovery", "accepted"),
        _item(2, 1, "sales_recovery", "done"),
        _item(4, 3, "reputation", "accepted"),
    ]
    _seed_plan(tenant, roadmap)

    # Default statuses = (accepted, in_progress): done + other agents excluded.
    mine = seams.items_for_agent(tenant, "sales_recovery")
    assert [it.seq for it in mine] == [1, 3, 5], "must be seq-ascending"
    assert all(isinstance(it, RoadmapItem) for it in mine)
    assert all(it.owning_agent == "sales_recovery" for it in mine)
    assert {it.status for it in mine} <= {"accepted", "in_progress"}
    assert mine[0].item_id == roadmap[1]["item_id"], "shaped from the stored entry"

    # Explicit statuses override the default.
    done = seams.items_for_agent(tenant, "sales_recovery", statuses=("done",))
    assert [it.seq for it in done] == [2]

    # The other agent sees ONLY its own item.
    theirs = seams.items_for_agent(tenant, "reputation")
    assert [it.seq for it in theirs] == [4]

    # A status with no matches is empty, not an error.
    assert seams.items_for_agent(tenant, "reputation", statuses=("dropped",)) == []


@needs_db
def test_items_for_agent_no_plan_returns_empty(substrate):  # type: ignore[no-untyped-def]
    """A specialist agent polling before Gap-4 ever generated → [] (no error)."""
    from orchestrator.business_plan import seams

    tenant = _new_tenant(substrate.dsn, name="items no plan")
    assert seams.items_for_agent(tenant, "sales_recovery") == []


# --- Gap-5 consume: report_item_status ---------------------------------------


@needs_db
def test_report_item_status_advances_own_item(substrate):  # type: ignore[no-untyped-def]
    """An agent advances ITS item: a NEW version is minted with the one item's
    status + provenance changed, siblings copied verbatim, item_id stable, the
    frozen fact_bundle + summary carried forward, v1 untouched (append-only)."""
    from orchestrator.business_plan import seams, store

    tenant = _new_tenant(substrate.dsn, name="report own item")
    mine = _item(1, 1, "sales_recovery", "accepted")
    sibling = _item(2, 2, "reputation", "accepted")
    _seed_plan(tenant, [mine, sibling])

    new_version = seams.report_item_status(
        tenant, mine["item_id"], "in_progress", agent="sales_recovery"
    )
    assert new_version == 2

    latest = store.get_active_plan(tenant)
    assert latest is not None and latest.version == 2
    assert latest.generated_by == "sales_recovery"
    assert latest.fact_bundle == _bundle(), "frozen facts must carry forward unchanged"
    assert latest.summary == _summary(), "summary is copied, not regenerated"

    updated = next(i for i in latest.roadmap if i["item_id"] == mine["item_id"])
    assert updated["status"] == "in_progress"
    assert updated["item_id"] == mine["item_id"], "item_id stable across versions"
    assert updated["provenance"] == {
        "origin": "agent_status",
        "editor": "sales_recovery",
        "prev_version": 1,
        "diff_from_prev": {"status": ["accepted", "in_progress"]},
    }
    untouched = next(i for i in latest.roadmap if i["item_id"] == sibling["item_id"])
    assert untouched == sibling, "siblings must be copied verbatim"

    # Append-only: v1 still holds the OLD status (the table IS the audit log).
    rows = _plan_rows(substrate.dsn, tenant)
    assert [r["version"] for r in rows] == [1, 2]
    v1_item = next(i for i in rows[0]["roadmap"] if i["item_id"] == mine["item_id"])
    assert v1_item["status"] == "accepted", "v1 must be immutable"


@needs_db
def test_report_item_status_idor_other_agents_item(substrate):  # type: ignore[no-untyped-def]
    """THE IDOR case: the ownership check is server-side against the STORED
    owning_agent — another agent reporting on this item gets PermissionError
    and NOTHING is minted."""
    from orchestrator.business_plan import seams

    tenant = _new_tenant(substrate.dsn, name="report IDOR")
    item = _item(1, 1, "sales_recovery", "accepted")
    _seed_plan(tenant, [item])

    with pytest.raises(PermissionError, match="does not own"):
        seams.report_item_status(tenant, item["item_id"], "done", agent="reputation")

    rows = _plan_rows(substrate.dsn, tenant)
    assert [r["version"] for r in rows] == [1], "a refused report must mint nothing"
    assert rows[0]["roadmap"][0]["status"] == "accepted", "status must be untouched"


@needs_db
def test_report_item_status_unknown_item_keyerror(substrate):  # type: ignore[no-untyped-def]
    from orchestrator.business_plan import seams

    tenant = _new_tenant(substrate.dsn, name="report unknown item")
    _seed_plan(tenant, [_item(1, 1, "sales_recovery", "accepted")])

    with pytest.raises(KeyError):
        seams.report_item_status(
            tenant, str(uuid4()), "done", agent="sales_recovery"
        )

    # And with NO plan at all → KeyError too (nothing to locate).
    bare = _new_tenant(substrate.dsn, name="report no plan")
    with pytest.raises(KeyError):
        seams.report_item_status(bare, str(uuid4()), "done", agent="sales_recovery")


# --- Gap-6 edit: edit_roadmap_item --------------------------------------------


@needs_db
def test_edit_single_field_mints_new_version_with_diff(substrate):  # type: ignore[no-untyped-def]
    """A single-field VTR edit (month 2 → 4) mints v+1: diff_from_prev records
    exactly the changed field, provenance is vtr_edit by vtr:<id>, the frozen
    fact_bundle carries forward, the sibling and the item_id are untouched."""
    from orchestrator.business_plan import seams, store

    tenant = _new_tenant(substrate.dsn, name="edit single field")
    target = _item(1, 2, "sales_recovery", "accepted")
    sibling = _item(2, 3, "reputation", "accepted")
    _seed_plan(tenant, [target, sibling])

    new_version = seams.edit_roadmap_item(
        tenant, target["item_id"], {"month": 4}, vtr_id="ops-7"
    )
    assert new_version == 2

    latest = store.get_active_plan(tenant)
    assert latest is not None and latest.version == 2
    assert latest.generated_by == "vtr:ops-7"
    assert latest.fact_bundle == _bundle(), "the SAME frozen bundle must carry forward"

    edited = next(i for i in latest.roadmap if i["item_id"] == target["item_id"])
    assert edited["month"] == 4
    assert edited["provenance"] == {
        "origin": "vtr_edit",
        "editor": "vtr:ops-7",
        "prev_version": 1,
        "diff_from_prev": {"month": [2, 4]},
    }
    untouched = next(i for i in latest.roadmap if i["item_id"] == sibling["item_id"])
    assert untouched == sibling


@needs_db
def test_edit_grounded_text_change_passes_reground(substrate):  # type: ignore[no-untyped-def]
    """An objective edit that introduces NO new claim-bearing tokens passes the
    re-ground and mints a version (the guard rejects hallucinations, not edits)."""
    from orchestrator.business_plan import seams, store

    tenant = _new_tenant(substrate.dsn, name="edit benign objective")
    item = _item(1, 1, "reputation", "accepted")
    _seed_plan(tenant, [item])

    new_version = seams.edit_roadmap_item(
        tenant,
        item["item_id"],
        {"objective": "Ask regular customers to leave a fresh review"},
        vtr_id="ops-7",
    )
    assert new_version == 2
    latest = store.get_active_plan(tenant)
    assert latest is not None
    edited = latest.roadmap[0]
    assert edited["objective"] == "Ask regular customers to leave a fresh review"
    assert edited["provenance"]["diff_from_prev"] == {
        "objective": [item["objective"], "Ask regular customers to leave a fresh review"]
    }


@needs_db
def test_edit_injecting_uncited_rating_rejected(substrate):  # type: ignore[no-untyped-def]
    """THE re-ground case: the bundle says the rating is 4.2 — a VTR edit
    claiming 'now 4.9 stars' is an uncited number and MUST be rejected with the
    violation listed; nothing is minted. Validation is NOT relaxed for humans."""
    from orchestrator.business_plan import seams

    tenant = _new_tenant(substrate.dsn, name="edit hostile rating")
    item = _item(1, 1, "reputation", "accepted")
    _seed_plan(tenant, [item])

    with pytest.raises(ValueError, match="4.9") as excinfo:
        seams.edit_roadmap_item(
            tenant,
            item["item_id"],
            {"why": "Rating is now 4.9 stars on Google."},
            vtr_id="ops-7",
        )
    assert "grounding violation" in str(excinfo.value)

    rows = _plan_rows(substrate.dsn, tenant)
    assert [r["version"] for r in rows] == [1], "a rejected edit must mint nothing"
    assert rows[0]["roadmap"][0]["why"] == item["why"], "the stored why is untouched"


@needs_db
def test_edit_unknown_item_id_keyerror(substrate):  # type: ignore[no-untyped-def]
    from orchestrator.business_plan import seams

    tenant = _new_tenant(substrate.dsn, name="edit unknown item")
    _seed_plan(tenant, [_item(1, 1, "menu_pricing", "proposed")])

    with pytest.raises(KeyError):
        seams.edit_roadmap_item(tenant, str(uuid4()), {"month": 3}, vtr_id="ops-7")

    rows = _plan_rows(substrate.dsn, tenant)
    assert [r["version"] for r in rows] == [1]
