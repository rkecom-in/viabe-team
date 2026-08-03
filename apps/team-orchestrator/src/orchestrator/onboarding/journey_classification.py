"""VT-720 (S4) — the ONE journey-state classification every converted onboarding route shares.

Two seams used to answer "what is still outstanding?" independently, and both got it wrong in the
same way (measured 3/3, vt721_x3):

  - ``enforce_journey_gate`` rendered a pending city CONFIRM as the ask "which city you're based
    in" — inventing ignorance of a value the draft holds;
  - ``emission_gate`` Layer 3d pasted the pending question VERBATIM over the Manager's reply, so an
    owner who asked "how long will this take?" got "We found your shop is in Agra — is that right?"
    back, byte-identical to the previous turn.

One state, two answers, both template-voiced. So the state is derived HERE, once, and handed to the
composer as findings (``RouteClassification``) rather than re-templated per seam.

The load-bearing distinction is confirm-vs-gap: a queued CONFIRM carries its ``draft_value``, which
means the value IS KNOWN and only the owner's yes is outstanding. Collapsing that into "missing" is
the fabrication; keeping it separate is the fix.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from orchestrator.manager.route_classification import RouteClassification


def classify_journey_state(
    g: dict[str, Any],
    intent: str,
    *,
    extra_constraints: tuple[str, ...] = (),
    suggested_disposition: str = "answer_status",
) -> RouteClassification:
    """Derive the classification for an onboarding journey row.

    ``facts_known`` = recorded answers (confirmed; may be stated, never re-asked). Internal
    bookkeeping keys (``__flow__``, ``owner_email__pending``, ``__connect_offer_at__`` — all carry
    ``__``) are not facts about the business and are filtered out.
    ``facts_pending_confirm`` = ``{field: draft_value}`` for queued CONFIRMs — held values awaiting a
    yes. ``facts_missing`` = queued gaps, the only fields anyone may ask for.
    """
    answers = {
        k: v for k, v in (g.get("answers") or {}).items()
        if "__" not in str(k) and str(v or "").strip()
    }
    queue = list(g.get("question_queue") or [])
    cursor = int(g.get("cursor") or 0)
    pending = queue[cursor:] if 0 <= cursor <= len(queue) else []

    pending_confirm: dict[str, Any] = {}
    missing: list[str] = []
    for q in pending:
        fieldname = str(q.get("field") or "").strip()
        if not fieldname:
            continue
        value = str(q.get("draft_value") or "").strip()
        if q.get("kind") == "confirm" and value:
            pending_confirm[fieldname] = value
        else:
            missing.append(fieldname)

    constraints = [
        "Do not name or assume any sales platform (Shopify, Zomato, …) the owner has not named "
        "themselves.",
    ]
    if g.get("status") != "active":
        constraints.append(
            "The business profile is COMPLETE. Say so plainly; you may OFFER a next step, never "
            "assume one has begun."
        )
    else:
        constraints.append(
            f"{len(pending_confirm) + len(missing)} item(s) are outstanding — setup is NOT finished. "
            "Be specific and brief about what is left; keep it to one short WhatsApp message."
        )
    return RouteClassification(
        intent=intent,
        facts_known=answers,
        facts_pending_confirm=pending_confirm,
        facts_missing=tuple(missing),
        constraints=(*constraints, *extra_constraints),
        suggested_disposition=suggested_disposition,
    )


def compose_for_journey(
    tenant_id: UUID | str,
    owner_message: str,
    g: dict[str, Any],
    classification: RouteClassification,
    locale: str,
    *,
    rejected_draft: str | None = None,
) -> str | None:
    """Hand a journey classification to the composer; return the Manager's own line, or ``None``.

    ``None`` on ANY failure (no key, LLM error, read miss) — every caller keeps a deterministic
    fallback, because a converted route may lose its voice but never its answer. Composes against
    wire-truth history + the asserted-facts ledger, exactly like a normal onboarding turn, and
    records/advances NOTHING.
    """
    try:
        from orchestrator.onboarding import turn_brain
        from orchestrator.onboarding.draft_profile import get_draft
        from orchestrator.onboarding.journey import _merged_recent_history

        draft = get_draft(tenant_id) or {}
        g_aware = dict(g)
        g_aware["recent_turns"] = _merged_recent_history(tenant_id, g, None)
        try:
            from orchestrator.manager.asserted_facts import active_assertions

            g_aware["asserted_facts"] = active_assertions(tenant_id)
        except Exception:  # noqa: BLE001 — advisory context, never a gate on the turn
            g_aware["asserted_facts"] = []
        return turn_brain.compose_classified_reply(
            g_aware,
            dict(draft.get("attributes") or {}),
            owner_message,
            classification,
            locale=locale,
            provenance=dict(draft.get("provenance") or {}),
            rejected_draft=rejected_draft,
        )
    except Exception:  # noqa: BLE001 — best-effort; the caller's deterministic line still answers
        return None


__all__ = ["classify_journey_state", "compose_for_journey"]
