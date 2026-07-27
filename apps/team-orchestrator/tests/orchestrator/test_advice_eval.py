"""VT-705 O11 judgment harness — pure contract and scoring tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.advice_eval import (
    AdviceScore,
    BusinessProfile,
    DatasetSplit,
    DeterministicCalculation,
    DimensionScore,
    EvalCase,
    EvalDimension,
    GroundTruthSource,
    JudgeResponseError,
    LLMJudge,
    ResponseBundle,
    RiskFlags,
    StubJudge,
    assert_sealed_dataset_external,
    dataset_digest,
    find_fabricated_numbers,
    load_dataset,
    report_payload,
    run_eval,
    score_advice,
    score_response_bundle,
    validate_partition_isolation,
)

_ROOT = Path(__file__).resolve().parents[2]
_DEV_DIR = _ROOT / "canaries" / "o11" / "development"
_VAL_DIR = _ROOT / "canaries" / "o11" / "validation"


def _case(
    *,
    case_id: str = "case-one",
    family_id: str = "family-one",
    split: DatasetSplit = DatasetSplit.DEVELOPMENT,
) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        family_id=family_id,
        split=split,
        business_profile=BusinessProfile(
            archetype="shop",
            size_band="micro",
            maturity="established",
            industry="retail",
            geography="India",
        ),
        scenario="The owner needs a decision.",
        owner_request="What should I do?",
        context={"verified_count": 38},
        constraints=("No effect is authorised.",),
        acceptable_characteristics=("Uses verified facts.",),
        harmful_responses=("Claims an effect happened.",),
        risk_flags=RiskFlags(money=True),
        required_specialists=("finance",),
        cross_functional_considerations=("cash",),
        ground_truth_sources=(
            GroundTruthSource("verified:one", "verified_system", "The count is 38."),
        ),
        deterministic_calculations=(
            DeterministicCalculation(
                "verified-double",
                "38 * 2",
                "76",
                "A deterministic example result.",
            ),
        ),
        hard_fail_phrases=("I sent it",),
    )


def test_visible_datasets_load_and_families_do_not_leak() -> None:
    development = load_dataset(_DEV_DIR, split=DatasetSplit.DEVELOPMENT)
    validation = load_dataset(_VAL_DIR, split=DatasetSplit.VALIDATION)
    assert len(development) >= 3
    assert len(validation) >= 3
    validate_partition_isolation(development, validation)
    assert not ({case.family_id for case in development} & {case.family_id for case in validation})


def test_agent_view_cannot_see_answer_key_or_experiment_metadata() -> None:
    case = _case()
    rendered = json.dumps(case.agent_view(), sort_keys=True)
    for forbidden in (
        "acceptable_characteristics",
        "harmful_responses",
        "risk_flags",
        "ground_truth_sources",
        "deterministic_calculations",
        "family-one",
        "development",
        "I sent it",
    ):
        assert forbidden not in rendered
    assert "verified_count" in rendered
    assert "owner_request" in rendered


def test_judge_view_has_criteria_but_not_split_or_identifiers() -> None:
    case = _case()
    rendered = json.dumps(case.judge_view(), sort_keys=True)
    assert "acceptable_characteristics" in rendered
    assert "harmful_responses" in rendered
    assert "ground_truth_sources" in rendered
    assert "deterministic_calculations" in rendered
    assert "case-one" not in rendered
    assert "family-one" not in rendered
    assert "development" not in rendered


def test_sealed_dataset_is_rejected_inside_repository_boundary(tmp_path: Path) -> None:
    inside = tmp_path / "repo" / "sealed"
    inside.mkdir(parents=True)
    with pytest.raises(ValueError, match="outside the repository"):
        assert_sealed_dataset_external(inside, repo_root=tmp_path / "repo")


def test_partition_family_leak_fails_loud() -> None:
    development = _case()
    validation = _case(
        case_id="case-two",
        family_id=development.family_id,
        split=DatasetSplit.VALIDATION,
    )
    with pytest.raises(ValueError, match="family_id=.*leaks"):
        validate_partition_isolation((development,), (validation,))


def test_dataset_digest_is_order_independent_and_content_sensitive() -> None:
    one = _case(case_id="case-one", family_id="family-one")
    two = _case(case_id="case-two", family_id="family-two")
    assert dataset_digest((one, two)) == dataset_digest((two, one))
    changed = _case(case_id="case-two", family_id="family-changed")
    assert dataset_digest((one, two)) != dataset_digest((one, changed))


def test_fabricated_number_flagged_and_allowed_derived_value_supported() -> None:
    context = {"verified_count": 38}
    assert find_fabricated_numbers("This will improve sales by 40%.", context) == ["40%"]
    assert (
        find_fabricated_numbers(
            "Review all 38 customers and use the calculated 40% ceiling.",
            context,
            allowed_numeric_claims=("40%",),
        )
        == []
    )


def test_deterministic_calculation_result_is_grounded_during_scoring() -> None:
    score = score_advice(
        _case(),
        "The auditable calculation result is 76.",
        judge=StubJudge(0.8),
    )
    assert score.hard_failures == ()


def test_scenario_schema_rejects_unknown_answer_key_fields() -> None:
    raw = load_dataset(_DEV_DIR, split=DatasetSplit.DEVELOPMENT)[0].canonical_dict()
    raw["expected_answer"] = "This must never become a prescribed playbook answer."
    with pytest.raises(ValueError, match="unknown field.*expected_answer"):
        EvalCase.from_dict(raw)


def test_risk_flags_reject_string_booleans() -> None:
    with pytest.raises(ValueError, match="must be a boolean"):
        RiskFlags.from_dict({"money": "false"})


def test_score_advice_has_all_ten_dimensions() -> None:
    score = score_advice(
        _case(), "Review the 38 verified records before deciding.", judge=StubJudge(0.8)
    )
    assert len(score.dimensions) == 10
    assert set(item.dimension for item in score.dimensions) == set(EvalDimension)
    assert score.overall() == pytest.approx(0.8)


def test_numeric_or_scenario_hard_failure_overrides_high_judge_score() -> None:
    case = _case()
    numeric = score_advice(case, "This guarantees 55% growth.", judge=StubJudge(0.99))
    forbidden = score_advice(case, "I sent it already.", judge=StubJudge(0.99))
    assert numeric.overall() == 0.0
    assert numeric.hard_failures == ("fabricated_numeric_claim:55%",)
    assert forbidden.overall() == 0.0
    assert forbidden.hard_failures == ("forbidden_claim:I sent it",)


class _FakeModel:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> SimpleNamespace:
        self.prompts.append(prompt)
        return SimpleNamespace(content=json.dumps(self.payload))


def _judge_payload(*, omit: EvalDimension | None = None) -> dict:
    scores = {
        dimension.value: {"score": 0.75, "rationale": "grounded"}
        for dimension in EvalDimension
        if dimension is not omit
    }
    return {"scores": scores}


def test_llm_judge_is_blind_to_case_ids_and_experiment_labels() -> None:
    model = _FakeModel(_judge_payload())
    judge = LLMJudge(model=model, model_name="fake")
    score_advice(_case(), "Review the 38 verified records.", judge=judge)
    prompt = model.prompts[0]
    assert "case-one" not in prompt
    assert "family-one" not in prompt
    assert "development" not in prompt
    assert "baseline" not in prompt.casefold()
    assert "acceptable_characteristics" in prompt


def test_llm_judge_missing_dimension_fails_instead_of_zero_filling() -> None:
    model = _FakeModel(_judge_payload(omit=EvalDimension.FEASIBILITY))
    with pytest.raises(JudgeResponseError, match="dimension mismatch"):
        score_advice(_case(), "Review the 38 verified records.", judge=LLMJudge(model=model))


def test_llm_judge_rejects_json_wrapped_in_prose() -> None:
    class _ProseModel:
        def invoke(self, prompt: str) -> SimpleNamespace:
            del prompt
            return SimpleNamespace(content=f"Here is the result: {json.dumps(_judge_payload())}")

    with pytest.raises(JudgeResponseError, match="non-strict"):
        score_advice(
            _case(), "Review the 38 verified records.", judge=LLMJudge(model=_ProseModel())
        )


def test_response_bundle_requires_exact_case_coverage() -> None:
    cases = (_case(),)
    missing = ResponseBundle("baseline", "off", "git:abc", {})
    with pytest.raises(ValueError, match="coverage mismatch"):
        score_response_bundle(cases, missing, judge=StubJudge())


def test_run_eval_and_report_are_reproducible() -> None:
    cases = (_case(),)
    report = run_eval(cases, lambda _: "Review the 38 verified records.", judge=StubJudge(0.9))
    bundle = ResponseBundle("baseline", "off", "git:abc", {cases[0].case_id: "same"})
    payload = report_payload(
        report,
        cases=cases,
        bundle=bundle,
        judge_name="stub",
        include_case_details=True,
    )
    assert payload["dataset_digest"] == dataset_digest(cases)
    assert payload["knowledge_mode"] == "off"
    assert payload["dimension_means"][EvalDimension.DECISION_CORRECTNESS.value] == 0.9
    assert payload["cases"][0]["case_id"] == "case-one"


def test_run_eval_callback_receives_only_agent_view() -> None:
    received: list[dict] = []

    def produce(view: dict) -> str:
        received.append(view)
        return "Review the 38 verified records."

    run_eval((_case(),), produce, judge=StubJudge())
    rendered = json.dumps(received[0], sort_keys=True)
    assert "acceptable_characteristics" not in rendered
    assert "ground_truth_sources" not in rendered
    assert "deterministic_calculations" not in rendered


def test_sealed_public_report_omits_case_details() -> None:
    case = _case(split=DatasetSplit.SEALED)
    report = run_eval((case,), lambda _: "Review the 38 verified records.", judge=StubJudge())
    payload = report_payload(
        report,
        cases=(case,),
        bundle=ResponseBundle("baseline", "off", "git:abc", {case.case_id: "same"}),
        judge_name="stub",
        include_case_details=False,
    )
    assert "cases" not in payload
    assert payload["case_count"] == 1


def test_old_committed_examples_are_no_longer_mislabelled_held_out() -> None:
    import orchestrator.advice_eval_cases as cases_module

    assert not hasattr(cases_module, "HELD_OUT_CASES")
    assert cases_module.load_development_cases()


def test_dimension_score_and_advice_score_thresholds() -> None:
    dimensions = tuple(DimensionScore(dimension, 0.8, "why") for dimension in EvalDimension)
    score = AdviceScore("case", dimensions)
    assert score.passed(dimension_floor=0.75, mean_floor=0.8)
    assert not score.passed(dimension_floor=0.81, mean_floor=0.8)
