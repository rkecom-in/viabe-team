#!/usr/bin/env python3
"""F4 / O11 — run the 25 India-SMB judgment scenarios through the PRODUCTION model, x3.

WHY THIS EXISTS
---------------
The "judgment is 88% commodity" result was measured on Codex and ChatGPT — frontier models. We ship
on gpt-5.6-luna. The conclusion has never been tested at our actual runtime, so it generalised from
the wrong sample. This run tests it where we actually live.

It answers three questions at once: is our production model good enough at business judgment; do
knowledge cards have any future (if luna already reaches Fazal's call, cards pad a model that is
fine); and is luna's judgment STABLE run-to-run — we already know the routing classifier is not, and
unstable judgment would be a bigger product problem than mere inaccuracy.

THIS SCRIPT DOES NOT SCORE. Raw answers only.
A builder scoring its own model's output is precisely the conflict the exercise exists to avoid;
scoring is Clau's, against a baseline this process must never see.

WHAT LUNA MUST NOT SEE
----------------------
Fazal's answers, the Codex/ChatGPT answers, and the comparison write-up. This script reads exactly
ONE file — the brief — and slices only the marked paste block out of it. It never globs the
calibration directory, so a repo-reading harness cannot drag the baseline into context.

USAGE (deployed dev, key flows OS-env -> process, never printed):
    railway run --environment development --service vt-orchestrator-service -- \
        uv run python canaries/luna_25_judgment_run.py
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BRIEF = REPO / ".viabe" / "calibration" / "CODEX-BRIEF-paste-this.md"
OUT = REPO / ".viabe" / "calibration" / "luna-25-answers.json"

_START = "## ⬇️ EVERYTHING BELOW THIS LINE IS THE PASTE ⬇️"
_END = "## ⬆️ END OF PASTE ⬆️"

#: Codex received exactly the marked block. Anything else and we are comparing PROMPTS, not models.
#: Extracted rather than retyped for that reason.
RUNS = 3

#: The tier the decisive-judgment turn would actually use. Resolved through the seam, never
#: hardcoded (VT-732 — there is a CI gate for exactly this), and the resolved string is recorded in
#: the output so the reader knows what actually answered.
_TIER = "complex"


def _paste_block() -> str:
    text = BRIEF.read_text(encoding="utf-8")
    try:
        start = text.index(_START) + len(_START)
        end = text.index(_END)
    except ValueError:
        raise SystemExit(
            "F4 FAIL: could not find the ⬇️/⬆️ paste markers in the brief. Refusing to guess at "
            "the prompt boundaries — a different prompt makes this a comparison of prompts, not "
            "of models."
        ) from None
    block = text[start:end].strip("\n")
    if not block.strip():
        raise SystemExit("F4 FAIL: the paste block is empty")
    return block


def _model():  # noqa: ANN201
    sys.path.insert(0, str(REPO / "apps" / "team-orchestrator" / "src"))
    from orchestrator.llm.provider import resolve_chat_model

    # NOTE: no temperature. The deployed gpt-5.6 family REJECTS the parameter (it 400s), so it is
    # deliberately not set — recorded here because "we did not pin temperature" is a fact the
    # stability reading depends on.
    return resolve_chat_model(_TIER, agent="calibration", call_site="o11_luna_judgment")


def _resolved_name(model) -> str:  # noqa: ANN001
    for attr in ("model", "model_name", "model_id"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__


def main() -> int:
    prompt = _paste_block()
    model = _model()
    resolved = _resolved_name(model)
    print(f"resolved model: {resolved}")

    # A silent tier change would invalidate the entire comparison — the whole point is to measure
    # the model we actually ship. Stop rather than produce numbers attributed to the wrong model.
    if "luna" not in resolved.lower():
        raise SystemExit(
            f"F4 STOP: resolved model {resolved!r} is not in the luna family. This run exists to "
            "measure our PRODUCTION model; attributing results to the wrong one is worse than no "
            "result. Check the tier policy env before re-running."
        )

    runs = []
    for idx in range(1, RUNS + 1):
        # Fresh invocation per run — no shared chat history. Stability is part of the measurement,
        # so run 2 must not be able to see run 1.
        print(f"run {idx}/{RUNS} …", flush=True)
        try:
            reply = model.invoke(prompt)
            content = getattr(reply, "content", reply)
            if isinstance(content, list):  # some providers return content blocks
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            runs.append({
                "run_index": idx,
                "resolved_model": resolved,
                "timestamp": datetime.now(UTC).isoformat(),
                "raw_answer": content,
                "error": None,
            })
        except Exception as exc:  # noqa: BLE001 — a failed run is DATA, not a reason to lose the others
            print(f"  run {idx} FAILED: {type(exc).__name__}", flush=True)
            runs.append({
                "run_index": idx,
                "resolved_model": resolved,
                "timestamp": datetime.now(UTC).isoformat(),
                "raw_answer": None,
                "error": f"{type(exc).__name__}: {exc}"[:400],
            })

    OUT.write_text(json.dumps({
        "purpose": "O11 — 25 India-SMB judgment scenarios through the PRODUCTION model",
        "resolved_model": resolved,
        "tier_requested": _TIER,
        "temperature": "NOT SET — the deployed gpt-5.6 family rejects the parameter",
        "runs_requested": RUNS,
        "prompt_sha_note": "prompt is the byte-identical ⬇️/⬆️ block from CODEX-BRIEF-paste-this.md",
        "scored": False,
        "scoring_note": "RAW ONLY. Scoring is Clau's — a builder scoring its own model's output is "
                        "the conflict this exercise exists to avoid.",
        "runs": runs,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    ok = sum(1 for r in runs if r["raw_answer"])
    print(f"wrote {OUT} — {ok}/{RUNS} runs returned an answer")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
