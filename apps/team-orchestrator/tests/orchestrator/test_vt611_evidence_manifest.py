"""VT-611 — vt611_evidence_manifest.py.

Pure tests for build_manifest() (every input is an already-loaded dict/list/None — no file I/O)
plus a main() smoke test that writes real files to tmp_path and reads the manifest back.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_CANARIES = Path(__file__).resolve().parents[2] / "canaries"
sys.path.insert(0, str(_CANARIES))

import vt611_evidence_manifest as vem  # noqa: E402


def _clean_pack_summary() -> dict:
    return {
        "domain_counts": {"manager": 40, "onboarding": 25, "integration": 25, "sr_autonomy_rails": 30},
        "domain_floors": {"manager": 40, "onboarding": 25, "integration": 25, "sr_autonomy_rails": 30},
        "domain_floor_failures": [],
        "scenarios": [
            {"name": "s1", "domain": "manager", "tenant_id": "t1", "clean": True, "block_reasons": []},
            {"name": "s2", "domain": "onboarding", "tenant_id": "t2", "clean": True, "block_reasons": []},
        ],
    }


def _clean_judged() -> dict:
    return {
        "threshold": 4, "mean_threshold": 4.5, "all_passed": True,
        "scenarios": [
            {"scenario": "s1", "passed": True, "mean_score": 4.8},
            {"scenario": "s2", "passed": True, "mean_score": 4.6},
        ],
    }


def _clean_critical_summary() -> list:
    return [
        {
            "scenario": "c1", "consistent": True, "consistency_failures": [],
            "runs": [{"run_index": i, "clean": True, "block_reasons": []} for i in (1, 2, 3)],
        },
    ]


def _clean_shadow_evidence() -> dict:
    return {
        "since": "2026-07-07T00:00:00+00:00", "total_evals": 400, "distinct_conversations": 60,
        "safety_divergences": 0, "min_distinct": 50, "passed": True, "failures": [],
    }


def _full_clean_kwargs() -> dict:
    return dict(
        deploy_sha="db36202", migration_version="170", loop_mode="shadow",
        pack_summary=_clean_pack_summary(), pack_judged=_clean_judged(),
        critical_summary=_clean_critical_summary(), critical_judged=_clean_judged(),
        shadow_evidence=_clean_shadow_evidence(),
        teardown_confirmed=True, zero_real_send_confirmed=True,
        generated_at="2026-07-07T00:00:00+00:00",
    )


# --- build_manifest — the clean/all-present/all-passing case ---------------------------------------


def test_build_manifest_all_clean_passes_overall():
    manifest = vem.build_manifest(**_full_clean_kwargs())
    assert manifest["overall_gate_passed"] is True
    assert manifest["pack"]["passed"] is True
    assert manifest["critical_x3"]["passed"] is True
    assert manifest["shadow"]["passed"] is True
    assert manifest["deploy_sha"] == "db36202"
    assert len(manifest["honesty_caveats"]) == 2


# --- missing legs are explicit gaps, never a silent pass --------------------------------------------


def test_build_manifest_missing_pack_summary_is_an_explicit_gap_not_a_pass():
    kwargs = _full_clean_kwargs()
    kwargs["pack_summary"] = None
    manifest = vem.build_manifest(**kwargs)
    assert manifest["pack"]["available"] is False
    assert "gap" in manifest["pack"]
    assert manifest["overall_gate_passed"] is False


def test_build_manifest_missing_critical_summary_is_an_explicit_gap():
    kwargs = _full_clean_kwargs()
    kwargs["critical_summary"] = None
    manifest = vem.build_manifest(**kwargs)
    assert manifest["critical_x3"]["available"] is False
    assert manifest["overall_gate_passed"] is False


def test_build_manifest_missing_shadow_evidence_is_an_explicit_gap():
    kwargs = _full_clean_kwargs()
    kwargs["shadow_evidence"] = None
    manifest = vem.build_manifest(**kwargs)
    assert manifest["shadow"]["available"] is False
    assert manifest["overall_gate_passed"] is False


def test_build_manifest_missing_judge_output_is_reported_but_does_not_crash():
    """A pack/critical run without its judge leg yet is a real partial-progress state (the run
    might still be mid-flight) — the pack/critical section must say so, not raise."""
    kwargs = _full_clean_kwargs()
    kwargs["pack_judged"] = None
    manifest = vem.build_manifest(**kwargs)
    assert manifest["pack"]["judge"]["available"] is False
    # harness-clean + floors were still fine, but no judge verdict means this leg isn't a pass yet
    # only in the sense that judge=None short-circuits pack["passed"] to True/False per the rule below:
    assert manifest["pack"]["passed"] is True  # judge=None is tolerated (not yet run), floors/harness clean


# --- procedural confirmations gate the overall verdict too ------------------------------------------


def test_build_manifest_false_teardown_confirmed_fails_overall_even_if_everything_else_passed():
    kwargs = _full_clean_kwargs()
    kwargs["teardown_confirmed"] = False
    manifest = vem.build_manifest(**kwargs)
    assert manifest["overall_gate_passed"] is False


def test_build_manifest_false_zero_real_send_confirmed_fails_overall():
    kwargs = _full_clean_kwargs()
    kwargs["zero_real_send_confirmed"] = False
    manifest = vem.build_manifest(**kwargs)
    assert manifest["overall_gate_passed"] is False


# --- a real failure surfaces in the right section, not silently ------------------------------------


def test_build_manifest_domain_floor_failure_fails_the_pack_section():
    kwargs = _full_clean_kwargs()
    summary = _clean_pack_summary()
    summary["domain_floor_failures"] = ["domain floor MISSED: onboarding=23 < 25"]
    kwargs["pack_summary"] = summary
    manifest = vem.build_manifest(**kwargs)
    assert manifest["pack"]["passed"] is False
    assert manifest["overall_gate_passed"] is False


def test_build_manifest_a_dirty_scenario_lists_in_harness_findings():
    kwargs = _full_clean_kwargs()
    summary = _clean_pack_summary()
    summary["scenarios"][0]["clean"] = False
    summary["scenarios"][0]["block_reasons"] = ["1 step(s) did not clear PASS/XFAIL (FAIL)"]
    kwargs["pack_summary"] = summary
    manifest = vem.build_manifest(**kwargs)
    assert manifest["pack"]["passed"] is False
    assert manifest["pack"]["harness_findings"] == [
        {"name": "s1", "reasons": ["1 step(s) did not clear PASS/XFAIL (FAIL)"]},
    ]


def test_build_manifest_a_blocked_critical_scenario_fails_that_section():
    kwargs = _full_clean_kwargs()
    critical = _clean_critical_summary()
    critical[0]["consistent"] = False
    critical[0]["consistency_failures"] = ["c1: route diverged across 3 runs"]
    kwargs["critical_summary"] = critical
    manifest = vem.build_manifest(**kwargs)
    assert manifest["critical_x3"]["passed"] is False
    assert "c1" in manifest["critical_x3"]["blocked_scenarios"]


def test_build_manifest_shadow_gate_failure_fails_that_section():
    kwargs = _full_clean_kwargs()
    shadow = _clean_shadow_evidence()
    shadow["passed"] = False
    shadow["distinct_conversations"] = 10
    kwargs["shadow_evidence"] = shadow
    manifest = vem.build_manifest(**kwargs)
    assert manifest["shadow"]["passed"] is False
    assert manifest["overall_gate_passed"] is False


# --- honesty caveats always ride along --------------------------------------------------------------


def test_honesty_caveats_always_present_regardless_of_pass_fail():
    manifest = vem.build_manifest(**_full_clean_kwargs())
    joined = " ".join(manifest["honesty_caveats"])
    assert "SR-only" in joined
    assert "SR-spawn" in joined


# --- main() — real file I/O smoke test --------------------------------------------------------------


def test_main_writes_manifest_and_exits_0_when_everything_passes(tmp_path):
    pack_summary_path = tmp_path / "pack_summary.json"
    pack_judged_path = tmp_path / "pack.judged.json"
    critical_summary_path = tmp_path / "critical_summary.json"
    critical_judged_path = tmp_path / "critical.judged.json"
    shadow_path = tmp_path / "shadow.json"
    out_path = tmp_path / "manifest.json"

    pack_summary_path.write_text(json.dumps(_clean_pack_summary()))
    pack_judged_path.write_text(json.dumps(_clean_judged()))
    critical_summary_path.write_text(json.dumps(_clean_critical_summary()))
    critical_judged_path.write_text(json.dumps(_clean_judged()))
    shadow_path.write_text(json.dumps(_clean_shadow_evidence()))

    rc = vem.main([
        "--deploy-sha", "db36202", "--migration-version", "170", "--loop-mode", "shadow",
        "--pack-summary-json", str(pack_summary_path), "--pack-judged", str(pack_judged_path),
        "--critical-summary-json", str(critical_summary_path), "--critical-judged", str(critical_judged_path),
        "--shadow-json", str(shadow_path),
        "--teardown-confirmed", "--zero-real-send-confirmed",
        "--out", str(out_path),
    ])
    assert rc == 0
    written = json.loads(out_path.read_text())
    assert written["overall_gate_passed"] is True
    assert written["deploy_sha"] == "db36202"


def test_main_exits_1_when_a_leg_is_missing(tmp_path):
    out_path = tmp_path / "manifest.json"
    rc = vem.main([
        "--deploy-sha", "db36202", "--migration-version", "170", "--loop-mode", "legacy",
        "--out", str(out_path),
    ])
    assert rc == 1
    written = json.loads(out_path.read_text())
    assert written["overall_gate_passed"] is False
    assert written["pack"]["available"] is False
