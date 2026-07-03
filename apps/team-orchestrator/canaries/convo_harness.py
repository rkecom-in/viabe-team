"""VT-582 — server-side conversation harness (CL-2026-07-03-conversing-surfaces-and-harness).

Lets the orchestrator's operator (Claude Code) drive FULL WhatsApp conversations against the
DEPLOYED dev orchestrator — inbound INJECTED at the ingress, outbound CAPTURED from the lifetime
conversation log — with ZERO real WhatsApp messages. The reproduction rig for the run-23
silent-drop class: send a message, watch whether a reply actually comes back.

    railway run --service vt-orchestrator-service --environment development -- \
        uv run --directory apps/team-orchestrator python canaries/convo_harness.py <cmd> ...

    setup     [--owner-inputs true|false] [--onboarded] [--name N] [--number N] [--phase P]
    send      <tenant_id> "<message>" [--ingress-url URL] [--timeout S]
    script    <tenant_id> <scenario.json|.yaml> [--ingress-url URL] [--timeout S]
    teardown  <tenant_id>

HOW OUTBOUND IS CAPTURED (no real send). Every owner-facing send funnels through
utils/twilio_send._client(), which on dev is wrapped by the VT-476 dev_send_guard. The harness
tenant's number is an obviously-bogus, NON-allowlisted +15550xxxxxx, so the guard MOCKS every
outbound (returns an ``MKDEV…`` SID, no Twilio call) while the calling flow proceeds identically —
and STILL records the 'assistant' turn into conversation_log. So the captured transcript = the new
conversation_log rows since the send, and the send-guard's own behaviour is ASSERTED: an assistant
turn whose message_sid starts with a real Twilio prefix (``SM``/``MM``) means a real send escaped —
a hard failure. Nothing is bypassed; the guard is verified by its own output.

SAFETY RAILS (binding):
  - Harness tenants MUST use a bogus non-allowlisted number so the send-guard mocks EVERYTHING; the
    ``send``/``script`` paths ASSERT no assistant turn carries a real Twilio SID (never a bypass).
  - ``teardown`` refuses any tenant whose business_name is not a ``convo-harness-…`` name — the
    harness never deletes a tenant it did not create.
  - The dev ingress secret is read from env/arg and used ONLY as a request header; it is NEVER
    printed. Bogus numbers are synthetic (US 555 test range) — printed whole is not real PII.

DB access: the dev DATABASE_URL role is the privileged pool role (bypasses RLS — the same posture
the live-drill scripts rely on). conversation_log reads ALSO set the operator-JWT-claim GUC so the
read passes its operator SELECT policy even under FORCE RLS. Ingress auth uses DEV_TEST_INGRESS_SECRET
(VT-582 ingress gate) — accepted only on EXPECTED_ENV=dev.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any

# --- constants ---------------------------------------------------------------------------------

_INGRESS_PATH = "/api/orchestrator/twilio-ingress"
# US 555-01xx directory-test range: obviously bogus, and NEVER in DEV_SEND_ALLOWLIST (the four +91
# Fazal-provided numbers), so the dev_send_guard mocks every outbound to it.
_BOGUS_PREFIX = "+15550"
# Harness-originated inbound MessageSids: realistic ``SM`` prefix (so the ingress + the brain's
# owner-turn record behave exactly as for a real inbound) but greppable as harness traffic.
_INBOUND_SID_PREFIX = "SMharness"
# A real Twilio *message* SID starts with SM/MM. A dev-guard mock starts with MKDEV. An assistant
# turn carrying a real prefix means a real send escaped the guard — the breach this harness guards.
_REAL_TWILIO_SID = re.compile(r"^(SM|MM)", re.IGNORECASE)
# pipeline_runs is terminal once it leaves 'running' (mig 052/110 status members).
_RUNNING = "running"
# Ingress reasons that mean NO run was started (nothing to poll / a setup problem for a harness tenant).
_NO_RUN_REASONS = frozenset({"unknown_sender", "rate_limit_exceeded", "error_logged"})

_HARNESS_NAME_PREFIX = "convo-harness-"


# --- pure helpers (unit-tested; import-clean, stdlib only) --------------------------------------


def bogus_number() -> str:
    """A fresh obviously-bogus, non-allowlisted +15550xxxxxx number (US 555 test range)."""
    return f"{_BOGUS_PREFIX}{uuid.uuid4().int % 10**6:06d}"


def fresh_inbound_sid() -> str:
    """A fresh harness inbound MessageSid — realistic ``SM…`` shape, greppable as harness traffic."""
    return f"{_INBOUND_SID_PREFIX}{uuid.uuid4().hex}"


def run_id_for_sid(message_sid: str) -> str:
    """The pipeline_runs.id the ingress derives for this MessageSid.

    MUST mirror twilio_ingress.twilio_ingress EXACTLY: ``uuid5(NAMESPACE_URL, message_sid)`` — this
    is how an external process (not in the DBOS context) locates the run to poll."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, message_sid))


