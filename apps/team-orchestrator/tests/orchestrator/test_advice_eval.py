"""VT-553 (Track D) — advice-quality eval harness (pure; StubJudge, no model)."""

from __future__ import annotations

from orchestrator.advice_eval import (
    AdviceScore,
    DimensionScore,
    EvalCase,
    EvalDimension,
    StubJudge,
    find_fabricated_numbers,
    run_eval,
    score_advice,
)


def test_fabricated_number_flagged_when_ungrounded():
    case_ctx = {"dormant_count": 38}
    # 40% is not grounded anywhere in the context → fabricated.
    assert "40%" in find_fabricated_numbers("Send an offer to lift sales 40%.", case_ctx)


def test_grounded_numbers_not_flagged():
    ctx = {"dormant_count": 38, "uplift": "40%"}
    # both 38 and 40% appear in context → nothing fabricated.
    assert find_fabricated_numbers("Reach the 38 dormant customers; expect ~40% to re-engage.", ctx) == []


def test_single_digits_not_flagged():
    # structural small numbers are not "significant claims".
    assert find_fabricated_numbers("Give them 3 reasons to return.", {}) == []


def test_score_advice_overall_and_pass():
    case = EvalCase(case_id="c1", scenario="s", context={"n": 38})
    s = score_advice(case, "Reach the 38 dormant customers with a warm note.", judge=StubJudge(0.8))
    assert len(s.dimensions) == 4
    assert abs(s.overall() - 0.8) < 1e-9
    assert s.passed(0.7) is True


def test_fabrication_hard_fails_even_with_high_judge():
    case = EvalCase(case_id="c2", scenario="s", context={"n": 38})
    # judge would give 0.95, but the ungrounded 55% hard-fails to 0.0.
    s = score_advice(case, "This will boost revenue 55% this month.", judge=StubJudge(0.95))
    assert s.fabricated_numbers == ("55%",)
    assert s.overall() == 0.0
    assert s.passed(0.5) is False


def test_run_eval_report_aggregates():
    cases = [
        EvalCase(case_id="a", scenario="s1", context={"n": 10}),
        EvalCase(case_id="b", scenario="s2", context={}),
    ]
    report = run_eval(cases, lambda c: "A warm, grounded suggestion.", judge=StubJudge(0.9))
    agg = report.aggregate()
    assert abs(agg[EvalDimension.TONE.value] - 0.9) < 1e-9
    assert report.pass_rate(0.8) == 1.0
    assert report.any_fabrication() is False


def test_held_out_cases_load_and_are_measurement_only():
    from orchestrator.advice_eval_cases import HELD_OUT_CASES

    assert len(HELD_OUT_CASES) >= 2
    for c in HELD_OUT_CASES:
        assert isinstance(c, EvalCase)
        assert c.scenario and isinstance(c.context, dict)
        # measurement-only: a held-out case must NOT ship an expected answer (that would be a corpus).
        assert not hasattr(c, "expected_answer")


def test_dimension_score_shape():
    ds = DimensionScore(EvalDimension.FACTUALITY, 0.5, "why")
    assert ds.dimension is EvalDimension.FACTUALITY and ds.score == 0.5
    assert isinstance(AdviceScore("c", (ds,)).overall(), float)
