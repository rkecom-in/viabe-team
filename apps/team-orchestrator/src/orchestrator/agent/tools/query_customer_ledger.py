"""VT-40 — query_customer_ledger standalone tool.

Deterministic per-tenant ledger lookup. Pydantic IO is the binding
contract; standalone callable. NOT wired to an Agent yet (VT-4 SDK
skeleton is Backlog).

VT-258 (wired): reads the LANDED schema. The customer is resolved by
`phone_token` via `phone_token_resolutions.customer_id` (populated by
dedup_and_merge, VT-54) — there is NO `customers.phone_token` column (the
earlier assumption). Ledger rows come from `customer_ledger_entries`
(VT-273, migration 061): `entry_date`, `amount_paise`, `notes` (mapped to the
IO's `description`). The prior graceful-empty / narrow-except tolerance
(VT-257/VT-264) is REMOVED now the schema exists — a real query error RAISES.
A missing customer or empty ledger returns an empty result (not an error).

NO PII in logs (CL-390): only tenant_id + counts logged; phone tokens
are referenced but never logged.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class LedgerEntry(BaseModel):
    """One historical entry against a customer's running balance."""

    model_config = ConfigDict(frozen=True)

    entry_date: date
    amount_paise: int
    description: str = Field(..., max_length=240)


class QueryCustomerLedgerInput(BaseModel):
    """Tenant + customer phone TOKEN (not raw phone) + bounds."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., min_length=1)
    customer_phone_token: str = Field(..., min_length=1)
    since_date: date | None = None
    limit: int = Field(default=100, ge=1, le=1000)


class QueryCustomerLedgerOutput(BaseModel):
    """Resolved customer + their ledger window + total."""

    model_config = ConfigDict(frozen=True)

    customer_id: str | None
    ledger_entries: list[LedgerEntry]
    total_balance_paise: int


def query_customer_ledger(
    payload: QueryCustomerLedgerInput,
    *,
    pool: Any | None = None,
) -> QueryCustomerLedgerOutput:
    """Read customer ledger for `tenant_id` + phone_token within bounds.

    `pool`: psycopg connection pool. Defaults to the DBOS-managed pool.
    Tests inject a mock pool.

    RLS: scopes app.current_tenant for the connection via set_config
    (session-scoped; pool reset clears it on return — see graph._reset_connection).
    """
    if pool is None:
        from orchestrator.graph import get_pool

        pool = get_pool()

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('app.current_tenant', %s, false)",
            (payload.tenant_id,),
        )
        # VT-258: resolve the customer by phone_token via phone_token_resolutions
        # (there is NO customers.phone_token column — the earlier assumption; the
        # link is phone_token_resolutions.customer_id, populated by
        # dedup_and_merge VT-54). Owner-pool + explicit tenant_id filter (that
        # table grants to the owner role, not app_role) — tenant-scoped by WHERE.
        cur.execute(
            "SELECT customer_id FROM phone_token_resolutions "
            "WHERE phone_token = %s AND tenant_id = %s LIMIT 1",
            (payload.customer_phone_token, payload.tenant_id),
        )
        row = cur.fetchone()
        customer_id = (
            None if row is None
            else (row["customer_id"] if isinstance(row, dict) else row[0])
        )
        if customer_id is None:
            logger.info(
                "query_customer_ledger: no customer match (tenant=%s, entries=0)",
                payload.tenant_id,
            )
            return QueryCustomerLedgerOutput(
                customer_id=None, ledger_entries=[], total_balance_paise=0
            )

        since_clause = ""
        params: tuple[Any, ...] = (customer_id, payload.limit)
        if payload.since_date is not None:
            since_clause = "AND entry_date >= %s"
            params = (customer_id, payload.since_date, payload.limit)
        cur.execute(
            f"""
            SELECT entry_date, amount_paise, notes
            FROM customer_ledger_entries
            WHERE customer_id = %s {since_clause}
            ORDER BY entry_date DESC, id DESC
            LIMIT %s
            """,
            params,
        )
        raw_entries = cur.fetchall()
        entries = [
            LedgerEntry(
                entry_date=(r["entry_date"] if isinstance(r, dict) else r[0]),
                amount_paise=int(r["amount_paise"] if isinstance(r, dict) else r[1]),
                # canonical column is `notes` (VT-273); map to the IO's
                # `description` field. NULL notes → "".
                description=str((r["notes"] if isinstance(r, dict) else r[2]) or "")[:240],
            )
            for r in raw_entries
        ]
        total = sum(e.amount_paise for e in entries)
        logger.info(
            "query_customer_ledger: tenant=%s entries=%d",
            payload.tenant_id, len(entries),
        )
        return QueryCustomerLedgerOutput(
            customer_id=str(customer_id),
            ledger_entries=entries,
            total_balance_paise=total,
        )


__all__ = [
    "LedgerEntry",
    "QueryCustomerLedgerInput",
    "QueryCustomerLedgerOutput",
    "query_customer_ledger",
]
