"""VT-606 (Loop Package 3) — the ``TEAM_MANAGER_LOOP_MODE`` feature flag.

Three modes, read ONCE at graph-build / dispatch time (never mid-run — a mode flip must not
change behavior inside an in-flight turn):

  - ``legacy``  — DEFAULT. The supervisor graph is built EXACTLY as it was before VT-606: every
    specialist routes straight to its pre-VT-606 edge target (``collapse`` for sales_recovery,
    ``END`` for integration/onboarding_conductor) — byte-identical node set + edge set. NOTHING
    in the new loop (triage, plan_store, manager_review, manager_task_workflow) runs. This is the
    production default and stays so until the VT-611 verification gate authorizes promotion
    (Standing bounds, manager-loop-program.md).
  - ``shadow``  — The LIVE graph shape is STILL legacy (A1: "the tagged-union CampaignPlan ->
    collapse -> VT-594 owner-surfacing path stays byte-compatible until enforce" — shadow must not
    touch what the owner sees or what effects fire). Additively, AFTER the legacy dispatch already
    produced its real reply/effect, a SEPARATE observational pass (``manager/shadow_eval.py``) runs
    triage + manager_review over the SAME turn's actual output and records what the loop WOULD have
    decided to ``tm_audit`` — comparison data for the 50-conversation shadow-acceptance bar
    (execution-plan §5), never a second dispatch, never a duplicate effect.
  - ``enforce`` — The supervisor graph is built with the NEW shape: every specialist routes to
    ``manager_review`` (not straight to collapse/END); only ``manager_review`` may advance or
    terminate a task. This is where ``manager_task_workflow`` actually drives a task end-to-end.
    Never the production default; reached only after VT-611's gate + Fazal's explicit promotion.

Unknown / unset values fail closed to ``legacy`` (never silently upgrade to a mode with more
capability than requested — the safe default is "do nothing new").
"""

from __future__ import annotations

import os
from typing import Literal, get_args

LoopMode = Literal["legacy", "shadow", "enforce"]

_VALID_MODES: tuple[LoopMode, ...] = get_args(LoopMode)
_ENV_VAR = "TEAM_MANAGER_LOOP_MODE"


def get_loop_mode() -> LoopMode:
    """Read ``TEAM_MANAGER_LOOP_MODE`` — fail-closed to ``'legacy'`` on anything unrecognized
    (unset, empty, a typo, or a not-yet-supported value)."""
    raw = os.environ.get(_ENV_VAR, "legacy").strip().lower()
    if raw in _VALID_MODES:
        return raw  # mypy narrows str -> LoopMode via the tuple-of-literals membership check
    return "legacy"


def is_legacy(mode: LoopMode | None = None) -> bool:
    return (mode if mode is not None else get_loop_mode()) == "legacy"


def is_shadow(mode: LoopMode | None = None) -> bool:
    return (mode if mode is not None else get_loop_mode()) == "shadow"


def is_enforce(mode: LoopMode | None = None) -> bool:
    return (mode if mode is not None else get_loop_mode()) == "enforce"


__all__ = ["LoopMode", "get_loop_mode", "is_enforce", "is_legacy", "is_shadow"]
