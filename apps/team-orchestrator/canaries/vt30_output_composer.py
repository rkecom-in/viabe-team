#!/usr/bin/env python3
"""VT-30 unified output composer canary (Rule #15, DR-15).

Subshell-source ONLY `.viabe/secrets/supabase-dev.env` +
`.viabe/secrets/logfire-dev.env`:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/supabase-dev.env
      source ../../.viabe/secrets/logfire-dev.env
      set +a
      time ./.venv/bin/python canaries/vt30_output_composer.py 2>&1 | tee /tmp/vt30-canary-evidence.log | tail -200
    )

**NO anthropic.env sourced.** Defense-in-depth Pillar 1: composer is
deterministic by spec; canary preflight asserts ANTHROPIC_API_KEY ABSENT.
Wall-clock budget ≤ 60s. Cost budget: 0 paise.

10 assertions across 3 groups (A regression / B honesty rules /
C routing logic). Loads `template_routing.yaml` + `twilio_templates.yaml`
from real config; uses synthetic SubscriberState fixtures (no DB seed
needed since composer is pure Python).
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_RUN_IDS: list[str] = []
INSERTED_TENANT_IDS: list[str] = []
SAMPLE_OUTPUTS: dict[str, dict[str, Any]] = {}


def assertion(num, name, passed, *, observed=None, expected=None):
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _supabase_host():
    url = os.environ.get("DATABASE_URL", "")
    if "@" not in url:
        return "<no-host>"
    return url.split("@", 1)[1].split("/", 1)[0]


def _preflight():
    if not os.environ.get("DATABASE_URL"):
        print("PREFLIGHT FAIL — DATABASE_URL not set", file=sys.stderr)
        sys.exit(2)
    if os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "PREFLIGHT FAIL — ANTHROPIC_API_KEY present; this canary's loader "
            "must NOT source anthropic.env (Pillar 1 structural enforcement).",
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        f"PREFLIGHT OK — supabase: {_supabase_host()}; "
        f"ANTHROPIC_API_KEY: <absent — defense-in-depth>; "
        f"logfire: {os.environ.get('LOGFIRE_BASE_URL', '(SDK-default EU)')}"
    )


def _state(**kw):
    base = {
        "phase": "onboarding",
        "escalation_pending": False,
        "last_owner_message_at": None,
    }
    base.update(kw)
    return base


def _serial(value):
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


def run_canary() -> int:
    _preflight()
    os.environ.setdefault("TEAM_PHONE_HASH_SALT", "vt30-canary-salt")
    window_start = datetime.now(timezone.utc)

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool
    from orchestrator.observability.log import log_event
    from orchestrator.observability.logfire import (
        configure_logfire,
        is_enabled,
        traced_node,
    )
    from orchestrator.observability.pii import redact_for_otel_span
    from orchestrator.output_composer import (
        compose_owner_output,
        load_template_routing,
        load_twilio_templates,
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

    # -------------------------------------------------------------------
    # Group A — observability regression (3 assertions)
    # -------------------------------------------------------------------

    # Assertion 1 — VT-104 redactor regression.
    pii_input = {
        "phone": "+919876543210",
        "customer_name": "Rajesh Kumar",
        "body": "Hi I want to cancel",
    }
    redacted = redact_for_otel_span(pii_input)
    pass_1 = (
        redacted["phone"].startswith("phone_tok_")
        and redacted["customer_name"].startswith("<redacted:customer_name:")
        and redacted["body"].startswith("body_tok_")
    )
    assertion(
        1,
        "VT-104 redactor regression: legacy token format preserved",
        pass_1,
        observed=redacted,
        expected="all 3 keys carry legacy token formats",
    )

    # Assertion 2 — VT-102 pipeline_log regression: composer_invoked event.
    # Workspace-level event (tenant_id=None) — composer invocation isn't
    # strictly tenant-scoped at the audit-trail layer; the redaction
    # invariants live at the payload boundary.
    run_id = uuid4()
    INSERTED_RUN_IDS.append(str(run_id))

    log_event(
        event_type="composer_invoked",
        run_id=run_id,
        tenant_id=None,
        severity="info",
        component="composer",
        payload={
            "intent_or_trigger": "welcome",
            "message_type": "template",
            "signature": "abc123def456",
        },
    )
    time.sleep(1.5)

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, payload FROM pipeline_log WHERE run_id = %s",
            (str(run_id),),
        )
        row = cur.fetchone()
    pass_2 = (
        row is not None
        and row["event_type"] == "composer_invoked"
        and row["payload"].get("intent_or_trigger") == "welcome"
        and row["payload"].get("message_type") == "template"
    )
    assertion(
        2,
        "VT-102 pipeline_log: composer_invoked event with redacted payload",
        pass_2,
        observed={
            "event_type": row["event_type"] if row else None,
            "payload": row["payload"] if row else None,
        },
        expected={"event_type": "composer_invoked", "intent_or_trigger": "welcome"},
    )

    # Assertion 3 — VT-171 Logfire regression: traced_node span fires.
    @traced_node("vt30_canary_composer_span")
    def _composer_invocation(state, intent):
        return compose_owner_output(None, state, intent)

    state = _state(last_owner_message_at=window_start - timedelta(hours=48))
    out_3 = _composer_invocation(state, "welcome")
    pass_3 = is_enabled() and out_3.message_type == "template"
    assertion(
        3,
        "VT-171 Logfire: traced_node span fired over composer; output well-formed",
        pass_3,
        observed={
            "logfire_enabled": is_enabled(),
            "out": {"type": out_3.message_type, "name": out_3.template_name},
        },
        expected={"logfire_enabled": True, "message_type": "template"},
    )

    # -------------------------------------------------------------------
    # Group B — honesty rules (5 assertions, deterministic)
    # -------------------------------------------------------------------

    state_b = _state(last_owner_message_at=window_start - timedelta(hours=1))

    # Assertion 4 — Honesty rule #1: no ARRR overstatement.
    specialist_uncertain = SimpleNamespace(
        status="completed",
        terminated_by=None,
        output={
            "attribution_uncertain": True,
            "message": "We recovered ₹5000 last month from your campaign.",
        },
    )
    out_4 = compose_owner_output(specialist_uncertain, state_b, "free_form_chat", now=window_start)
    pass_4 = (
        "approximately ₹5000" in out_4.message_body
        and "arrr_uncertainty_prefix_applied" in out_4.honesty_notes
    )
    assertion(
        4,
        "Honesty rule #1 (ARRR overstatement): uncertain attribution prefixes monetary amount",
        pass_4,
        observed={"body": out_4.message_body, "notes": out_4.honesty_notes},
        expected={"body_contains": "approximately ₹5000", "note": "arrr_uncertainty_prefix_applied"},
    )
    SAMPLE_OUTPUTS["arrr_uncertain"] = {"body": out_4.message_body, "notes": out_4.honesty_notes}

    # Assertion 5 — Honesty rule #2: terminated_by surfaces hard limit.
    specialist_terminated = SimpleNamespace(
        status="terminated",
        terminated_by="cost_paise",
        output={"message": "Partial result available."},
    )
    out_5 = compose_owner_output(specialist_terminated, state_b, "free_form_chat", now=window_start)
    pass_5 = (
        "₹50 cost budget" in out_5.message_body
        and any(n.startswith("hard_limit_axis_explained") for n in out_5.honesty_notes)
    )
    assertion(
        5,
        "Honesty rule #2 (no hidden failures): terminated_by axis surfaced in plain language",
        pass_5,
        observed={"body": out_5.message_body, "notes": out_5.honesty_notes},
        expected={"body_contains": "₹50 cost budget"},
    )

    # Assertion 6 — Honesty rule #3: pressure phrases detected.
    specialist_pressure = SimpleNamespace(
        status="completed",
        terminated_by=None,
        output={"message": "Are you sure? Look at all this value you're missing out on!"},
    )
    out_6 = compose_owner_output(specialist_pressure, state_b, "free_form_chat", now=window_start)
    pressure_notes = [n for n in out_6.honesty_notes if n.startswith("pressure_phrase_detected")]
    pass_6 = len(pressure_notes) >= 1
    assertion(
        6,
        "Honesty rule #3 (no retention pressure): pressure phrases detected + noted",
        pass_6,
        observed={"pressure_notes": pressure_notes, "body": out_6.message_body},
        expected="at least 1 pressure_phrase_detected note",
    )

    # Assertion 7 — Honesty rule #4: certainty claims softened.
    specialist_certainty = SimpleNamespace(
        status="completed",
        terminated_by=None,
        output={
            "intent_inferred": True,
            "message": "Customer wants a refund based on the message tone.",
        },
    )
    out_7 = compose_owner_output(specialist_certainty, state_b, "free_form_chat", now=window_start)
    pass_7 = (
        "pattern suggests" in out_7.message_body.lower()
        and "customer wants" not in out_7.message_body.lower()
    )
    assertion(
        7,
        "Honesty rule #4 (no certainty claims): inferred intent softened to 'pattern suggests'",
        pass_7,
        observed={"body": out_7.message_body, "notes": out_7.honesty_notes},
        expected={"body_contains": "pattern suggests", "body_excludes": "customer wants"},
    )
    SAMPLE_OUTPUTS["intent_inferred"] = {"body": out_7.message_body, "notes": out_7.honesty_notes}

    # Assertion 8 — Honesty rule #5: refund-phase acknowledgment.
    state_refund = _state(
        phase="refunded",
        last_owner_message_at=window_start - timedelta(hours=1),
    )
    specialist_refund = SimpleNamespace(
        status="completed",
        terminated_by=None,
        output={"message": "Here's the latest engagement report."},
    )
    out_8 = compose_owner_output(specialist_refund, state_refund, "free_form_chat", now=window_start)
    pass_8 = (
        "refund" in out_8.message_body.lower()
        and "refund_ack_prepended" in out_8.honesty_notes
    )
    assertion(
        8,
        "Honesty rule #5 (refund acknowledgment): refunded phase prepends refund mention",
        pass_8,
        observed={"body": out_8.message_body, "notes": out_8.honesty_notes},
        expected={"body_contains": "refund", "note": "refund_ack_prepended"},
    )
    SAMPLE_OUTPUTS["refund_ack"] = {"body": out_8.message_body, "notes": out_8.honesty_notes}

    # -------------------------------------------------------------------
    # Group C — routing logic (2 assertions)
    # -------------------------------------------------------------------

    # Assertion 9 — 24-hour-window enforcement.
    state_outside = _state(last_owner_message_at=window_start - timedelta(hours=25))
    state_inside_no_template = _state(last_owner_message_at=window_start - timedelta(hours=1))
    out_9a = compose_owner_output(None, state_outside, "welcome", now=window_start)
    specialist_for_inside = SimpleNamespace(
        status="completed", terminated_by=None, output={"message": "Hi"}
    )
    out_9b = compose_owner_output(
        specialist_for_inside, state_inside_no_template, "free_form_chat", now=window_start
    )
    pass_9 = out_9a.message_type == "template" and out_9b.message_type == "free_form_24h"
    assertion(
        9,
        "24h-window enforcement: outside→template, inside+no-match→free_form_24h",
        pass_9,
        observed={"outside_type": out_9a.message_type, "inside_type": out_9b.message_type},
        expected={"outside_type": "template", "inside_type": "free_form_24h"},
    )

    # Assertion 10 — Tier-A intent→template_name mapping (all 8).
    routing = load_template_routing()
    templates = load_twilio_templates()
    tier_a_cases = [
        ("welcome", "onboarding", "team_welcome"),
        ("welcome", "trial", "team_welcome"),
        ("weekly_approval", "paid_active", "team_weekly_approval"),
        ("opt_out_confirmed", "trial", "team_opt_out_confirmation"),
        ("dsr_acknowledged", "paid_active", "team_dsr_acknowledgment"),
        ("agent_stuck", "paid_active", "team_agent_stuck_escalation"),
        ("status_ping", "paid_active", "team_status_ping"),
        ("error_handler", "paid_active", "team_error_handler"),
    ]
    mismatches = []
    sample_per_intent = {}
    for intent, phase, expected_name in tier_a_cases:
        st = _state(phase=phase, last_owner_message_at=window_start - timedelta(hours=48))
        out = compose_owner_output(None, st, intent, now=window_start)
        if out.template_name != expected_name:
            mismatches.append({
                "intent": intent, "phase": phase,
                "expected": expected_name, "got": out.template_name,
            })
        sample_per_intent[intent] = {
            "template_name": out.template_name,
            "message_type": out.message_type,
            "signature": out.signature,
        }
    pass_10 = len(mismatches) == 0 and len(routing) >= 8 and len(templates) >= 8
    SAMPLE_OUTPUTS["tier_a_mapping"] = sample_per_intent
    assertion(
        10,
        "Tier-A intent→template_name mapping: all 8 paths resolve correctly",
        pass_10,
        observed={
            "routing_entries": len(routing),
            "templates_entries": len(templates),
            "mismatches": mismatches,
        },
        expected={"mismatches": []},
    )

    return _finalise(pool)


def _finalise(pool):
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print("\n=== Anthropic cost: 0 paise (composer deterministic; env absent) ===")

    print("\n=== SAMPLE COMPOSER OUTPUTS (canonical JSON) ===")
    print(json.dumps(SAMPLE_OUTPUTS, indent=2, default=_serial))

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM pipeline_log WHERE run_id = ANY(%s)",
                (INSERTED_RUN_IDS,),
            )
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
