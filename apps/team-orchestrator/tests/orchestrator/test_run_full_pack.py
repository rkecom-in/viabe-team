"""VT-611 — run_full_pack.py.

Pure-logic tests (discovery, domain floors, per-run harness-clean check — no DB/network) and
mocked orchestration tests for run_one_scenario/main (cmd_setup/run_scenario_steps/cmd_teardown
stubbed — no real turn is ever driven here). Mirrors test_run_critical_x3.py's structure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

_CANARIES = Path(__file__).resolve().parents[2] / "canaries"
sys.path.insert(0, str(_CANARIES))

import convo_harness as ch  # noqa: E402
import run_full_pack as rfp  # noqa: E402


def _sr(label: str, run_id: str | None = "run-1") -> ch.StepResult:
    return ch.StepResult(
        ok=(label in ("PASS", "XFAIL")), xfail=(label == "XFAIL"), label=label, reasons=[],
        transcript=[ch.Turn(role="owner", text="hi"), ch.Turn(role="assistant", text="hello")],
        run_status="completed", ingress_reason="started", run_id=run_id,
    )


# --- discover_all_scenarios ------------------------------------------------------------------------


def test_discover_all_scenarios_no_critical_filter_and_sorts(tmp_path):
    (tmp_path / "b.json").write_text(json.dumps({"name": "b", "critical": True}))
    (tmp_path / "a.json").write_text(json.dumps({"name": "a", "critical": False}))
    (tmp_path / "c.json").write_text(json.dumps({"name": "c"}))  # no "critical" key at all

    pairs = rfp.discover_all_scenarios(tmp_path)
    names = [s["name"] for _p, s in pairs]
    assert names == ["a", "b", "c"]  # ALL of them, sorted by filename


def test_discover_all_scenarios_empty_dir(tmp_path):
    assert rfp.discover_all_scenarios(tmp_path) == []


# --- check_domain_floors ---------------------------------------------------------------------------


def _pair(domain: str) -> tuple[Path, dict]:
    return (Path(f"{domain}.json"), {"domain": domain})


def test_check_domain_floors_passes_when_every_floor_met():
    pairs = (
        [_pair("manager")] * 40 + [_pair("onboarding")] * 25
        + [_pair("integration")] * 25 + [_pair("sr_autonomy_rails")] * 30
    )
    assert rfp.check_domain_floors(pairs) == []


def test_check_domain_floors_flags_a_shortfall_without_reclassifying():
    pairs = (
        [_pair("manager")] * 40 + [_pair("onboarding")] * 23  # 2 short
        + [_pair("integration")] * 25 + [_pair("sr_autonomy_rails")] * 30
    )
    failures = rfp.check_domain_floors(pairs)
    assert len(failures) == 1
    assert "onboarding=23 < 25" in failures[0]


def test_check_domain_floors_flags_multiple_shortfalls():
    pairs = [_pair("manager")] * 10
    failures = rfp.check_domain_floors(pairs)
    # manager itself is short (10 < 40) PLUS onboarding/integration/sr_autonomy_rails all missing (0 < floor)
    assert len(failures) == 4


def test_check_domain_floors_ignores_domains_with_no_floor():
    """A stray/unexpected domain value must not crash the floor check — it's simply not counted
    against any floor (an author-classification issue, not this function's job to police)."""
    pairs = [_pair("manager")] * 40 + [_pair("something_else")] * 5
    failures = rfp.check_domain_floors(pairs)
    assert any("onboarding" in f for f in failures)
    assert not any("something_else" in f for f in failures)


# --- check_harness_clean ---------------------------------------------------------------------------


def test_check_harness_clean_passes_on_pass_and_xfail_only():
    assert rfp.check_harness_clean([_sr("PASS"), _sr("XFAIL")]) == []


def test_check_harness_clean_flags_fail():
    failures = rfp.check_harness_clean([_sr("PASS"), _sr("FAIL")])
    assert failures and "FAIL" in failures[0]


def test_check_harness_clean_flags_xpass():
    failures = rfp.check_harness_clean([_sr("XPASS")])
    assert failures and "XPASS" in failures[0]


def test_check_harness_clean_flags_timeout():
    failures = rfp.check_harness_clean([_sr("TIMEOUT")])
    assert failures and "TIMEOUT" in failures[0]


# --- orchestration (mocked — no real turn/DB is ever driven) ---------------------------------------


def _stub_infra(monkeypatch):
    calls: dict[str, list] = {"setup": [], "teardown": []}

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


def test_run_one_scenario_provisions_and_tears_down_one_tenant(monkeypatch):
    calls = _stub_infra(monkeypatch)
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])

    scenario = {"name": "s1", "setup_args": ["--onboarded"], "steps": [{"message": "hi"}]}
    tenant_id, results = rfp.run_one_scenario(
        Path("s1.json"), scenario, ingress_url=None, timeout=5.0, keep_tenants=False,
    )
    assert tenant_id == "tenant-1"
    assert len(results) == 1
    assert calls["teardown"] == ["tenant-1"]


