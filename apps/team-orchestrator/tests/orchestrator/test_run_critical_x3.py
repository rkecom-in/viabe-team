"""VT-611 Package C — run_critical_x3.py.

Pure-logic tests (discovery, hashing, per-run clean check, cross-run consistency — no DB/network),
a realdb test for observe_route_and_grounded_count (mirrors test_convo_harness_db_asserts.py's
fixtures), and mocked orchestration tests for run_scenario_x3/main (cmd_setup/run_scenario_steps/
cmd_teardown stubbed — no real turn is ever driven here).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_CANARIES = Path(__file__).resolve().parents[2] / "canaries"
sys.path.insert(0, str(_CANARIES))

import convo_harness as ch  # noqa: E402
import run_critical_x3 as rx3  # noqa: E402


def _sr(label: str, run_id: str | None = "run-1") -> ch.StepResult:
    return ch.StepResult(
        ok=(label in ("PASS", "XFAIL")), xfail=(label == "XFAIL"), label=label, reasons=[],
        transcript=[ch.Turn(role="owner", text="hi"), ch.Turn(role="assistant", text="hello")],
        run_status="completed", ingress_reason="started", run_id=run_id,
    )


# --- discover_critical_scenarios -----------------------------------------------------------------


def test_discover_critical_scenarios_filters_and_sorts(tmp_path):
    (tmp_path / "b_critical.json").write_text(json.dumps({"name": "b", "critical": True}))
    (tmp_path / "a_noncritical.json").write_text(json.dumps({"name": "a", "critical": False}))
    (tmp_path / "c_critical.json").write_text(json.dumps({"name": "c", "critical": True}))
    (tmp_path / "d_unset.json").write_text(json.dumps({"name": "d"}))  # no "critical" key at all

    pairs = rx3.discover_critical_scenarios(tmp_path)
    names = [s["name"] for _p, s in pairs]
    assert names == ["b", "c"]  # sorted by filename (b_ before c_); a/d excluded


def test_discover_critical_scenarios_empty_dir(tmp_path):
    assert rx3.discover_critical_scenarios(tmp_path) == []


# --- transcript_hash / last_run_id ----------------------------------------------------------------


def test_transcript_hash_deterministic_and_content_sensitive():
    r1 = [_sr("PASS")]
    r2 = [_sr("PASS")]
    assert rx3.transcript_hash(r1) == rx3.transcript_hash(r2)

    r3 = [ch.StepResult(
        ok=True, xfail=False, label="PASS", reasons=[],
        transcript=[ch.Turn(role="owner", text="DIFFERENT"), ch.Turn(role="assistant", text="hello")],
        run_status="completed", ingress_reason="started", run_id="run-1",
    )]
    assert rx3.transcript_hash(r1) != rx3.transcript_hash(r3)


def test_last_run_id_finds_the_most_recent_non_none():
    results = [_sr("PASS", run_id="run-1"), _sr("PASS", run_id=None)]
    # the LAST step carries no run_id (e.g. an ingress rejection) -> fall back to the prior one.
    assert rx3.last_run_id(results) == "run-1"


def test_last_run_id_none_when_no_step_ever_got_one():
    results = [_sr("FAIL", run_id=None)]
    assert rx3.last_run_id(results) is None


# --- check_all_3_clean ----------------------------------------------------------------------------


def test_check_all_3_clean_passes_on_pass_and_xfail_only():
    assert rx3.check_all_3_clean([_sr("PASS"), _sr("XFAIL")]) == []


@pytest.mark.parametrize("bad_label", ["FAIL", "XPASS", "TIMEOUT"])
def test_check_all_3_clean_blocks_on_any_non_pass_xfail(bad_label):
    failures = rx3.check_all_3_clean([_sr("PASS"), _sr(bad_label)])
    assert failures and bad_label in failures[0]


# --- check_cross_run_consistency ------------------------------------------------------------------


def _obs(name, i, *, route="sales_recovery", count=8, outcome="completed"):
    return rx3.RunObservation(
        scenario_name=name, run_index=i, tenant_id=f"tenant-{i}", results=[_sr("PASS")],
        route=route, grounded_count=count, terminal_outcome=outcome, transcript_hash="h",
    )


def test_check_cross_run_consistency_passes_when_identical():
    runs = [_obs("s", 1), _obs("s", 2), _obs("s", 3)]
    assert rx3.check_cross_run_consistency(runs) == []


def test_check_cross_run_consistency_flags_route_divergence():
    runs = [_obs("s", 1, route="sales_recovery"), _obs("s", 2, route="none"), _obs("s", 3)]
    failures = rx3.check_cross_run_consistency(runs)
    assert any("route diverged" in f for f in failures)


def test_check_cross_run_consistency_flags_grounded_count_divergence():
    """The exact class B9 exists for: '8'/'a handful'/'~10' across 3 runs."""
    runs = [_obs("s", 1, count=8), _obs("s", 2, count=10), _obs("s", 3, count=8)]
    failures = rx3.check_cross_run_consistency(runs)
    assert any("grounded_count diverged" in f for f in failures)


def test_check_cross_run_consistency_flags_terminal_outcome_divergence():
    runs = [_obs("s", 1, outcome="completed"), _obs("s", 2, outcome="escalated"), _obs("s", 3)]
    failures = rx3.check_cross_run_consistency(runs)
    assert any("terminal_outcome diverged" in f for f in failures)


def test_check_cross_run_consistency_groups_by_scenario_independently():
    """A divergence in scenario A must not spuriously flag scenario B."""
    runs = [
        _obs("A", 1, route="sales_recovery"), _obs("A", 2, route="none"),
        _obs("B", 1, route="none"), _obs("B", 2, route="none"),
    ]
    failures = rx3.check_cross_run_consistency(runs)
    assert len(failures) == 1 and "A:" in failures[0]


# --- observe_route_and_grounded_count (realdb; skipped without DATABASE_URL via the fixture) ------


@pytest.fixture(scope="module")
def dsn():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    import apply_migrations

    url = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=url)
    assert not r["failed"], r["failed"]
    return url


def _new_tenant(dsn: str) -> str:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at) "
            "VALUES ('convo-harness-x3-test', 'founding', 'trial', now()) RETURNING id"
        ).fetchone()
    return str(row[0])


def _new_run(dsn: str, tenant_id: str) -> str:
    import psycopg
    from uuid import uuid4

    run_id = str(uuid4())
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status) VALUES (%s, %s, 'completed')",
            (run_id, tenant_id),
        )
    return run_id


def _new_campaign(dsn: str, tenant_id: str, run_id: str, cohort_size: int) -> str:
    import psycopg
    from psycopg.types.json import Jsonb
    from uuid import uuid4

    plan_json = {"target_cohort": {"cohort_size": cohort_size, "customer_ids": [str(uuid4())]}}
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO campaigns (tenant_id, run_id, plan_json, status, generated_at) "
            "VALUES (%s, %s, %s, 'proposed', now()) RETURNING id",
            (tenant_id, run_id, Jsonb(plan_json)),
        ).fetchone()
    return str(row[0])


def test_observe_route_and_grounded_count_none_when_no_run_id(dsn):
    import psycopg

    tenant = _new_tenant(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert rx3.observe_route_and_grounded_count(conn, tenant, None) == ("none", None)


def test_observe_route_and_grounded_count_none_when_no_campaign(dsn):
    import psycopg

    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert rx3.observe_route_and_grounded_count(conn, tenant, run_id) == ("none", None)


def test_observe_route_and_grounded_count_reads_cohort_size(dsn):
    import psycopg

    tenant = _new_tenant(dsn)
    run_id = _new_run(dsn, tenant)
    _new_campaign(dsn, tenant, run_id, cohort_size=8)
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert rx3.observe_route_and_grounded_count(conn, tenant, run_id) == ("sales_recovery", 8)


# --- orchestration (mocked — no real turn/DB is ever driven) --------------------------------------


@pytest.fixture()
def _stub_infra(monkeypatch):
    """Stub every real-I/O seam run_scenario_x3/main touch: _dsn/_ingress_base/_dev_secret/_connect
    (trivial), build_parser/cmd_setup/cmd_teardown (tenant provisioning), run_scenario_steps (the
    actual turn-driving loop). Returns a dict of call-tracking lists."""
    calls: dict[str, list] = {"setup": [], "teardown": [], "steps": []}

    monkeypatch.setattr(ch, "_dsn", lambda: "dsn")
    monkeypatch.setattr(ch, "_ingress_base", lambda url: "http://orch")
    monkeypatch.setattr(ch, "_dev_secret", lambda: "secret")
    monkeypatch.setattr(ch, "_connect", lambda dsn: MagicMock())

    def _fake_cmd_setup(ns):
        calls["setup"].append(ns)
        ns.tenant_id = f"tenant-{len(calls['setup'])}"
        return 0

    def _fake_cmd_teardown(ns):
        calls["teardown"].append(ns.tenant_id)
        return 0

    monkeypatch.setattr(ch, "cmd_setup", _fake_cmd_setup)
    monkeypatch.setattr(ch, "cmd_teardown", _fake_cmd_teardown)
    return calls


def test_run_scenario_x3_provisions_and_tears_down_3_fresh_tenants(monkeypatch, _stub_infra):
    monkeypatch.setattr(
        ch, "run_scenario_steps",
        lambda *a, **k: [_sr("PASS", run_id=None)],
    )
    monkeypatch.setattr(rx3, "observe_route_and_grounded_count", lambda conn, tid, rid: ("none", None))

    scenario = {"name": "s1", "setup_args": ["--onboarded"], "steps": [{"message": "hi"}]}
    obs = rx3.run_scenario_x3(
        Path("s1.json"), scenario, ingress_url=None, timeout=5.0, keep_tenants=False,
    )
    assert len(obs) == 3
    assert [o.tenant_id for o in obs] == ["tenant-1", "tenant-2", "tenant-3"]
    assert _stub_infra["teardown"] == ["tenant-1", "tenant-2", "tenant-3"]


def test_run_scenario_x3_keep_tenants_skips_teardown(monkeypatch, _stub_infra):
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])
    monkeypatch.setattr(rx3, "observe_route_and_grounded_count", lambda conn, tid, rid: ("none", None))

    scenario = {"name": "s1", "steps": [{"message": "hi"}]}
    rx3.run_scenario_x3(Path("s1.json"), scenario, ingress_url=None, timeout=5.0, keep_tenants=True)
    assert _stub_infra["teardown"] == []


def test_run_scenario_x3_tears_down_even_when_steps_raise(monkeypatch, _stub_infra):
    """A crash mid-run must not leak the synthetic tenant."""
    def _boom(*a, **k):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(ch, "run_scenario_steps", _boom)
    scenario = {"name": "s1", "steps": [{"message": "hi"}]}
    with pytest.raises(RuntimeError):
        rx3.run_scenario_x3(Path("s1.json"), scenario, ingress_url=None, timeout=5.0, keep_tenants=False)
    assert _stub_infra["teardown"] == ["tenant-1"]


def test_main_exit_0_when_all_clean_and_consistent(monkeypatch, tmp_path, _stub_infra):
    (tmp_path / "s1.json").write_text(json.dumps({
        "name": "s1", "critical": True, "steps": [{"message": "hi"}],
    }))
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])
    monkeypatch.setattr(rx3, "observe_route_and_grounded_count", lambda conn, tid, rid: ("none", None))

    rc = rx3.main(["--scenarios-dir", str(tmp_path)])
    assert rc == 0


def test_main_exit_1_when_a_run_fails(monkeypatch, tmp_path, _stub_infra):
    (tmp_path / "s1.json").write_text(json.dumps({
        "name": "s1", "critical": True, "steps": [{"message": "hi"}],
    }))
    calls = {"n": 0}

    def _flaky(*a, **k):
        calls["n"] += 1
        return [_sr("FAIL" if calls["n"] == 2 else "PASS", run_id=None)]

    monkeypatch.setattr(ch, "run_scenario_steps", _flaky)
    monkeypatch.setattr(rx3, "observe_route_and_grounded_count", lambda conn, tid, rid: ("none", None))

    rc = rx3.main(["--scenarios-dir", str(tmp_path)])
    assert rc == 1


# --- build_run_summary / --summary-json ------------------------------------------------------------


def test_build_run_summary_clean_and_consistent():
    runs = [_obs("s", 1), _obs("s", 2), _obs("s", 3)]
    summary = rx3.build_run_summary("s", runs)
    assert summary["scenario"] == "s"
    assert summary["consistent"] is True
    assert summary["consistency_failures"] == []
    assert len(summary["runs"]) == 3
    assert all(r["clean"] for r in summary["runs"])
    assert summary["runs"][0] == {
        "run_index": 1, "tenant_id": "tenant-1", "route": "sales_recovery", "grounded_count": 8,
        "terminal_outcome": "completed", "transcript_hash": "h", "clean": True, "block_reasons": [],
    }


def test_build_run_summary_flags_a_dirty_run_and_divergence():
    dirty = rx3.RunObservation(
        scenario_name="s", run_index=2, tenant_id="tenant-2", results=[_sr("FAIL")],
        route="none", grounded_count=None, terminal_outcome="completed", transcript_hash="h",
    )
    runs = [_obs("s", 1), dirty, _obs("s", 3)]
    summary = rx3.build_run_summary("s", runs)
    assert summary["consistent"] is False
    assert summary["consistency_failures"]  # route diverged (sales_recovery vs none)
    assert summary["runs"][1]["clean"] is False
    assert "FAIL" in summary["runs"][1]["block_reasons"][0]


def test_main_writes_summary_json(monkeypatch, tmp_path, _stub_infra):
    (tmp_path / "s1.json").write_text(json.dumps({
        "name": "s1", "critical": True, "steps": [{"message": "hi"}],
    }))
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])
    monkeypatch.setattr(rx3, "observe_route_and_grounded_count", lambda conn, tid, rid: ("none", None))

    summary_path = tmp_path / "summary.json"
    rc = rx3.main(["--scenarios-dir", str(tmp_path), "--summary-json", str(summary_path)])
    assert rc == 0
    written = json.loads(summary_path.read_text())
    assert len(written) == 1
    assert written[0]["scenario"] == "s1"
    assert len(written[0]["runs"]) == 3
    assert written[0]["consistent"] is True


def test_main_exit_1_on_cross_run_route_divergence(monkeypatch, tmp_path, _stub_infra):
    (tmp_path / "s1.json").write_text(json.dumps({
        "name": "s1", "critical": True, "steps": [{"message": "hi"}],
    }))
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])
    routes = iter(["sales_recovery", "none", "none"])
    monkeypatch.setattr(
        rx3, "observe_route_and_grounded_count", lambda conn, tid, rid: (next(routes), None)
    )

    rc = rx3.main(["--scenarios-dir", str(tmp_path)])
    assert rc == 1


def test_main_only_filters_to_the_named_scenario(monkeypatch, tmp_path, _stub_infra):
    (tmp_path / "s1.json").write_text(json.dumps({
        "name": "s1", "critical": True, "steps": [{"message": "hi"}],
    }))
    (tmp_path / "s2.json").write_text(json.dumps({
        "name": "s2", "critical": True, "steps": [{"message": "hi"}],
    }))
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])
    monkeypatch.setattr(rx3, "observe_route_and_grounded_count", lambda conn, tid, rid: ("none", None))

    rc = rx3.main(["--scenarios-dir", str(tmp_path), "--only", "s1"])
    assert rc == 0
    # only s1's tenants were provisioned (3), not s2's (would be 6 if both ran).
    assert len(_stub_infra["setup"]) == 3


def test_main_only_unknown_name_exits_2(tmp_path):
    rc = rx3.main(["--scenarios-dir", str(tmp_path), "--only", "nonexistent"])
    assert rc == 2


def test_main_writes_json_report_with_transcript_hash(monkeypatch, tmp_path, _stub_infra):
    (tmp_path / "s1.json").write_text(json.dumps({
        "name": "s1", "critical": True, "steps": [{"message": "hi"}],
    }))
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])
    monkeypatch.setattr(rx3, "observe_route_and_grounded_count", lambda conn, tid, rid: ("none", None))

    report_path = tmp_path / "bundle.json"
    rc = rx3.main(["--scenarios-dir", str(tmp_path), "--json-report", str(report_path)])
    assert rc == 0
    bundle = json.loads(report_path.read_text())
    assert len(bundle) == 3  # one entry per run
    names = [e["name"] for e in bundle]
    assert names == ["s1 [run 1/3]", "s1 [run 2/3]", "s1 [run 3/3]"]
    assert all("transcript_hash" in e for e in bundle)
