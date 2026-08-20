"""VT-752 item 1 — the dispatch boundaries that emitted nothing.

A campaign plan is emitted at T+52s on deployed dev and the `campaigns` row lands at T+480s. The SR
agent's own model call inside that window is 11-18s, so ~185s is spent BEFORE the specialist starts —
in triage, the task mint, the durable-workflow handoff and the queue — and not one of those
transitions wrote anything.

WHY NOT `pipeline_steps`, checked rather than assumed: its rows are point-in-time events (both
writers set only `started_at`; 129 rows on dev in 24h, zero with `ended_at`), and every row needs a
`run_id` FK while **the gap spans runs** — the webhook run mints the task, and the durable workflow
that does the work has no run of its own. A per-run table cannot measure a cross-run handoff.

So the boundaries go to `tm_audit_log`, keyed by the `manager_task` id, which is stable across the
process and run boundary.

T0 is `manager_tasks.created_at`, NOT a prior boundary row — because `tm_audit_log` is write-only for
the app (migration 147 gives `app_role` an INSERT policy and nothing else). A read-back design would
have returned zero rows in production while passing every test run on a superuser DSN: silently no
numbers, from an instrument whose whole job is numbers. `test_the_read_back_design_would_have_been_
silently_dead` pins that, since it is the kind of thing that gets "simplified" back in later.

These tests pin what the measurement depends on: one clock (the server's — the marks are written from
different processes), correlation isolation, and instrumentation that can never break the path it
measures.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("psycopg")

import psycopg  # noqa: E402

from orchestrator.observability import stage_timing as st  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set — VT-752 tests skipped"
)


@pytest.fixture(scope="module")
def dsn():
    return os.environ["DATABASE_URL"]


@pytest.fixture
def substrate(_dbpool, dsn):
    """`tenant_connection` (and therefore emit_tm_audit) needs the graph substrate initialised —
    without it every write fail-softs to a warning and the assertions below would pass vacuously on
    an empty timeline."""
    from orchestrator import graph as graphmod

    graphmod.init_substrate(dsn)
    return dsn


@pytest.fixture
def tenant(dsn):
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase) "
            "VALUES ('vt752 stage timing', 'founding', 'trial') RETURNING id"
        ).fetchone()
    return str(row["id"])


def _task(dsn: str, tenant_id: str, *, age_seconds: int = 0) -> str:
    """A real manager_tasks row — T0 for every boundary on it. `age_seconds` backdates created_at so
    a known wait can be asserted rather than a tautological >= 0."""
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        row = conn.execute(
            "INSERT INTO manager_tasks (tenant_id, objective, status, created_at) "
            "VALUES (%s, %s::jsonb, 'clarifying', now() - make_interval(secs => %s)) RETURNING id",
            (tenant_id, '{"goal": "vt752 probe"}', age_seconds),
        ).fetchone()
    return str(row["id"])


@pytest.mark.integration
def test_a_timeline_is_recorded_across_the_stages(tenant, substrate):
    """The whole point: four marks written as the real path writes them (two from the webhook side,
    two from the durable side) come back as ONE ordered timeline for the task."""
    task_id = _task(substrate, tenant)
    for stage in ("task_minted", "workflow_start_requested", "workflow_picked_up", "specialist_dispatch"):
        st.mark_stage(tenant, stage, task_id=task_id)

    timeline = st.read_stage_timeline(tenant, task_id, dsn=substrate)
    assert [t["stage"] for t in timeline] == [
        "task_minted", "workflow_start_requested", "workflow_picked_up", "specialist_dispatch",
    ]


@pytest.mark.integration
def test_the_first_mark_has_no_elapsed_and_later_marks_do(tenant, substrate):
    """`elapsed_ms` answers "which stage is slow"; `total_ms` answers "what is the owner waiting
    through". The first boundary has neither, and saying so beats reporting a zero that reads like a
    measurement."""
    task_id = _task(substrate, tenant, age_seconds=185)  # the measured pre-specialist gap
    st.mark_stage(tenant, "task_minted", task_id=task_id)
    st.mark_stage(tenant, "workflow_picked_up", task_id=task_id)

    first, second = st.read_stage_timeline(tenant, task_id, dsn=substrate)
    assert first["elapsed_ms"] is None, "the first boundary has nothing to be elapsed FROM"
    assert second["prev_stage"] == "task_minted", "the interval must name what it is measured from"
    assert second["elapsed_ms"] is not None and second["elapsed_ms"] >= 0
    # total_ms is the number that answers "what is the owner waiting through" — it must reflect the
    # task's real age, not the age of the instrumentation call.
    for entry in (first, second):
        assert entry["total_ms"] >= 185_000, (
            f"total_ms={entry['total_ms']} ignores the task's actual created_at — the instrument is "
            "measuring itself"
        )


@pytest.mark.integration
def test_marks_for_DIFFERENT_tasks_never_bleed_into_each_others_intervals(tenant, substrate):
    """Two owners' asks are in flight at once on a real tenant. If the correlation filter leaked, a
    second task's first mark would inherit the first task's clock and report a fabricated wait."""
    a, b = _task(substrate, tenant), _task(substrate, tenant)
    st.mark_stage(tenant, "task_minted", task_id=a)
    st.mark_stage(tenant, "workflow_picked_up", task_id=a)
    st.mark_stage(tenant, "task_minted", task_id=b)

    only_b = st.read_stage_timeline(tenant, b, dsn=substrate)
    assert len(only_b) == 1
    assert only_b[0]["elapsed_ms"] is None, "task B's first mark inherited task A's timeline"


@pytest.mark.integration
def test_a_mark_can_be_correlated_by_message_sid_before_a_task_exists(tenant, substrate):
    """Triage marks a boundary before a task id exists; the inbound sid is the fallback key."""
    sid = f"SMharness{uuid4().hex}"
    st.mark_stage(tenant, "triage_start", message_sid=sid)
    timeline = st.read_stage_timeline(tenant, sid, dsn=substrate)
    assert len(timeline) == 1 and timeline[0]["stage"] == "triage_start"


def test_instrumentation_NEVER_raises_on_the_hot_path(monkeypatch):
    """The property that makes it safe to call from inside a DBOS workflow and a webhook handler.
    A stage mark that can throw is a latency instrument that causes outages."""
    from orchestrator.observability import tm_audit

    def _boom(**_kw):
        raise RuntimeError("audit table gone")

    monkeypatch.setattr(tm_audit, "emit_tm_audit", _boom)
    st.mark_stage(str(uuid4()), "workflow_picked_up", task_id=str(uuid4()))  # must not raise


def test_a_mark_with_no_correlation_key_is_skipped_not_written(monkeypatch):
    """An un-keyed mark cannot be joined to anything, so it would be a row that looks like data and
    measures nothing. Refuse it loudly in the log rather than write it."""
    from orchestrator.observability import tm_audit

    calls = []
    monkeypatch.setattr(tm_audit, "emit_tm_audit", lambda **kw: calls.append(kw))
    st.mark_stage(str(uuid4()), "orphan_stage")
    assert calls == []


def test_the_boundaries_are_wired_where_the_gap_is():
    """A stage-timing module nothing calls measures nothing (the VT-720 dead-fix class). Assert the
    four call sites exist on the two sides of the cross-run handoff."""
    import pathlib

    from orchestrator.manager import triage_seam, workflow

    tri = pathlib.Path(triage_seam.__file__).read_text()
    wf = pathlib.Path(workflow.__file__).read_text()
    assert 'mark_stage(\n                tenant_id, "task_minted"' in tri or '"task_minted"' in tri
    assert '"workflow_start_requested"' in tri, "the webhook side of the handoff is not marked"
    assert '"workflow_picked_up"' in wf, "the durable side of the handoff is not marked"
    assert '"specialist_dispatch"' in wf, "the specialist boundary is not marked"
    # The pickup mark must precede the loop, or it measures the first cycle instead of the handoff.
    assert wf.index('"workflow_picked_up"') < wf.index("while cycles < LIMIT_MAX_CYCLES")


@pytest.mark.integration
def test_the_read_back_design_would_have_been_silently_dead(tenant, substrate):
    """WHY T0 IS THE TASK ROW. `tm_audit_log` grants `app_role` INSERT and nothing else (mig 147),
    so an app-credentialled read of the boundaries it just wrote returns NOTHING. A design that
    derived each interval by reading the previous boundary would have reported `None` for every
    number in production while passing on a superuser DSN — an instrument that measures nothing,
    silently. Pinned because "just read the last row" is the obvious refactor to make later.
    """
    task_id = _task(substrate, tenant)
    st.mark_stage(tenant, "task_minted", task_id=task_id)

    with psycopg.connect(substrate, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        pol = conn.execute(
            "SELECT cmd FROM pg_policies WHERE tablename = 'tm_audit_log' AND 'app_role' = ANY(roles)"
        ).fetchall()
    cmds = {r["cmd"] for r in pol}
    assert cmds == {"INSERT"}, (
        f"tm_audit_log's app_role grants changed to {cmds} — if SELECT was added, the read-back "
        "design becomes viable and this note should be revisited rather than left as folklore"
    )
