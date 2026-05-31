"""Canary: VT-197 Day-39 → L1 reflection loop (Rule #15).

Proves the learning loop end-to-end against a real Postgres: a Day-39 verdict,
distilled by the deterministic reflection writer, appears LABELED as
"Agent-learned (Day-39)" in the NEXT assemble_context_bundle — in its OWN section,
distinct from the owner-stated business_profile (which the loop never writes).

Deterministic (no Anthropic key needed). Preflight-SKIP only when DATABASE_URL is
absent. Fail-not-skip otherwise. CL-422 synthetic; _finalise cleans its rows.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from uuid import UUID, uuid4

_TENANTS: list[str] = []


def main() -> int:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEAM_SUPABASE_DB_URL")
    if not dsn:
        print("vt197 canary SKIP: DATABASE_URL unset (preflight)", file=sys.stderr)
        return 0
    try:
        import psycopg  # noqa: F401
    except Exception:  # noqa: BLE001
        print("vt197 canary SKIP: psycopg unavailable (preflight)", file=sys.stderr)
        return 0

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import apply_migrations

    apply_migrations.apply(dsn=dsn)

    import psycopg as pg

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    get_pool()  # ensure the shared pool is initialised (used by the L1 helpers)

    from orchestrator.billing.types import Day39Verdict
    from orchestrator.knowledge import assemble_context_bundle, upsert_business_profile
    from orchestrator.scheduled_triggers import _write_day39_reflection

    failures: list[str] = []
    try:
        tid = str(uuid4())
        _TENANTS.append(tid)
        with pg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO tenants (id, business_name, plan_tier, phase) "
                "VALUES (%s, 'VT197 Canary', 'founding', 'paid_active')",
                (tid,),
            )
        # Owner identity (so we can prove the two sections coexist + stay distinct).
        upsert_business_profile(tid, {"business_archetype": "canary_owner_archetype"})

        # A deterministic Day-39 verdict → the loop writes the reflection.
        verdict = Day39Verdict(
            tenant_id=UUID(tid),
            verdict="continue",
            arrr_paise=99999,
            cumulative_fees_paise=20000,
            decided_at=datetime.now(timezone.utc),
            already_decided=False,
        )
        _write_day39_reflection(tid, verdict)

        block = assemble_context_bundle(tid)
        if not block:
            failures.append("assemble_context_bundle returned None after reflection write")
        else:
            if "## Agent-learned (Day-39)" not in block:
                failures.append("missing labeled 'Agent-learned (Day-39)' section")
            if "## Owner-stated (business profile)" not in block:
                failures.append("missing labeled 'Owner-stated (business profile)' section")
            if "canary_owner_archetype" not in block:
                failures.append("owner identity missing from bundle")
            if "99999" not in block and "continue" not in block:
                failures.append("reflection content missing from bundle")

        # Scope guard: the loop wrote NO business_profile change (still the owner's).
        with pg.connect(dsn) as conn:
            n = conn.execute(
                "SELECT count(*) FROM l1_entities WHERE tenant_id=%s "
                "AND entity_type='business_profile'",
                (tid,),
            ).fetchone()[0]
        if n != 1:
            failures.append(f"business_profile count != 1 after reflection write (n={n})")
    finally:
        _finalise(dsn)

    if failures:
        print("vt197 canary FAILED:\n" + "\n".join(failures), file=sys.stderr)
        return 1
    print("vt197 canary: ALL CHECKS PASSED — Day-39 reflection appears labeled in the next bundle, owner identity untouched")
    return 0


def _finalise(dsn: str) -> None:
    import psycopg as pg

    for tid in _TENANTS:
        for tbl in ("l1_entities",):
            try:
                with pg.connect(dsn, autocommit=True) as conn:
                    conn.execute(f"DELETE FROM {tbl} WHERE tenant_id = %s", (tid,))  # noqa: S608
            except Exception:  # noqa: BLE001
                pass
        try:
            with pg.connect(dsn, autocommit=True) as conn:
                conn.execute("DELETE FROM tenants WHERE id = %s", (tid,))
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
