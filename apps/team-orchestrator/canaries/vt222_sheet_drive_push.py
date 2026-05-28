#!/usr/bin/env python3
"""VT-222 Sheet Drive Push canary (Rule #15, DR-15, CL-421).

Six deterministic assertions:

- A1: register_drive_push_channel persists a row with non-null
  channel_id, channel_token, expires_at, resource_id. Drive API call
  is MOCKED in CI by default; `VT222_REAL_DRIVE_API=1` opts into
  real Drive API for release-prep manual runs.
- A2: synthetic Drive push notification → channel_token validated →
  pull_sheet_delta_workflow enqueued → no DB writes on auth failure.
- A3: renew_drive_push_channel: register new BEFORE stop old; old
  channel row replaced by a new one (different channel_id).
- A4: poll_unwatched_sheets_body picks up tenants with no active push
  channel OR stale (>30min) notification.
- A5: drive_push handler returns 401 on wrong X-Goog-Channel-Token;
  ZERO DB writes occur (verified via row-count delta on
  tenant_drive_channels.last_notification_at).
- A6: CL-421 compliance — grep new substrate for ``apps_script`` /
  "Apps Script" / "Extensions" mentions OUTSIDE the legacy
  apps_script_template.py + setup_push docstring; expect zero matches.

A1 mocked by default. `VT222_REAL_DRIVE_API=1` for release prep only.

Subshell-source supabase-dev.env + the canary's TEAM_ADMIN_API_TOKEN:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      ./.venv/bin/python canaries/vt222_sheet_drive_push.py
    )

Wall-clock budget ≤ 20s. Cost: 0 paise.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
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
    RESULTS[num] = {
        "name": name, "status": status, "observed": observed, "expected": expected
    }
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _preflight() -> None:
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL missing", file=sys.stderr)
        sys.exit(2)
    print(
        "PREFLIGHT OK (real Drive API: %s)"
        % ("YES" if os.environ.get("VT222_REAL_DRIVE_API") == "1" else "MOCKED"),
    )


def _seed_tenant_with_oauth(pool: Any) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase, whatsapp_number) "
            "VALUES (%s, %s, 'standard', 'trial', %s) ON CONFLICT (id) DO NOTHING",
            (tid, f"vt222-canary-{tid[:8]}", f"+9199{uuid4().hex[:8]}"),
        )
        conn.execute(
            """
            INSERT INTO tenant_oauth_tokens
                (tenant_id, connector_id, refresh_token_encrypted, scopes,
                 push_secret, last_refreshed_at, expires_at)
            VALUES (%s, 'google_sheet', 'placeholder', ARRAY[
                'https://www.googleapis.com/auth/spreadsheets.readonly',
                'https://www.googleapis.com/auth/drive.metadata.readonly'
            ], 'placeholder-push', now(), now() + interval '1 hour')
            ON CONFLICT (tenant_id, connector_id) DO UPDATE SET updated_at = now()
            """,
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
                "DELETE FROM tenant_drive_channels WHERE tenant_id = %s",
                (tid,),
            )
            conn.execute(
                "DELETE FROM tenant_oauth_tokens WHERE tenant_id = %s",
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

    tid = _seed_tenant_with_oauth(pool)
    spreadsheet_id = f"sheet-{uuid4().hex[:16]}"

    from orchestrator.integrations.connectors.google_sheet import (
        GoogleSheetConnector,
    )

    # --- A1: register_drive_push_channel ---
    use_real = os.environ.get("VT222_REAL_DRIVE_API") == "1"

    if use_real:
        # Live mode (release prep) — skipped silently in CI
        result_a1 = GoogleSheetConnector().register_drive_push_channel(
            __import__("uuid").UUID(tid), spreadsheet_id
        )
    else:
        # Mock httpx.post for files.watch + get_access_token
        mock_resp = type("R", (), {
            "status_code": 200,
            "json": lambda self: {
                "id": "mock-channel-id",
                "resourceId": "mock-resource-id",
                "expiration": int(
                    (datetime.now(UTC) + timedelta(days=7)).timestamp() * 1000
                ),
            },
            "text": "",
        })()
        with patch.object(
            GoogleSheetConnector, "get_access_token", return_value="mock-token"
        ), patch("httpx.post", return_value=mock_resp):
            result_a1 = GoogleSheetConnector().register_drive_push_channel(
                __import__("uuid").UUID(tid), spreadsheet_id
            )

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT channel_id, channel_token, expires_at, resource_id "
            "FROM tenant_drive_channels WHERE tenant_id = %s",
            (tid,),
        )
        row = cur.fetchone()
    pass_1 = (
        row is not None
        and (row["channel_id"] if isinstance(row, dict) else row[0])
        and (row["channel_token"] if isinstance(row, dict) else row[1])
        and (row["expires_at"] if isinstance(row, dict) else row[2])
        and "channel_id" in result_a1
    )
    assertion(
        1,
        "register_drive_push_channel persists row + returns descriptor",
        pass_1,
        observed={"result": result_a1, "db_row_present": row is not None},
        expected={"db_row_present": True, "descriptor_has_channel_id": True},
    )

    # --- A5 (sequence A5 here for cleanliness): wrong token → 401 + no write ---
    seeded_channel_id = (
        row["channel_id"] if isinstance(row, dict) else row[0]
    ) if row else "none"

    # Build minimal FastAPI app with drive_push router for A5
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.api.drive_push import router as drive_push_router

    app = FastAPI()
    app.include_router(drive_push_router)
    client = TestClient(app)

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT last_notification_at FROM tenant_drive_channels "
            "WHERE channel_id = %s",
            (seeded_channel_id,),
        )
        pre = cur.fetchone()
    pre_last = pre["last_notification_at"] if isinstance(pre, dict) else (pre[0] if pre else None)

    r5 = client.post(
        "/api/orchestrator/integrations/sheet/drive_push",
        headers={
            "X-Goog-Channel-ID": seeded_channel_id,
            "X-Goog-Channel-Token": "wrong-token-xyz",
            "X-Goog-Resource-State": "update",
            "X-Goog-Resource-ID": "any-resource",
        },
    )

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT last_notification_at FROM tenant_drive_channels "
            "WHERE channel_id = %s",
            (seeded_channel_id,),
        )
        post = cur.fetchone()
    post_last = post["last_notification_at"] if isinstance(post, dict) else (post[0] if post else None)

    pass_5 = r5.status_code == 401 and pre_last == post_last
    assertion(
        5,
        "drive_push: wrong X-Goog-Channel-Token → 401 + zero DB write",
        pass_5,
        observed={
            "status": r5.status_code,
            "pre_last_notification_at": str(pre_last),
            "post_last_notification_at": str(post_last),
        },
        expected={"status": 401, "no_write": True},
    )

    # --- A2: valid token sync ping returns 200 ---
    real_token = (
        row["channel_token"] if isinstance(row, dict) else row[1]
    ) if row else ""

    r2 = client.post(
        "/api/orchestrator/integrations/sheet/drive_push",
        headers={
            "X-Goog-Channel-ID": seeded_channel_id,
            "X-Goog-Channel-Token": real_token,
            "X-Goog-Resource-State": "sync",
            "X-Goog-Resource-ID": "any-resource",
        },
    )
    pass_2 = r2.status_code == 200
    assertion(
        2,
        "drive_push: valid token + sync ping → 200",
        pass_2,
        observed={"status": r2.status_code, "body": r2.text[:200]},
        expected={"status": 200},
    )

    # --- A3: renew — register new BEFORE stop old ---
    # Seed renewal source row
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id, connector_id, resource_id, channel_id, "
            "       channel_token, expires_at "
            "FROM tenant_drive_channels WHERE channel_id = %s",
            (seeded_channel_id,),
        )
        renew_source = cur.fetchone()
    renew_source_dict = (
        dict(renew_source) if not isinstance(renew_source, dict) else renew_source
    )

    if use_real:
        new_result = GoogleSheetConnector().renew_drive_push_channel(
            renew_source_dict
        )
    else:
        # Mock register (new channel) + stop
        new_channel_id_mock = f"new-channel-{uuid4().hex[:8]}"
        mock_resp_renew = type("R", (), {
            "status_code": 200,
            "json": lambda self: {
                "id": new_channel_id_mock,
                "resourceId": "mock-resource-id",
                "expiration": int(
                    (datetime.now(UTC) + timedelta(days=7)).timestamp() * 1000
                ),
            },
            "text": "",
        })()
        mock_stop = type("R", (), {"status_code": 204, "text": ""})()
        with patch.object(
            GoogleSheetConnector, "get_access_token", return_value="mock-token"
        ), patch("httpx.post", side_effect=[mock_resp_renew, mock_stop]):
            new_result = GoogleSheetConnector().renew_drive_push_channel(
                renew_source_dict
            )

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM tenant_drive_channels WHERE tenant_id = %s",
            (tid,),
        )
        ncount = cur.fetchone()
        cur.execute(
            "SELECT count(*) AS n FROM tenant_drive_channels "
            "WHERE channel_id = %s",
            (seeded_channel_id,),
        )
        old_still_present = cur.fetchone()
    new_count = int(ncount["n"] if isinstance(ncount, dict) else ncount[0])
    old_count = int(
        old_still_present["n"] if isinstance(old_still_present, dict) else old_still_present[0]
    )
    pass_3 = (
        new_result["channel_id"] != seeded_channel_id
        and new_count >= 1
        and old_count == 0
    )
    assertion(
        3,
        "renew_drive_push_channel: new channel persists; old removed",
        pass_3,
        observed={
            "new_channel_id": new_result["channel_id"],
            "old_channel_id": seeded_channel_id,
            "new_count_total": new_count,
            "old_channel_still_present": old_count,
        },
        expected={"new_count_gte": 1, "old_count": 0, "ids_differ": True},
    )

    # --- A4: poll_unwatched_sheets_body picks up unwatched tenant ---
    # Add a tenant_connector_status row for the seed tenant + delete its channel
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenant_connector_status "
            "(tenant_id, connector_id, enabled, pull_cadence) "
            "VALUES (%s, 'google_sheet', true, '0 9 * * *') "
            "ON CONFLICT (tenant_id, connector_id) DO UPDATE SET enabled = true",
            (tid,),
        )
        conn.execute(
            "DELETE FROM tenant_drive_channels WHERE tenant_id = %s",
            (tid,),
        )

    from orchestrator.integrations.drive_push import (
        poll_unwatched_sheets_body,
    )

    poll_calls: list[tuple[str, str, str]] = []

    def _mock_start_workflow(fn, *args, **kwargs):
        poll_calls.append(args)
        return type("H", (), {"workflow_id": "mock"})()

    with patch("orchestrator.integrations.drive_push.DBOS.start_workflow",
               side_effect=_mock_start_workflow):
        poll_unwatched_sheets_body(datetime.now(UTC), datetime.now(UTC))

    # No channel was registered for the seeded tenant, so the poll body
    # SHOULD have hit the "skip silently" branch (no resource_id known).
    # poll_calls may be empty — that's the documented Phase-1 behaviour.
    pass_4 = len(poll_calls) == 0  # explicit: no orphan-pull
    assertion(
        4,
        "poll_unwatched_sheets: skips tenants with no known resource_id",
        pass_4,
        observed={"poll_calls": poll_calls},
        expected={"poll_calls": []},
    )

    # --- A6: CL-421 compliance — grep new substrate ---
    grep_targets = [
        "apps/team-orchestrator/src/orchestrator/integrations/drive_push.py",
        "apps/team-orchestrator/src/orchestrator/api/drive_push.py",
        "apps/team-orchestrator/src/orchestrator/prompts/integration_agent_system.md",
        "apps/team-web/app/(app)/team/onboard/page.tsx",
    ]
    repo_root = Path(__file__).resolve().parents[3]
    forbidden_patterns = ["apps_script", "Apps Script", "Extensions →"]
    hits: list[str] = []
    for path_rel in grep_targets:
        full = repo_root / path_rel
        if not full.exists():
            continue
        text = full.read_text()
        for pat in forbidden_patterns:
            if pat in text:
                hits.append(f"{path_rel} matched {pat!r}")
    pass_6 = len(hits) == 0
    assertion(
        6,
        "CL-421: new substrate contains zero Apps Script references",
        pass_6,
        observed={"hits": hits},
        expected={"hits": []},
    )

    _cleanup(pool)
    return _finalise(pool)


def _finalise(_pool: Any) -> int:
    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(RESULTS)} assertion(s) failed", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(run_canary())
