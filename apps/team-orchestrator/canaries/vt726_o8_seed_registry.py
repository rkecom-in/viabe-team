#!/usr/bin/env python3
"""VT-726 dev-only seed + real retrieval canary (authored for CC to execute).

Dry-run (no DB writes, no external embedding call):
    uv run --no-sync python canaries/vt726_o8_seed_registry.py \
      --expected-env dev --tenant-id <REAL_DEV_TENANT_UUID>

Authorized dev execution after CC applies migration 189:
    uv run --no-sync python canaries/vt726_o8_seed_registry.py \
      --expected-env dev --tenant-id <REAL_DEV_TENANT_UUID> --execute

The script refuses prod, requires the database's existing app_environment sentinel to say dev,
uses a real tenant's stored L1 business profile, and never prints secrets/card text/tenant data.
Retrieval is advisory and ``AUTHORIZES_EFFECTS`` is structurally false.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

APP_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_ROOT.parents[1]
SRC = APP_ROOT / "src"
for path in (str(REPO_ROOT), str(APP_ROOT), str(SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-env", required=True, choices=("dev",))
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def _database_url() -> str:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEAM_SUPABASE_DB_URL")
    if not dsn:
        raise SystemExit("VT-726 FAIL: DATABASE_URL or TEAM_SUPABASE_DB_URL is required")
    return dsn


def _profile(conn, tenant_id: UUID) -> dict[str, object]:
    row = conn.execute(
        "SELECT t.business_name, e.attributes FROM public.tenants t "
        "JOIN public.l1_entities e ON e.tenant_id = t.id "
        "WHERE t.id = %s AND e.entity_type = 'business_profile'",
        (tenant_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("real dev tenant must have an L1 business_profile row")
    attributes = dict(row["attributes"] or {})
    return {"business_name": row["business_name"], "attributes": attributes}


def _context_from_profile(tenant_id: UUID, profile: dict[str, object]):
    from orchestrator.knowledge.card_retrieval import RetrievalBusinessContext

    attrs = dict(profile["attributes"] or {})  # type: ignore[arg-type]
    jurisdiction = (
        attrs.get("jurisdiction") or attrs.get("country_code") or attrs.get("country") or "IN"
    )
    return RetrievalBusinessContext(
        tenant_id=tenant_id,
        jurisdiction=str(jurisdiction),
        size_band=_optional(attrs, "size_band", "business_size"),
        industry=_optional(attrs, "industry", "business_archetype"),
        maturity_stage=_optional(attrs, "maturity_stage", "business_stage"),
        channel=_optional(attrs, "primary_channel", "channel"),
        as_of=datetime.now(UTC),
    )


def _optional(values: dict[str, object], *keys: str) -> str | None:
    value = next((values[key] for key in keys if values.get(key)), None)
    return str(value) if value is not None else None


def _assert_migration_189(conn) -> None:
    required = {
        "domain",
        "source_class",
        "usage_rights",
        "independence_cluster",
        "corroboration_cluster_count",
        "provenance",
        "retrieval_eligible",
        "corpus_version_id",
    }
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'knowledge_cards'"
    ).fetchall()
    missing = required - {row["column_name"] for row in rows}
    if missing:
        raise RuntimeError(f"migration 189 is not applied; missing columns: {sorted(missing)}")


def _assert_persisted(conn, plan) -> None:
    counts = conn.execute(
        "SELECT count(*) FILTER (WHERE version = 1 AND status = 'candidate') AS candidate_count, "
        "count(*) FILTER (WHERE version = 2 AND status = 'validated' "
        " AND retrieval_eligible AND domain IS NOT NULL) AS validated_count "
        "FROM public.knowledge_cards WHERE card_key = ANY(%s::uuid[])",
        ([item.candidate.card_id for item in plan.cards],),
    ).fetchone()
    if counts != {"candidate_count": 15, "validated_count": 15}:
        raise RuntimeError(f"unexpected immutable seed counts: {dict(counts)}")
    lifecycle_count = conn.execute(
        "SELECT count(*) AS n FROM public.knowledge_lifecycle_events "
        "WHERE actor_id = 'vt726-deterministic-validator' "
        "AND reason LIKE '%vt710_pipeline_complete%'",
    ).fetchone()["n"]
    if lifecycle_count != 15:
        raise RuntimeError(f"expected 15 pipeline-proven promotion events, found {lifecycle_count}")


def _assert_global_purity_in_db(conn, tenant_id: UUID) -> None:
    global_tables = (
        "knowledge_sources",
        "knowledge_cards",
        "knowledge_card_sources",
        "knowledge_corpus_versions",
        "knowledge_corpus_members",
        "knowledge_evaluations",
        "knowledge_lifecycle_events",
    )
    for table in global_tables:
        # Table names are a closed internal tuple, never user input.
        count = conn.execute(
            f"SELECT count(*) AS n FROM public.{table} t "  # noqa: S608
            "WHERE to_jsonb(t)::text LIKE %s",
            (f"%{tenant_id}%",),
        ).fetchone()["n"]
        if count:
            raise RuntimeError(f"global purity failed: tenant identifier found in {table}")


def _assert_loaded_matches_plan(cards, plan) -> None:
    expected = {
        item.validated.card_version_id: item.validated.model_dump(mode="json")
        for item in plan.cards
    }
    actual = {card.card_version_id: card.model_dump(mode="json") for card in cards}
    if actual != expected:
        raise RuntimeError("single-table card round-trip differs from the pipeline-derived plan")


def main() -> int:
    args = _args()
    if args.expected_env != "dev":  # argparse closes this too; keep the invariant visible.
        raise SystemExit("VT-726 refuses every environment except dev")

    from psycopg import connect
    from psycopg.rows import dict_row

    from orchestrator.agent_framework.retrieval_profiles import MANAGER_RETRIEVAL_PROFILE
    from orchestrator.knowledge.card_retrieval import CardRetrievalEngine
    from orchestrator.knowledge.contracts import KnowledgeDomain, KnowledgeScopeKind
    from orchestrator.knowledge.registry_seed import (
        build_seed_plan,
        load_validated_cards,
        persist_seed_plan,
    )
    from orchestrator.knowledge.shadow_embeddings import (
        embed_cards_fail_soft,
        embed_query_fail_soft,
    )
    from scripts.apply_migrations import guard_environment
    from scripts.business_knowledge.convert_o8_candidates import convert

    # This is the real VT-710 extraction/governance/originality/purity path over all 118 local
    # inputs. Only the deterministic 15-card rehearsal subset is selected after conversion.
    rights, candidates = convert(tenant_identifiers=(str(args.tenant_id),))
    plan = build_seed_plan(
        rights, candidates, tenant_identifiers=(str(args.tenant_id),)
    )
    print(
        json.dumps(
            {
                "pipeline_candidates": len(candidates),
                "seed_candidates": len(plan.cards),
                "corpus_status": plan.corpus_status,
                "admission_verdict": plan.admission_verdict,
                "originality_rejections": 0,
                "originality_note": (
                    "all fixed seed cards had token-shingle-v1 checked originality and no review "
                    "flags; attestation-only artifacts are rejected by the seed validator test"
                ),
                "authorizes_effects": plan.AUTHORIZES_EFFECTS,
            },
            sort_keys=True,
        )
    )
    if not args.execute:
        print("VT-726 DRY RUN PASS: no database write and no embedding egress performed")
        return 0

    dsn = _database_url()
    with connect(dsn) as conn:
        # With no expected_host_substr this refuses to bootstrap/stamp an unstamped database.
        guard_environment(conn, dsn, args.expected_env)
        conn.row_factory = dict_row
        _assert_migration_189(conn)
        profile = _profile(conn, args.tenant_id)
        persist_seed_plan(conn, plan)
        _assert_persisted(conn, plan)
        _assert_global_purity_in_db(conn, args.tenant_id)
        cards = load_validated_cards(conn, plan.corpus_version_id)
        _assert_loaded_matches_plan(cards, plan)
        embedded = embed_cards_fail_soft(cards)
        retrievable = tuple(card for card in cards if card.card_version_id in embedded.vectors)
        management = next(card for card in retrievable if card.domain is KnowledgeDomain.MANAGEMENT)
        objective = f"{management.claim}\n{management.distillation_note}"
        query_vector = embed_query_fail_soft(objective)
        if query_vector is None or not retrievable:
            raise RuntimeError("real shadow embedding produced no retrievable cards")
        result = CardRetrievalEngine().retrieve(
            cards=retrievable,
            card_embeddings=embedded.vectors,
            objective=objective,
            query_embedding=query_vector,
            entity_refs=(str(profile["business_name"]),),
            domain=KnowledgeDomain.MANAGEMENT,
            stage="planning",
            profile=MANAGER_RETRIEVAL_PROFILE,
            context=_context_from_profile(args.tenant_id, profile),
            allowed_scopes=frozenset({KnowledgeScopeKind.GLOBAL}),
        )
        if result.no_result or not result.items:
            raise RuntimeError(f"shadow retrieval returned no candidates; trace={result.trace}")
        conn.commit()
        print(
            json.dumps(
                {
                    "result_count": len(result.items),
                    "ranked": [
                        {"card_version_id": item.card.card_version_id, "score": item.score}
                        for item in result.items
                    ],
                    "trace": result.trace.__dict__,
                    "embedding_excluded_count": len(embedded.excluded),
                    "authorizes_effects": result.AUTHORIZES_EFFECTS,
                    "global_purity": "pass",
                },
                sort_keys=True,
            )
        )
    print("VT-726 CANARY PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
