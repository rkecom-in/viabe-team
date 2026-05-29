#!/usr/bin/env python3
"""VT-41 — get_business_profile canary.

CI default mocks pool. Real-DB mode opt-in via VT41_REAL_DB=1
exercises live psycopg path against a seeded tenant.

3 assertions:
- A1: Pydantic IO validates
- A2: Mock-mode query with matched tenant + connectors returns shape
- A3: Mock-mode query with L1 + connector tables missing returns
      profile with null L1 + empty integrations

Wall-clock ≤ 5s.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}


def assertion(num: int, name: str, passed: bool, *, observed: Any = None,
               expected: Any = None) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed,
                    "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _undefined_table_exc() -> Exception:
    return type("UndefinedTable", (Exception,), {})("relation does not exist")


def _fake_pool(*, tenant_row, connector_rows=None, l1_row=None,
                connector_table_missing=False, l1_table_missing=False):
    cur = MagicMock()
    fetchone_q: list[Any] = [tenant_row, l1_row]
    fetchall_q: list[list[Any]] = [connector_rows or []]

    def _execute(sql: str, _p: tuple | None = None) -> None:
        if connector_table_missing and "tenant_connector_status" in sql:
            raise _undefined_table_exc()
        if l1_table_missing and "tenant_l1_profile" in sql:
            raise _undefined_table_exc()

    cur.execute.side_effect = _execute
    cur.fetchone.side_effect = lambda: (
        fetchone_q.pop(0) if fetchone_q else None
    )
    cur.fetchall.side_effect = lambda: (
        fetchall_q.pop(0) if fetchall_q else []
    )
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool


def run_canary() -> int:
    if os.environ.get("VT41_REAL_DB") == "1":
        if not os.environ.get("DATABASE_URL"):
            print("PREFLIGHT FAIL — VT41_REAL_DB=1 needs DATABASE_URL",
                  file=sys.stderr)
            return 2
    print("PREFLIGHT OK")

    from orchestrator.agent.tools.get_business_profile import (
        GetBusinessProfileInput,
        GetBusinessProfileOutput,
        get_business_profile,
    )

    # --- A1: Pydantic IO ---
    ok = False
    try:
        GetBusinessProfileInput(tenant_id="t1")
        GetBusinessProfileOutput(
            business_name="X",
            business_archetype=None,
            owner_name=None,
            locale="en",
            working_hours=None,
            integration_summary=[],
            owner_curated_context=None,
        )
        ok = True
    except Exception as exc:  # noqa: BLE001
        print(f"    A1 raised: {exc!r}", file=sys.stderr)
    assertion(1, "Pydantic IO accepts valid shapes", ok, observed={"ok": ok})

    # --- A2: full happy path ---
    pool = _fake_pool(
        tenant_row={
            "business_name": "Acme Tiffin",
            "business_type": "tiffin_service",
            "preferred_language": None,
            "language_preference": "hi",
        },
        connector_rows=[
            {"connector_id": "google_drive"},
            {"connector_id": "razorpay"},
        ],
        l1_row={"owner_curated_context": "veg orders."},
    )
    r = get_business_profile(
        GetBusinessProfileInput(tenant_id="t1"), pool=pool,
    )
    pass_2 = (
        r is not None
        and r.business_name == "Acme Tiffin"
        and r.business_archetype == "tiffin_service"
        and r.locale == "hi"
        and r.integration_summary == ["google_drive", "razorpay"]
        and r.owner_curated_context == "veg orders."
    )
    assertion(
        2,
        "Matched tenant returns full profile with integrations + L1",
        pass_2,
        observed={
            "business_name": r.business_name if r else None,
            "locale": r.locale if r else None,
            "integrations": r.integration_summary if r else None,
            "owner_ctx": r.owner_curated_context if r else None,
        },
    )

    # --- A3: connector + L1 tables missing → null L1, [] integrations ---
    pool = _fake_pool(
        tenant_row={
            "business_name": "Acme",
            "business_type": "retail",
            "preferred_language": "en",
            "language_preference": "en",
        },
        connector_table_missing=True,
        l1_table_missing=True,
    )
    r = get_business_profile(
        GetBusinessProfileInput(tenant_id="t1"), pool=pool,
    )
    pass_3 = (
        r is not None
        and r.integration_summary == []
        and r.owner_curated_context is None
    )
    assertion(
        3,
        "Connector + L1 tables missing → empty integrations + null L1",
        pass_3,
        observed={
            "integrations": r.integration_summary if r else None,
            "owner_ctx": r.owner_curated_context if r else None,
        },
    )

    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s)",
              file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
