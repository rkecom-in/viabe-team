#!/usr/bin/env python3
"""VT-705 O11 — generate the AGENT RESPONSE BUNDLE that ``o11_judgment_harness.py score`` consumes.

The missing half of the O11 loop. The harness validates datasets and scores bundles; nothing
produced a bundle, so the frozen no-O8 baseline could not be run.

CUSTODY (the part that matters):
  - The bundle generator reads a case's ``agent_view()`` ONLY. That projection structurally excludes
    the answer key — acceptable characteristics, harmful responses, risk flags, required
    specialists, ground-truth sources, deterministic calculations, split and family id. The
    generating operator never needs to read case bodies and this tool never prints them.
  - Nothing is echoed to stdout except case IDs and progress. A `--verbose` that dumped prompts
    would defeat the point, so there isn't one.
  - For ``--split sealed`` both the dataset dir and the output path must resolve OUTSIDE the
    repository; ``assert_sealed_dataset_external`` enforces it and fails loud.
  - The sealed set is not cryptographically protected and never claimed to be. What the contract
    actually protects is that the set never becomes a repo fixture and that nobody iterates against
    per-case results. This tool writes decisions and stops; scoring and custody of the detailed
    report belong to the custodian.

KNOWLEDGE MODE is recorded, not merely asserted: the bundle carries ``knowledge_mode`` and the
scorer can require it (``--require-knowledge-mode``), so a treatment run can never be mistaken for
the baseline it is measured against.

Usage (baseline; run from apps/team-orchestrator):

    uv run python canaries/o11_response_bundle.py \\
      --dataset-dir /Users/<you>/development/vt-dataset \\
      --split sealed --knowledge-mode off \\
      --run-label no-o8-baseline \\
      --output /Users/<you>/development/vt-dataset-out/no-o8-baseline.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orchestrator.advice_eval import (  # noqa: E402
    SCHEMA_VERSION,
    DatasetSplit,
    EvalCase,
    assert_sealed_dataset_external,
    dataset_digest,
    load_dataset,
)

_DEFAULT_MODEL = "claude-opus-4-8"

_SYSTEM = (
    "You are the Viabe Team-Manager: the operating brain for a small Indian business owner, acting "
    "as their COO. You are given one real business situation and the owner's request. Decide what "
    "you would actually DO and tell the owner, in plain language.\n\n"
    "Ground every number in the facts you were given — never invent a figure, date, rate or "
    "status, and never claim an action you have not taken. Where a decision depends on something "
    "you were not told, say what you would need rather than assuming it. Respect every stated "
    "constraint. If a step needs the owner's approval, or a specialist, say so.\n\n"
    "Answer with the decision and your reasoning. No preamble, no meta-commentary, no headings — "
    "just the substance, as you would put it to the owner."
)


def agent_version() -> str:
    """``git:<sha>`` for the tree that produced the bundle — the baseline is only comparable to a
    treatment run if we know what code answered. Marked ``-dirty`` when the tree is not clean,
    because an uncommitted baseline is not reproducible and should not silently look like one."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(_ROOT), capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(_ROOT), capture_output=True, text=True,
            check=True,
        ).stdout.strip()
        return f"git:{sha}{'-dirty' if dirty else ''}"
    except Exception:  # noqa: BLE001 — never block a run on git metadata
        return "git:unknown"


def _client(model_name: str) -> Any:
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=model_name, max_tokens=4096)  # type: ignore[call-arg]


def _response_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip()
    return str(content).strip()


def generate_decision(case: EvalCase, *, model: Any) -> str:
    """One decision for one case. Sends ``agent_view()`` and nothing else.

    Fails LOUD on an empty response: a blank decision scored as if it were an answer would quietly
    depress the baseline and make any later treatment look better than it is.
    """
    prompt = (
        f"{_SYSTEM}\n\nSITUATION:\n"
        f"{json.dumps(case.agent_view(), ensure_ascii=False, sort_keys=True, indent=2)}"
    )
    text = _response_text(model.invoke(prompt))
    if not text:
        raise RuntimeError(f"{case.case_id}: empty decision from the model")
    return text


def build_bundle(
    cases: Sequence[EvalCase], *, run_label: str, knowledge_mode: str, model: Any,
    progress: bool = True,
) -> dict[str, Any]:
    responses = []
    for index, case in enumerate(cases, start=1):
        if progress:
            print(f"  [{index}/{len(cases)}] {case.case_id}", flush=True)
        responses.append({"case_id": case.case_id, "decision": generate_decision(case, model=model)})
    return {
        "schema_version": SCHEMA_VERSION,
        "run_label": run_label,
        "knowledge_mode": knowledge_mode,
        "agent_version": agent_version(),
        "responses": responses,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate an O11 agent-response bundle")
    parser.add_argument("--dataset-dir", required=True)
    parser.add_argument("--split", choices=[s.value for s in DatasetSplit], required=True)
    parser.add_argument("--output", required=True, help="bundle path; must be external for sealed")
    parser.add_argument("--run-label", required=True, help='e.g. "no-o8-baseline"')
    parser.add_argument(
        "--knowledge-mode", required=True, choices=("off", "shadow", "active"),
        help="recorded in the bundle so a treatment run can never be read as the baseline",
    )
    parser.add_argument("--model", default=_DEFAULT_MODEL)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    split = DatasetSplit(args.split)
    output = Path(args.output)

    if split is DatasetSplit.SEALED:
        # Same rail the scorer enforces: neither the cases nor the decisions may land in the repo.
        assert_sealed_dataset_external(Path(args.dataset_dir))
        assert_sealed_dataset_external(output)

    cases = load_dataset(args.dataset_dir, split=split)
    print(f"=== O11 response bundle: {len(cases)} {split.value} case(s) ===")
    print(f"    dataset digest: {dataset_digest(cases)}")
    print(f"    run_label={args.run_label} knowledge_mode={args.knowledge_mode}")

    bundle = build_bundle(
        cases, run_label=args.run_label, knowledge_mode=args.knowledge_mode,
        model=_client(args.model),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"    wrote {output} ({len(bundle['responses'])} decisions, {bundle['agent_version']})")
    print("    next: o11_judgment_harness.py score --judge llm  (custodian)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
