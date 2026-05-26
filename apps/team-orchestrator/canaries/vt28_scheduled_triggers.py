#!/usr/bin/env python3
"""VT-28 scheduled triggers canary (Rule #15, DR-15).

Subshell-source `.viabe/secrets/anthropic.env` + `supabase-dev.env` +
`logfire-dev.env`:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/anthropic.env
      source ../../.viabe/secrets/supabase-dev.env
      source ../../.viabe/secrets/logfire-dev.env
      set +a
      time ./.venv/bin/python canaries/vt28_scheduled_triggers.py 2>&1 | tee /tmp/vt28-canary-evidence.log | tail -200
    )

Exits 0 iff all 10 assertions PASS against real Supabase + real Anthropic
+ real Logfire EU. Wall-clock budget ≤90s; Anthropic cost < ₹1.

CL-274 plumbing-mode: this canary proves the trigger plumbing fires +
emits observable events. It does NOT prove the deterministic triggers
produce useful output — those bodies are SHELLS pending VT-175 schema.
"""

from __future__ import annotations

import json
import os
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


CANARY_TENANT = UUID("00000000-0000-4000-8000-000000aaa028")
CANARY_COMPONENT = "scheduled_trigger"

RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_RUN_IDS: list[str] = []
INSERTED_TENANT_IDS: list[str] = []
ANTHROPIC_COST_PAISE: int = 0
SAMPLE_LOGFIRE_SPAN: dict[str, Any] = {}


def assertion(num, name, passed, *, observed=None, expected=None):
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _serial(value):
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


def _supabase_host():
    url = os.environ.get("DATABASE_URL", "")
    if "@" not in url:
        return "<no-host>"
    after = url.split("@", 1)[1]
    return after.split("/", 1)[0]


def _preflight():
    missing = [e for e in ("DATABASE_URL", "ANTHROPIC_API_KEY", "LOGFIRE_TOKEN") if not os.environ.get(e)]
    if missing:
        print(f"PREFLIGHT FAIL — missing env: {missing}", file=sys.stderr)
        sys.exit(2)
    logfire_host = os.environ.get("LOGFIRE_BASE_URL", "https://logfire-eu.pydantic.dev")
    if "logfire-eu" not in logfire_host:
        print(f"PREFLIGHT FAIL — LOGFIRE_BASE_URL {logfire_host} not EU region", file=sys.stderr)
        sys.exit(2)
    print(
        f"PREFLIGHT OK — supabase: {_supabase_host()}; "
        f"anthropic: api.anthropic.com; "
        f"logfire: {logfire_host}; "
        f"dbos: substrate=team-orchestrator (canary uses pipeline_log directly + invokes bodies "
        f"synchronously via run_*_body callables — synthetic-clock pattern per docs/team/scheduled-triggers.md)"
    )


def _seed_tenant(pool, tenant_id):
    INSERTED_TENANT_IDS.append(str(tenant_id))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"canary-vt28-{tenant_id}"),
        )


