"""VT-462 — the onboarding CONDUCTOR (dynamic, brain-conducted onboarding).

The SCRIPTED question-QUEUE (``journey._compose_queue`` → a fixed list read in cursor order) is
replaced by a DYNAMIC decision: the conductor reasons WHICH question to ask NEXT and HOW to phrase
it, given (a) the declarative prereq set = WHAT must still be collected, (b) the discovered draft
profile, (c) what the owner has already answered / volunteered / skipped. It is BOUNDED by the
registry (it never invents a required field outside the candidate set) and the deterministic
completion check OWNS "complete" — the conductor NEVER self-declares done.

Division (design §7): the **prereq set bounds WHAT**; the **conductor owns the ACTION = how/what to
ask next**. This module is that action-decider. REUSE, no duplication:

  - ``question_brain.compose_onboarding_questions`` is the CANDIDATE-question source (the
    registry-grounded, business-type-reasoned set of confirm + gap questions). The conductor does NOT
    build a parallel composer — it picks/orders/phrases FROM these candidates.
  - ``journey`` state (``answers`` / ``skipped`` / the draft) is the resumability substrate — the
    conductor reads it to know what's already done; it does NOT build a parallel state machine.

THE DYNAMIC DECISION (``decide_next_question``) — deterministic, LLM-free skeleton (the candidate
set already encodes the business-type REASONING via the question-brain's Haiku call):

  1. Pull the candidate question set (confirm-first, then gaps) from ``compose_onboarding_questions``,
     excluding fields the owner ALREADY answered (``answered=...``) — so a VOLUNTEERED or
     OUT-OF-ORDER answer is never re-asked (the candidate set drops answered fields at source).
  2. Drop any candidate the owner explicitly SKIPPED (revisit-later policy: skipped fields are
     deferred, not re-pressed every turn — they only come back if ``revisit_skipped=True``).
  3. The NEXT question = the FIRST remaining candidate (confirm-the-draft beats gap-fill — the
     never-assert ordering of the question-brain is preserved). No remaining candidate → ``None``
     (a SIGNAL that the registry-bounded set is satisfied — but the *decision to complete* is the
     deterministic check's, not this function's; see ``profile_collection_complete``).

WHY this is "dynamic" not "scripted": the candidate set is recomputed FROM CURRENT STATE every turn
(not frozen at journey-start), so it absorbs out-of-order / volunteered / corrected answers live —
the owner can answer Q3 before Q1, volunteer a field never asked, or correct a confirmed value, and
the next-question decision re-derives against the new known-set rather than blindly advancing a
cursor. The expensive business-type reasoning (which gap fields THIS business needs) stays in the
question-brain's one Haiku call (cached candidate basis); the conductor's per-turn pick is cheap +
deterministic over that basis.

CL-390: business context only — never third-party PII (inherited from the question-brain).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Sequence
from uuid import UUID

from orchestrator.onboarding import question_brain
from orchestrator.onboarding.question_brain import Question

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConductorDecision:
    """The conductor's per-turn decision.

    ``next_question`` is the ``Question`` to ask next (None = no registry-bounded question remains —
    the deterministic completion check then owns whether onboarding is COMPLETE). ``remaining`` is
    the full ordered set of still-needed candidates (diagnostics + the completion signal substrate);
    ``known`` / ``skipped`` echo the state the decision was made against (test/observability).
    """

    next_question: Question | None
    remaining: tuple[Question, ...]
    known: tuple[str, ...]
    skipped: tuple[str, ...]


def _candidate_questions(
    business_type: str | None,
    draft: dict[str, Any] | None,
    answered: Sequence[str],
    *,
    llm_fn: Callable[[str, dict[str, Any], list[str]], list[dict[str, Any]]] | None = None,
) -> list[Question]:
    """The registry-grounded candidate set (REUSE the question-brain) with ALREADY-ANSWERED fields
    excluded at source — so volunteered / out-of-order answers are never re-asked.

    Resolved via the module attribute (``question_brain.compose_onboarding_questions``) at CALL time
    so callers/tests can monkeypatch the composer (parity with the pre-VT-462 ``_compose_queue``,
    which imported it locally per-call)."""
    return question_brain.compose_onboarding_questions(
        business_type or "other",
        draft,
        answered=list(answered),
        llm_fn=llm_fn,
    )


def decide_next_question(
    *,
    business_type: str | None,
    draft: dict[str, Any] | None,
    answered: Sequence[str],
    skipped: Sequence[str],
    revisit_skipped: bool = False,
    llm_fn: Callable[[str, dict[str, Any], list[str]], list[dict[str, Any]]] | None = None,
) -> ConductorDecision:
    """Decide the NEXT onboarding question DYNAMICALLY from current state (bounded by the registry).

    The decision is recomputed from CURRENT state every turn (not a frozen queue), so it absorbs
    out-of-order / volunteered / corrected answers:

      - ``answered`` fields are dropped at the candidate source (never re-asked — handles volunteered
        + out-of-order info).
      - ``skipped`` fields are DEFERRED (revisit-later): excluded unless ``revisit_skipped=True``
        (e.g. a final pass before completing), so the owner isn't re-pressed every turn.
      - confirm-the-draft questions still beat gap-fill (the never-assert ordering is preserved).

    Returns a ``ConductorDecision``. ``next_question is None`` SIGNALS the registry-bounded set is
    satisfied — it is NOT a self-declaration of "complete" (that is the deterministic check's call).
    """
    answered_set = set(answered)
    skipped_set = set(skipped)

    candidates = _candidate_questions(business_type, draft, sorted(answered_set), llm_fn=llm_fn)

    remaining: list[Question] = []
    for q in candidates:
        if q.field in answered_set:
            continue  # already answered (defensive — the composer already drops these)
        if q.field in skipped_set and not revisit_skipped:
            continue  # deferred — revisit-later, not re-pressed every turn
        remaining.append(q)

    nxt = remaining[0] if remaining else None
    return ConductorDecision(
        next_question=nxt,
        remaining=tuple(remaining),
        known=tuple(sorted(answered_set)),
        skipped=tuple(sorted(skipped_set)),
    )


def profile_collection_complete(
    *,
    business_type: str | None,
    draft: dict[str, Any] | None,
    answered: Sequence[str],
    skipped: Sequence[str],
    llm_fn: Callable[[str, dict[str, Any], list[str]], list[dict[str, Any]]] | None = None,
) -> bool:
    """THE DETERMINISTIC completion check for profile-setup — the conductor NEVER self-declares this.

    Profile collection is complete IFF NO registry-bounded question remains that the owner has
    neither ANSWERED nor SKIPPED. Computed by re-running the candidate derivation with
    ``revisit_skipped=False`` (skipped fields are an OWNER decision to omit — they count as resolved,
    not as a gap that blocks completion) and asserting the remaining set is EMPTY.

    This is a pure function of state — the brain conducts the conversation, this owns "done". It is
    deliberately SEPARATE from ``decide_next_question`` so the completion decision can never be
    conflated with "the brain felt finished": a caller asks this, not the conductor's reasoning.

    NOTE — this gates the PROFILE-SETUP spine only (confirm-draft + business-context gaps). The
    FULL agent-activation bar (GST-verified + connector + customers + consent) stays the
    ``onboarding_gate`` / ``activation_registry`` deterministic check, evaluated AFTER the
    subsequent connect/integration step (design §3: "complete" = GST-verified + ≥1 connector +
    ≥1 customer + consent). Profile-collected is the FIRST deterministic gate; activation is the next.
    """
    decision = decide_next_question(
        business_type=business_type,
        draft=draft,
        answered=answered,
        skipped=skipped,
        revisit_skipped=False,
        llm_fn=llm_fn,
    )
    return decision.next_question is None


# --- The journey-state-backed conductor seam (resumability) ----------------------------------------


def next_question_for_tenant(
    tenant_id: UUID | str,
    *,
    llm_fn: Callable[[str, dict[str, Any], list[str]], list[dict[str, Any]]] | None = None,
) -> ConductorDecision:
    """Resume the conductor from JOURNEY STATE: read the tenant's draft + answers + skipped from the
    ``onboarding_journey`` row (the resumability substrate) and decide the next question.

    REUSE: journey state is the substrate (no parallel state machine). The conductor reads
    ``answers`` (the keys = answered fields, including volunteered/out-of-order/corrected ones) and
    ``skipped`` straight off the journey row, plus the discovered draft, and re-derives the next
    question — so a fresh inbound (each WhatsApp inbound is a new thread) resumes exactly where the
    owner left off, dynamically, without a frozen cursor.
    """
    from orchestrator.onboarding.draft_profile import get_draft
    from orchestrator.onboarding.journey import _tenant_phase_and_type, get_journey

    g = get_journey(tenant_id) or {}
    answers = dict(g.get("answers") or {})
    skipped = list(g.get("skipped") or [])
    _, business_type = _tenant_phase_and_type(tenant_id)
    draft = get_draft(tenant_id)

    return decide_next_question(
        business_type=business_type,
        draft=draft,
        answered=list(answers.keys()),
        skipped=skipped,
        llm_fn=llm_fn,
    )


__all__ = [
    "ConductorDecision",
    "decide_next_question",
    "next_question_for_tenant",
    "profile_collection_complete",
]
