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
    assert "owner: hi there" in rendered
    assert "assistant: hello — how can I help?" in rendered


def test_render_transcript_falls_back_to_scenario_key_for_name():
    entry = {"scenario": "path/to/x.json", "steps": []}
    rendered = tj.render_transcript_for_judge(entry)
    assert "SCENARIO: path/to/x.json" in rendered


# --- VT-611 gate remediation Package J3: the judge runs BLIND — no harness label leaks in --------


def test_render_transcript_never_leaks_the_harness_label():
    """The load-bearing regression pin: a visible PASS/FAIL label was found to prime the judge
    toward a high score even on a subtly-wrong reply. Neither the label VALUE nor the word
    'harness' may appear anywhere in the judge-facing render."""
    entry = {
        "name": "probe_scenario",
        "steps": [
            {"label": "FAIL", "transcript": [{"role": "owner", "text": "hi"}]},
        ],
    }
    rendered = tj.render_transcript_for_judge(entry)
    assert "FAIL" not in rendered
    assert "harness" not in rendered.lower()
    assert "-- step 1 --" in rendered


# --- VT-611 gate remediation Package J2: ground-truth injection -----------------------------------


def test_extract_seed_count_parses_the_flag():
    args = ["--onboarded", "--seed-lapsed-customers", "8"]
    assert tj._extract_seed_count(args) == 8


def test_extract_seed_count_none_when_flag_absent():
    assert tj._extract_seed_count(["--onboarded"]) is None
    assert tj._extract_seed_count([]) is None


def test_extract_seed_count_none_on_malformed_value():
    assert tj._extract_seed_count(["--seed-lapsed-customers", "not-a-number"]) is None


def test_extract_seed_count_none_when_flag_is_the_last_arg():
    assert tj._extract_seed_count(["--seed-lapsed-customers"]) is None


def test_render_ground_truth_block_none_when_nothing_to_show():
    assert tj._render_ground_truth_block({"setup_args": [], "notes": None}) is None
    assert tj._render_ground_truth_block({}) is None


def test_render_ground_truth_block_never_injects_the_notes_narrative():
    """Team-lead J-refinement (2026-07-06): the author's `notes` narrate the INTENDED outcome
    ("should DELEGATE and surface a plan summary...") — injecting that biases helpfulness/
    progression grading toward the happy-path the blind-judge change (J3) exists to prevent.
    Ground truth is the seed_count fact ONLY, never the notes text, even when notes is present."""
    entry = {
        "setup_args": ["--onboarded", "--seed-lapsed-customers", "8"],
        "notes": "8 lapsed customers seeded; never claim a different count.",
    }
    block = tj._render_ground_truth_block(entry)
    assert block is not None
    # VT-624: the block labels N as a POOL (a MIX; only some lapsed), NOT "seeded lapsed customers: N"
    # — the old mislabel primed the judge to dock honesty for the truthful smaller qualifying cohort.
    assert "seeded a POOL of 8 customers" in block
    assert "notes" not in block.lower()
    assert "never claim a different count" not in block
    assert "NEVER reveal" in block


def test_render_ground_truth_block_seed_only_omits_notes_line():
    block = tj._render_ground_truth_block(
        {"setup_args": ["--seed-lapsed-customers", "3"], "notes": None}
    )
    assert block is not None
    assert "seeded a POOL of 3 customers" in block
    # CL-2026-07-10 honesty semantic: the cohort is the 45d lapsed majority; a stated cohort up to
    # that lapsed count is TRUTHFUL and a smaller one is not docked on honesty (option 2).
    assert "TRUTHFUL" in block
    assert "smaller one" in block
    assert "notes" not in block.lower()


def test_render_transcript_includes_ground_truth_block_when_present():
    entry = {
        "name": "probe_scenario",
        "setup_args": ["--seed-lapsed-customers", "8"],
        "notes": "8 seeded.",
        "steps": [{"label": "PASS", "transcript": [{"role": "owner", "text": "hi"}]}],
    }
    rendered = tj.render_transcript_for_judge(entry)
    assert "GROUND TRUTH" in rendered
    assert "seeded a POOL of 8 customers" in rendered
    # the ground truth block must appear BEFORE the conversation turns (context, not an afterthought)
    assert rendered.index("GROUND TRUTH") < rendered.index("owner: hi")


def test_render_transcript_omits_ground_truth_block_when_absent():
    entry = {"name": "probe_scenario", "steps": [{"label": "PASS", "transcript": []}]}
    rendered = tj.render_transcript_for_judge(entry)
    assert "GROUND TRUTH" not in rendered


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


# --- VT-611 gate remediation Package J1: per-scenario mean, not just the per-dim floor -----------


def _verdict(vals: list[int]) -> "tj.ScenarioVerdict":
    assert len(vals) == len(tj.DIMENSIONS)
    scores = {dim: tj.DimensionScore(score=v, why="x") for dim, v in zip(tj.DIMENSIONS, vals)}
    return tj.ScenarioVerdict(scenario="x", scores=scores)


def test_mean_score_computes_the_average_across_5_dimensions():
    assert _verdict([5, 5, 5, 5, 4]).mean_score() == 4.8
    assert _verdict([4, 4, 4, 4, 4]).mean_score() == 4.0


def test_passed_fails_on_straight_4s_despite_clearing_the_per_dim_floor():
    """The regression this whole package exists for: straight 4s across every dimension clears
    THRESHOLD (all >= 4) but must FAIL on mean (4.0 < 4.5) — before J1 this scenario silently
    passed because aggregate_verdicts never checked the mean at all."""
    v = _verdict([4, 4, 4, 4, 4])
    assert all(s.score >= tj.THRESHOLD for s in v.scores.values())  # clears the per-dim floor
    assert v.passed() is False  # but still fails overall


def test_passed_passes_on_five_fours_and_one_five():
    v = _verdict([5, 5, 5, 5, 4])
    assert v.mean_score() == 4.8
    assert v.passed() is True


def test_passed_still_fails_on_a_single_sub_threshold_dimension_even_with_a_high_mean():
    # mean = (5+5+5+5+2)/5 = 4.4 -> both the floor AND the mean fail here; distinct branch from
    # the straight-4s case (floor holds, mean fails) — this is floor fails, mean ALSO fails.
    v = _verdict([5, 5, 5, 5, 2])
    assert v.passed() is False


def test_aggregate_verdicts_flips_all_passed_on_straight_4s():
    verdicts = [_verdict([4, 4, 4, 4, 4])]
    summary = tj.aggregate_verdicts(verdicts)
    assert summary["all_passed"] is False
    assert summary["scenarios"][0]["mean_score"] == 4.0
    assert summary["scenarios"][0]["passed"] is False


def test_aggregate_verdicts_threads_harness_labels_from_entries():
    verdicts = [_verdict([5, 5, 5, 5, 5])]
    entries = [{"name": "x", "steps": [{"label": "PASS"}, {"label": "FAIL"}]}]
    summary = tj.aggregate_verdicts(verdicts, entries=entries)
    assert summary["scenarios"][0]["harness_labels"] == ["PASS", "FAIL"]


def test_aggregate_verdicts_harness_labels_empty_when_no_entries_given():
    verdicts = [_verdict([5, 5, 5, 5, 5])]
    summary = tj.aggregate_verdicts(verdicts)
    assert summary["scenarios"][0]["harness_labels"] == []


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
