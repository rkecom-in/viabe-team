"""VT-735 — which service tier a given call gets, decided by CALL CLASS, not by a global switch.

Implements `.viabe/model-tier-policy.md` (RATIFIED Fazal 2026-08-06). The policy in one sentence:

    We pay for latency exactly where a waiting human feels it: nobody waiting -> FLEX (½×);
    a person waiting -> STANDARD (1×); a person waiting at a decisive moment -> FAST (2×).

WHY THIS IS NOT A GLOBAL FLAG
Before this module there was ONE ``TEAM_OPENAI_SERVICE_TIER`` applied to every OpenAI call. Flex is
~50% cheaper in exchange for DELAY, and latency is precisely what we fight on owner-facing turns
(the ~96s waits, the D1 ack, the SR timeout class). A blanket flex would have saved rupees by making
every owner stare at WhatsApp longer — the wrong trade against Fazal's own no-surprises rulings. So
flex is a per-call LATENCY CLASS.

THE CLASSIFICATION IS AN EXPLICIT ALLOW-LIST, NOT A HEURISTIC
Background sites are enumerated by name. A call site nobody has classified resolves to STANDARD —
the safe direction: an unclassified background job merely costs full price, whereas defaulting the
other way would silently put an owner-facing turn on the slow tier. Adding a site to
``_BACKGROUND_CALL_SITES`` is therefore a deliberate, reviewable act.

FAST IS TINY AND NEVER INHERITED
Fast is allow-listed by call site and can never be a default anything falls into. Today it holds
exactly one live entry: the approval-resolution classifier. Opt-out/STOP is in the policy's Fast
class but is resolved DETERMINISTICALLY (no LLM call), so there is no model latency to buy down —
listing it here would be theatre. The policy's third Fast case (first response to an active buying
customer) is explicitly future work and is not listed.
"""

from __future__ import annotations

import logging
import os
from uuid import UUID

logger = logging.getLogger(__name__)

STANDARD = "standard"
FLEX = "flex"
FAST = "fast"

#: Nobody is waiting: scheduled passes, queue-drained jobs, offline generation. These are the ONLY
#: sites eligible for flex. Sourced from the policy's own list plus the VT-732 call-site inventory.
_BACKGROUND_CALL_SITES: frozenset[str] = frozenset({
    "week_plan_revision",      # the daily 7-day-plan revision pass
    "distill",                 # memory distillation
    "theme_clustering",        # KG/theme population
    "type_reconcile",          # entity re-enrichment / reconciliation
    "impact_judge",            # §7C impact judgments
    "completion_verification", # post-hoc verification of a settled task
    "plan_validation",         # validation of an already-drafted plan
})

#: A person is waiting AND the seconds carry risk or money. Deliberately ONE entry — see the module
#: docstring. `classify_owner_message` is the LLM fallback behind `resolve_decision_from_reply`:
#: the owner has just said yes/no to a money action, and every second of delay is the window the
#: VT-734 duplicate-request race lives in. Paying 2× here is partly a SAFETY spend.
_FAST_CALL_SITES: frozenset[str] = frozenset({"classify_owner_message"})

#: Never flex, whatever else says so — a gate that flakes on capacity-unavailable is a gate nobody
#: trusts. Bundle GENERATION may flex; judge SCORING may not. Enforced ahead of every other rule.
_NEVER_FLEX_CALL_SITES: frozenset[str] = frozenset({
    "self_evaluate_gate",
    "impact_judge_scoring",
})

_FLEX_MODES = frozenset({"off", "background", "all"})


def flex_mode() -> str:
    """``TEAM_GPT_FLEX`` = off | background | all. Read FRESH per call, like the tier vars.

    Default ``background`` — the policy's intended posture. ``all`` exists so Fazal can force-test
    the flex path; it is NOT a prod posture and deliberately still cannot touch the Fast or
    never-flex sites.
    """
    raw = (os.environ.get("TEAM_GPT_FLEX") or "background").strip().lower()
    if raw not in _FLEX_MODES:
        logger.warning("TEAM_GPT_FLEX=%r invalid (expected off|background|all); using 'background'", raw)
        return "background"
    return raw


