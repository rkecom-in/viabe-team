"""VT-725 floor recalibration — dump per-(case, card) score components for EVERY card.

The engine's `retrieve()` applies top_k (<=20) and the floor, so it cannot show a 100-card
distribution. This re-computes the SAME arithmetic over the full pool and then CROSS-CHECKS itself
against the real engine's returned components, so the dump is provably the engine's numbers and not
a lookalike reimplementation.

Emits one JSON with, per case, every card's components under the current scale, plus the card claim
text needed for blind relevance labelling. No labels, no floor decision here — measurement only.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

sys.path.insert(0, "apps/team-orchestrator/src")
sys.path.insert(0, "apps/team-orchestrator/canaries")

from orchestrator.advice_eval import DatasetSplit, load_dataset  # noqa: E402
from orchestrator.agent_framework.retrieval_profiles import MANAGER_RETRIEVAL_PROFILE  # noqa: E402
from orchestrator.knowledge import card_retrieval as cr  # noqa: E402
from orchestrator.knowledge.card_serving import (  # noqa: E402
    embed_cards,
    embed_query,
    load_serving_corpus,
)
from orchestrator.knowledge.contracts import KnowledgeDomain, KnowledgeScopeKind  # noqa: E402

from o11_knowledge import _direct_connection, _jurisdiction_of, _objective_for  # noqa: E402

TENANT = UUID(sys.argv[1]) if len(sys.argv) > 1 else UUID("63211ce5-0000-0000-0000-000000000000")
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("floor_components.json")
DATASET_DIR = Path("apps/team-orchestrator/canaries/o11")
EVAL_DOMAIN = KnowledgeDomain.MANAGEMENT


def context_for(case, tenant_id):
    p = case.business_profile
    return cr.RetrievalBusinessContext(
        tenant_id=tenant_id,
        jurisdiction=_jurisdiction_of(p.geography),
        size_band=p.size_band or None,
        industry=p.industry or p.archetype or None,
        maturity_stage=p.maturity or None,
        channel=None,
        as_of=datetime.now(UTC),
    )


def main() -> int:
    conn = _direct_connection()
    try:
        corpus = load_serving_corpus(TENANT, MANAGER_RETRIEVAL_PROFILE, conn=conn)
    finally:
        conn.close()
    embeddings, unembeddable = embed_cards(corpus.cards, persisted=corpus.persisted_embeddings)
    cards = [c for c in corpus.cards if c.card_version_id in embeddings]
    print(f"pool: {len(corpus.cards)} loaded / {len(cards)} embedded / {len(unembeddable)} unembeddable")

    cases = []
    for split in (DatasetSplit.DEVELOPMENT, DatasetSplit.VALIDATION):
        cases.extend(load_dataset(DATASET_DIR / split.value, split=split))
    print(f"cases: {len(cases)}")

    engine = cr.CardRetrievalEngine()
    out = {"pool_size": len(cards), "unembeddable": len(unembeddable), "cases": []}

    for case in cases:
        objective = _objective_for(case)
        qvec = embed_query(objective)
        ctx = context_for(case, TENANT)
        objective_tokens = cr._tokens(objective)
        entity_refs = (case.business_profile.industry, case.business_profile.archetype)
        entity_tokens = set().union(*(cr._tokens(v) for v in entity_refs)) if entity_refs else set()

        rows = []
        for card in cards:
            applicable, unknown = cr._applicability(card, ctx)
            card_text = f"{card.claim} {card.distillation_note} {card.claim_key.canonical}"
            ctoks = cr._tokens(card_text)
            lexical = cr._jaccard(objective_tokens, ctoks)
            # Mirrors the engine exactly, INCLUDING the VT-725 inapplicability case: no query
            # entities means the dimension does not exist for this turn, which is None, not 0.0.
            entity = cr._jaccard(entity_tokens, ctoks) if entity_tokens else None
            semantic = cr._cosine(qvec, embeddings[card.card_version_id])
            comparable = bool(cr._tokens(card.claim_key.canonical) & (objective_tokens | entity_tokens))
            authority = (
                cr._SOURCE_AUTHORITY[card.source_class]
                if card.domain is EVAL_DOMAIN and comparable
                else 0.0
            )
            rows.append(
                {
                    "card_version_id": card.card_version_id,
                    "claim": card.claim,
                    "claim_key": card.claim_key.canonical,
                    "domain": card.domain.value,
                    "source_class": card.source_class.value,
                    "status": card.status.value,
                    "applicable": applicable,
                    "unknown_dimensions": list(unknown),
                    "semantic": semantic,
                    "lexical": lexical,
                    "entity": entity,
                    "entity_tokens_present": bool(entity_tokens),
                    "authority": authority,
                    "authority_domain_match": card.domain is EVAL_DOMAIN,
                    "authority_comparable": comparable,
                    "applicability": max(0.0, 1.0 - 0.15 * len(unknown)),
                    "recency": cr._recency(card, ctx.as_of),
                    "confidence": cr._CONFIDENCE[card.confidence],
                }
            )

        # Exactness proof: run the REAL engine at floor 0.0 and require identical components.
        probe = replace(MANAGER_RETRIEVAL_PROFILE, minimum_score=0.0, top_k=20)
        result = engine.retrieve(
            cards=cards,
            card_embeddings=embeddings,
            objective=objective,
            query_embedding=qvec,
            entity_refs=entity_refs,
            domain=EVAL_DOMAIN,
            stage="planning",
            profile=probe,
            context=ctx,
            allowed_scopes=frozenset({KnowledgeScopeKind.GLOBAL}),
            max_per_cluster=2,
        )
        by_id = {r["card_version_id"]: r for r in rows}
        checked = 0
        for item in result.items:
            mine = by_id[item.card.card_version_id]
            c = item.components
            for name, engine_value in (
                ("semantic", c.semantic), ("lexical", c.lexical), ("entity", c.entity),
                ("authority", c.authority), ("applicability", c.applicability),
                ("confidence", c.confidence),
            ):
                assert abs(mine[name] - engine_value) < 1e-12, (
                    f"{case.case_id}/{item.card.card_version_id}: {name} "
                    f"{mine[name]} != engine {engine_value}"
                )
            assert mine["recency"] == c.recency, f"recency mismatch {case.case_id}"
            checked += 1

        out["cases"].append(
            {
                "case_id": case.case_id,
                "split": case.split.value,
                "objective": objective,
                "jurisdiction": ctx.jurisdiction,
                "industry": ctx.industry,
                "entity_tokens": sorted(entity_tokens),
                "engine_crosschecked_cards": checked,
                "trace_considered": result.trace.considered,
                "trace_applicability_excluded": result.trace.applicability_excluded,
                "trace_scope_excluded": result.trace.scope_or_status_excluded,
                "cards": rows,
            }
        )
        print(f"  {case.case_id} ({case.split.value}): {len(rows)} cards, {checked} engine-crosschecked ✓")

    OUT.write_text(json.dumps(out, indent=1))
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
