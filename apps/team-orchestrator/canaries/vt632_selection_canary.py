"""CL-2026-07-10 SELECTION CANARY — prove the owner-facing lapsed COUNT equals the Sales-Recovery
send-cohort SIZE on DEPLOYED dev (option 2 coherence: "the number the owner hears == the set a
campaign targets").

Run AFTER seeding a synthetic tenant via the harness (which seeds consent server-side through the
dev endpoint, so the token-join matches the deployed salt):

    railway run --service vt-orchestrator-service --environment development -- \
        uv run --no-sync python canaries/convo_harness.py setup --onboarded \
        --seed-lapsed-customers 8 --name vt632-coh-<rand>          # prints TENANT_ID
    railway run --service vt-orchestrator-service --environment development -- \
        uv run --no-sync python canaries/vt632_selection_canary.py <TENANT_ID> [expected_lapsed]

Asserts, over the REAL deployed detection path (tenant_connection, app_role, RLS):
  count_lapsed(45) == len(detect_lapsed_customers) == expected_lapsed
i.e. every lapsed customer the owner is counted is exactly a member of the send cohort (all seeded
lapsed customers are sendable — subscribed + consent + no prior contact), and no <45d customer leaks
in. Synthetic tenant only, NO send. Prints counts + PASS/FAIL (ids abbreviated)."""

from __future__ import annotations

import os
import sys
from uuid import UUID


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("usage: vt632_selection_canary.py <TENANT_ID> [expected_lapsed]")
        return 2
    tid = str(UUID(sys.argv[1]))
    expected = int(sys.argv[2]) if len(sys.argv) == 3 else None

    dsn = os.environ.get("TEAM_SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("FAIL: no DATABASE_URL / TEAM_SUPABASE_DB_URL in env")
        return 2
    os.environ.setdefault("TEAM_SUPABASE_DB_URL", dsn)

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        from orchestrator.agents.sales_recovery_executor import detect_lapsed_customers
        from orchestrator.db import tenant_connection
        from orchestrator.db.wrappers import LAPSED_WINDOW_DAYS, CustomersWrapper

        with tenant_connection(tid) as conn:
            count = CustomersWrapper().count_lapsed(tid, days=LAPSED_WINDOW_DAYS, conn=conn)
            base = CustomersWrapper().count_with_sales(tid, conn=conn)
            cands = detect_lapsed_customers(tid, conn=conn)
    finally:
        shutdown_dbos()

    cohort = len(cands)
    cohort_ids = sorted(str(c.customer_id)[:8] for c in cands)
    print(f"[canary] tenant ..{tid[-6:]}  window={LAPSED_WINDOW_DAYS}d")
    print(f"[canary] customers_with_sales={base}  owner_count_lapsed={count}  sr_cohort_size={cohort}")
    print(f"[canary] cohort ids (abbrev): {cohort_ids}")

    ok_coherence = count == cohort
    ok_nonempty = cohort > 0
    ok_expected = expected is None or count == expected
    print(f"[assert] count == cohort         : {ok_coherence}  ({count} == {cohort})")
    print(f"[assert] cohort non-empty        : {ok_nonempty}")
    if expected is not None:
        print(f"[assert] count == expected({expected}) : {ok_expected}")

    passed = ok_coherence and ok_nonempty and ok_expected
    print(f"\nRESULT: {'COHERENCE PASS — owner count == SR cohort' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
