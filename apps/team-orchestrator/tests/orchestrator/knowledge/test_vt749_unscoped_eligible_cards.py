"""VT-749 gate (a) — pin the unscoped-eligible-card count so it cannot drift silently.

THE DEFECT this measures. `card_retrieval._dimension_match` returns True for an EMPTY dimension
**without ever consulting the context value**, appending an `unknown_<dim>` hedge instead. `_applicability`
hard-filters only on the effective window. So a card with no jurisdiction, no size band, no industry, no
maturity stage, no channel and `universal=false` is **applicable to every possible context** — every
tenant, every channel, every jurisdiction — while looking cautious.

The hedge that was supposed to restrain it is decorative: `applicability_score = max(0, 1 - 0.15 *
len(unknown_dimensions))` carries weight `0.08` of seven renormalized weights, so the **maximum** final-score
delta between an all-empty card and a fully scoped one is **0.083**, against a
`MEASURED_RETRIEVAL_FLOOR` of 0.250. And `hedge_reasons` is stored, copied and printed by three call
sites — no profile, gate, filter or threshold reads it. `_dimension_match`'s own docstring says empty is
*"treated as UNKNOWN on that dimension, not as unrestricted"*; the code does the opposite.
`card_serving.py` closed the mirror-image hole on the CONTEXT side by hardcoding `_JURISDICTION = "IN"`,
with a comment naming exactly this risk. The CARD side was left open.

WHY THIS IS A TEST AND NOT A VALIDATOR. Adding `retrieval_eligible ⇒ scoped or universal` to the card
contract was tried and reverted: it breaks the v3 plan build (22 failures + 10 errors, because the plan
itself promotes these cards) and — worse — it would break REHYDRATION of already-persisted rows, since
`registry_seed._card_from_row` → `model_validate` and `card_serving` RAISES rather than degrades. Dev
already holds these rows, so the validator would take the serving path down the first time it read one.
**Enforcement cannot precede the data.** So this file measures and pins; VT-749 scope 2 adds the
invariant once the 63 cards carry honest scopes.

**SCOPE 1 LANDED 2026-08-17 and the pin is INVERTED.** `registry_scoping` applies Clau's 63-card
delta through the plan builder, and the count this file exists to watch is now **ZERO** unscoped
eligible cards with **42** declaring `universal=true`. Both facts are pinned: the raw plan still
measures 63 (the defect is historical fact, not something to erase) and the SCOPED plan must measure
0. The inversion is the row's real product — after it, "applies everywhere" is a declared decision
rather than an absence, and a card that scopes nothing again is a test failure instead of a silent
addition to a corpus that matches every tenant.

WHY IT MATTERS BEYOND TIDINESS: VT-725 wires the retrieval call site. The moment retrieval serves this
corpus, these cards are eligible for every tenant — and the `o11` recall figure of **0.229 was measured
under exactly that condition**, so the recall number is itself partly an artifact of cards that match
indiscriminately.

These are exact counts on purpose. A range would let the corpus drift back toward unscoped without
anyone noticing, which is the failure mode this gate exists to prevent — and every number here is
reproducible from committed artifacts with no database.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pydantic")

from orchestrator.knowledge.registry_full import build_full_plan, load_independence_audit  # noqa: E402
from orchestrator.knowledge.registry_resolution import (  # noqa: E402
    build_resolution_plan,
    load_resolution_delta,
)

ROOT = Path(__file__).resolve().parents[5]
CORPUS = ROOT / "apps" / "team-orchestrator" / "knowledge_corpus"

#: The dimensions `_dimension_match` consults. An eligible card that fills NONE of them, and does not
#: declare `universal`, matches every context.
_SCOPING_DIMENSIONS = ("jurisdictions", "size_bands", "industries", "maturity_stages", "channels")

# Measured 2026-08-14 from the committed v3 artifacts. Exact, so drift is loud.
_EXPECTED_MEMBERS = 118
_EXPECTED_ELIGIBLE = 100
#: The defect as first measured, kept because the historical fact is what justified the row.
_EXPECTED_UNSCOPED_ELIGIBLE_RAW = 63
_EXPECTED_UNIVERSAL_ELIGIBLE_RAW = 0
#: After VT-749 scope 1. These are the numbers that now govern.
_EXPECTED_UNSCOPED_ELIGIBLE_SCOPED = 0
_EXPECTED_UNIVERSAL_ELIGIBLE_SCOPED = 42
#: Clau's classing, 2026-08-17. Pinned so a re-class is a deliberate edit with a reason.
_EXPECTED_CLASS_COUNTS = {"U": 42, "ST": 11, "OP": 6, "B2B": 2, "SUB": 1, "SCALE": 1}


def _jsonl(name: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (CORPUS / name).read_text(encoding="utf-8").splitlines()
        if line
    ]


@pytest.fixture(scope="module")
def v3_cards() -> list[Any]:
    """The v3 shadow plan's cards, built the same way the VT-727 tests build it."""
    audit = load_independence_audit(
        json.loads((CORPUS / "independence_audit.json").read_text(encoding="utf-8"))
    )
    full = build_full_plan(_jsonl("source_rights.jsonl"), _jsonl("candidate_cards.jsonl"), audit)
    plan = build_resolution_plan(full, load_resolution_delta(_jsonl("deferral_resolution_delta.jsonl")))
    # ResolutionPlan exposes .members as KnowledgeCard objects directly (not wrappers with .card) —
    # verified against the model rather than assumed from the sibling VT-727 tests, which read .promotions.
    return list(plan.members)


