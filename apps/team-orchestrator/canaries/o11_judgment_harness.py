#!/usr/bin/env python3
"""VT-705 O11 dataset validator and judgment scorer.

The script is intentionally a canary-style executable rather than a production
runtime seam.  Codex can validate and dry-run visible datasets with ``stub``;
the sealed-set custodian runs the real ``llm`` judge with credentials supplied
at execution time.  Missing credentials and malformed model output fail loud.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestrator.advice_eval import (  # noqa: E402
    DatasetSplit,
    LLMJudge,
    StubJudge,
    assert_sealed_dataset_external,
    dataset_digest,
    load_dataset,
    load_response_bundle,
    report_payload,
    score_response_bundle,
    validate_partition_isolation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="O11 business-judgment evaluation harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate", help="validate dataset schema and partition isolation"
    )
    validate.add_argument("--development-dir", required=True)
    validate.add_argument("--validation-dir", required=True)
    validate.add_argument("--sealed-dir")

    score = subparsers.add_parser("score", help="score a complete agent-response bundle")
    score.add_argument("--dataset-dir", required=True)
    score.add_argument("--split", choices=[split.value for split in DatasetSplit], required=True)
    score.add_argument("--responses", required=True)
    score.add_argument("--judge", choices=("stub", "llm"), required=True)
    score.add_argument("--model", default="claude-opus-4-8")
    score.add_argument("--stub-score", type=float, default=0.8)
    score.add_argument(
        "--output", required=True, help="aggregate report; case details omitted for sealed"
    )
    score.add_argument(
        "--custody-output",
        help="optional detailed sealed report; must be outside the repository",
    )
    score.add_argument("--require-knowledge-mode")
    score.add_argument("--dimension-floor", type=float)
    score.add_argument("--mean-floor", type=float)
    return parser


def _write_json(path: str | Path, payload: dict) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _validate(args: argparse.Namespace) -> int:
    development = load_dataset(args.development_dir, split=DatasetSplit.DEVELOPMENT)
    validation = load_dataset(args.validation_dir, split=DatasetSplit.VALIDATION)
    partitions = [development, validation]
    summary: dict[str, dict[str, str | int]] = {
        "development": {"cases": len(development), "digest": dataset_digest(development)},
        "validation": {"cases": len(validation), "digest": dataset_digest(validation)},
    }
    if args.sealed_dir:
        sealed = load_dataset(args.sealed_dir, split=DatasetSplit.SEALED)
        partitions.append(sealed)
        summary["sealed"] = {"cases": len(sealed), "digest": dataset_digest(sealed)}
    validate_partition_isolation(*partitions)
    print(json.dumps({"status": "PASS", "partitions": summary}, sort_keys=True))
    return 0


def _score(args: argparse.Namespace) -> int:
    split = DatasetSplit(args.split)
    if split is DatasetSplit.SEALED and args.judge == "stub":
        raise ValueError("sealed evaluation requires the real llm judge; stub is development-only")
    if args.custody_output:
        if split is not DatasetSplit.SEALED:
            raise ValueError("custody-output is reserved for sealed evaluation")
        assert_sealed_dataset_external(args.custody_output)
    if (args.dimension_floor is None) != (args.mean_floor is None):
        raise ValueError("dimension-floor and mean-floor must be supplied together")
    for label, value in (
        ("dimension-floor", args.dimension_floor),
        ("mean-floor", args.mean_floor),
    ):
        if value is not None and not 0.0 <= value <= 1.0:
            raise ValueError(f"{label} must be within 0..1")

    cases = load_dataset(args.dataset_dir, split=split)
    bundle = load_response_bundle(args.responses)
    if args.require_knowledge_mode and bundle.knowledge_mode != args.require_knowledge_mode:
        raise ValueError(
            f"knowledge_mode={bundle.knowledge_mode!r}; required {args.require_knowledge_mode!r}"
        )

    if args.judge == "llm":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is required for the real O11 judge (no skip)")
        judge = LLMJudge(model_name=args.model)
    else:
        judge = StubJudge(args.stub_score)

    report = score_response_bundle(cases, bundle, judge=judge)
    public = report_payload(
        report,
        cases=cases,
        bundle=bundle,
        judge_name=judge.name,
        include_case_details=split is not DatasetSplit.SEALED,
    )
    if args.dimension_floor is not None and args.mean_floor is not None:
        public["gate"] = {
            "dimension_floor": args.dimension_floor,
            "mean_floor": args.mean_floor,
            "pass_rate": report.pass_rate(
                dimension_floor=args.dimension_floor,
                mean_floor=args.mean_floor,
            ),
        }
    _write_json(args.output, public)

    if args.custody_output:
        detailed = report_payload(
            report,
            cases=cases,
            bundle=bundle,
            judge_name=judge.name,
            include_case_details=True,
        )
        _write_json(args.custody_output, detailed)

    print(json.dumps(public, ensure_ascii=False, sort_keys=True))
    if args.dimension_floor is not None and args.mean_floor is not None:
        return 0 if public["gate"]["pass_rate"] == 1.0 else 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            return _validate(args)
        return _score(args)
    except Exception as exc:  # noqa: BLE001 - canary contract: fail loud, never skip
        print(f"O11_FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
