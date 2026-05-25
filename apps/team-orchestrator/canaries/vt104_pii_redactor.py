#!/usr/bin/env python3
"""VT-104 PII redactor canary (Rule #15).

Subshell-source `.viabe/secrets/anthropic.env` + `supabase-dev.env`:

    cd apps/team-orchestrator
    (
      set -a
      source ../../.viabe/secrets/anthropic.env
      source ../../.viabe/secrets/supabase-dev.env
      set +a
      time ./.venv/bin/python canaries/vt104_pii_redactor.py 2>&1 | tee /tmp/vt104-canary-evidence.log | tail -200
    )

Exits 0 iff all 10 assertions PASS against real Anthropic + real Supabase.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


CANARY_TENANT_A = UUID("00000000-0000-4000-8000-000000aaa104")
CANARY_COMPONENT = "canary"

RESULTS: dict[int, dict[str, Any]] = {}
INSERTED_RUN_IDS: list[str] = []
INSERTED_TENANT_IDS: list[str] = []
ANTHROPIC_COST_PAISE: int = 0


def assertion(
    num: int,
    name: str,
    passed: bool,
    *,
    observed: Any = None,
    expected: Any = None,
) -> None:
    status = "PASS" if passed else "FAIL"
    RESULTS[num] = {"name": name, "status": status, "observed": observed, "expected": expected}
    print(f"[{num}] {status} — {name}")
    print(f"    observed: {observed}")
    if not passed and expected is not None:
        print(f"    expected: {expected}", file=sys.stderr)


def _default_serialiser(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


def _resolved_supabase_host() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if "@" not in url:
        return "<no-host>"
    after_at = url.split("@", 1)[1]
    return after_at.split("/", 1)[0]


def _resolved_anthropic_host() -> str:
    # Anthropic SDK uses api.anthropic.com; ANTHROPIC_BASE_URL can override.
    return os.environ.get("ANTHROPIC_BASE_URL", "api.anthropic.com")


def _preflight() -> None:
    missing: list[str] = []
    if not os.environ.get("DATABASE_URL"):
        missing.append("DATABASE_URL")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        print(
            f"PREFLIGHT FAIL — missing env: {missing}. Source secrets in subshell.",
            file=sys.stderr,
        )
        sys.exit(2)
    print(
        f"PREFLIGHT OK — supabase host: {_resolved_supabase_host()}; "
        f"anthropic host: {_resolved_anthropic_host()} (env-loaded)"
    )


def _seed_tenant(pool, tenant_id: UUID) -> None:
    INSERTED_TENANT_IDS.append(str(tenant_id))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (id, business_name, plan_tier, phase) "
            "VALUES (%s, %s, 'standard', 'onboarding') ON CONFLICT (id) DO NOTHING",
            (str(tenant_id), f"canary-pii-{tenant_id}"),
        )


def run_canary() -> int:
    _preflight()

    from orchestrator import graph as graph_mod
    from orchestrator.graph import get_pool
    from orchestrator.observability import (
        log_event,
        traceable_node,
    )
    from orchestrator.observability.pii import redact_for_langsmith
    from orchestrator.privacy.pii_redactor import redact

    os.environ["TEAM_PHONE_HASH_SALT"] = os.environ.get(
        "TEAM_PHONE_HASH_SALT", "vt104-canary-salt"
    )

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
    _seed_tenant(pool, CANARY_TENANT_A)

    # -------------------------------------------------------------------
    # Group A — Regression
    # -------------------------------------------------------------------

    # Assertion 1 — VT-101 LangSmith path still redacts.
    pii_input_dict = {
        "k": "Customer +919876543210 cancellation",
        "customer_name": "Rajesh Kumar",
        "body": "Hi I want to cancel",
    }

    @traceable_node("vt104_canary_regression")
    def _ls_node(payload: dict[str, Any]) -> dict[str, Any]:
        return {"echo": "ok"}

    run_id_1 = uuid4()
    INSERTED_RUN_IDS.append(str(run_id_1))
    _ls_node(pii_input_dict)
    redacted_dict = redact_for_langsmith(pii_input_dict)
    blob_1 = json.dumps(redacted_dict)
    leaked_1 = []
    if "919876543210" in blob_1:
        leaked_1.append("phone")
    if "Rajesh Kumar" in blob_1:
        leaked_1.append("customer_name")
    if "Hi I want to cancel" in blob_1:
        leaked_1.append("body")
    has_phone_tok = "phone_tok_" in blob_1
    has_body_tok = "body_tok_" in blob_1
    has_name_redaction = "<redacted:customer_name" in blob_1
    pass_1 = not leaked_1 and has_phone_tok and has_body_tok and has_name_redaction
    assertion(
        1,
        "VT-101 regression: redact_for_langsmith preserves byte-identical tokens",
        pass_1,
        observed={
            "leaked": leaked_1,
            "phone_tok": has_phone_tok,
            "body_tok": has_body_tok,
            "name_redaction": has_name_redaction,
            "blob_sample": blob_1[:200],
        },
        expected="leaked=[] AND all three legacy markers present",
    )

    # Assertion 2 — VT-102 pipeline_log path still redacts.
    run_id_2 = uuid4()
    INSERTED_RUN_IDS.append(str(run_id_2))
    log_event(
        "canary_test",
        run_id_2,
        CANARY_TENANT_A,
        "info",
        CANARY_COMPONENT,
        pii_input_dict,
    )
    time.sleep(1.0)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT payload FROM pipeline_log WHERE run_id = %s", (str(run_id_2),))
        row = cur.fetchone()
    persisted_2 = row["payload"] if row else {}
    blob_2 = json.dumps(persisted_2)
    pass_2 = (
        "919876543210" not in blob_2
        and "Rajesh Kumar" not in blob_2
        and "Hi I want to cancel" not in blob_2
        and "phone_tok_" in blob_2
        and "body_tok_" in blob_2
        and "<redacted:customer_name" in blob_2
    )
    assertion(
        2,
        "VT-102 regression: pipeline_log persists byte-identical legacy tokens",
        pass_2,
        observed={"row_payload": persisted_2},
        expected="legacy tokens present; no raw PII",
    )

    # -------------------------------------------------------------------
    # Group B — 7 PII pattern types
    # -------------------------------------------------------------------

    # Assertion 3 — Phone patterns (E.164 + 10-digit + space-split).
    cases_3 = [
        "+919876543210",
        "9876543210",
        "call me at 98765 43210",
    ]
    outs_3 = [redact(c) for c in cases_3]
    pass_3 = all(
        ("9876543210" not in o and "98765 43210" not in o and "phone_tok_" in o)
        for o in outs_3
    )
    assertion(
        3,
        "Phone E.164 + 10-digit Indian + space-split all redacted",
        pass_3,
        observed=list(zip(cases_3, outs_3, strict=False)),
        expected="every output contains phone_tok_ AND no raw digit run",
    )

    # Assertion 4 — Email redacted.
    emails = ["fazal@viabe.ai", "customer.support@gmail.com"]
    outs_4 = [redact(e) for e in emails]
    pass_4 = all("@" not in o and "<email:hash:" in o for o in outs_4)
    assertion(
        4,
        "Email redacted to <email:hash:HEX> with no @ in output",
        pass_4,
        observed=list(zip(emails, outs_4, strict=False)),
        expected="no @ remains; <email:hash:HEX> marker present",
    )

    # Assertion 5 — PAN + Aadhaar + IFSC + GST.
    out_pan = redact("ABCDE1234F")
    out_aadhaar = redact("123412341234")
    out_ifsc = redact("HDFC0001234")
    out_gst = redact("22AAAAA0000A1Z5")
    pass_5 = (
        out_pan == "<pan:redacted>"
        and "<aadhaar:redacted>" in out_aadhaar
        and out_ifsc == "<ifsc:redacted>"
        and out_gst == "<gst:redacted>"
    )
    assertion(
        5,
        "PAN/Aadhaar/IFSC/GST patterns redacted to type markers",
        pass_5,
        observed={
            "pan": out_pan,
            "aadhaar": out_aadhaar,
            "ifsc": out_ifsc,
            "gst": out_gst,
        },
        expected="all four return their <type:redacted> markers",
    )

    # Assertion 6 — Credit card: Luhn passes, non-Luhn doesn't trigger.
    cc_valid = redact("4532015112830366")
    cc_invalid = redact("1234567890123456")
    pass_6 = cc_valid == "<cc:redacted>" and cc_invalid == "1234567890123456"
    assertion(
        6,
        "CC Luhn-valid redacted; Luhn-invalid 16-digit sequence unchanged",
        pass_6,
        observed={"valid": cc_valid, "invalid": cc_invalid},
        expected={"valid": "<cc:redacted>", "invalid": "1234567890123456"},
    )

    # Assertion 7 — Long body threshold.
    long_body = "x" * 250
    short_body = "x" * 150
    out_long = redact(long_body)
    out_short = redact(short_body)
    pass_7 = out_long.startswith("<body:hash:") and out_short == short_body
    assertion(
        7,
        "Long body (>200 chars) hashed; short body (<200) unchanged",
        pass_7,
        observed={
            "long_marker_prefix": out_long[:30],
            "short_unchanged": out_short == short_body,
        },
        expected={"long": "<body:hash:HEX>", "short_unchanged": True},
    )

    # -------------------------------------------------------------------
    # Group C — Customer-name registry
    # -------------------------------------------------------------------

    # Assertion 8 — Registered name redacted; unregistered name not.
    registry = {"Rajesh Kumar"}.__contains__
    text = "Hi Rajesh Kumar are you there? Random Person not in registry."
    out_8 = redact(text, name_registry=registry)
    pass_8 = (
        "Rajesh Kumar" not in out_8
        and "<customer_name>" in out_8
        and "Random Person" in out_8
    )
    assertion(
        8,
        "Customer-name registry: known match redacted; unknown name unchanged (Phase-1 acknowledged false-negative)",
        pass_8,
        observed={"input": text, "output": out_8},
        expected="<customer_name> replaces 'Rajesh Kumar'; 'Random Person' untouched",
    )

    # -------------------------------------------------------------------
    # Group D — Idempotency + recursion
    # -------------------------------------------------------------------

    # Assertion 9 — Idempotency on complex nested PII.
    complex_pii = {
        "phone": "+919876543210",
        "body": "Hi I want to cancel",
        "customer_name": "Rajesh Kumar",
        "msg": "Reach me at fazal@viabe.ai or call 9876543210",
        "nested": {
            "pan": "ABCDE1234F",
            "card": "4532015112830366",
            "deep": {"deeper": {"phone": "+918765432109"}},
        },
    }
    once = redact(complex_pii)
    twice = redact(once)
    deep_value = once.get("nested", {}).get("deep", {}).get("deeper", {}).get("phone", "")
    deep_redacted = deep_value.startswith("phone_tok_")
    pass_9 = once == twice and deep_redacted
    assertion(
        9,
        "Idempotent on nested PII; depth-5 leaf still redacted",
        pass_9,
        observed={"once_eq_twice": once == twice, "deep_redacted": deep_redacted, "once_keys": list(once.keys())},
        expected="redact(redact(x)) == redact(x) AND deep leaf redacted",
    )

    # -------------------------------------------------------------------
    # Group E — Real Anthropic on-the-wire proof
    # -------------------------------------------------------------------

    # Assertion 10 — PII-stripped prompt actually sent to Anthropic.
    # Wire the registry callable so the customer-name (registry-driven path)
    # is redacted; brief explicitly ties customer-name tokenisation to the
    # tenant registry — without the callable it's a Phase-1 false negative
    # (documented in pii_redactor.py).
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
    # Haiku rate from model_pricing.yaml: 8300 paise / 1M input; 41500 / 1M output.
    cost_paise = (in_tokens * 8300 + out_tokens * 41500) // 1_000_000
    ANTHROPIC_COST_PAISE = cost_paise
    cost_under_one_rupee = cost_paise < 100
    pass_10 = pii_stripped and anthropic_ok and cost_under_one_rupee
    assertion(
        10,
        "Real Anthropic Haiku call with PII-stripped prompt; cost < ₹1",
        pass_10,
        observed={
            "host": _resolved_anthropic_host(),
            "pii_stripped_in_sent_prompt": pii_stripped,
            "anthropic_ok": anthropic_ok,
            "anthropic_err": anthropic_err,
            "in_tokens": in_tokens,
            "out_tokens": out_tokens,
            "cost_paise": cost_paise,
            "redacted_prompt_sent": redacted_prompt,
        },
        expected={
            "pii_stripped": True,
            "anthropic_ok": True,
            "cost_paise_lt_100": True,
        },
    )

    return _finalise(pool)


def _finalise(pool) -> int:
    print("\n=== CANARY SUMMARY ===")
    for n in sorted(RESULTS):
        r = RESULTS[n]
        print(f"  [{n}] {r['status']} — {r['name']}")

    print(f"\n=== Anthropic cost captured: {ANTHROPIC_COST_PAISE} paise (₹{ANTHROPIC_COST_PAISE/100:.4f}) ===")

    print("\n=== AUDIT ARTIFACT — top-10 inserted canary rows ===")
    audit_rows: list[dict[str, Any]] = []
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, run_id, tenant_id, event_type, severity, component, "
                "       payload, duration_ms, created_at "
                "  FROM pipeline_log "
                " WHERE run_id = ANY(%s) "
                " ORDER BY created_at ASC LIMIT 10",
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
                        "duration_ms": r["duration_ms"],
                        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    }
                )
    except BaseException as exc:  # noqa: BLE001
        print(f"audit fetch failed: {exc!r}", file=sys.stderr)
    print(json.dumps(audit_rows, indent=2, default=_default_serialiser))

    # Best-effort cleanup.
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM pipeline_log WHERE run_id = ANY(%s)",
                (INSERTED_RUN_IDS,),
            )
            cur.execute(
                "DELETE FROM tenants WHERE id = ANY(%s)",
                (INSERTED_TENANT_IDS,),
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
