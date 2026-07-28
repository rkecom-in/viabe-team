"""VT-719 — asserted-facts ledger canary (Rule #15): prove the ledger on the DEPLOYED-dev substrate.

Seeds a bogus tenant on the dev DB, then exercises the REAL orchestrator module
(``manager.asserted_facts``) against it — record (through ``tenant_connection``, i.e. the RLS'd
app path), same-value idempotence, contradiction_check, owned-change supersession, bulk read —
and verifies rows physically via the service connection. Tears down children-first.

NOTE (enforce-parity, found 2026-07-29): dev runs ENFORCE mode, where the journey full-walker
paced-flow (and with it the agent-pick writer site) does not consume turns — the pick lands via
the brain/conductor (rostered enforce-beat-parity row). The policy-grant writer is deterministic
approval-glue and mode-independent. This canary therefore proves the LEDGER + module live on
dev; the wire proof of each writer rides its own deterministic path's tests.

Run:  railway run --service vt-orchestrator-service --environment development -- \
        uv run python canaries/vt719_ledger_canary.py
"""

from __future__ import annotations

import os
import sys

import psycopg


def main() -> int:
    dsn = os.environ["DATABASE_URL"]
    ok: list[tuple[str, bool]] = []

    def check(name: str, cond: bool) -> None:
        ok.append((name, cond))
        print(f"{'PASS' if cond else 'FAIL'}: {name}")

    os.environ.setdefault("TEAM_SUPABASE_DB_URL", dsn)
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    tenant_id = None
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(
                "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
                "whatsapp_number, owner_inputs) "
                "VALUES ('VT-719 ledger canary', 'founding', 'paid_active', now(), "
                "'+15559990719', TRUE) RETURNING id"
            ).fetchone()
            tenant_id = str(row[0])
        print(f"seeded tenant {tenant_id}")

        from orchestrator.manager.asserted_facts import (
            active_assertions,
            contradiction_check,
            record_assertion,
        )

        check("record active_agent", record_assertion(
            tenant_id, "active_agent", "sales_recovery",
            statement_text="Sales Recovery is on your team — FREE 1-month trial starts now.",
            surface="journey", message_sid="SMcanary719",
        ))
        check("record trial_terms", record_assertion(
            tenant_id, "trial_terms", {"months": 1, "auto_charge": False, "cancel_anytime": True},
            statement_text="after the month it's paid ONLY if you choose to continue",
            surface="journey",
        ))
        check("same-value idempotent", record_assertion(tenant_id, "active_agent", "sales_recovery"))
        prior = contradiction_check(tenant_id, "active_agent", "campaigns")
        check("contradiction detected on flip", bool(prior and prior.get("fact_value") == "sales_recovery"))
        check("no contradiction on same value", contradiction_check(tenant_id, "active_agent", "sales_recovery") is None)
        check("owned change supersedes", record_assertion(
            tenant_id, "active_agent", "campaigns",
            statement_text="earlier I said Sales Recovery — that's now Campaigns because you asked to switch.",
        ))
        facts = active_assertions(tenant_id)
        keys = {f["fact_key"] for f in facts}
        check("bulk read: one active per key", keys == {"active_agent", "trial_terms"} and len(facts) == 2)
        check("active value is the owned change", next(
            (f["fact_value"] for f in facts if f["fact_key"] == "active_agent"), None) == "campaigns")

        with psycopg.connect(dsn, autocommit=True) as conn:
            n_all, n_sup = conn.execute(
                "SELECT count(*), count(*) FILTER (WHERE status='superseded') "
                "FROM manager_asserted_facts WHERE tenant_id = %s",
                (tenant_id,),
            ).fetchone()
        check("append-only chain (3 rows, 1 superseded)", n_all == 3 and n_sup == 1)
    finally:
        if tenant_id:
            with psycopg.connect(dsn, autocommit=True) as conn:
                tbls = [r[0] for r in conn.execute(
                    "SELECT DISTINCT c.table_name FROM information_schema.columns c "
                    "JOIN information_schema.tables t ON t.table_name=c.table_name "
                    "AND t.table_schema='public' AND t.table_type='BASE TABLE' "
                    "WHERE c.column_name='tenant_id' AND c.table_schema='public'"
                ).fetchall()]
                for _ in range(3):
                    left = []
                    for tbl in tbls:
                        try:
                            conn.execute(f"DELETE FROM {tbl} WHERE tenant_id = %s", (tenant_id,))
                        except Exception:  # noqa: BLE001 — FK ordering; retried next pass
                            left.append(tbl)
                    tbls = left
                conn.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
            print(f"teardown: tenant {tenant_id} deleted")
        shutdown_dbos()

    failed = [n for n, c in ok if not c]
    print(f"\n=== vt719 ledger canary: {len(ok) - len(failed)}/{len(ok)} PASS ===")
    return 1 if failed or not ok else 0


if __name__ == "__main__":
    sys.exit(main())
