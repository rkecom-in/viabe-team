"""VT-195 Phase 3 — seed the RKeCom founding tenant's L1 'business_profile'.

Idempotent (upsert_business_profile → ON CONFLICT). Reads FAZAL_TENANT_ID (the
real founding tenant) + DATABASE_URL from the env. Run with the secret folder
sourced:

  ( set -a; source ../../.viabe/secrets/supabase-dev.env; set +a;
    uv run python scripts/seed_l1_business_profile.py )

This is the tenant's OWN business identity (archetype, owner persona, operating
notes) — NOT customer PII, so CL-422 (no customer PII on dev) is satisfied.
Attributes are Fazal-confirmed (Cowork VT-195 Phase-3 task 2026-05-31).
"""

from __future__ import annotations

import os
import sys
from uuid import UUID

# Fazal-confirmed RKeCom business_profile (VT-195 Phase 3).
RKECOM_BUSINESS_PROFILE: dict[str, object] = {
    "business_archetype": "electronics_retail",
    "owner_persona": (
        "RKecom intends to be the biggest online electronics store, dealing in "
        "branded and high quality electronics products. Focus only on the sales "
        "part, do not offer any discount without my confirmation, and never "
        "update anything in my accounts book."
    ),
    # get_business_profile reads attributes->>'owner_curated_context' — surface
    # the operating note there.
    "owner_curated_context": (
        "Sales-focus only. NEVER offer a discount without the owner's explicit "
        "confirmation. NEVER modify the accounts book."
    ),
    "integration_map": {"google_sheets": "primary_customer_ledger"},
    "communication_prefs": {"default_language": "en", "formality": "warm_professional"},
    "working_hours": "Mon-Sat 10:00-20:00 IST",
    "escalation_thresholds": {"cohort_size_max": 500},
}


def main() -> int:
    tenant_id = os.environ.get("FAZAL_TENANT_ID")
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEAM_SUPABASE_DB_URL")
    if not tenant_id:
        print("FAIL: FAZAL_TENANT_ID unset (the founding tenant id)", file=sys.stderr)
        return 2
    if not dsn:
        print("FAIL: DATABASE_URL/TEAM_SUPABASE_DB_URL unset", file=sys.stderr)
        return 2
    try:
        UUID(tenant_id)
    except ValueError:
        print(f"FAIL: FAZAL_TENANT_ID is not a UUID: {tenant_id!r}", file=sys.stderr)
        return 2

    # Ensure the orchestrator pool exists (tenant_connection uses get_pool).
    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )

    from orchestrator.knowledge import assemble_context_bundle, upsert_business_profile

    entity_id = upsert_business_profile(tenant_id, RKECOM_BUSINESS_PROFILE)
    print(f"seeded business_profile entity {entity_id} for tenant {tenant_id}")

    # Verify the read path renders it (RLS-scoped).
    block = assemble_context_bundle(tenant_id)
    if not block or "electronics_retail" not in block:
        print(f"FAIL: assemble_context_bundle did not render the seed: {block!r}", file=sys.stderr)
        return 1
    print("verified assemble_context_bundle renders the seeded identity:\n" + block)
    get_pool().close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
