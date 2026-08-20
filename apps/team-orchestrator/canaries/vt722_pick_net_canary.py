"""VT-722 — enforce-path pick-net canary (the row's exit gate, Rule #15).

Proves ON DEPLOYED DEV (enforce mode) that an agent-pick tap arriving through the REAL ingress
records into the asserted-facts ledger even though the brain consumes the turn: seeds a bogus
owner_inputs=TRUE tenant, POSTs "Sales Recovery", polls `manager_asserted_facts` for the
active_agent row; then POSTs "Campaigns" and asserts the supersession chain (the VT-719
contradiction substrate firing on a live enforce-path flip). Children-first teardown.

Run:  railway run --service vt-orchestrator-service --environment development -- \
        uv run python canaries/vt722_pick_net_canary.py \
        --ingress-url https://vt-orchestrator-service-development.up.railway.app
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import convo_harness as ch  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingress-url", default=None)
    ap.add_argument("--wait", type=float, default=90.0)
    args = ap.parse_args()

    base = ch._ingress_base(args.ingress_url)
    secret = ch._dev_secret()
    dsn = ch._dsn()
    number = ch.bogus_number()

    import psycopg

    ok: list[tuple[str, bool]] = []

    def check(name: str, cond: bool) -> None:
        ok.append((name, cond))
        print(f"{'PASS' if cond else 'FAIL'}: {name}")

    def post(body: str) -> None:
        sid = ch.fresh_inbound_sid()
        r = ch._post_inbound(
            base, secret,
            {"From": f"whatsapp:{number}", "To": "whatsapp:+910000000000",
             "Body": body, "MessageSid": sid},
        )
        print(f"owner -> {body!r} (ingress: {r.get('reason')})")

    def ledger(tenant_id: str) -> list:
        with psycopg.connect(dsn, autocommit=True) as conn:
            return conn.execute(
                "SELECT fact_key, fact_value, status FROM manager_asserted_facts "
                "WHERE tenant_id = %s AND fact_key = 'active_agent' ORDER BY asserted_at",
                (tenant_id,),
            ).fetchall()

    def wait_for(tenant_id: str, cond, label: str) -> list:
        deadline = time.time() + args.wait
        rows: list = []
        while time.time() < deadline:
            rows = ledger(tenant_id)
            if cond(rows):
                return rows
            time.sleep(4)
        print(f"(timeout waiting for {label}; rows={rows})")
        return rows

    tenant_id = None
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(
                "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
                "whatsapp_number, owner_inputs) "
                "VALUES ('VT-722 pick-net canary', 'founding', 'paid_active', now(), %s, TRUE) "
                "RETURNING id",
                (number,),
            ).fetchone()
            tenant_id = str(row[0])
        print(f"seeded tenant {tenant_id} (owner_inputs=TRUE)")

        post("Sales Recovery")
        rows = wait_for(tenant_id, lambda r: len(r) >= 1, "first pick")
        check("pick recorded on live enforce path", len(rows) == 1)
        check("pick value is sales_recovery", bool(rows) and rows[0][1] == "sales_recovery")
        check("pick is active", bool(rows) and rows[0][2] == "active")

        post("Campaigns")
        rows = wait_for(tenant_id, lambda r: len(r) >= 2, "flip supersession")
        actives = [r for r in rows if r[2] == "active"]
        superseded = [r for r in rows if r[2] == "superseded"]
        check("flip recorded (2 rows)", len(rows) == 2)
        check("new active is campaigns", len(actives) == 1 and actives[0][1] == "campaigns")
        check("old pick superseded (contradiction substrate fired)",
              len(superseded) == 1 and superseded[0][1] == "sales_recovery")
    finally:
        if tenant_id:
            # The brain run the second tap triggered may still be WRITING (tm_audit_log,
            # episodic_events land seconds after the reply) — settle first, then sweep in
            # passes until the tenant delete succeeds.
            time.sleep(25)
            with psycopg.connect(dsn, autocommit=True) as conn:
                tbls = [r[0] for r in conn.execute(
                    "SELECT DISTINCT c.table_name FROM information_schema.columns c "
                    "JOIN information_schema.tables t ON t.table_name=c.table_name "
                    "AND t.table_schema='public' AND t.table_type='BASE TABLE' "
                    "WHERE c.column_name='tenant_id' AND c.table_schema='public'"
                ).fetchall()]
                for attempt in range(6):
                    left = []
                    for tbl in tbls:
                        try:
                            conn.execute(f"DELETE FROM {tbl} WHERE tenant_id = %s", (tenant_id,))
                        except Exception:  # noqa: BLE001 — FK ordering; retried next pass
                            left.append(tbl)
                    tbls = left
                    try:
                        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
                        break
                    except Exception:  # noqa: BLE001 — late async writes; settle + re-sweep
                        time.sleep(5)
                        tbls = tbls or [r[0] for r in conn.execute(
                            "SELECT DISTINCT c.table_name FROM information_schema.columns c "
                            "JOIN information_schema.tables t ON t.table_name=c.table_name "
                            "AND t.table_schema='public' AND t.table_type='BASE TABLE' "
                            "WHERE c.column_name='tenant_id' AND c.table_schema='public'"
                        ).fetchall()]
            print(f"teardown: tenant {tenant_id} deleted")

    failed = [n for n, c in ok if not c]
    print(f"\n=== vt722 pick-net canary: {len(ok) - len(failed)}/{len(ok)} PASS ===")
    return 1 if failed or not ok else 0


if __name__ == "__main__":
    sys.exit(main())
