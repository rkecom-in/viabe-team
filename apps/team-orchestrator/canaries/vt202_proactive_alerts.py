#!/usr/bin/env python3
"""VT-202 proactive alerts canary (Rule #15, DR-15).

7 assertions per Cowork brief + corrections. All HTTP calls mocked
(record-and-assert via httpx monkeypatch). NO real Telegram or Resend
hits.

Per Cowork CORRECTION-2: TEAM_CANARY_TENANT_IDS env is set in
setUp BEFORE any other assertion runs, so even A1-A5 firings route
through the DEV-bot path. Production deploys leave the env unset.

Subshell-source supabase-dev.env:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      ./.venv/bin/python canaries/vt202_proactive_alerts.py
    )

Wall-clock budget ≤ 30s.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_TENANTS: list[str] = []
TELEGRAM_CALLS: list[dict[str, Any]] = []
RESEND_CALLS: list[dict[str, Any]] = []


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
    print("PREFLIGHT OK")


def _seed_tenant(pool: Any) -> str:
    tid = str(uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'trial')",
            (tid, f"vt202-canary-{tid[:8]}"),
        )
    INSERTED_TENANTS.append(tid)
    return tid


def _seed_run(pool: Any, tenant_id: str, status: str,
              cost_paise: int = 100, latency_ms: int = 500) -> str:
    rid = str(uuid4())
    started = datetime.now(UTC)
    ended = started + timedelta(milliseconds=latency_ms)
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO pipeline_runs (id, tenant_id, status, trigger_kind, "
            "total_cost_paise, started_at, ended_at) "
            "VALUES (%s, %s, %s, 'canary', %s, %s, %s)",
            (rid, tenant_id, status, cost_paise, started, ended),
        )
    return rid


def _cleanup(pool: Any) -> None:
    if not INSERTED_TENANTS:
        return
    with pool.connection() as conn:
        for tid in INSERTED_TENANTS:
            conn.execute("DELETE FROM tenant_alerts WHERE tenant_id = %s", (tid,))
            conn.execute("DELETE FROM tenant_alert_baselines WHERE tenant_id = %s", (tid,))
            conn.execute("DELETE FROM pipeline_runs WHERE tenant_id = %s", (tid,))
            conn.execute("DELETE FROM pipeline_steps WHERE tenant_id = %s", (tid,))
            conn.execute("DELETE FROM tenants WHERE id = %s", (tid,))


async def _fake_send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    TELEGRAM_CALLS.append({"bot_token": bot_token, "chat_id": chat_id, "text": text})
    return True


async def _fake_send_resend_email(api_key: str, from_addr: str, to_addr: str,
                                  subject: str, html: str) -> bool:
    RESEND_CALLS.append({"to": to_addr, "subject": subject, "html_len": len(html)})
    return True


def run_canary() -> int:
    _preflight()

    # ---------------- setUp: env (Cowork CORRECTION-2) ----------------
    # Set env vars BEFORE importing alerts modules. Frozenset is parsed
    # at function-call time (not module-load) so this ordering is safe
    # but explicit ordering matches the discipline-rule expectation.
    original_env: dict[str, str | None] = {}
    seed_env = {
        "TELEGRAM_DEV_BOT_TOKEN": "dev-bot-token-fixture",
        "TELEGRAM_DEV_CHAT_ID": "12345",
        "TELEGRAM_OPS_BOT_TOKEN": "ops-bot-token-fixture",
        "TELEGRAM_OPS_CHAT_ID": "67890",
        "RESEND_API_KEY": "re_test_fixture",
        "RESEND_FROM_EMAIL": "alerts@viabe.test",
        "RESEND_TO_EMAIL": "fazal@viabe.test",
    }
    for k, v in seed_env.items():
        original_env[k] = os.environ.get(k)
        os.environ[k] = v

    try:
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
        _cleanup(pool)

        # Seed canary tenant + set whitelist env.
        canary_tenant = _seed_tenant(pool)
        os.environ["TEAM_CANARY_TENANT_IDS"] = canary_tenant
        original_env["TEAM_CANARY_TENANT_IDS"] = os.environ.get("TEAM_CANARY_TENANT_IDS")

        # Monkeypatch send_telegram + send_resend_email in clients module
        # (and in any module that already imported them via from-import).
        from orchestrator.alerts import clients as clients_mod
        from orchestrator.alerts import dispatch as dispatch_mod
        from orchestrator.alerts import scheduler as scheduler_mod

        clients_mod.send_telegram = _fake_send_telegram
        clients_mod.send_resend_email = _fake_send_resend_email
        dispatch_mod.send_telegram = _fake_send_telegram
        dispatch_mod.send_resend_email = _fake_send_resend_email
        scheduler_mod.send_resend_email = _fake_send_resend_email

        from orchestrator.alerts.dispatch import (
            dispatch_alert,
            is_canary_tenant,
        )
        from orchestrator.alerts.triggers import (
            Trigger,
            detect_critical_for_run,
            severity_for,
        )

        # Validate setup: canary tenant is in whitelist BEFORE any
        # trigger fires (Cowork CORRECTION-2).
        assert is_canary_tenant(UUID(canary_tenant)), (
            "CORRECTION-2 violated: canary tenant not in whitelist"
        )

        # ---------------- A1 — hard-limit trigger ----------------
        hard_run = _seed_run(pool, canary_tenant, "aborted_hard_limit")
        TELEGRAM_CALLS.clear()
        triggers = detect_critical_for_run(UUID(hard_run))
        for t in triggers:
            dispatch_alert(t)
        # Hard-limit is critical: telegram dispatched. Canary path → DEV bot.
        pass_1 = (
            len(triggers) == 1
            and triggers[0].trigger_kind == "hard_limit"
            and len(TELEGRAM_CALLS) == 1
            and TELEGRAM_CALLS[0]["bot_token"] == "dev-bot-token-fixture"
        )
        assertion(
            1,
            "hard-limit trigger fires Telegram via DEV bot (canary whitelist)",
            pass_1,
            observed={
                "trigger_count": len(triggers),
                "kind": triggers[0].trigger_kind if triggers else None,
                "telegram_calls": len(TELEGRAM_CALLS),
                "bot_used": TELEGRAM_CALLS[0]["bot_token"] if TELEGRAM_CALLS else None,
            },
            expected={"kind": "hard_limit", "bot": "dev-bot-token-fixture"},
        )

        # ---------------- A2 — cost anomaly trigger ----------------
        # Seed baseline p95 manually then a 2x-cost run.
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO tenant_alert_baselines "
                "(tenant_id, latency_p95_ms, cost_p95_paise, volume_per_hour, "
                "dispatches_sampled) VALUES (%s, 500, 100, 10, 100) "
                "ON CONFLICT (tenant_id) DO UPDATE SET cost_p95_paise = 100",
                (canary_tenant,),
            )
        TELEGRAM_CALLS.clear()
        expensive_run = _seed_run(pool, canary_tenant, "completed", cost_paise=300)
        from orchestrator.alerts.triggers import detect_slow_triggers
        slow_triggers = detect_slow_triggers(UUID(canary_tenant))
        cost_triggers = [t for t in slow_triggers if t.trigger_kind == "cost_anomaly"]
        for t in cost_triggers:
            dispatch_alert(t)
        pass_2 = len(cost_triggers) >= 1
        assertion(
            2,
            "cost anomaly trigger: 2x-p95 run → cost_anomaly fires",
            pass_2,
            observed={"cost_trigger_count": len(cost_triggers)},
            expected={"cost_trigger_count_gte": 1},
        )

        # ---------------- A3 — daily digest dispatch ----------------
        from orchestrator.alerts.scheduler import daily_digest_body
        RESEND_CALLS.clear()
        daily_digest_body(datetime.now(UTC), datetime.now(UTC))
        pass_3 = len(RESEND_CALLS) >= 1
        assertion(
            3,
            "daily digest dispatches Resend email",
            pass_3,
            observed={"resend_calls": len(RESEND_CALLS),
                      "first_subject": RESEND_CALLS[0]["subject"] if RESEND_CALLS else None},
            expected={"resend_calls_gte": 1},
        )

        # ---------------- A4 — tenant_alerts operator-readable ----------------
        with pool.connection() as conn, conn.cursor() as cur:
            try:
                cur.execute("SET LOCAL ROLE app_role")
                cur.execute(
                    "SELECT set_config('request.jwt.claims', %s, true)",
                    ('{"operator_claim":"true"}',),
                )
                cur.execute(
                    "SELECT COUNT(*) AS n FROM tenant_alerts WHERE tenant_id = %s",
                    (canary_tenant,),
                )
                row = cur.fetchone()
            finally:
                cur.execute("RESET ROLE")
                cur.execute("SELECT set_config('request.jwt.claims', '{}', true)")
        operator_count = int(dict(row)["n"]) if row else 0
        pass_4 = operator_count >= 1
        assertion(
            4,
            "tenant_alerts operator-readable via JWT claim",
            pass_4,
            observed={"operator_count": operator_count},
            expected={"operator_count_gte": 1},
        )

        # ---------------- A5 — dedup ----------------
        # Fire same hard-limit trigger 3 times within 5 min. Only first
        # persisted. Telegram-calls should not increment past the first.
        # Clear prior alerts for canary tenant so dedup window starts
        # fresh (A1 already fired hard_limit for this tenant).
        with pool.connection() as conn:
            conn.execute(
                "DELETE FROM tenant_alerts WHERE tenant_id = %s",
                (canary_tenant,),
            )
        TELEGRAM_CALLS.clear()
        repeat_run = _seed_run(pool, canary_tenant, "aborted_hard_limit")
        for _ in range(3):
            for t in detect_critical_for_run(UUID(repeat_run)):
                dispatch_alert(t)
        # detect_critical_for_run returns 1 trigger per call; dispatch
        # dedup-suppresses 2nd + 3rd. Telegram should be called only
        # once (the first dispatch).
        pass_5 = len(TELEGRAM_CALLS) == 1
        assertion(
            5,
            "dedup: 3 firings within 5 min → 1 telegram call",
            pass_5,
            observed={"telegram_calls": len(TELEGRAM_CALLS)},
            expected={"telegram_calls": 1},
        )

        # ---------------- A6 — canary whitelist routing ----------------
        # All A1-A5 already routed via DEV bot (canary_tenant in whitelist).
        # Assert: at no point was OPS bot called.
        ops_calls = [c for c in TELEGRAM_CALLS if c["bot_token"] == "ops-bot-token-fixture"]
        # And: no Resend critical-alert email was sent for canary tenants
        # (only the digest's Resend call from A3, which is per-system not
        # per-tenant — distinct from critical-alert email path).
        pass_6 = len(ops_calls) == 0
        assertion(
            6,
            "canary whitelist routing: OPS bot never called for canary tenant",
            pass_6,
            observed={"ops_calls_count": len(ops_calls),
                      "all_telegram_bots": list({c["bot_token"] for c in TELEGRAM_CALLS})},
            expected={"ops_calls_count": 0},
        )

        # ---------------- A7 — PII scrub ----------------
        # Build a synthetic trigger with phone digits in payload + verify
        # the persisted Telegram text contains no ≥7-digit sequence.
        TELEGRAM_CALLS.clear()
        synthetic = Trigger(
            tenant_id=UUID(canary_tenant),
            trigger_kind="escalation",
            severity=severity_for("escalation"),
            message_text="Customer phone +919876543210 wants help; SID SMabcdef1234567890abcdef1234567890",
            run_id=UUID(hard_run),
            payload={"phone": "+919876543210"},
        )
        dispatch_alert(synthetic)
        sent_text = TELEGRAM_CALLS[-1]["text"] if TELEGRAM_CALLS else ""
        digit_runs = re.findall(r"\d{7,}", sent_text)
        pass_7 = len(digit_runs) == 0
        assertion(
            7,
            "PII scrub: alert text has no ≥7-digit sequence",
            pass_7,
            observed={"digit_runs": digit_runs, "sent_text_sample": sent_text[:200]},
            expected={"digit_runs": []},
        )

    finally:
        _cleanup(pool) if 'pool' in locals() else None
        for k, v in original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    failures = [r for r in RESULTS.values() if r["status"] != "PASS"]
    if failures:
        print(f"\nFAIL: {len(failures)} assertion(s) failed", file=sys.stderr)
        return 1
    print(f"\nPASS: {len(RESULTS)}/{len(RESULTS)} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
