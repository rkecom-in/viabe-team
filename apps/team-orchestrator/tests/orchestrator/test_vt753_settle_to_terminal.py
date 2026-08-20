"""VT-753 — an assertion may claim a ROUTING verdict only after the turn reaches TERMINAL.

THE DEFECT THIS LOCKS OUT, stated as the failure it actually produced:

`_DB_ASSERT_SETTLE_S = 150.0` expired while the async chain was still working (MEASURED 382/416/373s,
VT-752). `assert_route` then read "no campaigns row" and the harness reported
`expected delegation ... observed route='none'` — a ROUTING claim manufactured from a LATENCY fact. In
the S1 gate (c) pass-1 re-drive that claim was FALSE for 3 of the 4 scenarios it flagged. The
retracted 24%/33%/40% delegation figures are very likely the same artifact.

Raising 150 to 900 would NOT have closed this, which is why these tests assert on the CONDITION and
never on a duration: a budget encodes a latency assumption that goes stale in silence, and the comment
above the old constant claimed "headroom" right up until it was 4x short. So:

- the settle polls the WORK (`manager_tasks.source_message_ref` -> PRODUCING) and stops at terminal;
- the ceiling is a hang-stop, and reaching it reports INDETERMINATE, never a routing failure;
- an unmeasured step is neither clean nor a miss.

The constants are monkeypatched down so these run in seconds. Every test states which VT-753 gate it
covers. The pure tests need no database; the SQL-shape tests at the bottom need DATABASE_URL.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

_CANARIES = Path(__file__).resolve().parents[2] / "canaries"
sys.path.insert(0, str(_CANARIES))

import convo_harness as ch  # noqa: E402 — after the sys.path insert
import run_full_pack as rfp  # noqa: E402


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch):
    """Scale the real constants down. The LOGIC under test is independent of their size — that is the
    entire point of the row — so the tests must be too."""
    monkeypatch.setattr(ch, "_DB_ASSERT_SETTLE_CEILING_S", 2.0)
    monkeypatch.setattr(ch, "_DB_ASSERT_SPAWN_GRACE_S", 0.5)
    monkeypatch.setattr(ch, "_DB_ASSERT_SETTLE_POLL_S", 0.05)


def test_a_tenant_wide_assert_waits_on_TENANT_WIDE_work_not_just_this_turns_task(monkeypatch):
    """VT-753 — the settle's scope must match the ASSERT's scope.

    `tenant_wide: true` exists precisely because the satisfying write may belong to a different run
    (campaign INSERTed on turn N, checked on turn N+1). So a tenant_wide assert must not wait only on
    THIS turn's task — it would stop while the work that would satisfy it is still running under
    another key. Caught on the first VT-753 re-drive scenario, whose step-0 asserts are all tenant_wide.
    """
    seen: list[str | None] = []

    def fake_in_flight(dsn, tenant_id, turn_sid, *, spawn_grace_left):
        seen.append(turn_sid)
        return False

    monkeypatch.setattr(ch, "_turn_work_in_flight", fake_in_flight)
    monkeypatch.setattr(ch, "_evaluate_db_asserts", lambda *a, **k: ["still failing"])

    ch._settle_db_asserts(
        "dsn://fake", "tenant-1", "run-1",
        {"assert_route": {"expect_sr_delegation": True, "tenant_wide": True}},
        turn_sid="SM-vt753", db_failures=["fail"],
    )
    assert seen == [None], (
        f"a tenant_wide assert must probe TENANT-WIDE (sid None), not this turn's sid: {seen}"
    )

    seen.clear()
    ch._settle_db_asserts(
        "dsn://fake", "tenant-1", "run-1",
        {"assert_route": {"expect_sr_delegation": True}},  # turn-scoped
        turn_sid="SM-vt753", db_failures=["fail"],
    )
    assert seen == ["SM-vt753"], (
        f"a turn-scoped assert must still wait on THIS turn's task only: {seen}"
    )


def _settle(monkeypatch, *, in_flight, evaluations):
    """Drive `_settle_db_asserts` with a scripted work-state probe and a scripted assert sequence.

    `in_flight` is a callable(call_index) -> bool. `evaluations` is a list of failure-lists consumed in
    order; the last entry repeats once exhausted. Returns (remaining, state, probe_calls, eval_calls).
    """
    counters = {"probe": 0, "eval": 0}

    def fake_in_flight(dsn, tenant_id, turn_sid, *, spawn_grace_left):
        i = counters["probe"]
        counters["probe"] += 1
        return in_flight(i)

    def fake_eval(dsn, tenant_id, run_id, step, *, turn_sid=None):
        i = counters["eval"]
        counters["eval"] += 1
        return list(evaluations[min(i, len(evaluations) - 1)])

    monkeypatch.setattr(ch, "_turn_work_in_flight", fake_in_flight)
    monkeypatch.setattr(ch, "_evaluate_db_asserts", fake_eval)

    remaining, state = ch._settle_db_asserts(
        "dsn://fake", "tenant-1", "run-1", {"assert_route": {"expect_sr_delegation": True}},
        turn_sid="SM-vt753", db_failures=["assert_route: expected delegation ..."],
    )
    return remaining, state, counters["probe"], counters["eval"]


# --- gate (a): a routing verdict is unreachable before terminal ---------------------------------


def test_a_routing_verdict_is_unreachable_while_the_work_is_still_producing(monkeypatch):
    """GATE (a). The assert fails on every read and the work never finishes: the settle must NOT
    hand back a verdict. Before this row that same situation returned a routing FAIL."""
    remaining, state, probes, evals = _settle(
        monkeypatch, in_flight=lambda i: True, evaluations=[["still failing"]],
    )
    assert state == "indeterminate", (
        "the work never reached terminal, so no routing verdict was available — returning anything "
        "other than INDETERMINATE here is the VT-753 defect verbatim"
    )
    assert remaining, "the failure list must survive so the report can show WHAT was unmeasured"
    assert probes >= 2 and evals >= 2, (
        f"the settle must actually poll rather than decide on one read (probes={probes}, evals={evals})"
    )


def test_the_settle_keeps_waiting_until_the_work_reports_terminal(monkeypatch):
    """GATE (a), the positive half: terminal is what ENDS the wait — not elapsed time."""
    remaining, state, probes, _ = _settle(
        monkeypatch,
        in_flight=lambda i: i < 3,  # producing for three probes, then terminal
        evaluations=[["fail"], ["fail"], ["fail"], []],
    )
    assert state == "terminal"
    assert probes == 4, f"expected to poll until the 4th probe reported terminal, polled {probes}"
    assert remaining == [], (
        "the re-read AFTER terminal is load-bearing: a task can flip to terminal in the same instant "
        "its campaign INSERT commits, and judging on the pre-flip read calls that a failure one poll early"
    )


def test_work_finished_and_the_assert_still_fails_is_a_REAL_verdict(monkeypatch):
    """The row must not turn every failure into 'unmeasured'. Terminal + still failing = the product
    had its chance and did not do the thing. That is a FAIL and must stay one."""
    remaining, state, _, _ = _settle(
        monkeypatch, in_flight=lambda i: False, evaluations=[["campaigns row absent"]],
    )
    assert state == "terminal"
    assert remaining == ["campaigns row absent"]


def test_a_late_landing_still_settles_clean_after_many_polls(monkeypatch):
    """The campaign lands late but before the ceiling: PASS, not a miss. Note this one alone does NOT
    discriminate the fix from the old budget (a landing inside ANY deadline was always found) — it is
    here to pin that settle-to-terminal did not break the ordinary late-but-fine case. The
    discriminating test is the one below."""
    remaining, state, _, evals = _settle(
        monkeypatch, in_flight=lambda i: True, evaluations=[["fail"]] * 12 + [[]],
    )
    assert state == "settled"
    assert remaining == []
    assert evals >= 13


def test_running_out_of_time_yields_NO_VERDICT_where_the_old_budget_yielded_a_FALSE_ONE(monkeypatch):
    """THE DISCRIMINATING TEST — the whole row in one assertion.

    Same input in both worlds: the work is still producing and the assert is still failing when time
    runs out. The old clock-only settle returned that as a verdict, so the harness printed
    `expected delegation ... observed route='none'` for a turn that had simply not finished — FALSE for
    3 of the 4 scenarios it flagged in the S1 pass-1 re-drive. The fix returns INDETERMINATE.

    Run this file with VT753_LEGACY_BUDGET=1 to watch it fail against the old behaviour; that is how
    the RED was demonstrated before the green was believed.
    """
    remaining, state, _, _ = _settle(
        monkeypatch, in_flight=lambda i: True, evaluations=[["campaigns row absent"]],
    )
    assert state != "terminal", (
        "running out of time is NOT a terminal state, and calling it one is how a latency fact became "
        "a routing claim. This assertion is the entire point of VT-753."
    )
    assert state == "indeterminate"
    assert remaining, "and the unmeasured asserts must still be reported, not swallowed"


# --- gate (b): the ceiling reports INDETERMINATE, bucketed apart ---------------------------------


def test_an_indeterminate_step_is_neither_clean_nor_counted_as_a_miss():
    """GATE (b). Both halves matter and they pull in opposite directions: unmeasured must not read as
    green (or timing out becomes a way to pass), and must not read as a defect (or the miss rate is
    inflated by exactly the artifact this row removes)."""
    def step(label):
        return ch.StepResult(
            ok=label in ("PASS", "XFAIL"), xfail=label == "XFAIL", label=label,
            reasons=[], transcript=[], run_status="completed", ingress_reason=None,
        )

    results = [step("PASS"), step("PASS"), step("INDETERMINATE")]

    findings = rfp.check_harness_clean(results)
    assert findings, "an INDETERMINATE step must NOT count as harness-clean"
    assert "INDETERMINATE" in findings[0]
    assert "UNMEASURED" in findings[0], (
        "the finding text must say it is unmeasured — a bare label count reads as a defect tally, "
        "which is how the withdrawn gate (c) number happened"
    )


def test_the_written_json_report_buckets_indeterminate_apart_from_failed(tmp_path):
    """GATE (b), machine-readable half — asserted on the ARTIFACT a classifier actually reads, not on
    the source text. The withdrawn gate (c) number came out of a script parsing these reports; if
    `failed` silently absorbed the unmeasured steps, the same blend would happen again."""
    def step(label):
        return ch.StepResult(
            ok=label in ("PASS", "XFAIL"), xfail=label == "XFAIL", label=label,
            reasons=[], transcript=[], run_status="completed", ingress_reason=None,
        )

    out = tmp_path / "report.json"
    scenario = {"name": "sr_probe", "steps": [{}, {}, {}], "domain": "sr_autonomy_rails"}
    rfp._write_json_report(
        str(out), "sr_probe", scenario, "tenant-1",
        [step("PASS"), step("FAIL"), step("INDETERMINATE")],
    )

    written = json.loads(out.read_text())
    entry = written[0] if isinstance(written, list) else written
    summary = entry["summary"]
    assert summary["indeterminate"] == 1, "the unmeasured bucket must reach the report"
    assert summary["failed"] == 1, (
        "exactly the ONE real failure — the INDETERMINATE step must not be folded in, or every "
        "miss-rate computed off this file inherits the artifact"
    )


# --- the probe's fail-soft DIRECTION -------------------------------------------------------------


def test_a_probe_error_settles_toward_waiting_never_toward_a_verdict(monkeypatch):
    """The single most dangerous line in the fix. `_tenant_has_producing_task` fail-softs to False
    because the late-reply sweep merely loses a no-op. Here False means 'the work is done, go ahead and
    declare a routing verdict' — so an error that returned False would MANUFACTURE the false verdict
    VT-753 exists to stop. Errors must return True: worst case is INDETERMINATE, never a wrong claim."""
    def boom(*a, **k):
        raise RuntimeError("pooler ate the connection")

    monkeypatch.setattr(ch, "_connect", boom)
    assert ch._turn_work_in_flight("dsn://x", "t1", "SM-1", spawn_grace_left=False) is True
    # And the neighbouring probe keeps its own (correct, opposite) direction — proving the two are
    # deliberately different rather than one of them being an oversight.
    assert ch._tenant_has_producing_task("dsn://x", "t1") is False


# --- SQL shape, against a live Postgres ----------------------------------------------------------

_needs_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set — VT-753 SQL tests skipped"
)


@pytest.fixture(scope="module")
def dsn():
    pytest.importorskip("psycopg")
    import apply_migrations

    url = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=url)
    assert not r["failed"], r["failed"]
    return url


def _tenant_with_task(dsn: str, *, sid: str | None, status: str | None) -> str:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        tenant_id = str(
            conn.execute(
                "INSERT INTO tenants (business_name, plan_tier, phase) "
                "VALUES ('vt753 settle probe', 'founding', 'trial') RETURNING id"
            ).fetchone()[0]
        )
        if status is not None:
            conn.execute(
                # objective is jsonb, not text — the probe never reads it, but the NOT NULL is real.
                "INSERT INTO manager_tasks (tenant_id, objective, status, attempt, max_attempts, "
                " source_message_ref) VALUES (%s, %s::jsonb, %s, 0, 3, %s)",
                (tenant_id, '{"goal": "vt753 settle probe"}', status, sid),
            )
    return tenant_id


@_needs_db
@pytest.mark.integration
def test_the_probe_reads_a_producing_task_for_THIS_turn_as_in_flight(dsn):
    """`_PRODUCING_TASK_STATUSES` scoped by source_message_ref — the same key `_campaign_id_for_run`
    follows from a turn to its async dispatch run. If these two ever disagree on the join, the settle
    waits on one task while the assert reads another."""
    sid = f"SM-vt753-{uuid4().hex[:10]}"
    tenant_id = _tenant_with_task(dsn, sid=sid, status="running")
    assert ch._turn_work_in_flight(dsn, tenant_id, sid, spawn_grace_left=False) is True


@_needs_db
@pytest.mark.integration
def test_a_task_parked_for_the_owner_is_TERMINAL_for_settle_purposes(dsn):
    """`waiting_owner` means the draft ALREADY emitted and the task is parked pending a human. Waiting
    on it would hang every approval-gate scenario to the ceiling and report the whole SR pack
    INDETERMINATE — which would trade a false FAIL for a false 'unmeasured'."""
    sid = f"SM-vt753-{uuid4().hex[:10]}"
    tenant_id = _tenant_with_task(dsn, sid=sid, status="waiting_owner")
    assert ch._turn_work_in_flight(dsn, tenant_id, sid, spawn_grace_left=False) is False


@_needs_db
@pytest.mark.integration
def test_no_task_for_the_turn_is_in_flight_only_while_the_spawn_grace_holds(dsn):
    """dispatch_brain spawns out-of-band, so a zero-task read right after the turn means 'not yet'.
    Once the grace elapses a still-taskless turn genuinely is not delegating and the assert may
    decide — otherwise every true non-delegation would report INDETERMINATE instead of PASS."""
    sid = f"SM-vt753-{uuid4().hex[:10]}"
    tenant_id = _tenant_with_task(dsn, sid=sid, status=None)
    assert ch._turn_work_in_flight(dsn, tenant_id, sid, spawn_grace_left=True) is True
    assert ch._turn_work_in_flight(dsn, tenant_id, sid, spawn_grace_left=False) is False
