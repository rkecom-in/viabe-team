"""Live-drill seed (Cowork 20260702T110000Z step 2) — seed ONE lapsed/dormant customer for the
drill tenant so Sales Recovery has a real win-back target. Run AFTER Fazal's fresh signup creates
the tenant, via:

    railway run --service vt-orchestrator-service --environment development -- \
        uv run --directory apps/team-orchestrator python canaries/drill_seed_lapsed_customer.py <TENANT_ID>

The customer phone is +919820463598 — one of the four CL-2026-07-01 Fazal-PROVIDED allowlisted
numbers (never fabricated; dev send-guard passes it through, everything else stays mocked).

Seeds exactly what CustomersWrapper.lapsed_candidates requires:
  - customers row: subscribed, no open complaint, phone_e164 set.
  - customer_ledger_entries 'sale' rows: a purchase history whose MAX(entry_date) is ~45d ago
    (dormant) with real lifetime spend (single-customer percentiles = own values ⇒ passes floors).
  - record_of_consent: phone_token = hash_phone(phone) (TEAM_PHONE_HASH_SALT from the injected env),
    consent_text_version = first of MARKETING_CONSENT_VERSIONS, opted_out_at NULL.
  - no recent agent_customer_contacts (fresh tenant ⇒ none).

Prints ids + booleans only (never the full number — last-4)."""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from uuid import UUID

_CUSTOMER_PHONE = "+919820463598"  # Fazal-provided allowlisted number (CL-2026-07-01)
_NAME = "Drill Winback Customer"
_DAYS_DORMANT = 45


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: drill_seed_lapsed_customer.py <TENANT_ID>")
        return 2
    tid = str(UUID(sys.argv[1]))

    import psycopg

    dsn = os.environ.get("TEAM_SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    salt = os.environ.get("TEAM_PHONE_HASH_SALT", "")
    versions = [v for v in os.environ.get("MARKETING_CONSENT_VERSIONS", "").split(",") if v.strip()]
    if not dsn or not salt or not versions:
        print(f"FAIL: env incomplete (db={'set' if dsn else 'unset'} salt={'set' if salt else 'unset'} "
              f"consent_versions={len(versions)})")
        return 2

    from orchestrator.utils.phone_token import hash_phone  # same derivation the cohort SQL pins

    token = hash_phone(_CUSTOMER_PHONE)

    with psycopg.connect(dsn, autocommit=True) as c:
        t = c.execute("SELECT business_name FROM tenants WHERE id = %s", (tid,)).fetchone()
        if t is None:
            print(f"FAIL: tenant {tid[:8]}… not found — run AFTER the fresh signup")
            return 1
        # Idempotent (re-runnable mid-drill): reuse the existing customer row on conflict; the
        # ledger entry_key + a consent existence check dedupe the rest.
        # (idx_customers_tenant_phone is a PARTIAL unique index — ON CONFLICT needs the
        # predicate; a SELECT-first is simpler and equally race-free for a manual drill seed.)
        cust = c.execute(
            "SELECT id FROM customers WHERE tenant_id = %s AND phone_e164 = %s",
            (tid, _CUSTOMER_PHONE),
        ).fetchone()
        if cust is None:
            cust = c.execute(
                "INSERT INTO customers (tenant_id, display_name, phone_e164, opt_out_status) "
                "VALUES (%s, %s, %s, 'subscribed') RETURNING id",
                (tid, _NAME, _CUSTOMER_PHONE),
            ).fetchone()
        cid = str(cust[0])
        last_sale = date.today() - timedelta(days=_DAYS_DORMANT)
        for i, (d, paise) in enumerate([
            (last_sale - timedelta(days=120), 45_000_00),
            (last_sale - timedelta(days=60), 30_000_00),
            (last_sale, 25_000_00),
        ]):
            c.execute(
                "INSERT INTO customer_ledger_entries (tenant_id, customer_id, amount_paise, "
                "entry_type, entry_date, acquired_via, source_confidence, entry_key) "
                "VALUES (%s, %s, %s, 'sale', %s, 'drill_seed', 1.0, %s) "
                "ON CONFLICT DO NOTHING",
                (tid, cid, paise, d, f"drill-seed-{cid[:8]}-{i}"),
            )
        c.execute(
            "INSERT INTO record_of_consent (tenant_id, phone_token, consent_text_version, "
            "consent_method, source) "
            "SELECT %s, %s, %s, 'qr_optin', 'drill_seed' "
            "WHERE NOT EXISTS (SELECT 1 FROM record_of_consent WHERE tenant_id = %s "
            "                  AND phone_token = %s AND opted_out_at IS NULL)",
            (tid, token, versions[0], tid, token),
        )
    print(f"[seed] customer ..{_CUSTOMER_PHONE[-4:]} id={cid[:8]}… seeded: 3 sales, "
          f"last {_DAYS_DORMANT}d ago, consent v={versions[0]}, subscribed")

    # Verify-by-use through the REAL live detection path: launch the DBOS substrate (exactly the
    # realdb-test fixture posture) so tenant_connection (app_role, RLS-scoped) exists, then call the
    # executor's own detect_lapsed_customers — the wrapper's VT-306 guard requires the app_role conn.
    os.environ.setdefault("TEAM_SUPABASE_DB_URL", dsn)
    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    try:
        from orchestrator.agents.sales_recovery_executor import detect_lapsed_customers
        from orchestrator.db import tenant_connection

        with tenant_connection(tid) as tc:
            cands = detect_lapsed_customers(tid, conn=tc)
    finally:
        shutdown_dbos()
    hit = any(str(x.customer_id) == cid for x in cands)
    print(f"[verify] detect_lapsed_customers (REAL executor path) surfaces the seed: {hit} "
          f"(n={len(cands)})")
    print(f"\nRESULT: {'SEED PASS' if hit else 'SEED FAIL — candidate not surfaced'}")
    return 0 if hit else 1


if __name__ == "__main__":
    sys.exit(main())