def test_run_one_scenario_keep_tenants_skips_teardown(monkeypatch):
    calls = _stub_infra(monkeypatch)
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])

    scenario = {"name": "s1", "steps": [{"message": "hi"}]}
    rfp.run_one_scenario(Path("s1.json"), scenario, ingress_url=None, timeout=5.0, keep_tenants=True)
    assert calls["teardown"] == []


def test_run_one_scenario_tears_down_even_when_steps_raise(monkeypatch):
    calls = _stub_infra(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(ch, "run_scenario_steps", _boom)
    scenario = {"name": "s1", "steps": [{"message": "hi"}]}
    try:
        rfp.run_one_scenario(Path("s1.json"), scenario, ingress_url=None, timeout=5.0, keep_tenants=False)
        raise AssertionError("expected RuntimeError to propagate")
    except RuntimeError:
        pass
    assert calls["teardown"] == ["tenant-1"]


def test_main_exit_0_when_floors_met_and_all_clean(monkeypatch, tmp_path):
    calls = _stub_infra(monkeypatch)
    (tmp_path / "s1.json").write_text(json.dumps({
        "name": "s1", "domain": "manager", "steps": [{"message": "hi"}],
    }))
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])
    monkeypatch.setattr(rfp, "DOMAIN_FLOORS", {"manager": 1})  # only assert the floor this test seeds

    rc = rfp.main(["--scenarios-dir", str(tmp_path)])
    assert rc == 0
    assert calls["teardown"] == ["tenant-1"]


def test_main_exit_1_when_a_scenario_fails(monkeypatch, tmp_path):
    _stub_infra(monkeypatch)
    (tmp_path / "s1.json").write_text(json.dumps({
        "name": "s1", "domain": "manager", "steps": [{"message": "hi"}],
    }))
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("FAIL", run_id=None)])
    monkeypatch.setattr(rfp, "DOMAIN_FLOORS", {"manager": 1})

    rc = rfp.main(["--scenarios-dir", str(tmp_path)])
    assert rc == 1


def test_main_exit_1_when_domain_floor_missed_even_if_every_scenario_clean(monkeypatch, tmp_path):
    _stub_infra(monkeypatch)
    (tmp_path / "s1.json").write_text(json.dumps({
        "name": "s1", "domain": "manager", "steps": [{"message": "hi"}],
    }))
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])
    monkeypatch.setattr(rfp, "DOMAIN_FLOORS", {"manager": 2})  # need 2, only seeded 1

    rc = rfp.main(["--scenarios-dir", str(tmp_path)])
    assert rc == 1


def test_main_only_filters_to_named_scenario(monkeypatch, tmp_path):
    calls = _stub_infra(monkeypatch)
    (tmp_path / "s1.json").write_text(json.dumps({
        "name": "s1", "domain": "manager", "steps": [{"message": "hi"}],
    }))
    (tmp_path / "s2.json").write_text(json.dumps({
        "name": "s2", "domain": "manager", "steps": [{"message": "hi"}],
    }))
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])
    monkeypatch.setattr(rfp, "DOMAIN_FLOORS", {})  # floor check not the point of this test

    rc = rfp.main(["--scenarios-dir", str(tmp_path), "--only", "s2"])
    assert rc == 0
    assert len(calls["setup"]) == 1  # only s2 ran, not s1


def test_main_unknown_only_name_returns_2(monkeypatch, tmp_path):
    _stub_infra(monkeypatch)
    (tmp_path / "s1.json").write_text(json.dumps({"name": "s1", "steps": [{"message": "hi"}]}))
    rc = rfp.main(["--scenarios-dir", str(tmp_path), "--only", "nonexistent"])
    assert rc == 2


def test_main_writes_json_report(monkeypatch, tmp_path):
    _stub_infra(monkeypatch)
    (tmp_path / "s1.json").write_text(json.dumps({
        "name": "s1", "domain": "manager", "steps": [{"message": "hi"}],
    }))
    monkeypatch.setattr(ch, "run_scenario_steps", lambda *a, **k: [_sr("PASS", run_id=None)])
    monkeypatch.setattr(rfp, "DOMAIN_FLOORS", {"manager": 1})

    report_path = tmp_path / "bundle.json"
    rc = rfp.main(["--scenarios-dir", str(tmp_path), "--json-report", str(report_path)])
    assert rc == 0
    bundle = json.loads(report_path.read_text())
    assert len(bundle) == 1
    assert bundle[0]["name"] == "s1"
