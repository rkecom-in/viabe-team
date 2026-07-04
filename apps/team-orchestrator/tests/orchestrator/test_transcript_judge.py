"""VT-598 — transcript_judge.py pure-function tests (no Anthropic API call).

transcript_judge.py imports the `anthropic` SDK lazily (inside `_client()`), exactly mirroring
convo_harness.py's dep-less-at-import-time posture — these tests exercise everything ELSE: bundle
loading, transcript rendering, batching, the judge's JSON-response parser (given canned strings, as
if the model had already replied), and verdict aggregation. `judge_batch` / `_client` (the actual
API-calling seam) are deliberately NOT exercised here — that only runs against a live key on
deployed dev, per the module docstring.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_CANARIES = Path(__file__).resolve().parents[2] / "canaries"
sys.path.insert(0, str(_CANARIES))

import transcript_judge as tj  # noqa: E402 — after the sys.path insert

# --- load_bundle --------------------------------------------------------------------------------


def test_load_bundle_top_level_list(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps([{"name": "a"}, {"name": "b"}]), encoding="utf-8")
    entries = tj.load_bundle(str(path))
    assert [e["name"] for e in entries] == ["a", "b"]


def test_load_bundle_wrapped_scenarios_key(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps({"scenarios": [{"name": "a"}]}), encoding="utf-8")
    entries = tj.load_bundle(str(path))
    assert [e["name"] for e in entries] == ["a"]


def test_load_bundle_rejects_unrecognized_shape(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps({"nonsense": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="unrecognized bundle shape"):
        tj.load_bundle(str(path))


# --- render_transcript_for_judge ----------------------------------------------------------------


def test_render_transcript_includes_name_steps_and_turns():
    entry = {
        "name": "probe_scenario",
        "steps": [
            {
                "label": "PASS",
                "transcript": [
                    {"role": "owner", "text": "hi there"},
                    {"role": "assistant", "text": "hello — how can I help?"},
                ],
            },
        ],
    }
    rendered = tj.render_transcript_for_judge(entry)
    assert "SCENARIO: probe_scenario" in rendered
    assert "harness label: PASS" in rendered
    assert "owner: hi there" in rendered
    assert "assistant: hello — how can I help?" in rendered


def test_render_transcript_falls_back_to_scenario_key_for_name():
    entry = {"scenario": "path/to/x.json", "steps": []}
    rendered = tj.render_transcript_for_judge(entry)
    assert "SCENARIO: path/to/x.json" in rendered


# --- batch_entries -------------------------------------------------------------------------------


def test_batch_entries_splits_evenly():
    entries = [{"name": str(i)} for i in range(8)]
    batches = tj.batch_entries(entries, 4)
    assert len(batches) == 2
    assert [e["name"] for e in batches[0]] == ["0", "1", "2", "3"]


def test_batch_entries_last_batch_partial():
    entries = [{"name": str(i)} for i in range(5)]
    batches = tj.batch_entries(entries, 4)
    assert len(batches) == 2
    assert len(batches[0]) == 4
    assert len(batches[1]) == 1


def test_batch_entries_rejects_zero_or_negative():
    with pytest.raises(ValueError):
        tj.batch_entries([{"name": "a"}], 0)


# --- parse_judge_response ------------------------------------------------------------------------


def _valid_scores(all_5=True):
    val = 5 if all_5 else 2
    return {dim: {"score": val, "why": "looks fine"} for dim in tj.DIMENSIONS}


def test_parse_judge_response_happy_path():
    raw = json.dumps([{"scenario": "probe_scenario", "scores": _valid_scores()}])
    verdicts = tj.parse_judge_response(raw)
    assert len(verdicts) == 1
    assert verdicts[0].scenario == "probe_scenario"
    assert verdicts[0].passed() is True
    assert verdicts[0].min_score() == 5


def test_parse_judge_response_strips_code_fence():
    raw = "```json\n" + json.dumps([{"scenario": "x", "scores": _valid_scores()}]) + "\n```"
    verdicts = tj.parse_judge_response(raw)
    assert verdicts[0].scenario == "x"


def test_parse_judge_response_low_score_fails_verdict():
    raw = json.dumps([{"scenario": "x", "scores": _valid_scores(all_5=False)}])
    verdicts = tj.parse_judge_response(raw)
    assert verdicts[0].passed() is False
    assert verdicts[0].min_score() == 2


def test_parse_judge_response_rejects_non_array():
    with pytest.raises(ValueError, match="not a JSON array"):
        tj.parse_judge_response(json.dumps({"scenario": "x"}))


def test_parse_judge_response_rejects_missing_scenario_key():
    with pytest.raises(ValueError, match="missing required keys"):
        tj.parse_judge_response(json.dumps([{"scores": _valid_scores()}]))


def test_parse_judge_response_rejects_missing_dimension():
    scores = _valid_scores()
    del scores["honesty"]
    with pytest.raises(ValueError, match="missing dimension 'honesty'"):
        tj.parse_judge_response(json.dumps([{"scenario": "x", "scores": scores}]))


def test_parse_judge_response_rejects_out_of_range_score():
    scores = _valid_scores()
    scores["honesty"]["score"] = 7
    with pytest.raises(ValueError, match="out of range"):
        tj.parse_judge_response(json.dumps([{"scenario": "x", "scores": scores}]))


def test_parse_judge_response_rejects_unparseable_json():
    with pytest.raises(ValueError, match="unparseable JSON"):
        tj.parse_judge_response("not json at all {{{")


# --- aggregate_verdicts ---------------------------------------------------------------------------


def test_aggregate_verdicts_all_pass():
    verdicts = tj.parse_judge_response(json.dumps([
        {"scenario": "a", "scores": _valid_scores()},
        {"scenario": "b", "scores": _valid_scores()},
    ]))
    summary = tj.aggregate_verdicts(verdicts)
    assert summary["all_passed"] is True
    assert len(summary["scenarios"]) == 2
    assert all(row["passed"] for row in summary["scenarios"])


def test_aggregate_verdicts_one_failure_flips_all_passed():
    verdicts = tj.parse_judge_response(json.dumps([
        {"scenario": "a", "scores": _valid_scores()},
        {"scenario": "b", "scores": _valid_scores(all_5=False)},
    ]))
    summary = tj.aggregate_verdicts(verdicts)
    assert summary["all_passed"] is False
    passed_map = {row["scenario"]: row["passed"] for row in summary["scenarios"]}
    assert passed_map == {"a": True, "b": False}


def test_aggregate_verdicts_round_trips_through_json():
    verdicts = tj.parse_judge_response(json.dumps([{"scenario": "a", "scores": _valid_scores()}]))
    summary = tj.aggregate_verdicts(verdicts)
    json.loads(json.dumps(summary))  # must be plain-JSON-serializable
