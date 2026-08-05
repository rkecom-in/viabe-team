#!/usr/bin/env python3
"""VT-727 dev-only canary for the governed 36-card deferral-resolution delta.

Dry-run (no DB writes and no network egress):
    uv run --no-sync python canaries/vt727_o8_deferral_resolution.py \
      --expected-env dev --tenant-id <REAL_DEV_TENANT_UUID>

Authorized execution belongs to CC. ``--execute`` performs real dev-database writes but no
Voyage call: the 36 corrected versions keep the exact embedded expression and copy their v1
vectors inside Postgres. The knowledge result remains advisory and never authorizes an effect.
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


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-env", required=True, choices=("dev",))
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def _database_url() -> str:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEAM_SUPABASE_DB_URL")
    if not dsn:
        raise SystemExit("VT-727 routes-out FAIL: database URL is required")
    return dsn


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _assert_resolution_registry(conn, plan) -> None:
    counts = conn.execute(
        "SELECT count(*) FILTER (WHERE version = 2 AND status = 'validated' "
        "AND retrieval_eligible) AS validated_count, "
        "count(*) FILTER (WHERE version = 1 AND status = 'research_only' "
        "AND NOT retrieval_eligible) AS t4_deferred_count "
        "FROM public.knowledge_cards WHERE card_key = ANY(%s::uuid[])",
        ([card.card_id for card in plan.members],),
    ).fetchone()
    expected = {"validated_count": 100, "t4_deferred_count": 18}
    if dict(counts) != expected:
        raise RuntimeError(f"resolution registry count mismatch: {dict(counts)} != {expected}")

    members = conn.execute(
        "SELECT count(*) AS n FROM public.knowledge_corpus_members WHERE corpus_version_id = %s",
        (plan.corpus_version_id,),
    ).fetchone()["n"]
    if members != 118:
        raise RuntimeError(f"resolved corpus has {members} members, expected 118")

    lifecycle = conn.execute(
        "SELECT count(*) AS n FROM public.knowledge_lifecycle_events "
        "WHERE actor_id = 'vt727-deferral-resolution-validator' "
        "AND reason LIKE '%vt727-deferral-routes-out%'",
    ).fetchone()["n"]
    if lifecycle != 36:
        raise RuntimeError(f"resolution lifecycle events={lifecycle}, expected 36")

    embeddings = conn.execute(
        "SELECT count(*) AS n FROM public.knowledge_card_embeddings e "
        "JOIN public.knowledge_corpus_members m ON m.card_id = e.card_id "
        "WHERE m.corpus_version_id = %s",
        (plan.corpus_version_id,),
    ).fetchone()["n"]
    if embeddings != 118:
        raise RuntimeError(f"resolved-corpus embeddings={embeddings}, expected 118")

    from orchestrator.knowledge.registry_seed import _card_from_row

    rows = conn.execute(
        "SELECT id, card_key, version, corpus_version_id, claim, claim_key, claim_value, "
        "distillation_note, source_class, domain, authority, confidence, independence_cluster, "
        "corroboration_cluster_count, jurisdictions, size_bands, industries, maturity_stages, "
        "channels, applicability_universal, effective_from, effective_until, provenance, "
        "usage_rights, retention_class, scope, default_assignment, status, retrieval_eligible, "
        "expires_at FROM public.knowledge_cards WHERE id = ANY(%s::uuid[]) ORDER BY id",
        ([item.validated.card_version_id for item in plan.promotions],),
    ).fetchall()
    expected_cards = {
        item.validated.card_version_id: item.validated.model_dump(mode="json")
        for item in plan.promotions
    }
    actual_cards = {
        card.card_version_id: card.model_dump(mode="json")
        for card in (_card_from_row(row) for row in rows)
    }
    if actual_cards != expected_cards:
        raise RuntimeError("persisted resolved cards differ from the evidence-bound plan")


def _assert_global_purity(conn, tenant_id: UUID) -> None:
    tables = (
        "knowledge_sources",
        "knowledge_cards",
        "knowledge_card_sources",
        "knowledge_corpus_versions",
        "knowledge_corpus_members",
        "knowledge_card_embeddings",
        "knowledge_evaluations",
        "knowledge_lifecycle_events",
    )
    for table in tables:
        count = conn.execute(
            f"SELECT count(*) AS n FROM public.{table} t "  # noqa: S608 - closed tuple
            "WHERE to_jsonb(t)::text LIKE %s",
            (f"%{tenant_id}%",),
        ).fetchone()["n"]
        if count:
            raise RuntimeError(f"global purity failed: tenant identifier found in {table}")


def main() -> int:
    args = _args()
    from orchestrator.knowledge.registry_full import build_full_plan, load_independence_audit
    from orchestrator.knowledge.registry_resolution import (
        build_resolution_plan,
        copy_resolution_embeddings,
        load_resolution_delta,
        persist_resolution_plan,
    )

    rights = _jsonl(CORPUS / "source_rights.jsonl")
    candidates = _jsonl(CORPUS / "candidate_cards.jsonl")
    audit = load_independence_audit(
        json.loads((CORPUS / "independence_audit.json").read_text(encoding="utf-8"))
    )
    full_plan = build_full_plan(
        rights,
        candidates,
        audit,
        tenant_identifiers=(str(args.tenant_id),),
    )
    delta = load_resolution_delta(_jsonl(CORPUS / "deferral_resolution_delta.jsonl"))
    plan = build_resolution_plan(full_plan, delta, tenant_identifiers=(str(args.tenant_id),))
    print(
        json.dumps(
            {
                "resolved": len(plan.promotions),
                "shadow_validated": plan.shadow_validated_count,
                "t4_deferred_untouched": plan.deferred_count,
                "corpus_status": plan.corpus_status,
                "admission_verdict": plan.admission_verdict,
                "authorizes_effects": plan.AUTHORIZES_EFFECTS,
                "voyage_egress": False,
            },
            sort_keys=True,
        )
    )
    if not args.execute:
        print("VT-727 routes-out DRY RUN PASS: no DB write and no network egress performed")
        return 0

    from psycopg import connect
    from psycopg.rows import dict_row
    from scripts.apply_migrations import guard_environment

    dsn = _database_url()
    with connect(dsn) as conn:
        guard_environment(conn, dsn, args.expected_env)
        conn.row_factory = dict_row
        persist_resolution_plan(conn, plan)
        copy_resolution_embeddings(conn, plan)
        _assert_resolution_registry(conn, plan)
        _assert_global_purity(conn, args.tenant_id)
        conn.commit()
    print("VT-727 routes-out CANARY PASS: DB writes complete; no Voyage egress performed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