@pytest.fixture(scope="module")
def scoping_rows() -> Any:
    from orchestrator.knowledge.registry_scoping import load_applicability_scoping

    return load_applicability_scoping(_jsonl("vt749_applicability_scoping.jsonl"))


@pytest.fixture(scope="module")
def scoped_cards(v3_cards, scoping_rows) -> list[Any]:
    """The v3 plan with VT-749 scope 1 applied — what the corpus MEANS now."""
    from orchestrator.knowledge.registry_scoping import apply_applicability_scoping

    return list(apply_applicability_scoping(v3_cards, scoping_rows))


def _applicability(card: Any) -> Any:
    return getattr(card, "applicability", None)


def _is_eligible(card: Any) -> bool:
    # `retrieval_eligible` is a TOP-LEVEL KnowledgeCard field, not nested under a `serving` object —
    # verified by introspecting the model rather than inferred from a sibling test.
    return bool(getattr(card, "retrieval_eligible", False))


def _scoped_dimension_count(card: Any) -> int:
    app = _applicability(card)
    if app is None:
        return 0
    return sum(1 for dim in _SCOPING_DIMENSIONS if getattr(app, dim, None))


def _is_universal(card: Any) -> bool:
    app = _applicability(card)
    return bool(getattr(app, "universal", False))


def test_the_v3_plan_still_has_118_members_and_100_retrieval_eligible(v3_cards):
    """Anchor. If these move, every count below is about a different corpus and must be re-derived
    rather than adjusted."""
    assert len(v3_cards) == _EXPECTED_MEMBERS
    assert sum(1 for c in v3_cards if _is_eligible(c)) == _EXPECTED_ELIGIBLE


def test_exactly_63_retrieval_eligible_cards_SCOPE_NOTHING(v3_cards):
    """THE MEASUREMENT. An eligible card with zero scoping dimensions and no `universal` flag matches
    every tenant, channel and jurisdiction — while the only penalty is 0.083 of a score no gate reads.

    If this number FALLS, VT-749 scope 1 is landing and the expectation should be lowered deliberately,
    with the scoping decisions recorded. If it RISES, unscoped cards are being added and the gate has
    done its job. Either way it must be a decision, not a surprise.
    """
    unscoped = [
        c for c in v3_cards
        if _is_eligible(c) and _scoped_dimension_count(c) == 0 and not _is_universal(c)
    ]
    assert len(unscoped) == _EXPECTED_UNSCOPED_ELIGIBLE_RAW, (
        f"expected {_EXPECTED_UNSCOPED_ELIGIBLE_RAW} unscoped-but-eligible cards, found {len(unscoped)}. "
        "These match EVERY context. If the corpus was intentionally rescoped, update the constant in "
        "the same change and say which cards were scoped and on what judgment (VT-749 scope 1)."
    )


