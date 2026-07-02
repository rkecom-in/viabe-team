"""VT-566 — the flywheel's read-back leg.

Two layers:
  * PURE render/tier tests (no DB) — ``render_lessons_block`` branches on ``owner_feedback.tier``
    (CL-2026-07-02-implicit-feedback-weak-signal) and frames lessons as reasoning input, not a
    script (CL-2026-07-01-no-fixed-playbook).
  * REALDB reader tests (live Postgres, RLS-enforced) — a captured owner verdict is retrieval-
    eligible AT CAPTURE and comes back through ``get_recent_lessons``; the loop-closure assertion
    proves a reject captured in run N renders into the manager's context on the next build.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

# ---------------------------------------------------------------------------
# PURE render/tier tests (no DB)
# ---------------------------------------------------------------------------


def test_render_returns_none_when_empty():
    from orchestrator.agents.lesson_readback import render_lessons_block

    assert render_lessons_block([], []) is None


def test_render_corrections_are_authoritative_lessons():
    from orchestrator.agents.lesson_readback import render_lessons_block

    lessons = [
        {"kind": "reject", "verb": "rejected", "correction_text": "off-brand tone",
         "template_hint": "team_winback_simple"},
        {"kind": "edit", "verb": "needs_changes", "correction_text": "make it softer",
         "template_hint": "team_winback_simple"},
        {"kind": "approve", "verb": "approved", "correction_text": None,
         "template_hint": "team_welcome4"},
    ]
    block = render_lessons_block(lessons, [])
    assert block is not None
    assert "## Lessons from this owner" in block
    assert "[rejected · rejected] off-brand tone (on team_winback_simple)" in block
    assert "[corrected · needs_changes] make it softer (on team_winback_simple)" in block
    assert "[approved as-is] (on team_welcome4)" in block
    # No weak block when there are no implicit rows.
    assert "## Outcome signals (weak)" not in block


def test_render_framing_instructs_reasoning_not_compliance():
    """CL-2026-07-01-no-fixed-playbook: lessons inform judgement, they do not script it."""
    from orchestrator.agents.lesson_readback import render_lessons_block

    block = render_lessons_block(
        [{"kind": "reject", "verb": "rejected", "correction_text": "x", "template_hint": None}], []
    )
    assert "do not script it" in block
    assert "reason with them" in block


def test_render_explicit_owner_feedback_is_authoritative():
    from orchestrator.agents.lesson_readback import render_lessons_block

    outcomes = [
        {"tier": "emoji", "signal": "thumbs_up"},
        {"tier": "dashboard", "signal": "thumbs_down"},
    ]
    block = render_lessons_block([], outcomes)
    assert block is not None
    assert "## Lessons from this owner" in block
    assert "[owner feedback] thumbs_up" in block
    assert "[owner feedback] thumbs_down" in block
    # Explicit feedback is NOT weak — no weak block, no weak prefix.
    assert "## Outcome signals (weak)" not in block


def test_render_implicit_is_downweighted_and_excluded_from_lessons():
    """CL-2026-07-02-implicit-feedback-weak-signal: implicit rows render ONLY as a clearly-weak,
    outcome-derived line — never as a correction/lesson."""
    from orchestrator.agents.lesson_readback import render_lessons_block

    block = render_lessons_block([], [{"tier": "implicit", "signal": "thumbs_down"}])
    assert block is not None
    assert "## Outcome signals (weak)" in block
    assert "[weak signal — outcome-derived, not owner-stated] thumbs_down" in block
    # An implicit row must never surface as an owner lesson/feedback.
    assert "## Lessons from this owner" not in block
    assert "[owner feedback]" not in block


def test_render_tier_branch_separates_explicit_from_implicit():
    """The same signal string on different tiers renders in different blocks with different weight."""
    from orchestrator.agents.lesson_readback import render_lessons_block

    outcomes = [
        {"tier": "emoji", "signal": "thumbs_down"},      # explicit → owner feedback
        {"tier": "implicit", "signal": "thumbs_down"},   # implicit → weak
    ]
    block = render_lessons_block([], outcomes)
    assert "[owner feedback] thumbs_down" in block  # explicit, authoritative
    assert "[weak signal — outcome-derived, not owner-stated] thumbs_down" in block  # implicit, weak


# ---------------------------------------------------------------------------
# REALDB reader tests (live Postgres, RLS-enforced)
# ---------------------------------------------------------------------------

pytest.importorskip("psycopg")


@pytest.fixture(scope="module")
def pool():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set — read-back realdb tests skipped")
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


def _seed_tenant(pool) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"lr-{tid[:8]}"),
        )
    return tid


def _snapshot(template: str) -> dict:
    return {"drafts": [{"template_name": template, "params": {}}], "draft_count": 1,
            "captured": 1, "truncated": False}


def _capture(tid, *, agent, kind, verb, text=None, template="team_winback_simple",
             authority="owner", run_id=None) -> None:
    from orchestrator.agents.correction_store import record_correction
    from orchestrator.db import tenant_connection

    with tenant_connection(tid) as conn:
        record_correction(
            conn, tid, agent=agent, correction_kind=kind, decision_verb=verb,
            owner_feedback=text, run_id=run_id, proposal_snapshot=_snapshot(template),
            authority=authority,
        )


def _feedback(tid, *, tier, signal) -> None:
    from orchestrator.db import tenant_connection

    with tenant_connection(tid) as conn:
        conn.execute(
            "INSERT INTO owner_feedback (tenant_id, run_id, tier, signal) "
            "VALUES (%s, %s, %s, %s)",
            (tid, str(uuid4()), tier, signal),
        )


def test_owner_verdict_is_eligible_at_capture_and_reads_back(pool):
    """A first-party owner verdict is retrieval-eligible AT CAPTURE, so the reader returns it —
    the read-back leg is not permanently empty (the whole point of VT-566)."""
    from orchestrator.agents.correction_store import get_recent_lessons

    tid = _seed_tenant(pool)
    _capture(tid, agent="sales_recovery", kind="reject", verb="rejected", text="off-brand tone")
    lessons = get_recent_lessons(tid)
    assert len(lessons) == 1
    hit = lessons[0]
    assert hit["kind"] == "reject"
    assert "off-brand" in hit["correction_text"]
    assert hit["template_hint"] == "team_winback_simple"  # lifted from proposal_snapshot
    assert hit["authority"] == "owner"


def test_non_first_party_authority_stays_default_closed(pool):
    """A non-owner/vtr authority ('system') is NOT eligible at capture — it stays default-closed,
    so the reader excludes it (the contamination boundary the gate protects)."""
    from orchestrator.agents.correction_store import get_recent_lessons

    tid = _seed_tenant(pool)
    _capture(tid, agent="sales_recovery", kind="reject", verb="rejected",
             text="system-derived note", authority="system")
    assert get_recent_lessons(tid) == []


def test_agent_scope_filters_lane_but_none_reads_cross_lane(pool):
    """agent=<lane> scopes to that lane; agent=None returns the owner's lessons across all lanes
    (the manager holds the cross-functional context)."""
    from orchestrator.agents.correction_store import get_recent_lessons

    tid = _seed_tenant(pool)
    _capture(tid, agent="sales_recovery", kind="reject", verb="rejected", text="sr note")
    _capture(tid, agent="reactivation", kind="edit", verb="needs_changes", text="react note")

    all_lanes = get_recent_lessons(tid)  # agent=None
    assert {x["verb"] for x in all_lanes} == {"rejected", "needs_changes"}
    sr_only = get_recent_lessons(tid, agent="sales_recovery")
    assert [x["correction_text"] for x in sr_only] == ["sr note"]


def test_reader_is_tenant_scoped(pool):
    """RLS: a lesson captured for tenant A never leaks into tenant B's read-back."""
    from orchestrator.agents.correction_store import get_recent_lessons

    ta, tb = _seed_tenant(pool), _seed_tenant(pool)
    _capture(ta, agent="sales_recovery", kind="reject", verb="rejected", text="tenant-A only")
    assert get_recent_lessons(tb) == []
    assert any("tenant-A" in x["correction_text"] for x in get_recent_lessons(ta))


