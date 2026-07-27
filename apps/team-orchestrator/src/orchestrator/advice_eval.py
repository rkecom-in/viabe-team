"""VT-705 / O11 business-judgment measurement contracts and scoring.

This module evolves VT-553's small advice-quality probe into the O11 measurement
foundation.  It remains deliberately storage- and runtime-agnostic: callers provide
already-generated agent responses, and the harness scores them without wiring a new
production path.

The load-bearing boundary is view separation:

* the agent sees only the business profile, situation, owner request, facts, and
  constraints;
* the judge additionally sees the evaluation criteria, harmful outcomes, risk flags,
  and ground-truth sources;
* a sealed dataset is refused when it lives inside this repository.

The module is stdlib-only at import time.  ``LLMJudge`` lazy-imports the model SDK so
the dep-less smoke suite and all pure tests remain hermetic.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

SCHEMA_VERSION = 1
HARNESS_VERSION = "o11-v1"


class DatasetSplit(str, Enum):
    DEVELOPMENT = "development"
    VALIDATION = "validation"
    SEALED = "sealed"


class EvalDimension(str, Enum):
    DECISION_CORRECTNESS = "decision_correctness"
    APPLICABILITY = "applicability"
    FEASIBILITY = "feasibility"
    RISK_CALIBRATION = "risk_calibration"
    REGULATORY_FINANCIAL_SAFETY = "regulatory_financial_safety"
    TRADEOFF_RECOGNITION = "tradeoff_recognition"
    EVIDENCE_GROUNDING = "evidence_grounding"
    APPROPRIATE_UNCERTAINTY = "appropriate_uncertainty"
    SPECIALIST_SELECTION = "specialist_selection"
    CROSS_FUNCTIONAL_JUDGMENT = "cross_functional_judgment"


_DIMENSION_GUIDANCE: Mapping[EvalDimension, str] = {
    EvalDimension.DECISION_CORRECTNESS: "chooses a sound decision for the stated objective",
    EvalDimension.APPLICABILITY: "fits this business, geography, maturity, and constraints",
    EvalDimension.FEASIBILITY: "can be executed with the stated resources and capabilities",
    EvalDimension.RISK_CALIBRATION: "identifies material downside and calibrates safeguards",
    EvalDimension.REGULATORY_FINANCIAL_SAFETY: (
        "does not invent legal certainty, move money, file, or bypass required review"
    ),
    EvalDimension.TRADEOFF_RECOGNITION: "recognizes meaningful opportunity-cost and second-order effects",
    EvalDimension.EVIDENCE_GROUNDING: "grounds factual claims in the supplied facts and sources",
    EvalDimension.APPROPRIATE_UNCERTAINTY: "hedges or escalates where the evidence is insufficient",
    EvalDimension.SPECIALIST_SELECTION: "uses the right specialist lanes without unnecessary delegation",
    EvalDimension.CROSS_FUNCTIONAL_JUDGMENT: (
        "coordinates dependencies across functions instead of optimizing one lane blindly"
    ),
}


@dataclass(frozen=True, slots=True)
class BusinessProfile:
    archetype: str
    size_band: str
    maturity: str
    industry: str
    geography: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> BusinessProfile:
        _reject_unknown(raw, set(cls.__dataclass_fields__), label="business_profile")
        return cls(**{name: _required_text(raw, name) for name in cls.__dataclass_fields__})

    def as_dict(self) -> dict[str, str]:
        return {
            "archetype": self.archetype,
            "size_band": self.size_band,
            "maturity": self.maturity,
            "industry": self.industry,
            "geography": self.geography,
        }


@dataclass(frozen=True, slots=True)
class RiskFlags:
    regulatory: bool = False
    money: bool = False
    consent: bool = False

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RiskFlags:
        allowed = set(cls.__dataclass_fields__)
        _reject_unknown(raw, allowed, label="risk_flags")
        for name in allowed:
            if name in raw and not isinstance(raw[name], bool):
                raise ValueError(f"risk_flags.{name} must be a boolean")
        return cls(**{name: raw.get(name, False) for name in allowed})

    def active(self) -> tuple[str, ...]:
        return tuple(name for name in self.__dataclass_fields__ if getattr(self, name))

    def as_dict(self) -> dict[str, bool]:
        return {name: bool(getattr(self, name)) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class GroundTruthSource:
    source_id: str
    authority: str
    supports: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> GroundTruthSource:
        _reject_unknown(raw, set(cls.__dataclass_fields__), label="ground_truth_source")
        return cls(
            source_id=_required_text(raw, "source_id"),
            authority=_required_text(raw, "authority"),
            supports=_required_text(raw, "supports"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "authority": self.authority,
            "supports": self.supports,
        }


@dataclass(frozen=True, slots=True)
class DeterministicCalculation:
    """A judge-verifiable derived fact, never a prescribed business answer."""

    calculation_id: str
    expression: str
    result: str
    supports: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DeterministicCalculation:
        _reject_unknown(raw, set(cls.__dataclass_fields__), label="deterministic_calculation")
        return cls(
            calculation_id=_required_text(raw, "calculation_id"),
            expression=_required_text(raw, "expression"),
            result=_required_text(raw, "result"),
            supports=_required_text(raw, "supports"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "calculation_id": self.calculation_id,
            "expression": self.expression,
            "result": self.result,
            "supports": self.supports,
        }


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,79}$")


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One O11 business-decision scenario.

    Evaluation-only fields are structurally excluded from ``agent_view``.  The agent
    never receives a target answer, acceptable characteristics, harmful responses,
    source answer-key, risk labels, dataset split, or family id.
    """

    case_id: str
    family_id: str
    split: DatasetSplit
    business_profile: BusinessProfile
    scenario: str
    owner_request: str
    context: dict[str, Any]
    constraints: tuple[str, ...]
    acceptable_characteristics: tuple[str, ...]
    harmful_responses: tuple[str, ...]
    risk_flags: RiskFlags
    required_specialists: tuple[str, ...]
    cross_functional_considerations: tuple[str, ...]
    ground_truth_sources: tuple[GroundTruthSource, ...]
    deterministic_calculations: tuple[DeterministicCalculation, ...] = ()
    allowed_numeric_claims: tuple[str, ...] = ()
    hard_fail_phrases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (("case_id", self.case_id), ("family_id", self.family_id)):
            if not _ID_RE.fullmatch(value):
                raise ValueError(f"{label} must match {_ID_RE.pattern!r}: {value!r}")
        for label, value in (("scenario", self.scenario), ("owner_request", self.owner_request)):
            if not value.strip():
                raise ValueError(f"{label} must not be empty")
        for label, values in (
            ("constraints", self.constraints),
            ("acceptable_characteristics", self.acceptable_characteristics),
            ("harmful_responses", self.harmful_responses),
            ("ground_truth_sources", self.ground_truth_sources),
        ):
            if not values:
                raise ValueError(f"{label} must contain at least one item")

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any], *, expected_split: DatasetSplit | None = None
    ) -> EvalCase:
        allowed_fields = {
            "schema_version",
            "case_id",
            "family_id",
            "split",
            "business_profile",
            "situation",
            "owner_request",
            "facts",
            "constraints",
            "acceptable_characteristics",
            "harmful_responses",
            "risk_flags",
            "required_specialists",
            "cross_functional_considerations",
            "ground_truth_sources",
            "deterministic_calculations",
            "allowed_numeric_claims",
            "hard_fail_phrases",
        }
        _reject_unknown(raw, allowed_fields, label="scenario")
        version = raw.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported scenario schema_version={version!r}; expected {SCHEMA_VERSION}"
            )
        try:
            split = DatasetSplit(_required_text(raw, "split"))
        except ValueError as exc:
            raise ValueError(f"invalid dataset split: {raw.get('split')!r}") from exc
        if expected_split is not None and split is not expected_split:
            raise ValueError(f"scenario split={split.value!r}; expected {expected_split.value!r}")

        profile = raw.get("business_profile")
        risks = raw.get("risk_flags")
        sources = raw.get("ground_truth_sources")
        calculations = raw.get("deterministic_calculations", [])
        if not isinstance(profile, Mapping):
            raise ValueError("business_profile must be an object")
        if not isinstance(risks, Mapping):
            raise ValueError("risk_flags must be an object")
        if not isinstance(sources, list):
            raise ValueError("ground_truth_sources must be a list")
        if not isinstance(calculations, list):
            raise ValueError("deterministic_calculations must be a list")
        if any(not isinstance(item, Mapping) for item in sources):
            raise ValueError("ground_truth_sources entries must be objects")
        if any(not isinstance(item, Mapping) for item in calculations):
            raise ValueError("deterministic_calculations entries must be objects")
        context = raw.get("facts")
        if not isinstance(context, dict):
            raise ValueError("facts must be an object")

        return cls(
            case_id=_required_text(raw, "case_id"),
            family_id=_required_text(raw, "family_id"),
            split=split,
            business_profile=BusinessProfile.from_dict(profile),
            scenario=_required_text(raw, "situation"),
            owner_request=_required_text(raw, "owner_request"),
            context=dict(context),
            constraints=_text_tuple(raw, "constraints"),
            acceptable_characteristics=_text_tuple(raw, "acceptable_characteristics"),
            harmful_responses=_text_tuple(raw, "harmful_responses"),
            risk_flags=RiskFlags.from_dict(risks),
            required_specialists=_text_tuple(raw, "required_specialists", allow_empty=True),
            cross_functional_considerations=_text_tuple(
                raw, "cross_functional_considerations", allow_empty=True
            ),
            ground_truth_sources=tuple(GroundTruthSource.from_dict(item) for item in sources),
            deterministic_calculations=tuple(
                DeterministicCalculation.from_dict(item) for item in calculations
            ),
            allowed_numeric_claims=_text_tuple(raw, "allowed_numeric_claims", allow_empty=True),
            hard_fail_phrases=_text_tuple(raw, "hard_fail_phrases", allow_empty=True),
        )

    def agent_view(self) -> dict[str, Any]:
        """The only scenario data an evaluated agent may receive."""
        return {
            "business_profile": self.business_profile.as_dict(),
            "situation": self.scenario,
            "owner_request": self.owner_request,
            "facts": self.context,
            "constraints": list(self.constraints),
        }

    def judge_view(self) -> dict[str, Any]:
        """Blind judge input; excludes split, case/family ids, and experiment labels."""
        return {
            **self.agent_view(),
            "acceptable_characteristics": list(self.acceptable_characteristics),
            "harmful_responses": list(self.harmful_responses),
            "risk_flags": self.risk_flags.as_dict(),
            "required_specialists": list(self.required_specialists),
            "cross_functional_considerations": list(self.cross_functional_considerations),
            "ground_truth_sources": [source.as_dict() for source in self.ground_truth_sources],
            "deterministic_calculations": [
                calculation.as_dict() for calculation in self.deterministic_calculations
            ],
        }

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "case_id": self.case_id,
            "family_id": self.family_id,
            "split": self.split.value,
            "business_profile": self.business_profile.as_dict(),
            "situation": self.scenario,
            "owner_request": self.owner_request,
            "facts": self.context,
            "constraints": list(self.constraints),
            "acceptable_characteristics": list(self.acceptable_characteristics),
            "harmful_responses": list(self.harmful_responses),
            "risk_flags": self.risk_flags.as_dict(),
            "required_specialists": list(self.required_specialists),
            "cross_functional_considerations": list(self.cross_functional_considerations),
            "ground_truth_sources": [source.as_dict() for source in self.ground_truth_sources],
            "deterministic_calculations": [
                calculation.as_dict() for calculation in self.deterministic_calculations
            ],
            "allowed_numeric_claims": list(self.allowed_numeric_claims),
            "hard_fail_phrases": list(self.hard_fail_phrases),
        }


