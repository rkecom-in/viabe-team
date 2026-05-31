"""Canary: VT-195 L1 Context Composer read path (Rule #15).

Against a real Postgres (DATABASE_URL), seeds a 'business_profile' l1_entities
entity for two tenants and verifies:
  1. assemble_context_bundle renders the tenant's identity block.
  2. CROSS-TENANT RLS DENIAL: tenant B's entity is invisible under tenant A's
     GUC via the production read path (tenant_connection -> app_role; RLS real).
  3. assemble_context_bundle returns None for a tenant with no entity.

Fail-not-skip: any failure -> sys.exit(1). Skips ONLY when DATABASE_URL/dbos are
absent (preflight), like the other DB canaries.

CL-422: synthetic data only. CL-390: no customer PII (owner_curated_context is
owner-authored business context).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from uuid import UUID

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger("vt195.canary")

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _ensure_path() -> None:
    src = str(_REPO_ROOT / "apps" / "team-orchestrator" / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    scripts = str(_REPO_ROOT / "apps" / "team-orchestrator" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)


def main() -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.warning("vt195 canary SKIP: DATABASE_URL unset (preflight, not a failure)")
        return
    try:
        import psycopg  # noqa: F401
    except Exception:  # noqa: BLE001
        logger.warning("vt195 canary SKIP: psycopg unavailable (preflight)")
        return

    _ensure_path()
    import apply_migrations

    apply_migrations.apply(dsn=dsn)
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        import psycopg as pg

        from orchestrator.db import tenant_connection
        from orchestrator.knowledge.l1 import assemble_context_bundle

        def _new_tenant(name: str) -> UUID:
            with pg.connect(dsn, autocommit=True) as conn:
                row = conn.execute(
                    "INSERT INTO tenants (business_name, plan_tier, phase) "
                    "VALUES (%s, 'founding', 'onboarding') RETURNING id",
                    (name,),
                ).fetchone()
            return UUID(str(row[0]))

        def _seed(tenant_id: UUID, attrs: dict) -> None:
            with pg.connect(dsn, autocommit=True) as conn:
                conn.execute(
                    "INSERT INTO l1_entities (tenant_id, entity_type, attributes) "
                    "VALUES (%s, 'business_profile', %s::jsonb)",
                    (str(tenant_id), json.dumps(attrs)),
                )

        a = _new_tenant("VT195 Canary A")
        b = _new_tenant("VT195 Canary B")
        empty = _new_tenant("VT195 Canary Empty")
        _seed(a, {"business_archetype": "canary_archetype_A"})
        _seed(b, {"business_archetype": "canary_archetype_B_SECRET"})

        failures: list[str] = []

        block_a = assemble_context_bundle(a)
        if not block_a or "canary_archetype_A" not in block_a:
            failures.append("assemble_context_bundle(A) did not render A's identity")
        if block_a and "canary_archetype_B_SECRET" in block_a:
            failures.append("CROSS-TENANT LEAK: B's data in A's bundle")

        if assemble_context_bundle(empty) is not None:
            failures.append("assemble_context_bundle(empty) should be None")

        # Real RLS denial via the production read path.
        with tenant_connection(a) as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM l1_entities WHERE tenant_id = %s",
                (str(b),),
            ).fetchone()
        n = row["n"] if isinstance(row, dict) else row[0]
        if n != 0:
            failures.append(f"RLS DENIAL FAILED: B's entity visible under A's GUC (n={n})")

        if failures:
            logger.error("vt195 canary FAILED:\n%s", "\n".join(failures))
            sys.exit(1)
        logger.info("vt195 canary: ALL CHECKS PASSED (assemble + RLS denial + empty)")
    finally:
        shutdown_dbos()


if __name__ == "__main__":
    main()
