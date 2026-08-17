"""VT-749 scope 1 — the applicability scoping delta, applied through the plan builder.

## What this fixes

`card_retrieval._dimension_match` returns True for an EMPTY dimension without consulting the context,
and the hedge that was supposed to restrain that is worth at most **0.083** of a final score against a
**0.250** retrieval floor — a penalty no gate reads. So an eligible card that declares no jurisdiction,
size band, industry, maturity stage or channel, and does not declare `universal`, **matches every
tenant in every context** while looking cautious. Measured on the committed v3 artifacts: **63 of the
100 retrieval-eligible cards** were in exactly that state, and **not one** eligible card declared
`universal=true` — so the 63 were not cards claiming universality, they were cards claiming nothing and
receiving it by default.

This module applies the product judgment (Clau, 2026-08-17) that gives each of the 63 an HONEST scope,
so that "applies everywhere" becomes a DECLARED property rather than an absence. That inversion is the
row's real product: after this, `universal=true` means someone decided it.

## TWO id layers, and the delta carries both

The delta's `card_id` is the stable logical card id — the key the PLAN uses, and the right key for a
scoping judgment, because the judgment is about the card, not about one serialization of it. Its
`card_version_id` is the PERSISTED `knowledge_cards.id`, which is the key the DATABASE landing needs.
Measured against both: all 63 `card_id`s match plan members, all 63 `card_version_id`s match persisted
rows, and neither set matches the other layer. So this module joins on `card_id` and records the version
id for the DB step rather than pretending one key addresses both.

## Why a delta file and not an edit of `candidate_cards.jsonl`

Same reason the deferral resolution is a delta: the source artifact records what was INGESTED, and a
later judgment about scope is a separate, reviewable act with its own author and date. Editing the
ingestion record in place would make the two indistinguishable a month from now.

## The classes (Clau's judgment; the reasoning is in `.viabe/sprint/VT-749.md`)

| class | n | scope |
|---|---|---|
| `U` | 42 | `universal: true` — pure judgment-process cards (triage, arbitration, pre-mortems, cadence, negotiation discipline, cash-forecast and payment controls) |
| `ST` | 11 | `size_bands: small, medium` — presume staff/teams to act on |
| `OP` | 6 | `channels: online_presence` — the mechanism needs an online surface |
| `B2B` | 2 | `industries: wholesale_distribution, b2b_services` |
| `SUB` | 1 | `industries: subscription_services` |
| `SCALE` | 1 | `size_bands + maturity_stages: scaling` |

The vocabulary above is the corpus vocabulary; future cards use these values rather than synonyms.

## What is preserved

The patch replaces SCOPING dimensions only. `effective_from` / `effective_to` are carried over from the
card's own applicability untouched — dropping a card's time window while "scoping" it would silently
widen exactly what this row narrows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.knowledge.contracts import Applicability, KnowledgeCard


class ScopingError(RuntimeError):
    """The scoping delta does not describe the corpus it claims to scope."""


#: Exact, like every other count in this row: a range would let the corpus drift back toward unscoped
#: without anyone noticing, which is the failure mode the gate exists to prevent.
_EXPECTED_PATCHES = 63

#: The dimensions `card_retrieval._dimension_match` consults. A card filling none of them, and not
#: declaring `universal`, matches every context.
SCOPING_DIMENSIONS = ("jurisdictions", "size_bands", "industries", "maturity_stages", "channels")


class ApplicabilityPatch(BaseModel):
    """The scoping half of an ``Applicability`` — no effective window, by construction.

    ``extra="forbid"`` so a typo'd dimension name is a load error rather than a patch that silently
    scopes nothing, which would leave the card exactly as universal as before while looking fixed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    jurisdictions: tuple[str, ...] = ()
    size_bands: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    maturity_stages: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()
    universal: bool = False

    def is_empty(self) -> bool:
        return not self.universal and not any(
            getattr(self, dim) for dim in SCOPING_DIMENSIONS
        )


class ScopingRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    card_id: str = Field(min_length=1, max_length=200)
    card_version_id: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=200)
    scoping_class: str = Field(min_length=1, max_length=16, alias="class")
    applicability_patch: ApplicabilityPatch