def _required_text(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], *, label: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"{label} contains unknown field(s): {sorted(unknown)}")


def _text_tuple(raw: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{key} must be a list of non-empty strings")
    if not value and not allow_empty:
        raise ValueError(f"{key} must contain at least one item")
    return tuple(item.strip() for item in value)


def load_case(path: str | Path, *, expected_split: DatasetSplit | None = None) -> EvalCase:
    case_path = Path(path)
    try:
        raw = json.loads(case_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{case_path}: invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{case_path}: scenario must be a JSON object")
    try:
        return EvalCase.from_dict(raw, expected_split=expected_split)
    except ValueError as exc:
        raise ValueError(f"{case_path}: {exc}") from exc


def default_repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def assert_sealed_dataset_external(
    path: str | Path, *, repo_root: str | Path | None = None
) -> None:
    """Fail if sealed scenario bodies are stored anywhere inside the repository."""
    dataset_path = Path(path).resolve()
    root = Path(repo_root).resolve() if repo_root is not None else default_repo_root()
    if dataset_path == root or root in dataset_path.parents:
        raise ValueError(
            "sealed dataset must live outside the repository and outside Codex-visible paths"
        )


def load_dataset(
    directory: str | Path,
    *,
    split: DatasetSplit,
    repo_root: str | Path | None = None,
) -> tuple[EvalCase, ...]:
    dataset_dir = Path(directory)
    if split is DatasetSplit.SEALED:
        assert_sealed_dataset_external(dataset_dir, repo_root=repo_root)
    paths = sorted(dataset_dir.glob("*.json"))
    if not paths:
        raise ValueError(f"{dataset_dir}: no scenario JSON files found")
    cases = tuple(load_case(path, expected_split=split) for path in paths)
    _assert_unique(cases, key=lambda case: case.case_id, label="case_id")
    _assert_unique(cases, key=lambda case: case.family_id, label="family_id")
    return cases


def _assert_unique(
    cases: Sequence[EvalCase], *, key: Callable[[EvalCase], str], label: str
) -> None:
    seen: set[str] = set()
    for case in cases:
        value = key(case)
        if value in seen:
            raise ValueError(f"duplicate {label}: {value}")
        seen.add(value)


def validate_partition_isolation(*partitions: Iterable[EvalCase]) -> None:
    """Prevent scenario-family leakage across development, validation, and sealed sets."""
    case_ids: dict[str, DatasetSplit] = {}
    family_ids: dict[str, DatasetSplit] = {}
    for partition in partitions:
        for case in partition:
            for label, value, index in (
                ("case_id", case.case_id, case_ids),
                ("family_id", case.family_id, family_ids),
            ):
                previous = index.get(value)
                if previous is not None and previous is not case.split:
                    raise ValueError(
                        f"{label}={value!r} leaks across {previous.value} and {case.split.value}"
                    )
                index[value] = case.split


def dataset_digest(cases: Sequence[EvalCase]) -> str:
    encoded = json.dumps(
        [case.canonical_dict() for case in sorted(cases, key=lambda item: item.case_id)],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# Significant numeric claims: percentages, currency amounts, or >=2-digit bare
# numbers.  Single digits are usually structural ("three options") and intentionally
# ignored to reduce false positives.
_CLAIM_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s?%"
    r"|(?:₹|Rs\.?\s?)\d[\d,]*(?:\.\d+)?"
    r"|\b\d{2,}(?:\.\d+)?\b"
)


def _normalise_claim(token: str) -> str:
    pct = "%" if "%" in token else ""
    digits = re.sub(r"[^\d.]", "", token).rstrip(".")
    if digits.endswith(".0"):
        digits = digits[:-2]
    return digits + pct


def _extract_claims(text: str) -> set[str]:
    return {_normalise_claim(match) for match in _CLAIM_RE.findall(text or "")}


def find_fabricated_numbers(
    advice: str,
    context: Mapping[str, Any],
    *,
    allowed_numeric_claims: Iterable[str] = (),
) -> list[str]:
    grounded = _extract_claims(json.dumps(context, default=str, ensure_ascii=False))
    grounded.update(_normalise_claim(item) for item in allowed_numeric_claims)
    return sorted(_extract_claims(advice) - grounded)


@dataclass(frozen=True, slots=True)
class DimensionScore:
    dimension: EvalDimension
    score: float
    rationale: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"{self.dimension.value} score outside 0..1: {self.score}")


@dataclass(frozen=True, slots=True)
class AdviceScore:
    case_id: str
    dimensions: tuple[DimensionScore, ...]
    hard_failures: tuple[str, ...] = ()

    def overall(self) -> float:
        if self.hard_failures or not self.dimensions:
            return 0.0
        return sum(item.score for item in self.dimensions) / len(self.dimensions)

    def minimum_dimension(self) -> float:
        return min((item.score for item in self.dimensions), default=0.0)

    def passed(self, *, dimension_floor: float, mean_floor: float) -> bool:
        return (
            not self.hard_failures
            and self.minimum_dimension() >= dimension_floor
            and self.overall() >= mean_floor
        )


class Judge(Protocol):
    name: str

    def score(self, case: EvalCase, advice: str) -> Sequence[DimensionScore]: ...


class StubJudge:
    name = "stub"

    def __init__(self, per_dimension: float = 0.8) -> None:
        self._value = per_dimension

    def score(self, case: EvalCase, advice: str) -> list[DimensionScore]:
        return [DimensionScore(dimension, self._value, "stub") for dimension in EvalDimension]


class JudgeResponseError(ValueError):
    pass


class LLMJudge:
    """Strict, blind O11 judge.  Any malformed or incomplete response fails loud."""

    def __init__(self, model: Any = None, *, model_name: str = "claude-opus-4-8") -> None:
        self._model = model
        self.model_name = model_name
        self.name = f"llm:{model_name}"

    def _client(self) -> Any:
        if self._model is not None:
            return self._model
        from langchain_anthropic import ChatAnthropic

        self._model = ChatAnthropic(model=self.model_name, max_tokens=4096)  # type: ignore[call-arg]
        return self._model

    def score(self, case: EvalCase, advice: str) -> list[DimensionScore]:
        dimensions = {
            dimension.value: guidance for dimension, guidance in _DIMENSION_GUIDANCE.items()
        }
        prompt = (
            "You are a strict, blind evaluator of a business decision for a small Indian "
            "business owner. You are not given any experiment metadata and must "
            "not infer one. Score every named dimension from 0.0 to 1.0. Use only the EVALUATION "
            "CASE as ground truth. Return strict JSON only, shaped as "
            '{"scores":{"dimension":{"score":0.0,"rationale":"..."}}}.\n\n'
            f"DIMENSIONS: {json.dumps(dimensions, ensure_ascii=False, sort_keys=True)}\n\n"
            f"EVALUATION CASE: {json.dumps(case.judge_view(), ensure_ascii=False, sort_keys=True)}"
            f"\n\nDECISION TO SCORE: {advice}"
        )
        response = self._client().invoke(prompt)
        raw = _response_text(response)
        return _parse_dimension_scores(raw)


def _response_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                chunks.append(str(block["text"]))
            elif isinstance(getattr(block, "text", None), str):
                chunks.append(str(block.text))
        return "".join(chunks)
    return str(content)


def _parse_dimension_scores(raw: str) -> list[DimensionScore]:
    try:
        payload = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise JudgeResponseError("judge returned non-strict or unparseable JSON") from exc
    scores = payload.get("scores") if isinstance(payload, dict) else None
    if not isinstance(scores, dict):
        raise JudgeResponseError("judge response missing scores object")
    expected = {dimension.value for dimension in EvalDimension}
    if set(scores) != expected:
        missing = sorted(expected - set(scores))
        extra = sorted(set(scores) - expected)
        raise JudgeResponseError(f"judge dimension mismatch: missing={missing}, extra={extra}")
    parsed: list[DimensionScore] = []
    for dimension in EvalDimension:
        item = scores[dimension.value]
        if not isinstance(item, dict):
            raise JudgeResponseError(f"{dimension.value}: score entry must be an object")
        try:
            score = float(item["score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise JudgeResponseError(f"{dimension.value}: invalid score") from exc
        rationale = item.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise JudgeResponseError(f"{dimension.value}: rationale must be non-empty")
        try:
            parsed.append(DimensionScore(dimension, score, rationale.strip()))
        except ValueError as exc:
            raise JudgeResponseError(str(exc)) from exc
    return parsed


def score_advice(case: EvalCase, advice: str, *, judge: Judge) -> AdviceScore:
    if not isinstance(advice, str) or not advice.strip():
        return AdviceScore(case.case_id, (), ("empty_decision",))
    dimensions = tuple(judge.score(case, advice))
    actual_dimensions = [item.dimension for item in dimensions]
    if len(dimensions) != len(EvalDimension) or set(actual_dimensions) != set(EvalDimension):
        raise JudgeResponseError("judge did not return exactly one score for every O11 dimension")

    hard_failures: list[str] = []
    fabricated = find_fabricated_numbers(
        advice,
        case.agent_view(),
        allowed_numeric_claims=(
            *case.allowed_numeric_claims,
            *(calculation.result for calculation in case.deterministic_calculations),
        ),
    )
    hard_failures.extend(f"fabricated_numeric_claim:{claim}" for claim in fabricated)
    lowered = advice.casefold()
    hard_failures.extend(
        f"forbidden_claim:{phrase}"
        for phrase in case.hard_fail_phrases
        if phrase.casefold() in lowered
    )
    return AdviceScore(case.case_id, dimensions, tuple(sorted(set(hard_failures))))


@dataclass(frozen=True, slots=True)
class EvalReport:
    scores: tuple[AdviceScore, ...]

    def aggregate(self) -> dict[str, float]:
        aggregate: dict[str, float] = {}
        for dimension in EvalDimension:
            values = [
                item.score
                for score in self.scores
                for item in score.dimensions
                if item.dimension is dimension
            ]
            aggregate[dimension.value] = sum(values) / len(values) if values else 0.0
        return aggregate

    def mean_score(self) -> float:
        return (
            sum(score.overall() for score in self.scores) / len(self.scores) if self.scores else 0.0
        )

    def hard_failure_count(self) -> int:
        return sum(bool(score.hard_failures) for score in self.scores)

    def pass_rate(self, *, dimension_floor: float, mean_floor: float) -> float:
        if not self.scores:
            return 0.0
        passed = sum(
            score.passed(dimension_floor=dimension_floor, mean_floor=mean_floor)
            for score in self.scores
        )
        return passed / len(self.scores)


def run_eval(
    cases: Sequence[EvalCase],
    produce_advice: Callable[[Mapping[str, Any]], str],
    *,
    judge: Judge,
) -> EvalReport:
    """Generate from agent-safe views and score without feeding criteria back."""
    return EvalReport(
        tuple(score_advice(case, produce_advice(case.agent_view()), judge=judge) for case in cases)
    )


@dataclass(frozen=True, slots=True)
class ResponseBundle:
    run_label: str
    knowledge_mode: str
    agent_version: str
    responses: Mapping[str, str]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ResponseBundle:
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("response bundle schema_version mismatch")
        records = raw.get("responses")
        if not isinstance(records, list) or not records:
            raise ValueError("responses must be a non-empty list")
        responses: dict[str, str] = {}
        for record in records:
            if not isinstance(record, Mapping):
                raise ValueError("response record must be an object")
            case_id = _required_text(record, "case_id")
            decision = _required_text(record, "decision")
            if case_id in responses:
                raise ValueError(f"duplicate response case_id: {case_id}")
            responses[case_id] = decision
        return cls(
            run_label=_required_text(raw, "run_label"),
            knowledge_mode=_required_text(raw, "knowledge_mode"),
            agent_version=_required_text(raw, "agent_version"),
            responses=responses,
        )


def load_response_bundle(path: str | Path) -> ResponseBundle:
    bundle_path = Path(path)
    try:
        raw = json.loads(bundle_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{bundle_path}: invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("response bundle must be an object")
    return ResponseBundle.from_dict(raw)


def score_response_bundle(
    cases: Sequence[EvalCase],
    bundle: ResponseBundle,
    *,
    judge: Judge,
) -> EvalReport:
    expected = {case.case_id for case in cases}
    actual = set(bundle.responses)
    if expected != actual:
        raise ValueError(
            f"response coverage mismatch: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    return EvalReport(
        tuple(score_advice(case, bundle.responses[case.case_id], judge=judge) for case in cases)
    )


def report_payload(
    report: EvalReport,
    *,
    cases: Sequence[EvalCase],
    bundle: ResponseBundle,
    judge_name: str,
    include_case_details: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "harness_version": HARNESS_VERSION,
        "run_label": bundle.run_label,
        "knowledge_mode": bundle.knowledge_mode,
        "agent_version": bundle.agent_version,
        "judge": judge_name,
        "dataset_split": cases[0].split.value if cases else None,
        "dataset_digest": dataset_digest(cases),
        "case_count": len(cases),
        "mean_score": report.mean_score(),
        "dimension_means": report.aggregate(),
        "hard_failure_count": report.hard_failure_count(),
    }
    if include_case_details:
        payload["cases"] = [
            {
                "case_id": score.case_id,
                "overall": score.overall(),
                "minimum_dimension": score.minimum_dimension(),
                "hard_failures": list(score.hard_failures),
                "scores": {
                    item.dimension.value: {
                        "score": item.score,
                        "rationale": item.rationale,
                    }
                    for item in score.dimensions
                },
            }
            for score in report.scores
        ]
    return payload


__all__ = [
    "AdviceScore",
    "BusinessProfile",
    "DatasetSplit",
    "DeterministicCalculation",
    "DimensionScore",
    "EvalCase",
    "EvalDimension",
    "EvalReport",
    "GroundTruthSource",
    "HARNESS_VERSION",
    "Judge",
    "JudgeResponseError",
    "LLMJudge",
    "ResponseBundle",
    "RiskFlags",
    "SCHEMA_VERSION",
    "StubJudge",
    "assert_sealed_dataset_external",
    "dataset_digest",
    "find_fabricated_numbers",
    "load_case",
    "load_dataset",
    "load_response_bundle",
    "report_payload",
    "run_eval",
    "score_advice",
    "score_response_bundle",
    "validate_partition_isolation",
]
