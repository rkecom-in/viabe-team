"""VT-725 scope 1 — the ONE seam through which a turn asks the card corpus for anything.

## What was wrong

`card_serving.retrieve_cards_for_turn` has existed and worked since VT-723. It had **zero callers in
`src/`** — the only caller in the repo was its own canary. The engine, twelve tables and 118 eligible
cards were complete and unreachable: the Manager could not read a single card. That is the whole
distance between having a moat and the moat doing anything.

## Why a seam and not three call sites

There are already TWO steering mechanisms (O8 cards + per-tenant assignment, the deliberate lever;
VT-566 lesson read-back, the automatic one) and they must share ONE path into a turn. Two
prompt-assembly paths drift — that is exactly the failure VT-718/720 fixed on the output side. So
every retrieval a turn performs goes through this module, and the eventual flip to injection has one
place to happen rather than three.

## Shadow, and why that is structural rather than a promise

`knowledge_serving_mode()` reads `TEAM_KNOWLEDGE_SERVING` and can only return `off` (default) or
`shadow`; `active` is deliberately unreachable from an env var. On top of that
`CardServingResult.INJECTS_INTO_PROMPT` is a `ClassVar[bool] = False` — no instance can flip it, and
the result object carries card REFS and scores, never claim text. This module asserts that ClassVar
before it returns anything, so if a future change ever makes a served result injectable, the seam
refuses instead of quietly becoming a prompt path.

**Nothing here inserts a system block.** The caller receives a content-free attribution trace for
logging. Injection is a separate, code-level step gated on the baseline comparison and on Fazal (D3).

## Why it lands with serving OFF

Retrieval writes `decision_evidence_links` on every call — the causality substrate §6 needs for
ablation. VT-749 measured that **63 of the 100 retrieval-eligible cards scope NOTHING**, so an
unscoped card matches every context. Collecting the first evidence we ever have about the corpus
while most of it matches indiscriminately manufactures a baseline we would then have to distrust —
the same shape as the retracted recall figures, one layer up. So the call sites land wired and
DARK: `TEAM_KNOWLEDGE_SERVING` stays unset until VT-749 scope 1 lands, and the flip to shadow on dev
is then one variable, not a deploy of new code.

## Fail-soft is the contract, not the aspiration

The Manager worked without cards for months and must still. Every path here returns ``None`` on any
failure, including a misconfigured identity, and logs the failure TYPE only — a pydantic error
renders the offending card's claim text into its message, and card content has no business in a log.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover — import-light at runtime (dispatch imports this module)
    from orchestrator.knowledge.card_serving import CardServingResult

logger = logging.getLogger(__name__)


def _serving_enabled() -> bool:
    """True only in shadow. Checked BEFORE importing the serving stack, so a wired-but-dark call
    site costs one env read per turn and touches no database."""
    from orchestrator.feature_flags import knowledge_serving_mode

    return knowledge_serving_mode() == "shadow"


def _retrieve(
    *,
    tenant_id: UUID | str,
    run_id: UUID | str,
    identity: str,
    decision_id: str,
    objective: str,
    stage_name: str,
    conn: Any = None,
) -> CardServingResult | None:
    if not objective or not objective.strip():
        # No objective means no query. Retrieving against an empty string would score every card
        # on recency and confidence alone and call the result relevance.
        return None
    if not _serving_enabled():
        return None
    try:
        from orchestrator.agent_framework.retrieval_profiles import primary_domain_for
        from orchestrator.knowledge.card_serving import (
            CardServingResult as _Result,
            retrieve_cards_for_turn,
        )
        from orchestrator.knowledge.contracts import RetrievalStage

        if _Result.INJECTS_INTO_PROMPT:
            # The tripwire. Shadow's whole safety argument is that a served result cannot reach a
            # prompt; if that stops being true, this seam stops serving rather than becoming the
            # injection path by default.
            logger.error(
                "turn_retrieval: CardServingResult.INJECTS_INTO_PROMPT is True — refusing to serve "
                "(VT-725 is shadow-only; injection is a D3 decision, not a code drift)"
            )
            return None

        result = retrieve_cards_for_turn(
            tenant_id=tenant_id,
            run_id=run_id,
            decision_id=decision_id,
            objective=objective,
            stage=RetrievalStage(stage_name),
            domain=primary_domain_for(identity),
            identity=identity,
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 — a knowledge miss is never a failed turn
        logger.warning(
            "turn_retrieval: degraded to no-cards (tenant=%s identity=%s decision=%s error=%s)",
            tenant_id,
            identity,
            decision_id,
            type(exc).__name__,
        )
        return None

    # Counts and ids only — never a claim, a distillation note or a card key.
    logger.info(
        "turn_retrieval: identity=%s decision=%s candidates=%d selected=%d conflicts=%d "
        "degraded=%s links=%d elapsed_ms=%.1f",
        identity,
        decision_id,
        result.candidates,
        len(result.selected_card_refs),
        result.conflicts,
        result.degraded_reason,
        result.evidence_links_written,
        result.elapsed_ms,
    )
    return result


def manager_turn_decision_id(run_id: UUID | str, message_ref: str | None) -> str:
    """The attribution key for a Manager turn.

    Migration 183's uniqueness is ``(tenant, run, decision, card, disposition)``, which is what makes
    a REPLAYED DBOS step idempotent instead of a double-count in the ablation data. So the key must
    be derived from something stable across a replay — the inbound message ref when there is one,
    else the run — never a fresh uuid.
    """
    return f"manager_turn:{message_ref or str(run_id)}"


def specialist_decision_id(identity: str, task_ref: str | None) -> str:
    """The attribution key for one specialist dispatch. Stable across a replay for the same reason."""
    return f"specialist:{identity}:{task_ref or 'dispatch'}"


def retrieve_for_manager_turn(
    *,
    tenant_id: UUID | str,
    run_id: UUID | str,
    objective: str,
    message_ref: str | None = None,
    conn: Any = None,
) -> CardServingResult | None:
    """Retrieve for the Manager's framing-context assembly. Returns a content-free trace, or None.

    ``stage=planning``: the Manager's declared stages are triage/planning/review/verification, and
    this call happens where the turn assembles its framing context — the planning beat of the
    reasoning arc (understand → identify capability → define outcome → assemble framing context).
    """
    from orchestrator.agent_framework.retrieval_profiles import MANAGER_IDENTITY

    return _retrieve(
        tenant_id=tenant_id,
        run_id=run_id,
        identity=MANAGER_IDENTITY,
        decision_id=manager_turn_decision_id(run_id, message_ref),
        objective=objective,
        stage_name="planning",
        conn=conn,
    )


def retrieve_for_specialist(
    *,
    tenant_id: UUID | str,
    run_id: UUID | str,
    identity: str,
    objective: str,
    task_ref: str | None = None,
    conn: Any = None,
) -> CardServingResult | None:
    """Retrieve for one specialist dispatch — narrow by construction (scope 5).

    The narrowness is the PROFILE's, not this function's: ``retrieval_profile_for`` raises for an
    undeclared identity rather than inheriting the Manager's breadth, and the specialist's declared
    ``assignment_scopes`` are its own lane plus nothing. This seam only names the identity honestly;
    an identity with no declared profile retrieves nothing, which is the intended answer.
    """
    return _retrieve(
        tenant_id=tenant_id,
        run_id=run_id,
        identity=identity,
        decision_id=specialist_decision_id(identity, task_ref),
        objective=objective,
        stage_name="specialist",
        conn=conn,
    )


__all__ = [
    "manager_turn_decision_id",
    "retrieve_for_manager_turn",
    "retrieve_for_specialist",
    "specialist_decision_id",
]