def test_no_eligible_card_claims_universal_so_the_63_are_not_deliberate(v3_cards):
    """The distinction that makes the 63 a defect rather than a design. `universal=true` is the honest
    way to say "this applies everywhere" — and NOT ONE eligible card uses it. So the 63 are not cards
    declaring universality; they are cards that declare nothing and get universality by default."""
    universal_eligible = [c for c in v3_cards if _is_eligible(c) and _is_universal(c)]
    assert len(universal_eligible) == _EXPECTED_UNIVERSAL_ELIGIBLE_RAW, (
        "an eligible card now declares universal=true — if that is the intended fix for part of the 63, "
        "this constant moves and the unscoped count must fall by the same amount"
    )


def test_the_hedge_is_still_worth_less_than_the_retrieval_floor():
    """Pins WHY the hedge cannot be the control. Six unknown dimensions cost at most
    `0.15 * 6` of the applicability sub-score, weighted 0.08 — a final-score delta far under the
    retrieval floor. If someone later argues "the hedge handles it", this is the arithmetic.

    Read from the module's own constants so it tracks a reweighting instead of asserting a stale number.
    """
    from orchestrator.knowledge import card_retrieval as cr

    weights = getattr(cr, "_SCORE_WEIGHTS", None) or getattr(cr, "SCORE_WEIGHTS", None)
    if weights is None or "applicability" not in dict(weights):
        pytest.skip("score weights not exposed under a known name — re-point this assertion")
    w = dict(weights)
    applicability_weight = float(w["applicability"]) / float(sum(float(v) for v in w.values()))
    max_delta = applicability_weight * 1.0  # a fully-hedged card can lose at most its whole sub-score
    floor = float(getattr(cr, "MEASURED_RETRIEVAL_FLOOR", 0.25))
    assert max_delta < floor, (
        f"the applicability hedge can move the final score by at most {max_delta:.3f}, against a "
        f"retrieval floor of {floor:.3f} — it cannot exclude an unscoped card, so it is a label and "
        "not a control. VT-749 scope 3 decides whether it should carry weight at all."
    )


def test_the_worklist_the_scoping_delta_had_to_cover(v3_cards, scoping_rows):
    """The worklist scope 1 was built against — kept as the JOIN that proves the delta covered exactly
    the measured set, rather than 63 cards of its own choosing. Deriving this by hand from the
    artifacts is how the count got re-derived three times before it was a test."""
    # Keyed on card_id: the plan's KnowledgeCard carries no legacy_id at this stage (provenance holds
    # source_ids / publisher / retrieved_at / tainted), and card_id is the stable identifier the
    # scoping work will address them by.
    unscoped = sorted(
        str(getattr(c, "card_id", ""))
        for c in v3_cards
        if _is_eligible(c) and _scoped_dimension_count(c) == 0 and not _is_universal(c)
    )
    assert len(unscoped) == _EXPECTED_UNSCOPED_ELIGIBLE_RAW
    assert all(unscoped), "every unscoped card must be identifiable — an unnamed one cannot be fixed"
    assert sorted(row.card_id for row in scoping_rows) == unscoped, (
        "the scoping delta must target EXACTLY the measured unscoped set — not a superset (scoping a "
        "card nobody reviewed as unscoped) and not a subset (leaving match-everything cards behind)"
    )