def is_real_twilio_sid(sid: str | None) -> bool:
    """True if ``sid`` looks like a REAL Twilio message SID (SM/MM…) — i.e. NOT a dev-guard mock."""
    return bool(sid) and bool(_REAL_TWILIO_SID.match(sid or ""))


@dataclass
class Turn:
    """One captured conversation_log row (or the operator's own injected inbound)."""

    role: str  # 'owner' | 'assistant'
    text: str
    message_sid: str | None = None
    surface: str | None = None


@dataclass
class StepResult:
    ok: bool
    xfail: bool  # a failure that was EXPECTED (a known, marked gap) — green for the exit code
    label: str  # PASS | FAIL | XFAIL | XPASS
    reasons: list[str]
    transcript: list[Turn]
    run_status: str | None
    ingress_reason: str | None


def assistant_turns(turns: list[Turn]) -> list[Turn]:
    return [t for t in turns if t.role == "assistant"]


def concat_assistant_text(turns: list[Turn]) -> str:
    return "\n".join(t.text for t in assistant_turns(turns))


def evaluate_assertions(
    turns: list[Turn],
    *,
    assert_no_silent: bool = True,
    assert_contains: list[str] | None = None,
    assert_not_contains: list[str] | None = None,
) -> list[str]:
    """Return a list of failure reasons (empty ⇒ all assertions held).

    assert_no_silent (default ON): ≥1 assistant reply MUST exist — the run-23 silent-drop check.
    assert_contains / assert_not_contains: case-insensitive substring checks over the concatenated
    assistant text."""
    failures: list[str] = []
    replies = assistant_turns(turns)
    haystack = concat_assistant_text(turns).lower()
    if assert_no_silent and not replies:
        failures.append("assert_no_silent: NO assistant reply was produced (silent drop)")
    for needle in assert_contains or []:
        if needle.lower() not in haystack:
            failures.append(f"assert_contains: reply is missing {needle!r}")
    for needle in assert_not_contains or []:
        if needle.lower() in haystack:
            failures.append(f"assert_not_contains: reply unexpectedly contains {needle!r}")
    return failures


def classify_step(
    failures: list[str], *, expected_fail: bool
) -> tuple[bool, bool, str]:
    """(ok_for_exit_code, is_xfail, label). expected_fail inverts: a failing marked-gap step is
    XFAIL (green); a passing marked-gap step is XPASS (flagged, still green — the gap may have
    closed)."""
    failed = bool(failures)
    if expected_fail:
        if failed:
            return True, True, "XFAIL"
        return True, False, "XPASS"
    return (not failed), False, ("PASS" if not failed else "FAIL")


# --- env / connection --------------------------------------------------------------------------


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEAM_SUPABASE_DB_URL")
    if not dsn:
        _die("no DB URL in env (DATABASE_URL / TEAM_SUPABASE_DB_URL) — run under `railway run`")
    return dsn


def _ingress_base(arg_url: str | None) -> str:
    base = arg_url or os.environ.get("TEAM_ORCHESTRATOR_URL")
    if not base:
        _die(
            "no ingress URL: pass --ingress-url https://<deployed-dev-orchestrator> "
            "or set TEAM_ORCHESTRATOR_URL"
        )
    return base.rstrip("/")


