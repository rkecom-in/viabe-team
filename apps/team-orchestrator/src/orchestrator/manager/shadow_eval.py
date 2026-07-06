"""VT-611 (Phase B2, Finding A) — the shadow-mode OBSERVATIONAL manager_review pass.

``loop_mode.py``'s own docstring promises this module: "shadow -> ... AFTER the legacy dispatch
already produced its real reply/effect, a SEPARATE observational pass (``manager/shadow_eval.py``)
runs triage + manager_review over the SAME turn's actual output and records what the loop WOULD
have decided to ``tm_audit`` — comparison data for the 50-conversation shadow-acceptance bar." The
triage half of that promise is already live (``triage_seam.py``, shadow branch). This module is the
manager_review half — the ONLY new code this row adds.

THE LOAD-BEARING SAFETY PROPERTY: this module is PROVABLY EFFECT-FREE. It reuses
``manager.review``'s own EXTRACT + DECIDE pieces (``extract_specialist_return`` /
``adapt_campaign_plan_to_specialist_return`` / ``to_legacy_specialist_return`` / ``decide_next_
action``) — all pure or read-only — and NEVER calls ``manager.review.manager_review`` itself (which
persists: ``plan_store.complete_step`` / ``task_store.set_*_status`` / ``pending_questions.ask`` /
``incident_store.create_incident``). It never sends, never mutates business data, never drives a
real plan. The ONE write it performs is the ``tm_audit_log`` insert recording the divergence itself
— the audit spine's own exemption for its own write (VT-514 "can't-audit ⇒ can't-act" is about
BUSINESS actions; the audit row IS the deliverable here, not a side effect of one).

Divergence classification (the payload the shadow-acceptance bar reads):
  - ``safety_divergence`` — the specialist's raw output carries a CONSEQUENTIAL effect intent
    (``customer_send`` / ``spend`` / ``commitment``) AND EITHER (a) the effect's own magnitude is
    OUT_OF_POLICY per the tenant's real, deterministic ``business_policy`` bound (an unapproved
    spend legacy would let through with no Package-3 gate at all), OR (b) manager_review's own
    trust-but-verify read of the SAME raw output would NOT accept it as-is (ESCALATE/REVISE/
    CLARIFY) — i.e. the new loop's grounding/extraction check catches something legacy's
    ungated pass-through let stand. This is the class the shadow-acceptance bar (execution-plan §5)
    must show ZERO of before an enforce promotion.
  - ``intent_divergence`` — the new loop's decision differs from a straight accept, but NOTHING
    consequential was at stake (no effect intent, or the turn's legacy outcome never actually
    completed this turn — see ``legacy_final_status`` below) — a benign routing disagreement.
  - ``no_divergence`` — the new loop agrees legacy's outcome was fine.

``legacy_final_status`` (default ``"completed"``): the ACTUAL terminal status ``dispatch_brain``
reached for this turn (its own ``FinalStatus``). A specialist's raw output is only ever worth
running this pass over when legacy actually reached a specialist's terminal path (``"completed"``
per ``dispatch.py``'s own branch — an ``"escalated"``/``"aborted_hard_limit"`` terminal means legacy
itself already stopped short, so no real customer-facing effect landed this turn regardless of what
the raw output claims) — passed explicitly rather than assumed so a caller's honest "legacy didn't
even complete" fact can demote an otherwise-safety-shaped divergence to intent-only.

NOT YET WIRED (this row's honest scope, per Finding A's own report): the live call site — feeding
this the SAME turn's actual ``raw_output``/``campaign_plan`` from inside ``dispatch_brain`` after
``_classify_terminal`` — is a separate, reviewed change to that file (see the accompanying report).
This module is complete + fully tested standalone so that wiring is a single call, not a redesign.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from anthropic import Anthropic

from orchestrator.agents.business_policy import PolicyActionClass, assert_within_policy
from orchestrator.manager.decision import ManagerDecisionKind, decide_next_action
from orchestrator.manager.plan_models import PlanSpecialistReturn
from orchestrator.manager.review import (
    adapt_campaign_plan_to_specialist_return,
    extract_specialist_return,
    to_legacy_specialist_return,
)
from orchestrator.observability.tm_audit import emit_tm_audit

if TYPE_CHECKING:
    from orchestrator.agent.schemas.campaign_plan import CampaignPlan

logger = logging.getLogger("orchestrator.manager.shadow_eval")

ShadowDivergenceClass = Literal["safety_divergence", "intent_divergence", "no_divergence"]

# Effect classes a customer/owner would actually feel this turn — "config" (an internal setting
# change) is deliberately excluded: a routing disagreement over a config-only step has no
# customer-facing or spend consequence, so it can never rise above intent_divergence.
_CONSEQUENTIAL_EFFECT_CLASSES = frozenset({"customer_send", "spend", "commitment"})

# decide_next_action outcomes that mean "the new loop would let this stand as-is" — mirrors
# review._DECISION_TO_OUTCOME's ACCEPT/NEXT_SPECIALIST -> complete/continue branches (both mean
# "accepted", differing only on whether a real plan has a next step — irrelevant here: shadow_eval
# judges ONE turn in isolation, never a real multi-step plan, so has_next_step is always False and
# these two kinds collapse to the same "accepted" meaning for divergence purposes).
_ACCEPTED_KINDS = frozenset({ManagerDecisionKind.ACCEPT, ManagerDecisionKind.NEXT_SPECIALIST})


@dataclass(frozen=True, slots=True)
class ShadowEvalResult:
    """The full record of one shadow observational pass — everything a caller/test needs without
    re-deriving it. Never carries a live side effect of its own beyond the tm_audit row."""

    specialist_return: PlanSpecialistReturn
    shadow_decision_kind: ManagerDecisionKind
    divergence_class: ShadowDivergenceClass
    divergence_detail: str
    out_of_policy_effect_classes: tuple[str, ...]
    audit_id: UUID | None


def _out_of_policy_effect_classes(
    tenant_id: UUID | str, ret: PlanSpecialistReturn
) -> tuple[str, ...]:
    """Deterministic, read-only: for every SPEND effect intent the specialist's output claims,
    bound-check its magnitude against the tenant's REAL, stored policy ceiling (the same
    ``decide_within_policy`` ladder every live send/spend path routes through — never a parallel
    rule). Never raises, never mutates (``assert_within_policy`` is a SELECT + pure compute).

    Scoped to ``spend`` only: ``customer_send``'s policy bound also checks the target SEGMENT, which
    ``EffectIntent`` does not carry (only ``effect_class``/``summary``/``magnitude_minor``) — bound-
    checking it here with no segment would default-deny every customer_send intent that isn't the
    ``"all"`` wildcard, a false positive this module must not manufacture. ``customer_send``'s real
    consent/opt-out/segment gate already runs, unconditionally, on the live send path regardless of
    loop mode (VT-460) — this check adds the ADDITIONAL spend-ceiling signal that has no other
    always-on guard until enforce wires ``manager_review`` itself into the graph.
    """
    hits: list[str] = []
    for effect in ret.effect_intents:
        if effect.effect_class != "spend" or effect.magnitude_minor is None:
            continue
        check = assert_within_policy(
            tenant_id,
            PolicyActionClass.SPEND,
            {"magnitude_minor": effect.magnitude_minor},
        )
        if check.out_of_policy:
            hits.append(effect.effect_class)
    return tuple(hits)


def _classify_divergence(
    *,
    ret: PlanSpecialistReturn,
    decision_kind: ManagerDecisionKind,
    legacy_final_status: str,
    out_of_policy: tuple[str, ...],
    has_consequential_effect: bool,
) -> tuple[ShadowDivergenceClass, str]:
    accepted = decision_kind in _ACCEPTED_KINDS
    # Legacy never actually completed this turn (it escalated/aborted before reaching a real
    # effect) — whatever the raw output claims, nothing customer-facing landed, so the ceiling on
    # this turn's divergence is "benign routing disagreement", never safety.
    legacy_landed = legacy_final_status == "completed"

    if out_of_policy:
        return (
            "safety_divergence",
            f"effect magnitude out-of-policy for effect_class(es)={out_of_policy} — legacy has "
            "no Package-3 gate to catch this today",
        )
    if legacy_landed and has_consequential_effect and not accepted:
        return (
            "safety_divergence",
            f"shadow_decision={decision_kind.value!r} would gate a consequential effect "
            f"(specialist_status={ret.status!r}) that legacy let proceed unreviewed",
        )
    if not accepted:
        return (
            "intent_divergence",
            f"shadow_decision={decision_kind.value!r} differs from legacy's unreviewed completion "
            f"(specialist_status={ret.status!r}); no consequential effect at stake this turn",
        )
    return ("no_divergence", "shadow agrees with legacy's outcome")


def evaluate_turn_shadow(
    tenant_id: UUID | str,
    *,
    turn_ref: str,
    situation: str,
    desired_outcome: str,
    acceptance_criteria: list[str],
    raw_output: str,
    campaign_plan: "CampaignPlan | None" = None,
    legacy_final_status: str = "completed",
    run_id: UUID | None = None,
    client: Anthropic | None = None,
) -> ShadowEvalResult:
    """The shadow-mode observational manager_review pass for ONE turn. Mirrors ``manager.review.
    manager_review``'s extract+decide halves EXACTLY (same functions, same fail-closed extraction
    fallback) but stops before its persistence half — nothing here ever advances a real task/step,
    asks a real owner question, or opens a real incident. ``run_id`` is only ever passed when the
    caller holds a value KNOWN to already have a ``pipeline_runs`` row (tm_audit's ``run_id`` column
    FKs it) — ``turn_ref`` (e.g. the inbound message SID) is the always-available correlation key,
    recorded as ``trace_id`` (no FK).
    """
    if campaign_plan is not None:
        ret = adapt_campaign_plan_to_specialist_return(tenant_id, campaign_plan)
    else:
        try:
            ret = extract_specialist_return(
                situation=situation,
                desired_outcome=desired_outcome,
                acceptance_criteria=acceptance_criteria,
                raw_output=raw_output,
                client=client,
            )
        except ValueError as exc:
            logger.warning(
                "shadow_eval: structured extraction failed for turn=%s (fail-closed -> "
                "escalate-shaped): %s",
                turn_ref, exc,
            )
            ret = PlanSpecialistReturn(
                status="failed",
                action_summary="",
                outcome_summary="shadow_eval could not extract a structured result",
                reason_code="extraction_failed",
            )

    legacy_ret = to_legacy_specialist_return(ret)
    # has_next_step=False, always: shadow_eval judges ONE turn in isolation, never a real
    # multi-step plan (no plan is ever driven in shadow mode — see plan_store's shadow=True status,
    # which never advances) — see _ACCEPTED_KINDS' docstring for why this doesn't matter here.
    decision = decide_next_action(legacy_ret, has_next_step=False)

    out_of_policy = _out_of_policy_effect_classes(tenant_id, ret)
    # A campaign_plan turn is customer-send-shaped BY CONSTRUCTION regardless of whether the
    # adapter ultimately grounded it — an ungrounded/cross-tenant cohort still carries EMPTY
    # effect_intents (adapt_campaign_plan_to_specialist_return only populates them on the grounded
    # branch), so reading effect_intents alone would miss exactly the hallucinated-cohort case
    # Package 6 exists to catch. The free-text (raw_output) path has no such prior — it relies
    # entirely on extraction's OWN read of what effect the specialist claims.
    has_consequential_effect = campaign_plan is not None or any(
        e.effect_class in _CONSEQUENTIAL_EFFECT_CLASSES for e in ret.effect_intents
    )
    divergence_class, divergence_detail = _classify_divergence(
        ret=ret,
        decision_kind=decision.kind,
        legacy_final_status=legacy_final_status,
        out_of_policy=out_of_policy,
        has_consequential_effect=has_consequential_effect,
    )

    audit_id = emit_tm_audit(
        event_layer="decides",
        event_kind="shadow_divergence",
        actor="team_manager",
        tenant_id=tenant_id,
        run_id=run_id,
        trace_id=turn_ref,
        summary=(
            f"shadow_eval: turn={turn_ref} legacy={legacy_final_status!r} -> "
            f"shadow={decision.kind.value!r} class={divergence_class!r}"
        ),
        decision={
            "turn_ref": turn_ref,
            "legacy_decision": legacy_final_status,
            "shadow_decision": decision.kind.value,
            "class": divergence_class,
            "detail": divergence_detail,
            "specialist_status": ret.status,
            "out_of_policy_effect_classes": list(out_of_policy),
        },
        status="blocked" if divergence_class == "safety_divergence" else "ok",
    )

    return ShadowEvalResult(
        specialist_return=ret,
        shadow_decision_kind=decision.kind,
        divergence_class=divergence_class,
        divergence_detail=divergence_detail,
        out_of_policy_effect_classes=out_of_policy,
        audit_id=audit_id,
    )


__all__ = ["ShadowDivergenceClass", "ShadowEvalResult", "evaluate_turn_shadow"]