# --- VT-749 scope 1: the inverted pin, which is what now governs -----------------------------


def test_after_scoping_NO_eligible_card_scopes_nothing(scoped_cards):
    """THE INVERSION, and the row's real product.

    Before: 63 eligible cards declared nothing and matched every tenant in every context. After: zero.
    If this rises, an unscoped card has been added to a corpus that VT-725 serves — which is the
    condition the `o11` recall figure of 0.229 was measured under, so it also silently invalidates any
    recall comparison made across it.
    """
    unscoped = [
        c for c in scoped_cards
        if _is_eligible(c) and _scoped_dimension_count(c) == 0 and not _is_universal(c)
    ]
    assert len(unscoped) == _EXPECTED_UNSCOPED_ELIGIBLE_SCOPED, (
        f"{len(unscoped)} eligible card(s) still scope nothing: "
        f"{[getattr(c, 'card_id', '?') for c in unscoped][:8]}. Every eligible card must declare its "
        "applicability or declare `universal=true` deliberately (VT-749 scope 1)."
    )


def test_after_scoping_universal_is_declared_by_exactly_the_42_judgment_cards(scoped_cards):
    """`universal=true` is now a DECISION, which is the whole difference from the defect. 42 cards
    claim it — the pure judgment-process ones (decision triage, arbitration, pre-mortems, cadence,
    negotiation discipline, cash and payment controls) that genuinely apply to every tenant we serve.
    """
    universal_eligible = [c for c in scoped_cards if _is_eligible(c) and _is_universal(c)]
    assert len(universal_eligible) == _EXPECTED_UNIVERSAL_ELIGIBLE_SCOPED


def test_scoping_preserves_the_corpus_shape(scoped_cards, v3_cards):
    """Scoping is a scope change and nothing else: same members, same eligibility, same claims. If a
    card count moved, the delta did more than it was reviewed to do."""
    assert len(scoped_cards) == len(v3_cards) == _EXPECTED_MEMBERS
    assert sum(1 for c in scoped_cards if _is_eligible(c)) == _EXPECTED_ELIGIBLE
    before = {c.card_id: c.claim for c in v3_cards}
    assert {c.card_id: c.claim for c in scoped_cards} == before


def test_the_delta_classes_are_pinned(scoping_rows):
    """A re-class must be a deliberate edit with a reason, not a quiet reshuffle."""
    import collections

    counts = collections.Counter(row.scoping_class for row in scoping_rows)
    assert dict(counts) == _EXPECTED_CLASS_COUNTS


def test_scoping_cannot_be_applied_twice(scoped_cards, scoping_rows):
    """Applying a scoping judgment onto already-scoped cards would overwrite a decision this delta was
    never reviewed against. It must refuse rather than win."""
    from orchestrator.knowledge.registry_scoping import ScopingError, apply_applicability_scoping

    with pytest.raises(ScopingError):
        apply_applicability_scoping(scoped_cards, scoping_rows)


def test_a_patch_that_scopes_nothing_is_refused():
    """The failure mode worth guarding: a patch with a typo'd or empty dimension set would leave the
    card exactly as universal-by-default as before, while the delta reports 63 cards 'scoped'."""
    from orchestrator.knowledge.registry_scoping import ScopingError, load_applicability_scoping

    rows = _jsonl("vt749_applicability_scoping.jsonl")
    rows[0] = {**rows[0], "applicability_patch": {}}
    with pytest.raises(ScopingError):
        load_applicability_scoping(rows)


def test_the_effective_window_survives_scoping(v3_cards, scoped_cards):
    """A card's time bound is not a scoping dimension. Dropping it while 'scoping' the card would
    widen exactly what this row narrows."""
    before = {
        c.card_id: (c.applicability.effective_from, c.applicability.effective_to) for c in v3_cards
    }
    after = {
        c.card_id: (c.applicability.effective_from, c.applicability.effective_to)
        for c in scoped_cards
    }
    assert after == before
