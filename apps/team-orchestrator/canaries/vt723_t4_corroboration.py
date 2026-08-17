#!/usr/bin/env python3
"""Dev-only canary for the VT-723 T4 corroboration delta.

Dry run performs no database write, provider call, or other network egress. Authorized --execute
belongs to CC: it writes the governed evidence state to dev Postgres. The corrected plan performs
no card transitions and therefore copies no vectors. It performs no Voyage embedding egress and
grants no retrieval/effect authority.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import UUID

APP_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_ROOT.parents[1]
SRC = APP_ROOT / "src"
for path in (str(REPO_ROOT), str(APP_ROOT), str(SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)

CORPUS = APP_ROOT / "knowledge_corpus"


def args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-env", required=True, choices=("dev",))
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def jsonl(name: str) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (CORPUS / name).read_text(encoding="utf-8").splitlines()
        if line
    ]


def database_url() -> str:
    value = os.environ.get("DATABASE_URL") or os.environ.get("TEAM_SUPABASE_DB_URL")
    if not value:
        raise SystemExit("VT-723 FAIL: database URL is required")
    return value


def build_plan(tenant_id: UUID):
    from orchestrator.knowledge.ingestion import CandidateArtifact
    from orchestrator.knowledge.registry_full import build_full_plan, load_independence_audit
    from orchestrator.knowledge.registry_resolution import (
        build_resolution_plan,
        load_resolution_delta,
    )
    from orchestrator.knowledge.t4_corroboration import (
        build_corroboration_plan,
        load_delta,
        load_source_manifest,
    )

    audit = load_independence_audit(
        json.loads((CORPUS / "independence_audit.json").read_text(encoding="utf-8"))
    )
    full = build_full_plan(
        jsonl("source_rights.jsonl"),
        jsonl("candidate_cards.jsonl"),
        audit,
        tenant_identifiers=(str(tenant_id),),
    )
    parent = build_resolution_plan(
        full,
        load_resolution_delta(jsonl("deferral_resolution_delta.jsonl")),
        tenant_identifiers=(str(tenant_id),),
    )
    sources = load_source_manifest(jsonl("t4_corroboration_sources.jsonl"))
    candidates = tuple(
        CandidateArtifact.model_validate(row) for row in jsonl("t4_corroboration_candidates.jsonl")
    )
    delta = load_delta(jsonl("t4_corroboration_delta.jsonl"))
    return build_corroboration_plan(
        parent,
        sources,
        candidates,
        delta,
        tenant_identifiers=(str(tenant_id),),
    )


def assert_registry(conn, plan, tenant_id: UUID) -> None:
    counts = conn.execute(
        "SELECT count(*) FILTER (WHERE status = 'candidate' AND NOT retrieval_eligible) "
        "AS candidate_count, count(*) FILTER (WHERE status = 'disputed' AND NOT retrieval_eligible) "
        "AS disputed_count FROM public.knowledge_cards WHERE id = ANY(%s::uuid[])",
        ([item.resolved.card_version_id for item in plan.transitions],),
    ).fetchone()
    expected_counts = {
        "candidate_count": plan.candidate_count,
        "disputed_count": plan.disputed_count,
    }
    if dict(counts) != expected_counts:
        raise RuntimeError(f"T4 transition count mismatch: {dict(counts)}")

    members = conn.execute(
        "SELECT count(*) AS n FROM public.knowledge_corpus_members WHERE corpus_version_id = %s",
        (plan.corpus_version_id,),
    ).fetchone()["n"]
    if members != 118:
        raise RuntimeError(f"v4 corpus has {members} members, expected 118")

    events = conn.execute(
        "SELECT count(*) AS n FROM public.knowledge_lifecycle_events "
        "WHERE actor_id = 'vt723-corroboration-validator' "
        "AND card_version_ref = ANY(%s::uuid[])",
        ([item.resolved.card_version_id for item in plan.transitions],),
    ).fetchone()["n"]
    if events != len(plan.transitions):
        raise RuntimeError(f"T4 lifecycle events={events}, expected {len(plan.transitions)}")

    embeddings = conn.execute(
        "SELECT count(*) AS n FROM public.knowledge_card_embeddings e "
        "JOIN public.knowledge_corpus_members m ON m.card_id = e.card_id "
        "WHERE m.corpus_version_id = %s",
        (plan.corpus_version_id,),
    ).fetchone()["n"]
    if embeddings != 118:
        raise RuntimeError(f"v4 corpus embeddings={embeddings}, expected 118")

    for table in (
        "knowledge_sources",
        "knowledge_cards",
        "knowledge_card_sources",
        "knowledge_corpus_versions",
        "knowledge_corpus_members",
        "knowledge_lifecycle_events",
    ):
        found = conn.execute(
            f"SELECT count(*) AS n FROM public.{table} t WHERE to_jsonb(t)::text LIKE %s",  # noqa: S608
            (f"%{tenant_id}%",),
        ).fetchone()["n"]
        if found:
            raise RuntimeError(f"global purity failed: tenant identifier found in {table}")


def main() -> int:
    parsed = args()
    plan = build_plan(parsed.tenant_id)
    print(
        json.dumps(
            {
                "new_source_records": len(plan.sources),
                "independent_source_clusters": len(
                    {source.independence_cluster for source in plan.sources}
                ),
                "candidate_transitions": plan.candidate_count,
                "disputed_transitions": plan.disputed_count,
                "research_only_recorded_absence": len(plan.unresolved_legacy_ids),
                "retrieval_enabled": False,
                "authorizes_effects": plan.AUTHORIZES_EFFECTS,
                "voyage_egress": False,
            },
            sort_keys=True,
        )
    )
    if not parsed.execute:
        print("VT-723 DRY RUN PASS: no database write and no network egress performed")
        return 0

    from psycopg import connect
    from psycopg.rows import dict_row

    from orchestrator.knowledge.t4_corroboration import (
        assert_corpus_verified,
        copy_corroboration_embeddings,
        persist_corroboration_plan,
    )

    # BEFORE the connection is even opened. Every card must be either a hash-bound citation that
    # earns its recorded tier or explicitly disclosed T4 judgment. The record carries before/after
    # verdicts and the gate has no waiver path.
    candidate_rows = jsonl("t4_corroboration_candidates.jsonl")
    expected_cards = {
        row["card"]["card_version_id"]: (
            row["card"]["source_class"],
            row["card"]["provenance"]["source_ids"][0],
        )
        for row in candidate_rows
    }
    assert_corpus_verified(jsonl("t4_corroboration_verification.jsonl"), expected_cards)
    from scripts.apply_migrations import guard_environment

    dsn = database_url()
    with connect(dsn) as conn:
        guard_environment(conn, dsn, parsed.expected_env)
        conn.row_factory = dict_row
        persist_corroboration_plan(conn, plan)
        copy_corroboration_embeddings(conn, plan)
        assert_registry(conn, plan, parsed.tenant_id)
        conn.commit()
    print("VT-723 CANARY PASS: dev writes complete; no Voyage egress; nothing activated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