def test_outcome_signal_reader_returns_tier(pool):
    from orchestrator.agents.lesson_readback import get_recent_outcome_signals

    tid = _seed_tenant(pool)
    _feedback(tid, tier="implicit", signal="thumbs_down")
    _feedback(tid, tier="emoji", signal="thumbs_up")
    rows = get_recent_outcome_signals(tid)
    tiers = {r["tier"] for r in rows}
    assert tiers == {"implicit", "emoji"}


def test_loop_closure_captured_reject_renders_into_next_run_context(pool, monkeypatch):
    """THE loop-closure assertion: a reject captured in run N renders into the manager's ``##
    Lessons from this owner`` block on the NEXT build (run N+1) — capture → retrieve, proven."""
    pytest.importorskip("langchain_anthropic")
    pytest.importorskip("langgraph")
    from orchestrator.agent.dispatch import _build_manager_lessons_block

    monkeypatch.setenv("MANAGER_MEMORY_RETRIEVAL", "true")
    tid = _seed_tenant(pool)
    run_n = str(uuid4())
    # run N: the owner rejects a draft; the verdict is captured.
    _capture(tid, agent="sales_recovery", kind="reject", verb="rejected",
             text="too pushy, soften it", run_id=run_n)

    # run N+1: the manager assembles its context and picks up the captured lesson.
    block = _build_manager_lessons_block(tid)
    assert block is not None
    assert "## Lessons from this owner" in block
    assert "too pushy, soften it" in block  # the run-N verdict steers the run-(N+1) context


def test_lessons_block_gated_off_even_with_captured_lessons(pool, monkeypatch):
    """Double gate: with the config flag OFF, nothing surfaces even though eligible lessons exist."""
    pytest.importorskip("langchain_anthropic")
    pytest.importorskip("langgraph")
    from orchestrator.agent import dispatch

    monkeypatch.delenv("MANAGER_MEMORY_RETRIEVAL", raising=False)
    tid = _seed_tenant(pool)
    _capture(tid, agent="sales_recovery", kind="reject", verb="rejected", text="present but gated")
    assert dispatch._build_manager_lessons_block(tid) is None
