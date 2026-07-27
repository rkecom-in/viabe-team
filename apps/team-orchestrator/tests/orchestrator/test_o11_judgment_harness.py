"""VT-705 CLI contract tests; no model or network calls."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CANARIES = _ROOT / "canaries"
if str(_CANARIES) not in sys.path:
    sys.path.insert(0, str(_CANARIES))

import o11_judgment_harness as harness  # noqa: E402

from orchestrator.advice_eval import DatasetSplit, load_dataset  # noqa: E402

_DEV_DIR = _CANARIES / "o11" / "development"
_VAL_DIR = _CANARIES / "o11" / "validation"


def _response_bundle(path: Path, case_ids: list[str], *, knowledge_mode: str = "off") -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_label": "no-o8-baseline",
                "knowledge_mode": knowledge_mode,
                "agent_version": "git:test",
                "responses": [
                    {
                        "case_id": case_id,
                        "decision": "Use the verified facts, do not claim an effect, and ask for approval.",
                    }
                    for case_id in case_ids
                ],
            }
        ),
        encoding="utf-8",
    )


def test_validate_visible_partitions(capsys) -> None:
    exit_code = harness.main(
        [
            "validate",
            "--development-dir",
            str(_DEV_DIR),
            "--validation-dir",
            str(_VAL_DIR),
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PASS"
    assert payload["partitions"]["development"]["cases"] >= 3


def test_stub_score_writes_detailed_visible_report(tmp_path: Path) -> None:
    cases = load_dataset(_DEV_DIR, split=DatasetSplit.DEVELOPMENT)
    responses = tmp_path / "responses.json"
    output = tmp_path / "report.json"
    _response_bundle(responses, [case.case_id for case in cases])
    exit_code = harness.main(
        [
            "score",
            "--dataset-dir",
            str(_DEV_DIR),
            "--split",
            "development",
            "--responses",
            str(responses),
            "--judge",
            "stub",
            "--output",
            str(output),
            "--require-knowledge-mode",
            "off",
        ]
    )
    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["dataset_split"] == "development"
    assert len(payload["cases"]) == len(cases)
    assert payload["knowledge_mode"] == "off"


def test_incomplete_response_bundle_fails_loud(tmp_path: Path, capsys) -> None:
    responses = tmp_path / "responses.json"
    output = tmp_path / "report.json"
    _response_bundle(responses, ["not-a-real-case"])
    exit_code = harness.main(
        [
            "score",
            "--dataset-dir",
            str(_DEV_DIR),
            "--split",
            "development",
            "--responses",
            str(responses),
            "--judge",
            "stub",
            "--output",
            str(output),
        ]
    )
    assert exit_code == 2
    assert "coverage mismatch" in capsys.readouterr().err
    assert not output.exists()


def test_sealed_stub_is_rejected_before_scoring(tmp_path: Path, capsys) -> None:
    dataset = tmp_path / "sealed"
    dataset.mkdir()
    visible = json.loads(next(_DEV_DIR.glob("*.json")).read_text(encoding="utf-8"))
    visible.update(
        {
            "case_id": "sealed-opaque-one",
            "family_id": "sealed-family-one",
            "split": "sealed",
        }
    )
    (dataset / "case.json").write_text(json.dumps(visible), encoding="utf-8")
    responses = tmp_path / "responses.json"
    _response_bundle(responses, ["sealed-opaque-one"])
    exit_code = harness.main(
        [
            "score",
            "--dataset-dir",
            str(dataset),
            "--split",
            "sealed",
            "--responses",
            str(responses),
            "--judge",
            "stub",
            "--output",
            str(tmp_path / "out.json"),
        ]
    )
    assert exit_code == 2
    assert "real llm judge" in capsys.readouterr().err


def test_real_judge_missing_key_fails_never_skips(tmp_path: Path, capsys, monkeypatch) -> None:
    dataset = tmp_path / "sealed"
    dataset.mkdir()
    visible = json.loads(next(_DEV_DIR.glob("*.json")).read_text(encoding="utf-8"))
    visible.update(
        {
            "case_id": "sealed-opaque-two",
            "family_id": "sealed-family-two",
            "split": "sealed",
        }
    )
    (dataset / "case.json").write_text(json.dumps(visible), encoding="utf-8")
    responses = tmp_path / "responses.json"
    _response_bundle(responses, ["sealed-opaque-two"])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    exit_code = harness.main(
        [
            "score",
            "--dataset-dir",
            str(dataset),
            "--split",
            "sealed",
            "--responses",
            str(responses),
            "--judge",
            "llm",
            "--output",
            str(tmp_path / "out.json"),
        ]
    )
    assert exit_code == 2
    assert "ANTHROPIC_API_KEY is required" in capsys.readouterr().err


def test_threshold_gate_returns_one_on_measurement_failure(tmp_path: Path) -> None:
    cases = load_dataset(_DEV_DIR, split=DatasetSplit.DEVELOPMENT)
    responses = tmp_path / "responses.json"
    _response_bundle(responses, [case.case_id for case in cases])
    exit_code = harness.main(
        [
            "score",
            "--dataset-dir",
            str(_DEV_DIR),
            "--split",
            "development",
            "--responses",
            str(responses),
            "--judge",
            "stub",
            "--stub-score",
            "0.5",
            "--dimension-floor",
            "0.8",
            "--mean-floor",
            "0.8",
            "--output",
            str(tmp_path / "out.json"),
        ]
    )
    assert exit_code == 1
