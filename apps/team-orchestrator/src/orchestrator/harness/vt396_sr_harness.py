"""VT-396 — DEV-ONLY Sales-Recovery e2e harness (Cowork-gated 2026-06-23).

Seeds a SYNTHETIC consented lapsed customer for a tenant, verifies lapsed-detection, simulates a
STOP, and dispatches ONE real sales-recovery run (drafts + arms owner approval). The actual
WhatsApp SEND still goes through the UNCHANGED gate stack + owner approval — this harness NEVER
auto-approves and NEVER auto-sends, and never bypasses caps / opt-out / consent.

BINDING (CL-438 / CL-422):
  • DEV ONLY. Every mutating entry point asserts the connected DB is NOT prod before touching data.
  • The win-back recipient MUST be a SYNTHETIC test number, never a real customer.
  • Marketing detection stays fail-closed unless the dev env's ``MARKETING_CONSENT_VERSIONS``
    admits the seeded consent version — counsel C1–C3 remains the real production gate.
  • Uses ``team_winback_simple`` ONLY — never the money-bearing offer-template variant, which
    stays always-confirm and is out of scope for this harness.

This module is never imported by the production request path (it lives under ``harness/``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import psycopg

from orchestrator.agents.coordinator import AgentItemContext, ItemExecutionResult
from orchestrator.agents.sales_recovery_executor import (
    WINBACK_TEMPLATE_NAME,
    LapsedCandidate,
    SalesRecoveryAgent,
    detect_lapsed_customers,
)
from orchestrator.db import tenant_connection
from orchestrator.graph import get_pool
from orchestrator.privacy import consent

logger = logging.getLogger(__name__)

# Env names that are NEVER acceptable for a seed/dispatch. The guard is a PROD-deny (not a
# dev-only-allow) so the same harness works against dev + ephemeral CI/local test DBs, while a
# connected production DB is refused outright. Mirrors apply_migrations' --expected-env spirit.
_PROD_ENV_NAMES = frozenset({"prod", "production"})

# One lapsed sale: 120 days ago, ₹750 (75000 paise). With a single seeded customer the tenant
# percentiles collapse to that customer, so any lapsed, non-zero-spend sale clears p75/p50.
DEFAULT_SALES: tuple[tuple[int, int], ...] = ((120, 75000),)


@dataclass(frozen=True, slots=True)
class SeedResult:
    """What a seed produced — IDs + the token/version so a caller can assert the join end-to-end."""

    tenant_id: UUID
    customer_id: UUID
    phone_token: str
    consent_version: str
    sales_count: int
    reused_existing_customer: bool


def _env_name(conn: Any) -> str:
    """Read the ``app_environment`` singleton sentinel (VT-362).

    A missing ROW or a missing TABLE both yield ``'unknown'`` (allowed). The sentinel is created
    only on a provisioned env (``apply_migrations --expected-env``) — prod ALWAYS has it set to
    ``'prod'`` by construction, so its absence means an ephemeral local/CI DB, never production.
    Only an explicit prod name is refused; everything else is permitted.
    """
    try:
        row = conn.execute("SELECT name FROM app_environment LIMIT 1").fetchone()
    except psycopg.errors.UndefinedTable:
        return "unknown"
    if row is None:
        return "unknown"
    return str(row["name"] if isinstance(row, dict) else row[0])


def assert_not_prod(conn: Any) -> str:
    """Raise if the connected DB is production. Returns the env name on success."""
    name = _env_name(conn)
    if name in _PROD_ENV_NAMES:
        raise RuntimeError(
            f"VT-396 harness refuses to run against env={name!r} — this is a DEV-ONLY harness "
            "(CL-438/CL-422: no synthetic seed or test send against production)."
        )
    return name


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def seed_synthetic_customer(
    tenant_id: UUID | str,
    phone_e164: str,
    *,
    consent_version: str,
    display_name: str = "Synthetic Test Customer",
    sales: tuple[tuple[int, int], ...] | None = None,
) -> SeedResult:
    """Seed ONE synthetic, consented, lapsed customer for ``tenant_id`` so lapsed-detection
    returns it (once the dev ``MARKETING_CONSENT_VERSIONS`` admits ``consent_version``).

    Idempotent: re-seeding the same (tenant, phone) reuses the existing customer and skips
    already-present sale rows (deterministic ``entry_key``). Consent is written through the REAL
    ``privacy.consent.record_consent`` so ``record_of_consent.phone_token`` matches the salted
    hash that ``detect_lapsed_customers`` joins on.

    ``phone_e164`` MUST be a synthetic test number (caller's responsibility — never a real
    customer, CL-422). ``sales`` = ``[(days_ago, amount_paise), ...]``; defaults to one lapsed sale.
    """
    tid = _as_uuid(tenant_id)
    sale_rows = sales if sales is not None else DEFAULT_SALES

    with get_pool().connection() as conn:
        assert_not_prod(conn)

        existing = conn.execute(
            "SELECT id FROM customers WHERE tenant_id = %s AND phone_e164 = %s",
            (str(tid), phone_e164),
        ).fetchone()
        if existing is not None:
            customer_id = _as_uuid(existing["id"] if isinstance(existing, dict) else existing[0])
            reused = True
        else:
            row = conn.execute(
                "INSERT INTO customers "
                "(tenant_id, display_name, phone_e164, opt_out_status, complaint_status, source) "
                "VALUES (%s, %s, %s, 'subscribed', 'none', 'vt396_harness') RETURNING id",
                (str(tid), display_name, phone_e164),
            ).fetchone()
            assert row is not None
            customer_id = _as_uuid(row["id"] if isinstance(row, dict) else row[0])
            reused = False

        for idx, (days_ago, amount_paise) in enumerate(sale_rows):
            # Deterministic key → ON CONFLICT no-op makes re-seeding idempotent.
            entry_key = f"vt396-{customer_id}-{idx}"
            conn.execute(
                "INSERT INTO customer_ledger_entries "
                "(tenant_id, customer_id, amount_paise, entry_type, entry_date, "
                " acquired_via, source_confidence, entry_key) "
                "VALUES (%s, %s, %s, 'sale', CURRENT_DATE - %s::int, "
                " 'vt396_harness', 1.0, %s) "
                "ON CONFLICT (tenant_id, entry_key) DO NOTHING",
                (str(tid), str(customer_id), amount_paise, days_ago, entry_key),
            )

    # Consent through the real writer (its own pool connection) — guarantees the token join.
    record = consent.record_consent(
        tid, phone_e164, consent_text_version=consent_version, consent_method="qr_optin"
    )

    result = SeedResult(
        tenant_id=tid,
        customer_id=customer_id,
        phone_token=record.phone_token,
        consent_version=consent_version,
        sales_count=len(sale_rows),
        reused_existing_customer=reused,
    )
    logger.info("vt396 seed: %s", result)
    return result


def detect_for_tenant(tenant_id: UUID | str, *, limit: int = 50) -> list[LapsedCandidate]:
    """Run lapsed-detection for ``tenant_id`` under an RLS-scoped tenant connection. Read-only.

    Returns ``[]`` whenever ``MARKETING_CONSENT_VERSIONS`` is empty (the fail-closed default) —
    so on prod (env unset) this is always empty even if called.
    """
    tid = _as_uuid(tenant_id)
    with tenant_connection(tid) as conn:
        return detect_lapsed_customers(tid, conn=conn, limit=limit)


def opt_out_synthetic(tenant_id: UUID | str, phone_e164: str) -> bool:
    """Simulate the customer texting STOP — opt the synthetic number out (§5 floor check).

    After this, ``detect_for_tenant`` must exclude the customer (consent ``opted_out_at`` stamped).
    """
    tid = _as_uuid(tenant_id)
    with get_pool().connection() as conn:
        assert_not_prod(conn)
    return consent.opt_out_for_phone(tid, phone_e164)


def _create_work_item(conn: Any, tenant_id: UUID) -> UUID:
    row = conn.execute(
        "INSERT INTO agent_work_items (tenant_id, item_id, agent, status) "
        "VALUES (%s, %s, 'sales_recovery', 'drafting') RETURNING id",
        (str(tenant_id), f"vt396-item-{tenant_id}"),
    ).fetchone()
    assert row is not None
    return _as_uuid(row["id"] if isinstance(row, dict) else row[0])


def dispatch_once(tenant_id: UUID | str, *, run_id: str | None = None) -> ItemExecutionResult:
    """Dispatch ONE real sales-recovery run for ``tenant_id``: detect → draft (``team_winback_simple``)
    → persist batch → ARM owner approval. Returns the execution result (batch_id + counters).

    This runs the production ``SalesRecoveryAgent.execute_item`` with its real LLM + real approval
    arm — so it ISSUES the owner-approval WhatsApp. It does NOT send to the customer; that happens
    only when the owner approves, through the unchanged ``agent_send_draft`` gate stack. DEV-ONLY.
    """
    tid = _as_uuid(tenant_id)
    with get_pool().connection() as conn:
        assert_not_prod(conn)
        work_item_id = _create_work_item(conn, tid)

    ctx = AgentItemContext(
        tenant_id=str(tid),
        item_id=f"vt396-roadmap-{tid}",
        agent="sales_recovery",
        work_item_id=str(work_item_id),
        run_id=run_id or str(work_item_id),
    )
    result = SalesRecoveryAgent().execute_item(ctx)
    logger.info(
        "vt396 dispatch: tenant=%s status=%s batch=%s counters=%s",
        tid,
        result.work_item_status,
        result.batch_id,
        result.counters,
    )
    return result


# ---------------------------------------------------------------------------
# CLI — dev ops entry point. Bootstraps the substrate from DATABASE_URL, then runs the subcommand.
#   python -m orchestrator.harness.vt396_sr_harness seed     --tenant <uuid> --phone +91… --version dev-test-v0
#   python -m orchestrator.harness.vt396_sr_harness detect   --tenant <uuid>
#   python -m orchestrator.harness.vt396_sr_harness optout   --tenant <uuid> --phone +91…
#   python -m orchestrator.harness.vt396_sr_harness dispatch --tenant <uuid>
# ---------------------------------------------------------------------------


def _bootstrap() -> None:
    from orchestrator.graph import init_substrate

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL not set")
    init_substrate(database_url)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI shell
    parser = argparse.ArgumentParser(description="VT-396 dev-only Sales-Recovery e2e harness.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_seed = sub.add_parser("seed", help="seed a synthetic consented lapsed customer")
    p_seed.add_argument("--tenant", required=True, type=UUID)
    p_seed.add_argument("--phone", required=True, help="SYNTHETIC test E.164 number")
    p_seed.add_argument("--version", required=True, help="consent_text_version (e.g. dev-test-v0)")
    p_seed.add_argument("--name", default="Synthetic Test Customer")

    p_detect = sub.add_parser("detect", help="run lapsed-detection (read-only)")
    p_detect.add_argument("--tenant", required=True, type=UUID)

    p_optout = sub.add_parser("optout", help="simulate STOP for the synthetic number")
    p_optout.add_argument("--tenant", required=True, type=UUID)
    p_optout.add_argument("--phone", required=True)

    p_dispatch = sub.add_parser("dispatch", help="dispatch ONE real run (drafts + arms approval)")
    p_dispatch.add_argument("--tenant", required=True, type=UUID)

    args = parser.parse_args(argv)
    _bootstrap()

    if args.cmd == "seed":
        result = seed_synthetic_customer(
            args.tenant, args.phone, consent_version=args.version, display_name=args.name
        )
        print(
            json.dumps(
                {
                    **result.__dict__,
                    "tenant_id": str(result.tenant_id),
                    "customer_id": str(result.customer_id),
                },
                indent=2,
            )
        )
    elif args.cmd == "detect":
        cands = detect_for_tenant(args.tenant)
        print(
            json.dumps(
                {
                    "count": len(cands),
                    "candidates": [
                        {
                            "customer_id": str(c.customer_id),
                            "days_since_last_sale": c.days_since_last_sale,
                            "lifetime_spend_paise": c.lifetime_spend_paise,
                        }
                        for c in cands
                    ],
                },
                indent=2,
            )
        )
    elif args.cmd == "optout":
        print(json.dumps({"opted_out": opt_out_synthetic(args.tenant, args.phone)}))
    elif args.cmd == "dispatch":
        result = cast("Any", dispatch_once(args.tenant))
        print(
            json.dumps(
                {
                    "work_item_status": result.work_item_status,
                    "batch_id": result.batch_id,
                    "counters": result.counters,
                },
                indent=2,
            )
        )
    return 0


__all__ = [
    "SeedResult",
    "WINBACK_TEMPLATE_NAME",
    "assert_not_prod",
    "detect_for_tenant",
    "dispatch_once",
    "opt_out_synthetic",
    "seed_synthetic_customer",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
