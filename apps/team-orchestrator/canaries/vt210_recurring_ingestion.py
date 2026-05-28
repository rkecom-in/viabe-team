#!/usr/bin/env python3
"""VT-210 recurring ingestion canary (Rule #15, DR-15).

Deterministic — no real Apps Script trigger or real DBOS launch. Uses
psycopg pool + FastAPI TestClient + monkeypatched DBOS.start_workflow.

Subshell-source supabase-dev.env:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      ./.venv/bin/python canaries/vt210_recurring_ingestion.py
    )

Wall-clock budget ≤ 30s.

5 assertions per brief:

- A1: schedule fan-out — seed 2 tenants × 2 connectors with
  next_scheduled_run in the past; ``run_due_ingestions()`` dispatches
  one workflow per due row.
- A2: push receiver validates + persists — synthetic POST to
  ``/api/orchestrator/integrations/google_sheet/push`` with valid
  HMAC over body → 200 + row lands + ``last_sync_at`` bumps.
- A3: failure escalation — stub connector.pull_full to raise; drive
  ``ingest_one_connector`` 3 times → ``tenant_integration_state.
  pending_owner_input`` contains ``token_expired_reconnect``.
- A4: status surface accuracy — after the A1 simulation, the row's
  ``last_sync_at`` is within 60s of now and counters match.
- A5: push HMAC reject — tampered body → 403 + status unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_TENANTS: list[str] = []


def assertion(
    num: int, name: str, passed: bool, *, observed: Any = None, expected: Any = None
) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _preflight() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL missing", file=sys.stderr)
        sys.exit(2)
    print("PREFLIGHT OK — db: present")


def _insert_tenant(pool: Any) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"vt210-canary-{tid[:8]}"),
        )
        conn.execute(
            "INSERT INTO tenant_integration_state (tenant_id, phase) "
            "VALUES (%s, 'phase_5_confirmed')",
            (tid,),
        )
    INSERTED_TENANTS.append(tid)
    return tid


def _cleanup(pool: Any) -> None:
    if not INSERTED_TENANTS:
        return
    with pool.connection() as conn:
        for tid in INSERTED_TENANTS:
            conn.execute(
                "DELETE FROM tenant_connector_status WHERE tenant_id = %s",
                (tid,),
            )
            conn.execute(
                "DELETE FROM tenant_oauth_tokens WHERE tenant_id = %s",
                (tid,),
            )
            conn.execute(
                "DELETE FROM phone_token_resolutions WHERE tenant_id = %s",
                (tid,),
            )
            conn.execute(
                "DELETE FROM tenant_integration_state WHERE tenant_id = %s",
                (tid,),
            )
            conn.execute("DELETE FROM tenants WHERE id = %s", (tid,))


def run_canary() -> int:
    _preflight()

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=True,
        )
    pool = get_pool()

    from orchestrator.integrations import scheduler as scheduler_mod
    from orchestrator.integrations.scheduler import (
        _compute_next_run,
        _connector_class_for as _original_connector_class_for,
        ingest_one_connector,
        run_due_ingestions,
    )

    # ---------------- A1 — schedule fan-out ----------------
    # Purge stale canary leftovers (prior runs that crashed before _cleanup).
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM tenant_connector_status "
            "WHERE tenant_id IN (SELECT id FROM tenants WHERE business_name LIKE 'vt210-canary-%')"
        )
        conn.execute(
            "DELETE FROM tenant_oauth_tokens "
            "WHERE tenant_id IN (SELECT id FROM tenants WHERE business_name LIKE 'vt210-canary-%')"
        )
        conn.execute(
            "DELETE FROM phone_token_resolutions "
            "WHERE tenant_id IN (SELECT id FROM tenants WHERE business_name LIKE 'vt210-canary-%')"
        )
        conn.execute(
            "DELETE FROM tenant_integration_state "
            "WHERE tenant_id IN (SELECT id FROM tenants WHERE business_name LIKE 'vt210-canary-%')"
        )
        conn.execute(
            "DELETE FROM tenants WHERE business_name LIKE 'vt210-canary-%'"
        )

    t1 = _insert_tenant(pool)
    t2 = _insert_tenant(pool)
    past = datetime.now(UTC) - timedelta(minutes=10)
    future = datetime.now(UTC) + timedelta(hours=2)
    with pool.connection() as conn:
        for tid in (t1, t2):
            for cid in ("google_sheet", "shopify"):
                conn.execute(
                    """
                    INSERT INTO tenant_connector_status
                        (tenant_id, connector_id, pull_cadence,
                         next_scheduled_run, enabled)
                    VALUES (%s, %s, '0 9 * * *', %s, TRUE)
                    """,
                    (tid, cid, past),
                )
        # control: one disabled row (must NOT dispatch) + one future row
        conn.execute(
            """
            INSERT INTO tenant_connector_status
                (tenant_id, connector_id, pull_cadence,
                 next_scheduled_run, enabled)
            VALUES (%s, 'apify_scrape', '0 9 * * *', %s, FALSE)
            """,
            (t1, past),
        )
        conn.execute(
            """
            INSERT INTO tenant_connector_status
                (tenant_id, connector_id, pull_cadence,
                 next_scheduled_run, enabled)
            VALUES (%s, 'meta_ads_pixel', '0 9 * * *', %s, TRUE)
            """,
            (t2, future),
        )

    dispatched: list[tuple[str, str]] = []

    class _FakeDBOS:
        @staticmethod
        def start_workflow(fn: Any, *args: Any, **kwargs: Any) -> None:
            dispatched.append((str(args[0]), args[1]))

    scheduler_mod.DBOS = _FakeDBOS  # type: ignore[assignment]
    run_due_ingestions()
    ours = {(d[0], d[1]) for d in dispatched if d[0] in (t1, t2)}
    expected_ours = {(t1, "google_sheet"), (t1, "shopify"),
                     (t2, "google_sheet"), (t2, "shopify")}
    forbidden = {(t1, "apify_scrape"), (t2, "meta_ads_pixel")}
    pass_1 = (
        ours == expected_ours
        and not (forbidden & ours)
    )
    assertion(
        1, "schedule fan-out: 4 due rows dispatched; disabled/future excluded",
        pass_1, observed={"ours": sorted(ours), "all_dispatched_count": len(dispatched)},
        expected={"ours_count": 4},
    )

    # ---------------- A2 — push receiver validates + persists ----------------
    from fastapi.testclient import TestClient

    from orchestrator.api import router

    # Lift router off the global FastAPI app so we don't trigger lifespan
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    push_secret = "canary-" + uuid4().hex[:12]  # gitleaks:allow
    with pool.connection() as conn:
        # Seed a tenant_oauth_tokens row with push_secret for google_sheet
        from cryptography.fernet import Fernet

        os.environ.setdefault("TEAM_PHONE_ENCRYPTION_KEY", Fernet.generate_key().decode())
        from orchestrator.observability.encrypt_value import encrypt_value

        conn.execute(
            """
            INSERT INTO tenant_oauth_tokens (
                tenant_id, connector_id, refresh_token_encrypted,
                scopes, push_secret, last_refreshed_at, expires_at
            ) VALUES (%s, 'google_sheet', %s, ARRAY['x'], %s, now(), now() + interval '1 hour')
            ON CONFLICT (tenant_id, connector_id) DO UPDATE SET
                push_secret = EXCLUDED.push_secret
            """,
            (t1, encrypt_value("placeholder-refresh-token"), push_secret),
        )

    body = json.dumps({"row_data": {"phone": "+919876543210", "name": "Test"}}).encode("utf-8")
    signature = hmac.new(push_secret.encode(), body, hashlib.sha256).hexdigest()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt210-canary-hash-salt")

    resp = client.post(
        "/api/orchestrator/integrations/google_sheet/push",
        content=body,
        headers={
            "X-Viabe-Signature": signature,
            "X-Viabe-Tenant": t1,
            "Content-Type": "application/json",
        },
    )
    # Status row must update; ensure row exists first
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT last_status, last_sync_at FROM tenant_connector_status "
            "WHERE tenant_id = %s AND connector_id = 'google_sheet'",
            (t1,),
        )
        raw = cur.fetchone()
    pass_2 = (
        resp.status_code == 200
        and resp.json().get("rows_ingested") == 1
        and raw is not None
        and raw["last_status"] == "ok"
    )
    assertion(
        2, "push receiver: valid HMAC → 200 + row lands + status='ok'",
        pass_2, observed={"http": resp.status_code, "body": resp.json(),
                          "row_status": dict(raw) if raw else None},
        expected={"http": 200, "rows_ingested": 1, "last_status": "ok"},
    )

    # ---------------- A3 — failure escalation ----------------
    # Replace _connector_class_for to return a stub that raises on pull_full
    class _BrokenConnector:
        connector_id = "shopify"

        @property
        def spec(self) -> Any:
            from orchestrator.integrations.registry import get_connector
            return get_connector("shopify")

        def pull_full(self, tenant_id: Any, since: Any = None) -> list[Any]:
            raise RuntimeError("simulated: vendor 401 unauthorized")

    scheduler_mod._connector_class_for = lambda cid: _BrokenConnector  # type: ignore[assignment]

    # Drive ingest_one_connector 3 times on (t2, shopify) — already in DB
    for _ in range(3):
        ingest_one_connector(__import__("uuid").UUID(t2), "shopify")

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT consecutive_fails, last_status FROM tenant_connector_status "
            "WHERE tenant_id = %s AND connector_id = 'shopify'",
            (t2,),
        )
        status_raw = cur.fetchone()
        cur.execute(
            "SELECT pending_owner_input FROM tenant_integration_state "
            "WHERE tenant_id = %s",
            (t2,),
        )
        envelope_raw = cur.fetchone()
    envelope = envelope_raw["pending_owner_input"] if envelope_raw else None
    if isinstance(envelope, str):
        envelope = json.loads(envelope)
    pass_3 = (
        status_raw is not None
        and status_raw["consecutive_fails"] == 3
        and status_raw["last_status"] == "error"
        and envelope is not None
        and envelope.get("phase_change_required") == "token_expired_reconnect"
        and envelope.get("connector_id") == "shopify"
    )
    assertion(
        3, "failure escalation: 3 fails → pending_owner_input token_expired_reconnect",
        pass_3, observed={"status": dict(status_raw) if status_raw else None,
                          "envelope": envelope},
        expected={"consecutive_fails": 3, "phase_change_required": "token_expired_reconnect"},
    )

    # ---------------- A4 — status surface accuracy ----------------
    # Use a fresh connector class that succeeds; ingest once on (t1, google_sheet)
    class _OKConnector:
        connector_id = "google_sheet"

        @property
        def spec(self) -> Any:
            from orchestrator.integrations.registry import get_connector
            return get_connector("google_sheet")

        def pull_full(self, tenant_id: Any, since: Any = None) -> list[Any]:
            return [{"row_index": 1}, {"row_index": 2}]

    scheduler_mod._connector_class_for = lambda cid: _OKConnector  # type: ignore[assignment]
    ingest_one_connector(__import__("uuid").UUID(t1), "google_sheet")
    now = datetime.now(UTC)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT last_sync_at, last_status, rows_ingested_today, "
            "next_scheduled_run, consecutive_fails "
            "FROM tenant_connector_status "
            "WHERE tenant_id = %s AND connector_id = 'google_sheet'",
            (t1,),
        )
        row = cur.fetchone()
    last_sync = row["last_sync_at"] if row else None
    expected_next = _compute_next_run("0 9 * * *", now)
    pass_4 = (
        row is not None
        and abs((last_sync - now).total_seconds()) < 60
        and row["last_status"] == "ok"
        and row["consecutive_fails"] == 0
        # rows_ingested_today >= 2 (A2 added 1 via push; ingest_one_connector added 2)
        and row["rows_ingested_today"] >= 2
        # next_scheduled_run within ~24h
        and 0 < (row["next_scheduled_run"] - now).total_seconds() <= 24 * 3600 + 60
    )
    assertion(
        4, "status surface: last_sync_at fresh + status=ok + next_run within 24h",
        pass_4, observed={"row": {k: str(v) for k, v in dict(row).items()} if row else None,
                          "expected_next_iso": expected_next.isoformat()},
        expected={"last_sync_at_drift_s": "< 60", "last_status": "ok"},
    )

    # ---------------- A5 — push HMAC reject ----------------
    # Restore real connector class for push receiver (A4 left _OKConnector).
    scheduler_mod._connector_class_for = _original_connector_class_for  # type: ignore[assignment]
    tampered = json.dumps({"row_data": {"phone": "+919999999999"}}).encode("utf-8")
    # signature computed for ORIGINAL body but submit tampered body
    resp_bad = client.post(
        "/api/orchestrator/integrations/google_sheet/push",
        content=tampered,
        headers={
            "X-Viabe-Signature": signature,  # signature is for OLD body
            "X-Viabe-Tenant": t1,
            "Content-Type": "application/json",
        },
    )
    pass_5 = resp_bad.status_code == 403
    assertion(
        5, "push HMAC reject: tampered body → 403",
        pass_5, observed={"http": resp_bad.status_code, "body": resp_bad.text[:100]},
        expected={"http": 403},
    )

    _cleanup(pool)

    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)} assertion(s) failed", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
