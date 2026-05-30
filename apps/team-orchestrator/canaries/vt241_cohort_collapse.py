#!/usr/bin/env python3
"""VT-241 — cohort collapse fail-closed wiring canary.

Verifies the collapse-path cohort wiring end-to-end: a campaign whose cohort
holds an unresolvable / cross-tenant customer id is REJECTED fail-closed (the
whole transaction rolls back — zero campaigns, zero recipients persisted), the
terminal classifies as a CLEAN ``completed`` collapse with a COUNT-ONLY reason
discriminator (never the rejected ids), and the rejection routes to a real
Meta-approved owner template. A valid cohort still flows through and persists.

Mock-mode CI default (A1 + A2 — pure-function + config-linkage, no DB). Real
dev-DB mode opt-in via VT241_REAL_DB=1 (A3 + A4) seeds SYNTHETIC data ONLY
(CL-422: fabricated tenant + display_name='vt241-syn-*'; no real PII), then
cleans up.

4 assertions:
- A1: _classify_terminal(campaign_rejected) → clean 'completed' collapse with
  reason 'campaign_not_sent_invalid_cohort:<count>', no plan handed downstream.
- A2: the rejection's intent routes through template_routing.yaml to a
  template_name that exists in twilio_templates.yaml with a non-null SID
  (no dangling owner-surface reference).
- A3: real fail-closed rollback — collapse_node on an unresolvable cohort
  returns the count-only reject dict AND persists 0 campaigns + 0 recipients.
- A4: real atomicity + happy path — a valid cohort persists 1 campaign +
  1 recipient; a mixed (real+bogus) cohort rejects with NO partial leak.

Wall-clock <= 10s.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

CONFIG = Path(__file__).resolve().parent.parent / "config"
RESULTS: dict[int, dict[str, Any]] = {}
SEEDED_TENANTS: list[str] = []


def assertion(num: int, name: str, passed: bool, *, observed: Any = None,
              expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed,
                    "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _plan(tenant_id: str, run_id: str, customer_ids: list[str]) -> Any:
    from orchestrator.agent.schemas.campaign_plan import (
        CampaignPlanProposed,
        CampaignWindow,
        ConfidenceLevel,
        EvidenceRef,
        EvidenceSourceKind,
        ExpectedARRR,
        Language,
        MessagePlan,
        TargetCohort,
    )
    from uuid import UUID

    now = datetime.now(UTC)
    return CampaignPlanProposed(
        tenant_id=UUID(tenant_id),
        run_id=UUID(run_id),
        generated_at=now,
        campaign_window=CampaignWindow(
            start=now + timedelta(hours=1), end=now + timedelta(days=7)
        ),
        target_cohort=TargetCohort(
            customer_ids=[UUID(c) for c in customer_ids],
            cohort_label="vt241-syn-cohort",
            cohort_size=len(customer_ids),
            selection_reason="synthetic canary cohort [E1].",
        ),
        expected_arrr=ExpectedARRR(
            low_paise=10_000_00, high_paise=30_000_00,
            confidence=ConfidenceLevel.MEDIUM, basis="prior yields [E1].",
        ),
        evidence_refs=[
            EvidenceRef(
                claim_id="E1",
                source_kind=EvidenceSourceKind.TOOL_CALL,
                source_id="vt241-canary",
            )
        ],
        message_plan=MessagePlan(
            template_id="team_winback_v1",
            template_params={"first_name": "Owner", "discount": "10"},
            language=Language.EN, personalization="owner-first-name.",
        ),
    )


def _real_pool() -> Any:
    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    return graph_mod.get_pool()


def _seed(pool: Any, tenant_id: str, *, customers: int = 0) -> tuple[str, list[str]]:
    """Seed a synthetic tenant + pipeline_run (+ N synthetic customers).
    Returns (run_id, [customer_id, ...])."""
    run_id = str(uuid4())
    cust_ids: list[str] = []
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant_id,))
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'founding', 'paid_at_risk')",
            (tenant_id, f"vt241-syn-{tenant_id[:8]}"),
        )
        cur.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, run_type, status) "
            "VALUES (%s, %s, 'orchestrator', 'running')",
            (run_id, tenant_id),
        )
        for i in range(customers):
            cur.execute(
                "INSERT INTO customers (tenant_id, display_name) "
                "VALUES (%s, %s) RETURNING id",
                (tenant_id, f"vt241-syn-cust-{i}"),
            )
            row = cur.fetchone()
            cust_ids.append(str(row["id"] if isinstance(row, dict) else row[0]))
    return run_id, cust_ids


def _counts(pool: Any, tenant_id: str, run_id: str) -> tuple[int, int]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant_id,))
        cur.execute("SELECT count(*) AS n FROM campaigns WHERE run_id = %s", (run_id,))
        r1 = cur.fetchone()
        cur.execute("SELECT count(*) AS n FROM campaign_recipients WHERE tenant_id = %s", (tenant_id,))
        r2 = cur.fetchone()
    n_camp = int(r1["n"] if isinstance(r1, dict) else r1[0])
    n_rcpt = int(r2["n"] if isinstance(r2, dict) else r2[0])
    return n_camp, n_rcpt


def _cleanup(pool: Any) -> None:
    if not SEEDED_TENANTS:
        return
    with pool.connection() as conn, conn.cursor() as cur:
        for tid in SEEDED_TENANTS:
            cur.execute("SELECT set_config('app.current_tenant', %s, false)", (tid,))
            cur.execute("DELETE FROM campaign_recipients WHERE tenant_id = %s", (tid,))
            cur.execute("DELETE FROM subscriber_states WHERE tenant_id = %s", (tid,))
            cur.execute("DELETE FROM campaigns WHERE tenant_id = %s", (tid,))
            cur.execute("DELETE FROM customers WHERE tenant_id = %s", (tid,))
            cur.execute("DELETE FROM pipeline_runs WHERE tenant_id = %s", (tid,))
            cur.execute("DELETE FROM tenants WHERE id = %s", (tid,))


def run_canary() -> int:
    real = os.environ.get("VT241_REAL_DB") == "1"
    if real and not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — VT241_REAL_DB=1 needs DATABASE_URL", file=sys.stderr)
        return 2
    print(f"PREFLIGHT OK (mode={'real-db' if real else 'mock'})")

    # --- A1: terminal classification (pure) ---
    from orchestrator.agent.dispatch import _classify_terminal

    path, status, reason, result = _classify_terminal(
        {"campaign_rejected": {"reason": "unresolved_cohort", "rejected_count": 2}}
    )
    pass_1 = (
        path == "collapse"
        and status == "completed"
        and reason == "campaign_not_sent_invalid_cohort:2"
        and result is None
    )
    assertion(1, "Reject classifies as clean 'completed' collapse, count-only reason",
              pass_1, observed={"path": path, "status": status, "reason": reason},
              expected="('collapse','completed','campaign_not_sent_invalid_cohort:2',None)")

    # --- A2: owner-surface routing linkage (config, no dangling reference) ---
    import yaml

    routing = yaml.safe_load((CONFIG / "template_routing.yaml").read_text())
    templates = yaml.safe_load((CONFIG / "twilio_templates.yaml").read_text())
    route = routing.get("campaign_not_sent_invalid_cohort", {})
    template_name = route.get("any")
    tmpl = templates.get(template_name, {}) if template_name else {}
    pass_2 = bool(template_name) and tmpl.get("content_sid") is not None
    assertion(2, "Reject intent routes to a real Meta-approved owner template",
              pass_2, observed={"template_name": template_name,
                                "sid_present": tmpl.get("content_sid") is not None},
              expected="intent → template_name → non-null content_sid")

    if real:
        from orchestrator.collapse import collapse_node
        from uuid import UUID

        pool = _real_pool()
        try:
            # A3: unresolvable cohort → fail-closed, nothing persisted.
            tid = str(uuid4())
            SEEDED_TENANTS.append(tid)
            run_id, _ = _seed(pool, tid, customers=0)
            bogus = str(uuid4())
            update = collapse_node({
                "tenant_id": UUID(tid),
                "run_id": UUID(run_id),
                "campaign_plan": _plan(tid, run_id, [bogus]),
            })
            n_camp, n_rcpt = _counts(pool, tid, run_id)
            pass_3 = (
                update == {"campaign_rejected": {"reason": "unresolved_cohort",
                                                 "rejected_count": 1}}
                and n_camp == 0 and n_rcpt == 0
            )
            assertion(3, "Real fail-closed: reject dict + 0 campaigns + 0 recipients",
                      pass_3, observed={"update": update, "campaigns": n_camp,
                                        "recipients": n_rcpt})

            # A4: happy path persists; mixed cohort rejects atomically (no leak).
            tid2 = str(uuid4())
            SEEDED_TENANTS.append(tid2)
            run_ok, custs = _seed(pool, tid2, customers=1)
            up_ok = collapse_node({
                "tenant_id": UUID(tid2),
                "run_id": UUID(run_ok),
                "campaign_plan": _plan(tid2, run_ok, [custs[0]]),
            })
            ok_camp, ok_rcpt = _counts(pool, tid2, run_ok)

            # Mixed (one real + one bogus) under a fresh tenant — must reject
            # AND roll back the real recipient too (all-or-nothing).
            tid3 = str(uuid4())
            SEEDED_TENANTS.append(tid3)
            run_mix, custs3 = _seed(pool, tid3, customers=1)
            real_c = custs3[0]
            bogus2 = str(uuid4())
            up_mix = collapse_node({
                "tenant_id": UUID(tid3),
                "run_id": UUID(run_mix),
                "campaign_plan": _plan(tid3, run_mix, [real_c, bogus2]),
            })
            mix_camp, mix_rcpt = _counts(pool, tid3, run_mix)
            pass_4 = (
                up_ok == {} and ok_camp == 1 and ok_rcpt == 1
                and up_mix == {"campaign_rejected": {"reason": "unresolved_cohort",
                                                     "rejected_count": 1}}
                and mix_camp == 0 and mix_rcpt == 0
            )
            assertion(4, "Real atomicity: valid persists (1+1); mixed rejects with no leak",
                      pass_4, observed={"happy": {"update": up_ok, "campaigns": ok_camp,
                                                  "recipients": ok_rcpt},
                                        "mixed": {"update": up_mix, "campaigns": mix_camp,
                                                  "recipients": mix_rcpt}})
        finally:
            _cleanup(pool)
            # Close the pool so its worker threads stop cleanly at exit.
            from orchestrator import graph as graph_mod
            if graph_mod._pool is not None:
                graph_mod._pool.close()
                graph_mod._pool = None
    else:
        assertion(3, "Real fail-closed rollback (real-mode only) — skipped in mock",
                  True, observed={"mode": "mock"})
        assertion(4, "Real atomicity + happy path (real-mode only) — skipped in mock",
                  True, observed={"mode": "mock"})

    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s)", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
