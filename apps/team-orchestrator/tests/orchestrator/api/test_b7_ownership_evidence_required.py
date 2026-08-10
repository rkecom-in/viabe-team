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
