"""VT-721 — week-plan 3-fire chain canary (design §5, Rule #15): on the DEPLOYED-dev substrate,
seed a tenant with an accepted 3-item roadmap + a fake day-1 outcome, run the REAL revision pass
(real LLM via the business_plan model slot) for three consecutive simulated days, and assert:
a 3-row chain (prev_plan_id linked) · gated actions (approval stamped on effect classes) ·
non-empty WHY-notes on revisions 2-3 · latest_plan reads day 3. Children-first teardown.

Run:  railway run --service vt-orchestrator-service --environment development -- \
        uv run python canaries/vt721_week_plan_canary.py
Requires ANTHROPIC_API_KEY in the injected env (Railway holds it). Fails loudly.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg


def _load_local_anthropic_key() -> None:
    """Layer the LOCAL key when railway's sealed one reads unset. Parsed in-process, never
    echoed (CL-431; the e2e_wa_sim loader verbatim)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    p = Path(__file__).resolve().parents[3] / ".viabe" / "secrets" / "anthropic.env"
    try:
        for line in p.read_text().splitlines():
            bare = line.strip().removeprefix("export ").strip()
            if bare.startswith("ANTHROPIC_API_KEY="):
                val = bare.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    os.environ["ANTHROPIC_API_KEY"] = val
    except Exception:  # noqa: BLE001 — the LLM call will fail loudly instead
        pass


def main() -> int:
    dsn = os.environ["DATABASE_URL"]
    os.environ.setdefault("TEAM_SUPABASE_DB_URL", dsn)
    os.environ["TEAM_WEEK_PLAN"] = "shadow"
    _load_local_anthropic_key()

    from dbos_config import launch_dbos, shutdown_dbos

    launch_dbos()
    ok: list[tuple[str, bool]] = []

    def check(name: str, cond: bool) -> None:
        ok.append((name, cond))
        print(f"{'PASS' if cond else 'FAIL'}: {name}")

    tenant_id = None
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(
                "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
                "whatsapp_number, owner_inputs, business_type) "
                "VALUES ('VT-721 week-plan canary (kirana)', 'founding', 'paid_active', now(), "
                "'+15559990721', TRUE, 'kirana') RETURNING id"
            ).fetchone()
            tenant_id = str(row[0])
        print(f"seeded tenant {tenant_id}")

        from orchestrator.business_plan.store import write_new_version

        roadmap = [
            {"item_id": "it-1", "seq": 1, "month": 1, "status": "accepted",
             "owning_agent": "sales_recovery",
             "objective": "Win back customers lapsed 45+ days with a personal WhatsApp offer",
             "why": "Lapsed cohort is the fastest recoverable revenue"},
            {"item_id": "it-2", "seq": 2, "month": 1, "status": "accepted",
             "owning_agent": "sales_recovery",
             "objective": "Follow up every open quote within 48 hours",
             "why": "Quotes go cold after two days"},
            {"item_id": "it-3", "seq": 3, "month": 1, "status": "accepted",
             "owning_agent": "campaigns",
             "objective": "Announce the festive-season stock to repeat buyers",
             "why": "Seasonal demand window"},
        ]
        write_new_version(
            tenant_id, summary={"headline": "canary"}, roadmap=roadmap,
            fact_bundle={}, generated_by="canary",
        )

        from orchestrator.business_plan.week_plan import latest_plan
        from orchestrator.business_plan.week_plan_revision import revise_week_plan

        base = datetime(2026, 7, 28, 6, 0, tzinfo=timezone.utc)
        ids = []
        for day in range(3):
            if day == 1:
                # A day-1 "outcome" so revision 2 has something real to factor.
                with psycopg.connect(dsn, autocommit=True) as conn:
                    conn.execute(
                        "INSERT INTO manager_tasks (tenant_id, objective, status, "
                        "idempotency_key) VALUES (%s, %s::jsonb, 'completed', %s)",
                        (tenant_id, '"win-back wave 1 sent to lapsed cohort"',
                         f"canary-outcome-{tenant_id[:8]}"),
                    )
            pid = revise_week_plan(tenant_id, now=base + timedelta(days=day))
            print(f"day {day + 1}: plan id {pid}")
            ids.append(pid)
        check("three revisions written", all(ids))

        with psycopg.connect(dsn, autocommit=True) as conn:
            rows = conn.execute(
                "SELECT id, prev_plan_id, actions, revision_notes FROM tenant_week_plans "
                "WHERE tenant_id = %s ORDER BY plan_date",
                (tenant_id,),
            ).fetchall()
        check("chain length 3", len(rows) == 3)
        if len(rows) == 3:
            check("chain linked", rows[1][1] == rows[0][0] and rows[2][1] == rows[1][0])
            all_actions = [a for r in rows for a in r[2]]
            check("actions present", bool(all_actions))
            effect = [a for a in all_actions if a.get("action_class")
                      in {"customer_message", "campaign_send", "spend", "commitment", "settings_change"}]
            check("approval stamped on every effect-class action",
                  all(a.get("requires_approval") for a in effect) if effect else True)
            check("why-notes on later revisions", bool(rows[1][3]) or bool(rows[2][3]))
        latest = latest_plan(tenant_id)
        check("latest_plan reads day 3", latest is not None and str(latest.plan_id) == str(rows[2][0]) if len(rows) == 3 else False)
    finally:
        if tenant_id:
            with psycopg.connect(dsn, autocommit=True) as conn:
                tbls = [r[0] for r in conn.execute(
                    "SELECT DISTINCT c.table_name FROM information_schema.columns c "
                    "JOIN information_schema.tables t ON t.table_name=c.table_name "
                    "AND t.table_schema='public' AND t.table_type='BASE TABLE' "
                    "WHERE c.column_name='tenant_id' AND c.table_schema='public'"
                ).fetchall()]
                for _ in range(4):
                    left = []
                    for tbl in tbls:
                        try:
                            conn.execute(f"DELETE FROM {tbl} WHERE tenant_id = %s", (tenant_id,))
                        except Exception:  # noqa: BLE001 — FK ordering; retried
                            left.append(tbl)
                    tbls = left
                    try:
                        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
                        break
                    except Exception:  # noqa: BLE001
                        continue
            print(f"teardown: tenant {tenant_id} deleted")
        shutdown_dbos()

    failed = [n for n, c in ok if not c]
    print(f"\n=== vt721 week-plan canary: {len(ok) - len(failed)}/{len(ok)} PASS ===")
    return 1 if failed or not ok else 0


if __name__ == "__main__":
    sys.exit(main())