def _dev_secret() -> str:
    # Preferred: env (a CLI arg lands in `ps`). Read here, used ONLY as a header, NEVER printed.
    secret = os.environ.get("DEV_TEST_INGRESS_SECRET", "")
    if not secret:
        _die("DEV_TEST_INGRESS_SECRET not set in env (the dev ingress secret the harness authenticates with)")
    return secret


def _connect(dsn: str):
    import psycopg

    return psycopg.connect(dsn, autocommit=True)


def _die(msg: str) -> None:
    print(f"convo_harness: ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


# --- DB reads/writes ---------------------------------------------------------------------------


def _tenant_number(conn: Any, tenant_id: str) -> str:
    row = conn.execute(
        "SELECT whatsapp_number, business_name FROM tenants WHERE id = %s", (tenant_id,)
    ).fetchone()
    if row is None:
        _die(f"tenant {tenant_id} not found")
    number = row[0] if not isinstance(row, dict) else row["whatsapp_number"]
    if not number:
        _die(f"tenant {tenant_id} has no whatsapp_number")
    return str(number)


def _conversation_ids(conn: Any, tenant_id: str) -> set[str]:
    _set_operator_claim(conn)
    rows = conn.execute(
        "SELECT id FROM conversation_log WHERE tenant_id = %s", (tenant_id,)
    ).fetchall()
    return {str(r[0] if not isinstance(r, dict) else r["id"]) for r in rows}


def _new_conversation_turns(conn: Any, tenant_id: str, before_ids: set[str]) -> list[Turn]:
    _set_operator_claim(conn)
    rows = conn.execute(
        "SELECT id, role, text, message_sid, surface FROM conversation_log "
        "WHERE tenant_id = %s ORDER BY created_at ASC, id ASC",
        (tenant_id,),
    ).fetchall()
    out: list[Turn] = []
    for r in rows:
        rid = str(r[0] if not isinstance(r, dict) else r["id"])
        if rid in before_ids:
            continue
        if isinstance(r, dict):
            out.append(Turn(role=r["role"], text=r["text"], message_sid=r["message_sid"], surface=r["surface"]))
        else:
            out.append(Turn(role=r[1], text=r[2], message_sid=r[3], surface=r[4]))
    return out


def _set_operator_claim(conn: Any) -> None:
    """Defence-in-depth: satisfy conversation_log's operator SELECT policy even if the DATABASE_URL
    role does NOT bypass RLS. A no-op when the role already bypasses (dev privileged pool)."""
    try:
        conn.execute(
            "SELECT set_config('request.jwt.claims', '{\"operator_claim\":\"true\"}', false)"
        )
    except Exception:  # noqa: BLE001 — best-effort; on a bypass-RLS role the policy is moot anyway
        pass


def _poll_run_status(dsn: str, run_id: str, timeout: float) -> str | None:
    """Poll pipeline_runs.id until it leaves 'running' (terminal) or the timeout. None if the row
    never appears (the workflow may not have opened its run within the budget)."""
    deadline = time.time() + timeout
    last: str | None = None
    while time.time() < deadline:
        with _connect(dsn) as conn:
            row = conn.execute(
                "SELECT status FROM pipeline_runs WHERE id = %s", (run_id,)
            ).fetchone()
        if row is not None:
            last = str(row[0] if not isinstance(row, dict) else row["status"])
            if last != _RUNNING:
                return last
        time.sleep(1.5)
    return last


# --- ingress POST ------------------------------------------------------------------------------


def _post_inbound(base: str, secret: str, fields: dict[str, str]) -> dict[str, Any]:
    import requests

    resp = requests.post(
        f"{base}{_INGRESS_PATH}",
        json={"twilio_fields": fields},
        headers={"X-Internal-Secret": secret, "content-type": "application/json"},
        timeout=30,
    )
    if resp.status_code != 200:
        return {"workflow_id": None, "reason": f"http_{resp.status_code}"}
    return resp.json()


# --- one turn (shared by send + script) --------------------------------------------------------


def _drive_turn(
    dsn: str, base: str, secret: str, tenant_id: str, message: str, *, timeout: float
) -> StepResult:
    """Inject one inbound, poll to completion, capture the reply. No assertions here — the caller
    (send prints; script evaluates) decides what to assert."""
    with _connect(dsn) as conn:
        number = _tenant_number(conn, tenant_id)
        before_ids = _conversation_ids(conn, tenant_id)

    sid = fresh_inbound_sid()
    run_id = run_id_for_sid(sid)
    fields = {
        # Real inbounds arrive channel-prefixed; exercise the VT-567 strip path.
        "From": f"whatsapp:{number}",
        "To": "whatsapp:+910000000000",
        "Body": message,
        "MessageSid": sid,
        "NumMedia": "0",
    }
    ingress = _post_inbound(base, secret, fields)
    reason = str(ingress.get("reason", ""))

    run_status: str | None = None
    if reason in _NO_RUN_REASONS or reason.startswith("http_"):
        # No run started (or the ingress rejected the request). Nothing to poll; capture whatever
        # exists (usually nothing) so the caller sees the empty reply → silent-drop assertion fires.
        pass
    else:
        run_status = _poll_run_status(dsn, run_id, timeout)

    with _connect(dsn) as conn:
        new_turns = _new_conversation_turns(conn, tenant_id, before_ids)

    # Build the transcript: the operator's own inbound (echo-deduped against a brain-recorded owner
    # row for the same sid), then every new conversation_log row.
    transcript: list[Turn] = [Turn(role="owner", text=message, message_sid=sid, surface="(injected)")]
    for t in new_turns:
        if t.role == "owner" and t.message_sid == sid:
            continue  # the brain-route recording of the SAME inbound — don't double-print
        transcript.append(t)

    # SAFETY: assert the send-guard mocked every outbound — no assistant turn may carry a real SID.
    reasons: list[str] = []
    breach = [t for t in assistant_turns(new_turns) if is_real_twilio_sid(t.message_sid)]
    if breach:
        reasons.append(
            f"SEND-GUARD BREACH: {len(breach)} assistant turn(s) carry a REAL Twilio SID "
            f"(expected a mocked MKDEV…) — a real WhatsApp send escaped the dev guard"
        )
    ok = not reasons
    return StepResult(
        ok=ok, xfail=False, label=("PASS" if ok else "FAIL"), reasons=reasons,
        transcript=transcript, run_status=run_status, ingress_reason=reason,
    )


# --- transcript printing -----------------------------------------------------------------------


def _print_transcript(transcript: list[Turn]) -> None:
    for t in transcript:
        arrow = "owner →" if t.role == "owner" else "  → owner"
        meta = []
        if t.surface:
            meta.append(t.surface)
        if t.message_sid:
            meta.append(f"sid={t.message_sid}")
        suffix = f"   [{', '.join(meta)}]" if meta else ""
        print(f"    {arrow} {t.text}{suffix}")


# --- subcommands -------------------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    dsn = _dsn()
    name = args.name or f"{_HARNESS_NAME_PREFIX}{uuid.uuid4().hex[:8]}"
    if not name.startswith(_HARNESS_NAME_PREFIX):
        _die(f"--name must start with {_HARNESS_NAME_PREFIX!r} (teardown safety rail)")
    number = args.number or bogus_number()
    if not number.startswith(_BOGUS_PREFIX):
        _die(f"--number must be a bogus non-allowlisted {_BOGUS_PREFIX}… test number")
    owner_inputs = str(args.owner_inputs).lower() in ("true", "1", "yes")

    with _connect(dsn) as conn:
        row = conn.execute(
            "INSERT INTO tenants (business_name, plan_tier, phase, phase_entered_at, "
            "whatsapp_number, owner_inputs) "
            "VALUES (%s, 'founding', %s, now(), %s, %s) RETURNING id",
            (name, args.phase, number, owner_inputs),
        ).fetchone()
        tenant_id = str(row[0] if not isinstance(row, dict) else row["id"])
        if args.onboarded:
            # Pre-seed a COMPLETED onboarding journey (one row per tenant, PK=tenant_id) so the
            # tenant starts post-onboarding — the state the silent_drop_probe scenario needs.
            conn.execute(
                "INSERT INTO onboarding_journey (tenant_id, status, completed_at) "
                "VALUES (%s, 'complete', now()) ON CONFLICT (tenant_id) DO UPDATE "
                "SET status = 'complete', completed_at = now()",
                (tenant_id,),
            )

    print(f"tenant_id={tenant_id}")
    print(f"whatsapp_number={number}  (bogus, non-allowlisted → dev_send_guard mocks all sends)")
    print(f"owner_inputs={owner_inputs}  phase={args.phase}  onboarded={bool(args.onboarded)}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    dsn = _dsn()
    base = _ingress_base(args.ingress_url)
    secret = _dev_secret()
    result = _drive_turn(dsn, base, secret, args.tenant_id, args.message, timeout=args.timeout)

    print(f"\n[send] tenant={args.tenant_id}  ingress_reason={result.ingress_reason}  "
          f"run_status={result.run_status}")
    _print_transcript(result.transcript)
    replies = assistant_turns(result.transcript)
    print(f"[send] {len(replies)} assistant repl{'y' if len(replies) == 1 else 'ies'} captured")
    if result.reasons:
        for r in result.reasons:
            print(f"[send] !! {r}")
    if not replies:
        print("[send] !! SILENT: no assistant reply was produced for this inbound")
    return 0 if result.ok else 1


def cmd_script(args: argparse.Namespace) -> int:
    dsn = _dsn()
    base = _ingress_base(args.ingress_url)
    secret = _dev_secret()
    scenario = _load_scenario(args.file)
    scenario_xfail = bool(scenario.get("expected_fail", False))
    steps = scenario.get("steps", [])
    if not steps:
        _die(f"scenario {args.file} has no steps")

    print(f"\n=== scenario: {scenario.get('name', args.file)} "
          f"({'EXPECTED-FAIL' if scenario_xfail else 'expect-pass'}) ===")
    if scenario.get("notes"):
        print(f"    note: {scenario['notes']}")

    results: list[StepResult] = []
    for i, step in enumerate(steps, 1):
        message = step["message"]
        turn = _drive_turn(dsn, base, secret, args.tenant_id, message, timeout=args.timeout)
        # A send-guard breach (real SID) is a HARD failure regardless of expected_fail — never mask it.
        hard = list(turn.reasons)
        step_xfail = bool(step.get("expected_fail", scenario_xfail))
        failures = evaluate_assertions(
            turn.transcript,
            assert_no_silent=bool(step.get("assert_no_silent", True)),
            assert_contains=step.get("assert_contains"),
            assert_not_contains=step.get("assert_not_contains"),
        )
        ok, xfail, label = classify_step(failures, expected_fail=step_xfail)
        if hard:  # a real-send breach overrides any xfail leniency
            ok, xfail, label, failures = False, False, "FAIL", hard + failures

        print(f"\n  [step {i}] {label}  (run_status={turn.run_status}, reason={turn.ingress_reason})")
        if step.get("note"):
            print(f"      note: {step['note']}")
        _print_transcript(turn.transcript)
        for f in failures:
            print(f"      - {f}")

        results.append(StepResult(
            ok=ok, xfail=xfail, label=label, reasons=failures,
            transcript=turn.transcript, run_status=turn.run_status, ingress_reason=turn.ingress_reason,
        ))

    passed = sum(1 for r in results if r.label == "PASS")
    xfailed = sum(1 for r in results if r.label == "XFAIL")
    xpassed = sum(1 for r in results if r.label == "XPASS")
    failed = sum(1 for r in results if r.label == "FAIL")
    print(f"\n=== summary: {passed} PASS, {xfailed} XFAIL (known gap), {xpassed} XPASS, {failed} FAIL ===")
    if xpassed:
        print("    note: XPASS = a marked-gap step unexpectedly passed — the gap may have closed; re-check the mark.")
    return 0 if failed == 0 else 1


def cmd_teardown(args: argparse.Namespace) -> int:
    dsn = _dsn()
    with _connect(dsn) as conn:
        row = conn.execute(
            "SELECT business_name FROM tenants WHERE id = %s", (args.tenant_id,)
        ).fetchone()
        if row is None:
            print(f"[teardown] tenant {args.tenant_id} not found (already gone)")
            return 0
        name = str(row[0] if not isinstance(row, dict) else row["business_name"])
        # SAFETY RAIL: only ever delete a tenant the harness created.
        if not name.startswith(_HARNESS_NAME_PREFIX):
            _die(
                f"refusing to teardown tenant {args.tenant_id}: business_name {name!r} is not a "
                f"{_HARNESS_NAME_PREFIX!r} harness tenant"
            )
        # Dynamic non-cascade FK sweep (drill_stage_check pattern): every table FK-referencing
        # tenants WITHOUT ON DELETE CASCADE must be cleared first; catalog-derived so it tracks new
        # tables automatically.
        noncascade = conn.execute(
            "SELECT DISTINCT cl.relname AS tbl, att.attname AS col "
            "FROM pg_constraint con "
            "JOIN pg_class cl ON cl.oid = con.conrelid "
            "JOIN pg_attribute att ON att.attrelid = con.conrelid "
            "     AND att.attnum = ANY(con.conkey) "
            "WHERE con.contype = 'f' AND con.confrelid = 'public.tenants'::regclass "
            "  AND con.confdeltype <> 'c'",
        ).fetchall()
        swept = 0
        for _pass in (1, 2):  # a non-cascading table may itself be referenced by another
            for rec in noncascade:
                tbl = rec[0] if not isinstance(rec, dict) else rec["tbl"]
                col = rec[1] if not isinstance(rec, dict) else rec["col"]
                try:
                    conn.execute(f'DELETE FROM "{tbl}" WHERE "{col}" = %s', (args.tenant_id,))  # noqa: S608 — catalog-derived
                    swept += 1
                except Exception:  # noqa: BLE001 — retried on pass 2 / surfaced by the final delete
                    pass
        conn.execute("DELETE FROM tenants WHERE id = %s", (args.tenant_id,))
        left = conn.execute(
            "SELECT count(*) FROM tenants WHERE id = %s", (args.tenant_id,)
        ).fetchone()
        remaining = int(left[0] if not isinstance(left, dict) else left["count"])
    print(f"[teardown] tenant {args.tenant_id} ({name}): {len(noncascade)} non-cascade tables swept; "
          f"tenant rows left = {remaining}")
    return 0 if remaining == 0 else 1


# --- scenario loading --------------------------------------------------------------------------


def _load_scenario(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    if path.endswith((".yaml", ".yml")):
        import yaml  # available in the orchestrator env

        return yaml.safe_load(raw)
    return json.loads(raw)


# --- CLI ---------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="convo_harness", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="create a synthetic harness tenant")
    s.add_argument("--owner-inputs", default="true", help="owner_inputs consent (true|false; default true)")
    s.add_argument("--onboarded", action="store_true", help="pre-seed a COMPLETE onboarding journey")
    s.add_argument("--name", default=None, help="business_name (must start with 'convo-harness-')")
    s.add_argument("--number", default=None, help="bogus +15550… number (default: fresh random)")
    s.add_argument("--phase", default="trial", help="tenants.phase (default 'trial')")
    s.set_defaults(func=cmd_setup)

    se = sub.add_parser("send", help="inject one inbound + capture the reply")
    se.add_argument("tenant_id")
    se.add_argument("message")
    se.add_argument("--ingress-url", default=None, help="deployed dev orchestrator base URL")
    se.add_argument("--timeout", type=float, default=90.0, help="per-turn run-completion timeout (s)")
    se.set_defaults(func=cmd_send)

    sc = sub.add_parser("script", help="run an ordered scenario file with per-step assertions")
    sc.add_argument("tenant_id")
    sc.add_argument("file")
    sc.add_argument("--ingress-url", default=None, help="deployed dev orchestrator base URL")
    sc.add_argument("--timeout", type=float, default=90.0, help="per-turn run-completion timeout (s)")
    sc.set_defaults(func=cmd_script)

    td = sub.add_parser("teardown", help="FK-sweep + delete a harness tenant")
    td.add_argument("tenant_id")
    td.set_defaults(func=cmd_teardown)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
