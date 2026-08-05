#!/usr/bin/env python3
"""VT-727 dev-only full registry + persisted-embedding + retrieval canary.

Dry-run (no DB writes, no embedding egress):
    uv run --no-sync python canaries/vt727_o8_full_registry.py \
      --expected-env dev --tenant-id <REAL_DEV_TENANT_UUID>

Authorized execution belongs to CC after the allocated VT-727 migration is applied.  This script
refuses prod, never prints card text/tenant data/secrets, and proves retrieval remains advisory.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

APP_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_ROOT.parents[1]
SRC = APP_ROOT / "src"
for path in (str(REPO_ROOT), str(APP_ROOT), str(SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)

CORPUS = APP_ROOT / "knowledge_corpus"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-env", required=True, choices=("dev",))
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def _database_url() -> str:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEAM_SUPABASE_DB_URL")
    if not dsn:
        raise SystemExit("VT-727 FAIL: DATABASE_URL or TEAM_SUPABASE_DB_URL is required")
    return dsn


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _profile(conn, tenant_id: UUID) -> dict[str, object]:
    row = conn.execute(
        "SELECT t.business_name, e.attributes FROM public.tenants t "
        "JOIN public.l1_entities e ON e.tenant_id = t.id "
        "WHERE t.id = %s AND e.entity_type = 'business_profile'",
        (tenant_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("real dev tenant must have an L1 business_profile row")
    return {"business_name": row["business_name"], "attributes": dict(row["attributes"] or {})}


def _context(tenant_id: UUID, profile: dict[str, object]):
    from orchestrator.knowledge.card_retrieval import RetrievalBusinessContext

    attributes = dict(profile["attributes"] or {})  # type: ignore[arg-type]
    return RetrievalBusinessContext(
        tenant_id=tenant_id,
        jurisdiction=str(
            attributes.get("jurisdiction")
            or attributes.get("country_code")
            or attributes.get("country")
            or "IN"
        ),
        size_band=_optional(attributes, "size_band", "business_size"),
        industry=_optional(attributes, "industry", "business_archetype"),
        maturity_stage=_optional(attributes, "maturity_stage", "business_stage"),
        channel=_optional(attributes, "primary_channel", "channel"),
        as_of=datetime.now(UTC),
    )


def _optional(values: dict[str, object], *keys: str) -> str | None:
    value = next((values[key] for key in keys if values.get(key)), None)
    return str(value) if value is not None else None


def _assert_embedding_migration(conn) -> None:
    row = conn.execute(
        "SELECT to_regclass('public.knowledge_card_embeddings') AS table_name"
    ).fetchone()
    if row["table_name"] is None:
        raise RuntimeError("allocated VT-727 persisted-embedding migration is not applied")


def _assert_registry(conn, plan) -> None:
    counts = conn.execute(
        "SELECT count(*) FILTER (WHERE version = 1 AND status = 'candidate') AS candidate_count, "
        "count(*) FILTER (WHERE version = 1 AND status = 'research_only') AS research_count, "
        "count(*) FILTER (WHERE version = 2 AND status = 'validated' AND retrieval_eligible) "
        "AS validated_count FROM public.knowledge_cards "
        "WHERE card_key = ANY(%s::uuid[])",
        ([item.candidate.card_id for item in plan.cards],),
    ).fetchone()
    expected = {"candidate_count": 100, "research_count": 18, "validated_count": 64}
    if dict(counts) != expected:
        raise RuntimeError(f"full registry count mismatch: {dict(counts)} != {expected}")
    members = conn.execute(
        "SELECT count(*) AS n FROM public.knowledge_corpus_members WHERE corpus_version_id = %s",
        (plan.corpus_version_id,),
    ).fetchone()["n"]
    if members != 118:
        raise RuntimeError(f"full corpus has {members} members, expected 118")
    lifecycle = conn.execute(
        "SELECT count(*) AS n FROM public.knowledge_lifecycle_events "
        "WHERE actor_id IN ('vt726-deterministic-validator', 'vt727-deterministic-validator') "
        "AND reason LIKE '%vt710_pipeline_complete%'",
    ).fetchone()["n"]
    if lifecycle != 64:
        raise RuntimeError(f"pipeline-proven promotions={lifecycle}, expected 64")

    from orchestrator.knowledge.registry_seed import _card_from_row

    rows = conn.execute(
        "SELECT id, card_key, version, corpus_version_id, claim, claim_key, claim_value, "
        "distillation_note, source_class, domain, authority, confidence, independence_cluster, "
        "corroboration_cluster_count, jurisdictions, size_bands, industries, maturity_stages, "
        "channels, applicability_universal, effective_from, effective_until, provenance, "
        "usage_rights, retention_class, scope, default_assignment, status, retrieval_eligible, "
        "expires_at FROM public.knowledge_cards WHERE id = ANY(%s::uuid[]) ORDER BY id",
        ([item.representative.card_version_id for item in plan.cards],),
    ).fetchall()
    expected = {
        item.representative.card_version_id: item.representative.model_dump(mode="json")
        for item in plan.cards
    }
    actual = {
        card.card_version_id: card.model_dump(mode="json")
        for card in (_card_from_row(row) for row in rows)
    }
    if actual != expected:
        raise RuntimeError("persisted full-corpus cards differ from the governed plan")


def _assert_global_purity(conn, tenant_id: UUID) -> None:
    for table in (
        "knowledge_sources",
        "knowledge_cards",
        "knowledge_card_sources",
        "knowledge_corpus_versions",
        "knowledge_corpus_members",
        "knowledge_card_embeddings",
        "knowledge_evaluations",
        "knowledge_lifecycle_events",
    ):
        count = conn.execute(
            f"SELECT count(*) AS n FROM public.{table} t "  # noqa: S608 — closed tuple
            "WHERE to_jsonb(t)::text LIKE %s",
            (f"%{tenant_id}%",),
        ).fetchone()["n"]
        if count:
            raise RuntimeError(f"global purity failed: tenant identifier found in {table}")


def main() -> int:
    args = _args()
    from orchestrator.agent_framework.retrieval_profiles import MANAGER_RETRIEVAL_PROFILE
    from orchestrator.knowledge.card_retrieval import CardRetrievalEngine
    from orchestrator.knowledge.contracts import KnowledgeDomain, KnowledgeScopeKind
    from orchestrator.knowledge.persisted_embeddings import bind_embeddings, persist_embeddings
    from orchestrator.knowledge.registry_full import (
        build_full_plan,
        load_independence_audit,
        persist_full_plan,
    )
    from orchestrator.knowledge.shadow_embeddings import (
        embed_cards_fail_soft,
        embed_query_fail_soft,
    )

    rights = _jsonl(CORPUS / "source_rights.jsonl")
    candidates = _jsonl(CORPUS / "candidate_cards.jsonl")
    audit = load_independence_audit(
        json.loads((CORPUS / "independence_audit.json").read_text(encoding="utf-8"))
    )
    plan = build_full_plan(rights, candidates, audit, tenant_identifiers=(str(args.tenant_id),))
    print(
        json.dumps(
            {
                "pipeline_records": len(plan.cards),
                "shadow_validated": plan.promoted_count,
                "deferred": plan.deferred_count,
                "screened_cross_source_pairs": plan.screened_cross_source_pairs,
                "collapsed_retelling_groups": plan.collapsed_retelling_groups,
                "largest_source_share": plan.largest_source_share,
                "corpus_status": plan.corpus_status,
                "admission_verdict": plan.admission_verdict,
                "authorizes_effects": plan.AUTHORIZES_EFFECTS,
            },
            sort_keys=True,
        )
    )
    if not args.execute:
        print("VT-727 DRY RUN PASS: no DB write and no embedding egress performed")
        return 0

    from psycopg import connect
    from psycopg.rows import dict_row
    from scripts.apply_migrations import guard_environment

    dsn = _database_url()
    with connect(dsn) as conn:
        guard_environment(conn, dsn, args.expected_env)
        conn.row_factory = dict_row
        _assert_embedding_migration(conn)
        profile = _profile(conn, args.tenant_id)
        persist_full_plan(conn, plan)
        _assert_registry(conn, plan)

        representatives = tuple(item.representative for item in plan.cards)
        embedded = embed_cards_fail_soft(representatives, batch_size=64)
        if embedded.excluded or len(embedded.vectors) != 118:
            raise RuntimeError(
                f"full embedding incomplete: vectors={len(embedded.vectors)} "
                f"excluded={len(embedded.excluded)}"
            )
        persist_embeddings(conn, bind_embeddings(representatives, embedded.vectors))
        persisted_count = conn.execute(
            "SELECT count(*) AS n FROM public.knowledge_card_embeddings e "
            "JOIN public.knowledge_corpus_members m ON m.card_id = e.card_id "
            "WHERE m.corpus_version_id = %s",
            (plan.corpus_version_id,),
        ).fetchone()["n"]
        if persisted_count != 118:
            raise RuntimeError(f"persisted embeddings={persisted_count}, expected 118")
        _assert_global_purity(conn, args.tenant_id)

        retrievable = tuple(
            item.representative for item in plan.cards if item.representative.retrieval_eligible
        )
        management = next(card for card in retrievable if card.domain is KnowledgeDomain.MANAGEMENT)
        query = embed_query_fail_soft(f"{management.claim}\n{management.distillation_note}")
        if query is None:
            raise RuntimeError("real query embedding failed")
        result = CardRetrievalEngine().retrieve(
            cards=retrievable,
            card_embeddings=embedded.vectors,
            objective=f"{management.claim}\n{management.distillation_note}",
            query_embedding=query,
            entity_refs=(str(profile["business_name"]),),
            domain=KnowledgeDomain.MANAGEMENT,
            stage="planning",
            profile=MANAGER_RETRIEVAL_PROFILE,
            context=_context(args.tenant_id, profile),
            allowed_scopes=frozenset({KnowledgeScopeKind.GLOBAL}),
            max_per_cluster=2,
        )
        cluster_counts = Counter(item.card.independence_cluster for item in result.items)
        if result.no_result or not result.items or max(cluster_counts.values(), default=0) > 2:
            raise RuntimeError("full retrieval or max-two-per-cluster diversity gate failed")
        if len(result.items) > MANAGER_RETRIEVAL_PROFILE.top_k:
            raise RuntimeError("retrieval exceeded the Manager's declared top-k budget")
        conn.commit()
        print(
            json.dumps(
                {
                    "result_count": len(result.items),
                    "candidate_count": len(retrievable),
                    "max_selected_per_cluster": max(cluster_counts.values()),
                    "trace": result.trace.__dict__,
                    "persisted_embedding_count": persisted_count,
                    "global_purity": "pass",
                    "authorizes_effects": result.AUTHORIZES_EFFECTS,
                },
                sort_keys=True,
            )
        )
    print("VT-727 CANARY PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
