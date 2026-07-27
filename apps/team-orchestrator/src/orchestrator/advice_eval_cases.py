"""Compatibility loader for VT-705's visible development scenarios.

The old VT-553 module called three committed examples ``HELD_OUT_CASES``.  A case
committed beside production code is not held out, so that misleading constant is
removed.  True sealed cases are held outside the repository by the evaluation
custodian and loaded only through ``advice_eval.load_dataset``.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.advice_eval import DatasetSplit, EvalCase, load_dataset

_DEVELOPMENT_DIR = Path(__file__).resolve().parents[2] / "canaries" / "o11" / "development"


def load_development_cases() -> tuple[EvalCase, ...]:
    return load_dataset(_DEVELOPMENT_DIR, split=DatasetSplit.DEVELOPMENT)


__all__ = ["load_development_cases"]
