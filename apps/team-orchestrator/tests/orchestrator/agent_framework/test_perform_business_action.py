"""VT-101 Stage 2 — GateFacade.perform_business_action owns the WHOLE business-action round-trip.

Proves the §2 fix (ARCHITECTURE.md): the facade classifies AND issues-inside-choke (autonomous) or
arms the Pillar-7 approval and does NOT issue (requires-approval) — symmetric with
request_customer_send. Uses the REAL business_action_context / assert_in_business_action_context
(pure contextvar, the actual choke) + a monkeypatched gate decision + arm, so no DB/dbos is needed
beyond importing the choke module (importorskip keeps the dep-less smoke green).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

# The choke module carries the REAL business_action_context + assert_in + BusinessActionGate. It
# pulls dbos at import, so skip under the dep-less smoke (mirrors the other agent_framework tests).
bic = pytest.importorskip("orchestrator.agents.business_impact_choke")

from orchestrator.agent_framework.capabilities import Capability  # noqa: E402
from orchestrator.agent_framework.gate_facade import (  # noqa: E402
    BusinessActionOutcome,
    CapabilityNotDeclared,
    GateFacade,
)

_ACTION = "spend"


def _facade(caps, *, run_id: str | None = "run-1") -> GateFacade:
    return GateFacade(tenant_id=uuid4(), capabilities=frozenset(caps), run_id=run_id)


def _gate(decision) -> object:
    return bic.BusinessActionGate(
        decision=decision, reason="test", action_class=_ACTION,
        magnitude_minor=100, tier="autonomous",
    )


def _force_gate(monkeypatch, decision) -> None:
    monkeypatch.setattr(
        bic, "assert_or_gate_business_action", lambda *a, **k: _gate(decision)
    )


def test_autonomous_issues_effect_inside_the_choke(monkeypatch):
    _force_gate(monkeypatch, bic.BusinessActionDecision.AUTONOMOUS)
    ran = {}

    def effect():
        # Passes ONLY if we are inside business_action_context — the structural proof.
        bic.assert_in_business_action_context(_ACTION)
        ran["did"] = True
        return "EFFECT_DONE"

    out = _facade({Capability.REQUEST_BUSINESS_ACTION}).perform_business_action(
        _ACTION, 100, effect, summary="do the thing"
    )
    assert isinstance(out, BusinessActionOutcome)
    assert out.performed is True
    assert out.result == "EFFECT_DONE"
    assert out.armed is None
    assert ran.get("did") is True


def test_assert_in_context_outside_raises_control():
    # Control: the SAME guard, called OUTSIDE the choke, fails closed — so the pass above is real.
    with pytest.raises(bic.UngatedBusinessActionError):
        bic.assert_in_business_action_context(_ACTION)


def test_requires_owner_approval_arms_and_does_not_issue(monkeypatch):
    _force_gate(monkeypatch, bic.BusinessActionDecision.REQUIRES_OWNER_APPROVAL)
    monkeypatch.setattr(
        bic, "arm_business_action_approval", lambda *a, **k: "ARMED_SENTINEL"
    )
    ran = {"did": False}

    def effect():
        ran["did"] = True
        return "SHOULD_NOT_RUN"

    out = _facade({Capability.REQUEST_BUSINESS_ACTION}).perform_business_action(
        _ACTION, 100, effect, summary="please approve"
    )
    assert out.performed is False
    assert out.armed == "ARMED_SENTINEL"
    assert out.result is None
    assert ran["did"] is False  # the effect must NOT fire before approval


def test_proposer_scoped_facade_refuses_perform(monkeypatch):
    # The belt-and-braces the GATED_METHOD_BY_CAPABILITY note promises: perform_business_action is
    # _require-guarded identically, so a facade without the gated cap cannot reach it.
    with pytest.raises(CapabilityNotDeclared):
        _facade(frozenset()).perform_business_action(
            _ACTION, 100, lambda: None, summary="x"
        )


def test_requires_approval_without_run_id_fails_loud(monkeypatch):
    _force_gate(monkeypatch, bic.BusinessActionDecision.REQUIRES_OWNER_APPROVAL)
    with pytest.raises(ValueError, match="run_id"):
        _facade({Capability.REQUEST_BUSINESS_ACTION}, run_id=None).perform_business_action(
            _ACTION, 100, lambda: None, summary="x"
        )
