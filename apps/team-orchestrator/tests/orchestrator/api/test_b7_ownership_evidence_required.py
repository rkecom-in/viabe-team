"""B7 — verifying ownership must record WHAT was checked, not just who and when."""

from __future__ import annotations

import pytest

# orchestrator.api imports fastapi at package load; the dep-less smoke suite does not install it.
pytest.importorskip("fastapi")


def test_b7_verify_requires_evidence_but_reject_does_not() -> None:
    """B7 residue: `ownership_verified` is a NON-BYPASSABLE execute gate — flipping it true is what
    lets a tenant message its customers. Both note and evidence were optional, so a VTR could
    verify with the fields blank and the audit row recorded only booleans: who and when, never
    WHAT they saw. Unreviewable afterwards, and unanswerable if ownership is ever disputed.

    Required on VERIFY only. Demanding proof of a thing that was NOT established would push VTRs
    toward leaving bad tenants unreviewed, which is the opposite of the intent.
    """
    import inspect

    from orchestrator.api import ops_vtr_console

    src = inspect.getsource(ops_vtr_console.vtr_ownership_decision)
    assert "if verified and not evidence.strip():" in src, (
        "the evidence requirement must be scoped to the verify direction only"
    )
    assert "status_code=422" in src


def test_vt634_failed_workflow_surface_distinguishes_blind_from_clear() -> None:
    """B5 item 5 — `prod_workflow_diagnosis` was finished code called by NOTHING: no route, no CLI,
    no schedule. This is the route.

    The property that matters: `diagnosis_available: false` must be returned DISTINCTLY from an
    empty findings list. A console rendering "I cannot see" and "nothing is wrong" identically
    tells a VTR the money path is clear while it is blind — the most dangerous sentence this
    surface could produce.
    """
    import inspect

    from orchestrator.api import ops_vtr_console

    src = inspect.getsource(ops_vtr_console.vtr_failed_workflows)
    assert '"diagnosis_available": False' in src
    assert '"diagnosis_available": True' in src
    assert "DiagnosisUnavailable" in src, "an unreadable diagnosis must not fall through as empty"
    # Cross-tenant read: no assignment to scope to, so it must be exception-tier, not the per-tenant gate.
    assert "require_exception_tier(operator)" in src
    # Read-only: the surface must not contain or repair anything (the spec forbids silent
    # auto-resolution of an effectful failure).
    for mutating in ("UPDATE ", "INSERT ", "redrive", "cancel"):
        assert mutating not in src, f"the diagnosis surface must stay read-only (found {mutating!r})"