def load_applicability_scoping(rows: Sequence[Mapping[str, Any]]) -> tuple[ScopingRow, ...]:
    """Parse and validate the delta. Fails closed on anything that would scope less than it claims."""

    try:
        parsed = tuple(ScopingRow.model_validate(row) for row in rows)
    except (TypeError, ValueError) as exc:
        raise ScopingError(f"invalid VT-749 applicability scoping: {exc}") from exc
    if len(parsed) != _EXPECTED_PATCHES:
        raise ScopingError(
            f"expected {_EXPECTED_PATCHES} scoping rows, got {len(parsed)} — the delta must scope "
            "exactly the measured unscoped-but-eligible set"
        )
    if len({row.card_id for row in parsed}) != len(parsed):
        raise ScopingError("scoping rows must target distinct cards")
    if len({row.card_version_id for row in parsed}) != len(parsed):
        raise ScopingError("scoping rows must target distinct persisted card versions")
    empty = [row.card_version_id for row in parsed if row.applicability_patch.is_empty()]
    if empty:
        raise ScopingError(
            f"{len(empty)} patch(es) declare no scope and no universal flag — a patch that scopes "
            "nothing leaves the card matching every context while looking fixed"
        )
    return parsed


def _scoped_applicability(card: KnowledgeCard, patch: ApplicabilityPatch) -> Applicability:
    """Apply the patch's SCOPING dimensions, carrying the card's own effective window across.

    ``Applicability`` itself enforces universal-XOR-dimensions, so a patch that declared both would
    raise here rather than persisting an incoherent scope.
    """
    current = card.applicability
    return Applicability(
        jurisdictions=patch.jurisdictions,
        size_bands=patch.size_bands,
        industries=patch.industries,
        maturity_stages=patch.maturity_stages,
        channels=patch.channels,
        universal=patch.universal,
        effective_from=getattr(current, "effective_from", None),
        effective_to=getattr(current, "effective_to", None),
    )


def apply_applicability_scoping(
    cards: Sequence[KnowledgeCard], rows: Sequence[ScopingRow]
) -> tuple[KnowledgeCard, ...]:
    """Return the cards with the delta applied. Every row MUST match a card, and every targeted card
    MUST currently be unscoped — otherwise the delta is describing a corpus this is not. Joined on
    ``card_id`` (the plan's key); ``card_version_id`` addresses the persisted rows and is used by the
    database landing, not here.

    Requiring the target to be unscoped is the load-bearing check: it means this can never be applied
    twice with different judgments, and it means a card that acquired a scope some other way is a
    conflict to look at rather than something to overwrite.
    """
    by_card = {card.card_id: card for card in cards}
    missing = [row.card_id for row in rows if row.card_id not in by_card]
    if missing:
        raise ScopingError(
            f"{len(missing)} scoping row(s) target cards absent from the plan — the delta and the "
            "corpus disagree; re-derive rather than skipping them"
        )

    patch_by_card = {row.card_id: row.applicability_patch for row in rows}
    already_scoped = [
        cid
        for cid in patch_by_card
        if getattr(by_card[cid].applicability, "universal", False)
        or any(getattr(by_card[cid].applicability, dim, ()) for dim in SCOPING_DIMENSIONS)
    ]
    if already_scoped:
        raise ScopingError(
            f"{len(already_scoped)} targeted card(s) already carry a scope — refusing to overwrite a "
            "scoping decision this delta was not reviewed against"
        )

    return tuple(
        card.model_copy(
            update={"applicability": _scoped_applicability(card, patch_by_card[card.card_id])}
        )
        if card.card_id in patch_by_card
        else card
        for card in cards
    )


def unscoped_eligible(cards: Sequence[KnowledgeCard]) -> tuple[KnowledgeCard, ...]:
    """The cards this row exists to eliminate: retrieval-eligible, no declared scope, not universal."""
    return tuple(
        card
        for card in cards
        if card.retrieval_eligible
        and not getattr(card.applicability, "universal", False)
        and not any(getattr(card.applicability, dim, ()) for dim in SCOPING_DIMENSIONS)
    )


__all__ = [
    "SCOPING_DIMENSIONS",
    "ApplicabilityPatch",
    "ScopingError",
    "ScopingRow",
    "apply_applicability_scoping",
    "load_applicability_scoping",
    "unscoped_eligible",
]