def is_background(call_site: str | None) -> bool:
    return bool(call_site) and call_site in _BACKGROUND_CALL_SITES


def resolve_service_tier(
    call_site: str | None,
    *,
    tenant_id: UUID | str | None = None,
    fast_budget_check: object = None,
) -> str:
    """The Viabe-facing tier for ONE call. Returns ``standard`` | ``flex`` | ``fast``.

    Order matters and is the safety argument:
      1. never-flex sites short-circuit to standard — no mode can override a gate's reliability.
      2. Fast allow-list, subject to the per-tenant daily budget.
      3. background + flex enabled -> flex.
      4. everything else -> standard, including every unrecognised call site.

    ``fast_budget_check`` is injected so the budget lookup stays testable and so a budget-store
    failure can never take down a live approval turn (see ``_fast_allowed``).
    """
    if call_site in _NEVER_FLEX_CALL_SITES:
        return STANDARD

    if call_site in _FAST_CALL_SITES:
        if _fast_allowed(tenant_id, fast_budget_check):
            return FAST
        # Degrade to STANDARD, never to FLEX: a decisive moment never gets the slow tier.
        logger.warning(
            "VT-735 fast budget exhausted for tenant=%s call_site=%s — degrading to standard",
            tenant_id, call_site,
        )
        return STANDARD

    mode = flex_mode()
    if mode == "all" or (mode == "background" and is_background(call_site)):
        return FLEX
    return STANDARD


def _fast_allowed(tenant_id: UUID | str | None, fast_budget_check: object) -> bool:
    """Whether this tenant may still spend Fast today.

    FAIL-OPEN, deliberately and narrowly: if the budget store errors we allow the Fast call. The
    downside is a few rupees of overspend on a rare call; the downside of failing closed is slowing
    the approval path — the one turn where latency is a SAFETY property, because of the VT-734
    duplicate-request race. A budget is a cost control, and a cost control must not become a
    correctness risk. Exhaustion (a real, successful 'no') still degrades.
    """
    if fast_budget_check is None:
        return True
    try:
        return bool(fast_budget_check(tenant_id))  # type: ignore[operator]
    except Exception:  # noqa: BLE001 — see the fail-open rationale above
        logger.warning("VT-735 fast-budget check failed; allowing the call", exc_info=True)
        return True


def api_service_tier(viabe_tier: str) -> str | None:
    """Map the Viabe-facing tier onto the OpenAI ``service_tier`` request value.

    ``standard`` -> None (omit the field; 'standard' is not an OpenAI enum value and omitting it
    uses the account default). ``fast`` is sent as ``"fast"`` — VERIFIED against
    https://developers.openai.com/api/docs/pricing on 2026-08-06: the API accepts both ``priority``
    and ``fast``, Priority processing having been renamed Fast mode on 2026-07-30. We send the
    current name.
    """
    if viabe_tier == STANDARD:
        return None
    return viabe_tier


def billing_tier_for(viabe_tier: str) -> str:
    """What the LEDGER should record, which is not always what we asked the API for.

    ``flex`` and ``fast`` are recorded as themselves so ``compute_cost_usd`` applies the ½× discount
    or the 2× premium. Anything else records ``standard`` — including ``auto``, where OpenAI picks
    the tier server-side and we cannot know the billed rate at write time; recording full price is
    the conservative direction (under-costing is the error that flatters us).
    """
    return viabe_tier if viabe_tier in (FLEX, FAST) else STANDARD


__all__ = [
    "FAST", "FLEX", "STANDARD",
    "api_service_tier", "billing_tier_for", "flex_mode", "is_background", "resolve_service_tier",
]