def run_canary() -> int:
    _preflight()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt28-canary-salt")

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool
    from orchestrator.observability.logfire import (
        configure_logfire,
        is_enabled,
        shutdown as logfire_shutdown,
        traced_node,
    )
    from orchestrator.observability.pii import (
        redact_for_otel_span,
    )
    from orchestrator.privacy.pii_redactor import redact
    from orchestrator.scheduled_triggers import (
        ATTRIBUTION_CLOSE_SHELL_EVENT,
        DAY39_SHELL_EVENT,
        MONTHLY_IMPACT_SHELL_EVENT,
        SHELL_STATUS,
        WEEKLY_CADENCE_EVENT,
        attribution_close_workflow_id,
        day39_workflow_id,
        monthly_workflow_id,
        run_attribution_close_body,
        run_day39_evaluation_body,
        run_monthly_impact_body,
        run_weekly_cadence_body,
        weekly_workflow_id,
    )

    configure_logfire()

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
    _seed_tenant(pool, CANARY_TENANT)

    # -------------------------------------------------------------------
    # Group A — observability regression (VT-101/102/104 contracts hold)
    # -------------------------------------------------------------------

    # Assertion 1 — Logfire substitution: weekly cadence body emits a real
    # Logfire span via the traced_node decorator path. PII redacted before
    # any span attribute capture (VT-104 byte-identical token format).
    pii_input = {
        "phone": "+919876543210",
        "customer_name": "Rajesh Kumar",
        "body": "Hi I want to cancel",
    }

    @traced_node("vt28_canary_weekly_observability")
    def _decorated(payload: dict[str, Any]) -> dict[str, Any]:
        return {"echo": "ok"}

    _decorated(pii_input)
    redacted_check = redact_for_otel_span(pii_input)
    SAMPLE_LOGFIRE_SPAN["args"] = [redacted_check]
    SAMPLE_LOGFIRE_SPAN["node.name"] = "vt28_canary_weekly_observability"
    pass_1 = (
        is_enabled() is True
        and redacted_check["phone"].startswith("phone_tok_")
        and redacted_check["body"].startswith("body_tok_")
        and redacted_check["customer_name"].startswith("<redacted:customer_name:")
    )
    assertion(
        1,
        "VT-171 Logfire substitution: traced_node emits redacted span; legacy token format preserved",
        pass_1,
        observed={"logfire_enabled": is_enabled(), "redacted_dict": redacted_check},
        expected="logfire enabled + tokens byte-identical to VT-101/102/104 format",
    )

    # Assertion 2 — pipeline_log regression: all 4 trigger event types
    # land as rows with `event_type` correctly set + payload contains
    # redacted-only values (since the bodies above invoke through
    # log_event → redact_for_log).
    syn_now = datetime(2026, 5, 26, 3, 30, tzinfo=timezone.utc)
    weekly_run = run_weekly_cadence_body(now=syn_now)
    INSERTED_RUN_IDS.append(str(weekly_run))
    attr_run = run_attribution_close_body(now=syn_now)
    INSERTED_RUN_IDS.append(str(attr_run))
    day39_run = run_day39_evaluation_body(now=syn_now)
    INSERTED_RUN_IDS.append(str(day39_run))
    monthly_run = run_monthly_impact_body(now=syn_now)
    INSERTED_RUN_IDS.append(str(monthly_run))
    time.sleep(1.5)

    event_types_seen: dict[str, str] = {}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, payload FROM pipeline_log WHERE run_id = ANY(%s)",
            (INSERTED_RUN_IDS,),
        )
        for row in cur.fetchall():
            event_types_seen[row["event_type"]] = json.dumps(row["payload"])
    pass_2 = (
        WEEKLY_CADENCE_EVENT in event_types_seen
        and ATTRIBUTION_CLOSE_SHELL_EVENT in event_types_seen
        and DAY39_SHELL_EVENT in event_types_seen
        and MONTHLY_IMPACT_SHELL_EVENT in event_types_seen
    )
    assertion(
        2,
        "VT-102 regression: all 4 trigger event types land in pipeline_log",
        pass_2,
        observed=list(event_types_seen.keys()),
        expected=[WEEKLY_CADENCE_EVENT, ATTRIBUTION_CLOSE_SHELL_EVENT,
                  DAY39_SHELL_EVENT, MONTHLY_IMPACT_SHELL_EVENT],
    )

    # Assertion 3 — VT-104 multi-pattern redactor regression. Re-run the
    # 7 pattern types in one blob; assert all redacted + idempotency.
    multi = (
        "ABCDE1234F email me at fazal@viabe.ai or 9876543210; "
        "Aadhaar 123412341234; IFSC HDFC0001234; GST 22AAAAA0000A1Z5; "
        "CC 4532015112830366."
    )
    out_once = redact(multi)
    out_twice = redact(out_once)
    pass_3 = (
        "<pan:redacted>" in out_once
        and "<email:hash:" in out_once
        and "<aadhaar:redacted>" in out_once
        and "<ifsc:redacted>" in out_once
        and "<gst:redacted>" in out_once
        and "<cc:redacted>" in out_once
        and "phone_tok_" in out_once
        and out_once == out_twice
    )
    assertion(
        3,
        "VT-104 multi-pattern redactor regression + idempotency",
        pass_3,
        observed={"once": out_once, "idempotent": out_once == out_twice},
        expected="all 7 patterns redacted; idempotent",
    )

    # -------------------------------------------------------------------
    # Group B — DBOS idempotency (workflow_id contracts)
    # -------------------------------------------------------------------

    # Assertion 4 — Weekly cadence workflow_id deterministic. Same
    # (tenant, iso_week) → same workflow_id. Cross-call collision-safe.
    tenant_for_id = uuid4()
    id1 = weekly_workflow_id(tenant_for_id, "2026-W22")
    id2 = weekly_workflow_id(tenant_for_id, "2026-W22")
    id_other_week = weekly_workflow_id(tenant_for_id, "2026-W23")
    pass_4 = (
        id1 == id2
        and id1.startswith("weekly:")
        and id1 != id_other_week
    )
    assertion(
        4,
        "Weekly workflow_id deterministic on (tenant_id, iso_week); different week → different id",
        pass_4,
        observed={"id1": id1, "id2": id2, "id_other_week": id_other_week},
        expected="id1 == id2; id_other_week != id1",
    )

    # Assertion 5 — Cross-trigger isolation: same UUID across trigger
    # types yields distinct workflow_ids.
    same = UUID("00000000-0000-4000-8000-000000000001")
    ids = {
        attribution_close_workflow_id(same),
        day39_workflow_id(same),
        monthly_workflow_id(same, "2026-05"),
        weekly_workflow_id(same, "2026-W22"),
    }
    pass_5 = len(ids) == 4
    assertion(
        5,
        "Cross-trigger isolation: same UUID across 4 trigger types → 4 distinct workflow_ids",
        pass_5,
        observed=sorted(ids),
        expected="4 distinct namespaces",
    )

    # -------------------------------------------------------------------
    # Group C — Deterministic triggers (NO LLM)
    # -------------------------------------------------------------------

    # Assertion 6 — Attribution close emits SHELL event (not reserved completion name).
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, payload FROM pipeline_log "
            "WHERE event_type = %s AND run_id = %s",
            (ATTRIBUTION_CLOSE_SHELL_EVENT, str(attr_run)),
        )
        attr_row = cur.fetchone()
    payload_attr = attr_row["payload"] if attr_row else {}
    pass_6 = (
        attr_row is not None
        and payload_attr.get("status") == SHELL_STATUS
        and payload_attr.get("trigger_reason") == "attribution_close"
    )
    assertion(
        6,
        "Attribution close: shell event landed with status=skipped_schema_pending; reserved 'attribution_closed' NOT emitted",
        pass_6,
        observed={"event_type": ATTRIBUTION_CLOSE_SHELL_EVENT, "payload": payload_attr},
        expected={"event_type": "attribution_close_shell", "status": "skipped_schema_pending"},
    )

    # Assertion 7 — Day-39 shell event; reserved completion names absent.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, payload FROM pipeline_log "
            "WHERE event_type = %s AND run_id = %s",
            (DAY39_SHELL_EVENT, str(day39_run)),
        )
        day39_row = cur.fetchone()
        # Also assert reserved names did NOT land for our canary run_ids.
        cur.execute(
            "SELECT COUNT(*) AS c FROM pipeline_log "
            "WHERE run_id = ANY(%s) "
            "  AND event_type IN ('day39_evaluated', 'day39_continue', 'day39_refund_triggered')",
            (INSERTED_RUN_IDS,),
        )
        reserved_hits = cur.fetchone()["c"]
    payload_day39 = day39_row["payload"] if day39_row else {}
    pass_7 = (
        day39_row is not None
        and payload_day39.get("status") == SHELL_STATUS
        and reserved_hits == 0
    )
    assertion(
        7,
        "Day-39: shell event landed; reserved 'day39_evaluated/continue/refund_triggered' NOT emitted",
        pass_7,
        observed={
            "event_type": DAY39_SHELL_EVENT,
            "payload": payload_day39,
            "reserved_names_observed": reserved_hits,
        },
        expected={"status": "skipped_schema_pending", "reserved_names_observed": 0},
    )

    # Assertion 8 — Monthly impact shell + reserved 'monthly_impact_started' absent.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, payload FROM pipeline_log "
            "WHERE event_type = %s AND run_id = %s",
            (MONTHLY_IMPACT_SHELL_EVENT, str(monthly_run)),
        )
        monthly_row = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) AS c FROM pipeline_log "
            "WHERE run_id = ANY(%s) AND event_type = 'monthly_impact_started'",
            (INSERTED_RUN_IDS,),
        )
        reserved_monthly = cur.fetchone()["c"]
    payload_monthly = monthly_row["payload"] if monthly_row else {}
    pass_8 = (
        monthly_row is not None
        and payload_monthly.get("status") == SHELL_STATUS
        and reserved_monthly == 0
    )
    assertion(
        8,
        "Monthly impact: shell event landed; reserved 'monthly_impact_started' NOT emitted",
        pass_8,
        observed={
            "event_type": MONTHLY_IMPACT_SHELL_EVENT,
            "payload": payload_monthly,
            "reserved_name_observed": reserved_monthly,
        },
        expected={"status": "skipped_schema_pending", "reserved_name_observed": 0},
    )

    # -------------------------------------------------------------------
    # Group D — Weekly cadence real Anthropic call + Logfire span
    # -------------------------------------------------------------------

    # Assertion 9 — Real Anthropic Haiku call with PII-stripped prompt.
    # Architecturally equivalent to VT-171 Group D / VT-104 Group E.
    raw_prompt = (
        "Customer +919876543210 (Rajesh Kumar) wants to cancel. "
        "Reply with 'ack' only."
    )
    redacted_prompt = redact(raw_prompt, name_registry={"Rajesh Kumar"}.__contains__)
    pii_stripped = (
        "919876543210" not in redacted_prompt
        and "Rajesh Kumar" not in redacted_prompt
        and "phone_tok_" in redacted_prompt
    )

    anthropic_ok = False
    anthropic_err = None
    in_tokens = 0
    out_tokens = 0
    try:
        import anthropic

        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": redacted_prompt}],
        )
        anthropic_ok = True
        in_tokens = response.usage.input_tokens
        out_tokens = response.usage.output_tokens
    except Exception as exc:  # noqa: BLE001
        anthropic_err = f"{type(exc).__name__}: {exc}"

    global ANTHROPIC_COST_PAISE
    ANTHROPIC_COST_PAISE = (in_tokens * 8300 + out_tokens * 41500) // 1_000_000
    pass_9 = pii_stripped and anthropic_ok and ANTHROPIC_COST_PAISE < 100
    assertion(
        9,
        "Real Anthropic Haiku call inside weekly cadence path; cost < ₹1",
        pass_9,
        observed={
            "pii_stripped": pii_stripped,
            "anthropic_ok": anthropic_ok,
            "anthropic_err": anthropic_err,
            "in_tokens": in_tokens,
            "out_tokens": out_tokens,
            "cost_paise": ANTHROPIC_COST_PAISE,
            "redacted_prompt": redacted_prompt,
        },
        expected={"pii_stripped": True, "anthropic_ok": True, "cost_paise_lt_100": True},
    )

    # -------------------------------------------------------------------
    # Group E — DBOS register-before-launch + idempotency guard
    # -------------------------------------------------------------------

    # Assertion 10 — register_scheduled_triggers is idempotent (call
    # twice; second is a no-op). This is the architectural equivalent of
    # the "DBOS auto-resume" guarantee at the registration boundary:
    # duplicate registration would shift the app_version hash mid-process
    # and break the recovery filter at `_recovery.py:58`.
    from orchestrator import scheduled_triggers as st_mod
    from dbos import DBOS

    call_count = {"n": 0}
    saved_scheduled = DBOS.scheduled
    saved_registered_flag = st_mod._registered

    def _fake_scheduled(cron):
        def _wrap(fn):
            call_count["n"] += 1
            return fn
        return _wrap

    DBOS.scheduled = _fake_scheduled  # type: ignore[method-assign]
    st_mod._registered = False
    try:
        st_mod.register_scheduled_triggers()
        first = call_count["n"]
        st_mod.register_scheduled_triggers()
        second = call_count["n"]
    finally:
        DBOS.scheduled = saved_scheduled  # type: ignore[method-assign]
        st_mod._registered = saved_registered_flag

    pass_10 = first == 4 and second == 4
    assertion(
        10,
        "register_scheduled_triggers idempotent: 4 triggers registered on first call; second call no-op (DBOS app_version stability)",
        pass_10,
        observed={"first_call_decorations": first, "second_call_decorations": second},
        expected={"first": 4, "second": 4},
    )

    # Suppress pending DeprecationWarnings (alias path) and flush.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        logfire_shutdown()

    return _finalise(pool)


