"""VT-553 (Track D) — the advice-quality EVAL HARNESS (held-out measurement, NO corpus).

Per CL-2026-07-01-no-fixed-playbook: knowledge = the LLM's reasoning + the C3 learnable memory, NOT
an authored note-set. This harness MEASURES that output before a capability graduates — it never
authors, scripts, or confines advice, and it is NOT a retrieval corpus (the held-out cases are a
measurement set the agent must never read/train on).

Four dimensions: factuality / actionability / relevance / tone. The ONE surviving hard guardrail —
**no fabricated numbers/benchmarks** — is a deterministic rail here: any significant numeric claim in
the advice that is NOT grounded in the case context is a factuality hard-fail, independent of the
(softer) LLM judge.

Pure + dep-less: the LLM judge lazy-imports the model, so the harness is importable + unit-testable
with a deterministic stub judge. The real LLM-judge scoring runs on deployed dev (where the key lives).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol


class EvalDimension(str, Enum):
    FACTUALITY = "factuality"          # grounded, no fabricated numbers/benchmarks (the rail)
    ACTIONABILITY = "actionability"    # concrete next step the owner can take
    RELEVANCE = "relevance"            # fits the scenario + cohort
    TONE = "tone"                      # warm, owner-appropriate, not pushy


@dataclass(frozen=True)
class EvalCase:
    """A held-out MEASUREMENT scenario. NO expected_answer — the harness measures the agent's
    output, it does not compare to a target (a target would be a corpus)."""

    case_id: str
    scenario: str
    context: dict[str, Any] = field(default_factory=dict)  # the grounding a factual claim must trace to


@dataclass(frozen=True)
class DimensionScore:
    dimension: EvalDimension
    score: float          # 0.0 – 1.0
    rationale: str = ""


@dataclass(frozen=True)
class AdviceScore:
    case_id: str
    dimensions: tuple[DimensionScore, ...]
    fabricated_numbers: tuple[str, ...] = ()

    def overall(self) -> float:
        """Mean dimension score — but ANY fabricated number hard-fails to 0.0 (the surviving rail)."""
        if self.fabricated_numbers:
            return 0.0
        if not self.dimensions:
            return 0.0
        return sum(d.score for d in self.dimensions) / len(self.dimensions)

    def passed(self, threshold: float) -> bool:
        return not self.fabricated_numbers and self.overall() >= threshold


# ── the no-fabricated-numbers rail (deterministic) ──────────────────────────

# A "significant" numeric claim: a percentage, a currency amount, or a >=2-digit number. Single
# digits (structural — "3 tips", "top 5") are ignored to avoid false positives.
_CLAIM_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s?%"                 # percentages: 40%, 3.5 %
    r"|(?:₹|Rs\.?\s?)\d[\d,]*(?:\.\d+)?"      # currency: ₹5,000, Rs 1200
    r"|\b\d{2,}\b"                             # bare multi-digit: 1200
)


def _normalise_claim(tok: str) -> str:
    pct = "%" if "%" in tok else ""
    digits = re.sub(r"[^\d.]", "", tok).rstrip(".")
    if digits.endswith(".0"):
        digits = digits[:-2]
    return digits + pct


def _extract_claims(text: str) -> set[str]:
    return {_normalise_claim(m) for m in _CLAIM_RE.findall(text or "")}


def find_fabricated_numbers(advice: str, context: dict[str, Any]) -> list[str]:
    """Significant numeric claims in the advice NOT grounded anywhere in the case context."""
    grounded = _extract_claims(json.dumps(context, default=str))
    return sorted(_extract_claims(advice) - grounded)


# ── the judge ───────────────────────────────────────────────────────────────

class Judge(Protocol):
    def score(self, case: EvalCase, advice: str) -> list[DimensionScore]: ...


class StubJudge:
    """A deterministic judge for tests / dry runs — a fixed score per dimension, no model."""

    def __init__(self, per_dimension: float = 0.8) -> None:
        self._v = per_dimension

    def score(self, case: EvalCase, advice: str) -> list[DimensionScore]:
        return [DimensionScore(d, self._v, "stub") for d in EvalDimension]


class LLMJudge:
    """The real judge — an LLM scores each dimension 0..1 with a rationale. Lazy-imports the model so
    the harness stays dep-less-importable; runs on deployed dev (where the API key lives)."""

    def __init__(self, model: Any = None) -> None:
        self._model = model

    def _client(self) -> Any:
        if self._model is not None:
            return self._model
        from langchain_anthropic import ChatAnthropic

        self._model = ChatAnthropic(model="claude-opus-4-7", max_tokens=1024)  # type: ignore[call-arg]
        return self._model

    def score(self, case: EvalCase, advice: str) -> list[DimensionScore]:
        prompt = (
            "You are a strict evaluator of business advice for a small Indian business owner. "
            "Score the ADVICE on each dimension from 0.0 to 1.0. Return STRICT JSON: "
            '{"factuality":{"score":x,"rationale":"…"},"actionability":{…},"relevance":{…},"tone":{…}}. '
            "Factuality: every claim must be grounded in the CONTEXT; a fabricated number is 0. "
            f"\n\nSCENARIO: {case.scenario}\nCONTEXT: {json.dumps(case.context, default=str)}\n\nADVICE: {advice}"
        )
        resp = self._client().invoke(prompt)
        raw = resp.content if hasattr(resp, "content") else str(resp)
        data = _parse_judge_json(raw)
        out: list[DimensionScore] = []
        for d in EvalDimension:
            item = data.get(d.value, {}) if isinstance(data, dict) else {}
            score = float(item.get("score", 0.0)) if isinstance(item, dict) else 0.0
            out.append(DimensionScore(d, max(0.0, min(1.0, score)),
                                      str(item.get("rationale", "")) if isinstance(item, dict) else ""))
        return out


def _parse_judge_json(raw: str) -> dict[str, Any]:
    try:
        start, end = raw.index("{"), raw.rindex("}")
        return json.loads(raw[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        return {}


# ── scoring + report ────────────────────────────────────────────────────────

def score_advice(case: EvalCase, advice: str, *, judge: Judge) -> AdviceScore:
    dims = judge.score(case, advice)
    fabricated = find_fabricated_numbers(advice, case.context)
    return AdviceScore(case.case_id, tuple(dims), tuple(fabricated))


@dataclass(frozen=True)
class EvalReport:
    scores: tuple[AdviceScore, ...]

    def aggregate(self) -> dict[str, float]:
        """Mean score per dimension across all cases."""
        agg: dict[str, float] = {}
        for d in EvalDimension:
            vals = [ds.score for s in self.scores for ds in s.dimensions if ds.dimension is d]
            agg[d.value] = sum(vals) / len(vals) if vals else 0.0
        return agg

    def pass_rate(self, threshold: float) -> float:
        if not self.scores:
            return 0.0
        return sum(1 for s in self.scores if s.passed(threshold)) / len(self.scores)

    def any_fabrication(self) -> bool:
        return any(s.fabricated_numbers for s in self.scores)


def run_eval(
    cases: list[EvalCase],
    produce_advice: Callable[[EvalCase], str],
    *,
    judge: Judge,
) -> EvalReport:
    """Run each held-out case through ``produce_advice`` (the agent — LLM+memory) and score it. The
    harness MEASURES; it never feeds the case's criteria back into advice generation."""
    return EvalReport(tuple(score_advice(c, produce_advice(c), judge=judge) for c in cases))


__all__ = [
    "EvalDimension", "EvalCase", "DimensionScore", "AdviceScore", "EvalReport",
    "Judge", "StubJudge", "LLMJudge",
    "find_fabricated_numbers", "score_advice", "run_eval",
]
