"""VT-40 — query_customer_ledger standalone tool.

Deterministic per-tenant ledger lookup. Pydantic IO is the binding
contract; standalone callable. NOT wired to an Agent yet (VT-4 SDK
skeleton is Backlog).

Schema gap: there is no `customer_ledger_entries` table in main yet
(migrations 000-043; matches the pii_redactor comment for the parallel
`customers` gap). The tool implements the contract using the
forward-target schema and returns an empty ledger gracefully when the
relation is missing. The future VT row that adds the migration
replaces the graceful-empty branch with real rows without changing
the Pydantic IO.

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

    RLS: SET LOCAL app.current_tenant for the duration of the SELECT.
    """
    if pool is None:
        from orchestrator.graph import get_pool

        pool = get_pool()

    with pool.connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SET LOCAL app.current_tenant = %s", (payload.tenant_id,),
                )
                cur.execute(
                    """
                    SELECT id
                    FROM customers
                    WHERE tenant_id = %s AND phone_token = %s
                    LIMIT 1
                    """,
                    (payload.tenant_id, payload.customer_phone_token),
                )
                row = cur.fetchone()
                if row is None:
                    logger.info(
                        "query_customer_ledger: no customer match "
                        "(tenant=%s, entries=0)",
                        payload.tenant_id,
                    )
                    return QueryCustomerLedgerOutput(
                        customer_id=None,
                        ledger_entries=[],
                        total_balance_paise=0,
                    )
                customer_id = (
                    row["id"] if isinstance(row, dict) else row[0]
                )

                since_clause = ""
                params: tuple[Any, ...] = (customer_id, payload.limit)
                if payload.since_date is not None:
                    since_clause = "AND entry_date >= %s"
                    params = (
                        customer_id, payload.since_date, payload.limit,
                    )
                cur.execute(
                    f"""
                    SELECT entry_date, amount_paise, description
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
                        entry_date=(
                            r["entry_date"] if isinstance(r, dict) else r[0]
                        ),
                        amount_paise=int(
                            r["amount_paise"] if isinstance(r, dict) else r[1]
                        ),
                        description=str(
                            r["description"] if isinstance(r, dict) else r[2]
                        ),
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
        except Exception as exc:  # noqa: BLE001
            # psycopg.errors.UndefinedTable is the load-bearing case
            # (forward-target schema not landed yet). Caught broadly so
            # the import stays psycopg-free at module load; type name
            # match keeps the intent obvious.
            type_name = type(exc).__name__
            if type_name != "UndefinedTable":
                raise
            # Return empty gracefully so callers can advance development
            # without the migration. Future VT row adds the table; this
            # branch becomes unreachable.
            logger.info(
                "query_customer_ledger: customers/ledger schema absent "
                "(tenant=%s); returning empty result",
                payload.tenant_id,
            )
            return QueryCustomerLedgerOutput(
                customer_id=None,
                ledger_entries=[],
                total_balance_paise=0,
            )


__all__ = [
    "LedgerEntry",
    "QueryCustomerLedgerInput",
    "QueryCustomerLedgerOutput",
    "query_customer_ledger",
]