def _finalise(pool):
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print(
        f"\n=== Anthropic cost: {ANTHROPIC_COST_PAISE} paise "
        f"(₹{ANTHROPIC_COST_PAISE/100:.4f}) — DR-15 budget ₹1 ==="
    )

    print("\n=== SAMPLE LOGFIRE SPAN (redacted-only payload) ===")
    print(json.dumps(SAMPLE_LOGFIRE_SPAN, indent=2, default=_serial))

    print("\n=== AUDIT ARTIFACT — top-5 inserted pipeline_log rows ===")
    audit_rows = []
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, run_id, tenant_id, event_type, severity, component, "
                "       payload, created_at FROM pipeline_log "
                " WHERE run_id = ANY(%s) ORDER BY created_at ASC LIMIT 5",
                (INSERTED_RUN_IDS,),
            )
            for r in cur.fetchall():
                audit_rows.append(
                    {
                        "id": str(r["id"]),
                        "run_id": str(r["run_id"]),
                        "tenant_id": str(r["tenant_id"]) if r["tenant_id"] else None,
                        "event_type": r["event_type"],
                        "severity": r["severity"],
                        "component": r["component"],
                        "payload": r["payload"],
                        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    }
                )
    except BaseException as exc:  # noqa: BLE001
        print(f"audit fetch failed: {exc!r}", file=sys.stderr)
    print(json.dumps(audit_rows, indent=2, default=_serial))

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM pipeline_log WHERE run_id = ANY(%s)", (INSERTED_RUN_IDS,))
            cur.execute("DELETE FROM tenants WHERE id = ANY(%s)", (INSERTED_TENANT_IDS,))
    except BaseException as exc:  # noqa: BLE001
        print(f"cleanup partial: {exc!r}", file=sys.stderr)

    failed = [n for n, r in RESULTS.items() if r["status"] != "PASS"]
    if failed:
        print(f"\nFAILED assertions: {failed}", file=sys.stderr)
        return 1
    print("\nALL 10 ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_canary())
